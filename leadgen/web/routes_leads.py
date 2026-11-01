from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leadgen.auth import authenticate
from leadgen.ai import tasks as ai_tasks
from leadgen.models import AiInsight, Business, DuplicateCandidate, IcpProfile, OutreachDraft, PipelineStage, ResearchRun
from leadgen.research import service as research
from leadgen.services import audit, leads
from leadgen.services.leads import DuplicateLead, LeadError, LeadFilter, LeadInput
from leadgen.web.app import SessionDep, UserDep, flash, render, today_local

router = APIRouter()
LEAD_FIELDS = ["name", "website", "instagram", "linkedin_url", "phone", "email", "email_source", "city", "country",
               "industry", "deal_value", "follow_up_on", "notes"]


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _safe_next(target: str | None) -> str:
    return target if target and target.startswith("/") and not target.startswith("//") else "/leads"


def _get_lead(session: Session, lead_id: int) -> Business:
    biz = session.get(Business, lead_id)
    if not biz:
        raise HTTPException(404, "Lead not found")
    return biz


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _lead_input(form) -> LeadInput:
    return LeadInput(**{f: str(form.get(f, "")) for f in LEAD_FIELDS}, icp_id=_int_or_none(form.get("icp_id")))


def _lookups(session: Session) -> dict:
    return {
        "stages": list(session.scalars(select(PipelineStage).order_by(PipelineStage.position))),
        "icps": list(session.scalars(select(IcpProfile).order_by(IcpProfile.name))),
    }


def filter_from_query(request: Request) -> LeadFilter:
    q = request.query_params
    return LeadFilter(q=q.get("q", ""), stage_id=_int_or_none(q.get("stage_id")), icp_id=_int_or_none(q.get("icp_id")),
                      city=q.get("city", ""), industry=q.get("industry", ""), follow_up_due=q.get("due") == "1",
                      dnc=q.get("dnc", "exclude") if q.get("dnc") in ("exclude", "include", "only") else "exclude",
                      min_score=_int_or_none(q.get("min_score")), sort=q.get("sort", "updated"))


# --- auth -------------------------------------------------------------------------------------------------

@router.get("/login")
def login_form(request: Request):
    return render(request, "login.html", next=_safe_next(request.query_params.get("next")))


@router.post("/login")
async def login(request: Request, session: Session = SessionDep):
    form = await request.form()
    client = request.client.host if request.client else "unknown"
    throttle = request.app.state.throttle
    target = _safe_next(str(form.get("next", "")))
    if throttle.locked(client):
        return render(request, "login.html", status_code=429, next=target, error="Too many attempts. Wait a minute and try again.")
    user = authenticate(session, str(form.get("username", "")), str(form.get("password", "")))
    if not user:
        throttle.fail(client)
        audit.record(session, "anonymous", "auth.login_failed", "user", None, username=str(form.get("username", ""))[:64])
        session.commit()
        return render(request, "login.html", status_code=401, next=target, error="Wrong username or password.")
    throttle.reset(client)
    request.session.clear()
    request.session["user"] = user.username
    audit.record(session, user.username, "auth.login", "user", user.id)
    session.commit()
    return _see_other(target)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return _see_other("/login")


# --- leads ------------------------------------------------------------------------------------------------

@router.get("/")
def home(user: str = UserDep):
    return _see_other("/leads")


@router.get("/leads")
def list_leads(request: Request, session: Session = SessionDep, user: str = UserDep):
    flt = filter_from_query(request)
    rows = leads.query_leads(session, flt, today_local())
    open_dupes = session.scalar(select(func.count()).select_from(DuplicateCandidate).where(DuplicateCandidate.status == "open"))
    return render(request, "leads.html", leads=rows, flt=flt, today=today_local(), open_dupes=open_dupes,
                  export_query=request.url.query, **_lookups(session))


@router.get("/leads/new")
def new_lead(request: Request, session: Session = SessionDep, user: str = UserDep):
    return render(request, "lead_new.html", form={}, **_lookups(session))


@router.post("/leads")
async def create_lead(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    data = _lead_input(form)
    try:
        biz, fuzzy = leads.create_lead(session, data, actor=user, source="manual", source_ref=None,
                                       default_region=request.app.state.settings.default_country)
    except DuplicateLead as exc:
        session.rollback()
        return render(request, "lead_new.html", status_code=409, form=form, duplicates=exc.matches,
                      error="This business is already in your list.", **_lookups(session))
    except LeadError as exc:
        session.rollback()
        return render(request, "lead_new.html", status_code=422, form=form, error=str(exc), **_lookups(session))
    research.rescore(session, biz)
    session.commit()
    flash(request, f"Added “{biz.name}”.")
    if fuzzy:
        flash(request, f"Possible duplicate of {', '.join(m.business.name for m in fuzzy[:3])} — review under Duplicates.", "warn")
    return _see_other(f"/leads/{biz.id}")


@router.get("/leads/{lead_id}")
def lead_detail(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    biz = _get_lead(session, lead_id)
    dupes = list(session.scalars(select(DuplicateCandidate).where(
        DuplicateCandidate.status == "open",
        (DuplicateCandidate.business_id == biz.id) | (DuplicateCandidate.other_id == biz.id))))
    last_run = session.scalars(select(ResearchRun).where(ResearchRun.business_id == biz.id)
                               .order_by(ResearchRun.id.desc())).first()
    evidence = biz.evidence
    insight = session.scalars(select(AiInsight).where(AiInsight.business_id == biz.id).order_by(AiInsight.id.desc())).first()
    drafts = list(session.scalars(select(OutreachDraft).where(OutreachDraft.business_id == biz.id,
                                                              OutreachDraft.status != "discarded")
                                  .order_by(OutreachDraft.id.desc())))
    ai_ok, ai_msg = request.app.state.ai.status()
    return render(request, "lead_detail.html", lead=biz, dupes=dupes, channels=leads.CHANNELS,
                  insight=insight, drafts=drafts, ai_ok=ai_ok, ai_msg=ai_msg, draft_channels=ai_tasks.CHANNELS,
                  ai_pending=any(d.status == "pending" for d in drafts) or (insight is not None and insight.status == "pending"),
                  sender_set=bool(ai_tasks.get_sender(session)["your_name"]),
                  kinds=leads.ACTIVITY_KINDS, today=today_local(), last_run=last_run,
                  facts=[e for e in evidence if e.verified and e.kind != "fetch_issue"],
                  limits=[e for e in evidence if not e.verified],
                  evidence_by_id={e.id: e for e in evidence}, batch_running=request.app.state.batch.state["running"],
                  **_lookups(session))


@router.post("/leads/{lead_id}")
async def update_lead(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    biz = _get_lead(session, lead_id)
    form = await request.form()
    try:
        changed = leads.update_lead(session, biz, _lead_input(form), actor=user,
                                    default_region=request.app.state.settings.default_country)
    except DuplicateLead as exc:
        session.rollback()
        flash(request, f"Not saved: {exc}", "error")
        return _see_other(f"/leads/{lead_id}")
    except LeadError as exc:
        session.rollback()
        flash(request, f"Not saved: {exc}", "error")
        return _see_other(f"/leads/{lead_id}")
    research.rescore(session, biz)
    session.commit()
    flash(request, f"Saved ({', '.join(changed)})." if changed else "No changes.")
    return _see_other(f"/leads/{lead_id}")


@router.post("/leads/{lead_id}/stage")
async def set_stage(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    biz = _get_lead(session, lead_id)
    form = await request.form()
    stage = session.get(PipelineStage, _int_or_none(form.get("stage_id")) or 0)
    if not stage:
        raise HTTPException(422, "Unknown stage")
    leads.change_stage(session, biz, stage, actor=user)
    session.commit()
    flash(request, f"{biz.name} moved to {stage.name}.")
    return _see_other(_safe_next(str(form.get("next", ""))) if form.get("next") else f"/leads/{lead_id}")


@router.post("/leads/{lead_id}/activity")
async def log_activity(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    biz = _get_lead(session, lead_id)
    form = await request.form()
    occurred = None
    follow = None
    try:
        if form.get("occurred_at"):
            occurred = datetime.fromisoformat(str(form["occurred_at"]))
        if form.get("follow_up_on"):
            follow = date.fromisoformat(str(form["follow_up_on"]))
        leads.add_activity(session, biz, kind=str(form.get("kind", "note")), channel=str(form.get("channel", "")) or None,
                           summary=str(form.get("summary", "")), occurred_at=occurred, follow_up_on=follow, actor=user)
    except (LeadError, ValueError) as exc:
        session.rollback()
        flash(request, f"Not logged: {exc}", "error")
        return _see_other(f"/leads/{lead_id}")
    session.commit()
    flash(request, "Logged.")
    return _see_other(f"/leads/{lead_id}")


@router.post("/leads/{lead_id}/dnc")
async def toggle_dnc(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    biz = _get_lead(session, lead_id)
    form = await request.form()
    leads.set_do_not_contact(session, biz, form.get("value") == "1", actor=user)
    research.rescore(session, biz)
    session.commit()
    return _see_other(f"/leads/{lead_id}")


@router.get("/leads/{lead_id}/delete")
def confirm_delete(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    return render(request, "confirm_delete.html", lead=_get_lead(session, lead_id))


@router.post("/leads/{lead_id}/delete")
async def delete_lead(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    biz = _get_lead(session, lead_id)
    form = await request.form()
    if str(form.get("confirm_name", "")).strip() != biz.name:
        flash(request, "Not deleted: type the business name exactly to confirm.", "error")
        return _see_other(f"/leads/{lead_id}/delete")
    name = biz.name
    leads.delete_lead(session, biz, actor=user)
    session.commit()
    flash(request, f"Deleted {name} and its history.")
    return _see_other("/leads")


# --- pipeline & duplicates --------------------------------------------------------------------------------

@router.get("/pipeline")
def pipeline(request: Request, session: Session = SessionDep, user: str = UserDep):
    flt = filter_from_query(request)
    rows = leads.query_leads(session, flt, today_local())
    lookups = _lookups(session)
    columns = [(s, [b for b in rows if b.stage_id == s.id]) for s in lookups["stages"]]
    return render(request, "pipeline.html", columns=columns, flt=flt, today=today_local(), **lookups)


@router.get("/duplicates")
def duplicates(request: Request, session: Session = SessionDep, user: str = UserDep):
    rows = list(session.scalars(select(DuplicateCandidate).where(DuplicateCandidate.status == "open")
                                .order_by(DuplicateCandidate.similarity.desc())))
    return render(request, "duplicates.html", rows=rows)


@router.post("/duplicates/{cand_id}/merge")
async def merge_duplicate(cand_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    cand = session.get(DuplicateCandidate, cand_id)
    if not cand or cand.status != "open":
        raise HTTPException(404, "Duplicate suggestion not found")
    form = await request.form()
    keep_id = _int_or_none(form.get("keep_id"))
    if keep_id not in (cand.business_id, cand.other_id):
        raise HTTPException(422, "Choose which record to keep")
    remove_id = cand.other_id if keep_id == cand.business_id else cand.business_id
    keep, remove = session.get(Business, keep_id), session.get(Business, remove_id)
    leads.merge(session, keep, remove, actor=user)
    session.flush()
    research.rescore(session, keep)
    session.commit()
    flash(request, f"Merged into {keep.name}.")
    return _see_other(f"/leads/{keep.id}")


@router.post("/duplicates/{cand_id}/dismiss")
def dismiss_duplicate(cand_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    cand = session.get(DuplicateCandidate, cand_id)
    if not cand:
        raise HTTPException(404, "Duplicate suggestion not found")
    cand.status = "dismissed"
    audit.record(session, user, "duplicate.dismiss", "business", cand.business_id, other_id=cand.other_id)
    session.commit()
    flash(request, "Marked as not a duplicate.")
    return _see_other("/duplicates")
