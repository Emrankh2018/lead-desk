from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from leadgen import normalize
from leadgen.models import Business, IcpProfile, ResearchRun, utcnow
from leadgen.research import scoring, service
from leadgen.services import audit
from leadgen.web.app import SessionDep, UserDep, flash, render
from leadgen.web.routes_leads import _get_lead

router = APIRouter()
BATCH_LIMIT = 25
STALE_DAYS = 14


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.post("/leads/{lead_id}/research")
def research_one(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    biz = _get_lead(session, lead_id)
    if request.app.state.batch.state["running"]:
        flash(request, "A batch research run is in progress; try again when it finishes.", "warn")
        return _see_other(f"/leads/{lead_id}")
    run = service.research_lead(session, biz, actor=user, fetcher=request.app.state.fetcher_factory())
    session.commit()
    messages = {"ok": ("Research finished.", "ok"), "partial": ("Research finished with some pages unavailable.", "warn"),
                "failed": ("Research could not read the website. Nothing was verified; see the issues below.", "error"),
                "skipped": ("No website on record, so nothing was fetched. Score uses the details you entered.", "warn")}
    if run.status == "skipped" and biz.website:  # a directory or delivery-app page: say so instead of "no website"
        messages["skipped"] = (run.issues[0], "warn")
    flash(request, *messages[run.status])
    return _see_other(f"/leads/{lead_id}#research")


@router.post("/leads/{lead_id}/evidence")
async def add_evidence(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    biz = _get_lead(session, lead_id)
    form = await request.form()
    try:
        service.add_manual_evidence(session, biz, text=str(form.get("text", "")), source_url=str(form.get("source_url", "")), actor=user)
    except ValueError as exc:
        flash(request, f"Not saved: {exc}", "error")
        return _see_other(f"/leads/{lead_id}#research")
    session.commit()
    flash(request, "Observation saved.")
    return _see_other(f"/leads/{lead_id}#research")


@router.get("/research")
def research_page(request: Request, session: Session = SessionDep, user: str = UserDep):
    runs = list(session.scalars(select(ResearchRun).order_by(ResearchRun.id.desc()).limit(50)))
    pending = len(_batch_targets(session, "unresearched"))
    stale = len(_batch_targets(session, "stale"))
    return render(request, "research.html", runs=runs, batch=request.app.state.batch.state, pending=pending, stale=stale,
                  limit=BATCH_LIMIT)


def _batch_targets(session: Session, scope: str) -> list[int]:
    """Leads with a website to research. Any attempt in the last STALE_DAYS (even a failed or rate-limited one)
    excludes a lead, so batches never hammer a site that just refused us; use the lead's own button to retry."""
    cutoff = utcnow() - timedelta(days=STALE_DAYS)
    recent_attempt = exists().where(ResearchRun.business_id == Business.id, ResearchRun.started_at >= cutoff)
    stmt = select(Business.id, Business.website).where(Business.website.is_not(None), Business.do_not_contact.is_(False),
                                                       ~recent_attempt)
    if scope != "stale":
        stmt = stmt.where(Business.researched_at.is_(None))
    # A Zomato, Facebook or free Google page is not the business's site; there is nothing of theirs to research.
    return [i for i, site in session.execute(stmt.order_by(Business.id)) if not normalize.listing_host(site)]


@router.post("/research/batch")
async def start_batch(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    scope = "stale" if form.get("scope") == "stale" else "unresearched"
    ids = _batch_targets(session, scope)[:BATCH_LIMIT]
    if not ids:
        flash(request, "Nothing to research.", "warn")
        return _see_other("/research")
    if not request.app.state.batch.start(ids, user):
        flash(request, "A batch is already running.", "warn")
        return _see_other("/research")
    audit.record(session, user, "research.batch_start", "business", None, scope=scope, leads=len(ids))
    session.commit()
    flash(request, f"Researching {len(ids)} leads in the background (about {len(ids) * 8 // 60 + 1} min). Refresh to see progress.")
    return _see_other("/research")


@router.post("/settings/scoring/{icp_id}")
async def save_weights(icp_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    icp = session.get(IcpProfile, icp_id)
    if not icp:
        raise HTTPException(404, "Profile not found")
    form = await request.form()
    weights = {}
    for c in scoring.CRITERIA:
        try:
            weights[c.id] = max(0, min(100, int(str(form.get(c.id, c.default_weight)))))
        except ValueError:
            flash(request, f"Weight for {c.label} must be a whole number.", "error")
            return _see_other("/settings#scoring")
    icp.scoring_weights = weights
    audit.record(session, user, "icp.weights", "icp", icp.id, weights=weights)
    n = service.rescore_all(session)
    session.commit()
    flash(request, f"Weights saved; {n} leads rescored.")
    return _see_other("/settings#scoring")
