import json
import re
import secrets
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leadgen.models import AuditLog, Business, IcpProfile, Offer, PipelineStage
from leadgen.research import scoring
from leadgen.research.service import rescore_all
from leadgen.services import audit, exporter, importer, leads
from leadgen.web.app import SessionDep, UserDep, flash, render, today_local
from leadgen.web.routes_leads import _int_or_none, _lookups, filter_from_query

router = APIRouter()
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _csv_list(value) -> list[str]:
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


# --- import / export --------------------------------------------------------------------------------------

def _import_path(request: Request, token: str):
    if not _TOKEN_RE.match(token):
        raise HTTPException(400, "Invalid import reference")
    folder = request.app.state.settings.data_dir / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{token}.json"


@router.get("/import")
def import_form(request: Request, session: Session = SessionDep, user: str = UserDep):
    return render(request, "import.html", aliases=importer.COLUMN_ALIASES, **_lookups(session))


@router.post("/import")
async def import_preview(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    upload = form.get("file")
    icp_id = _int_or_none(form.get("icp_id"))
    if not upload or not getattr(upload, "filename", ""):
        flash(request, "Choose a CSV file first.", "error")
        return _see_other("/import")
    filename = re.sub(r"[^\w.\- ]", "_", upload.filename)[:100]
    try:
        mapping, rows = importer.parse(await upload.read(importer.MAX_BYTES + 1))
    except importer.ImportError_ as exc:
        flash(request, f"Import failed: {exc}", "error")
        return _see_other("/import")
    preview = importer.preview(session, rows, icp_id=icp_id, default_region=request.app.state.settings.default_country)
    token = secrets.token_urlsafe(24)
    _import_path(request, token).write_text(json.dumps(
        {"filename": filename, "icp_id": icp_id, "rows": importer.rows_to_json(preview)}, ensure_ascii=False), encoding="utf-8")
    counts = {s: sum(1 for r in preview if r.status == s) for s in ("new", "duplicate", "duplicate_in_file", "invalid")}
    return render(request, "import_preview.html", rows=preview, counts=counts, mapping=mapping, token=token, filename=filename)


@router.post("/import/commit")
async def import_commit(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    path = _import_path(request, str(form.get("token", "")))
    if not path.exists():
        flash(request, "That import preview has expired. Upload the file again.", "error")
        return _see_other("/import")
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = importer.commit(session, importer.rows_from_json(payload["rows"]), filename=payload["filename"],
                             icp_id=payload["icp_id"], actor=user, default_region=request.app.state.settings.default_country)
    audit.record(session, user, "import.commit", "import", None, filename=payload["filename"], **result)
    session.flush()
    rescore_all(session)
    session.commit()
    path.unlink(missing_ok=True)
    flash(request, f"Imported {result['created']} new leads; skipped {result['skipped']}.")
    return _see_other("/leads?sort=created")


@router.get("/export")
def export(request: Request, session: Session = SessionDep, user: str = UserDep):
    fmt = request.query_params.get("format", "csv")
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(400, "format must be csv or xlsx")
    rows = leads.query_leads(session, filter_from_query(request), today_local())
    audit.record(session, user, "export", "business", None, format=fmt, rows=len(rows), query=request.url.query[:500])
    session.commit()
    stamp = today_local().isoformat()
    if fmt == "csv":
        return Response(exporter.to_csv(rows), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="leads-{stamp}.csv"'})
    return Response(exporter.to_xlsx(rows), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="leads-{stamp}.xlsx"'})


# --- settings ---------------------------------------------------------------------------------------------

@router.get("/settings")
def settings_page(request: Request, session: Session = SessionDep, user: str = UserDep):
    counts = dict(session.execute(select(Business.stage_id, func.count()).group_by(Business.stage_id)).all())
    edit_icp = session.get(IcpProfile, _int_or_none(request.query_params.get("icp")) or 0)
    return render(request, "settings.html", stage_counts=counts, edit_icp=edit_icp, criteria=scoring.CRITERIA,
                  weights_for=scoring.weights_for,
                  offers=list(session.scalars(select(Offer).order_by(Offer.id))), **_lookups(session))


@router.post("/settings/stages")
async def add_stage(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40]
    if not key or session.scalars(select(PipelineStage).where(PipelineStage.key == key)).first():
        flash(request, "Stage name is empty or already exists.", "error")
        return _see_other("/settings")
    last = session.scalar(select(func.max(PipelineStage.position))) or 0
    stage = PipelineStage(key=key, name=name[:80], position=last + 1)
    session.add(stage)
    session.flush()
    audit.record(session, user, "stage.create", "stage", stage.id, name=name)
    session.commit()
    flash(request, f"Added stage {name}.")
    return _see_other("/settings")


@router.post("/settings/stages/{stage_id}")
async def edit_stage(stage_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    stage = session.get(PipelineStage, stage_id)
    if not stage:
        raise HTTPException(404, "Stage not found")
    form = await request.form()
    action = form.get("action")
    stages = list(session.scalars(select(PipelineStage).order_by(PipelineStage.position)))
    if action in ("up", "down"):
        i = stages.index(stage)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(stages):
            stages[i], stages[j] = stages[j], stages[i]
            for pos, s in enumerate(stages):
                s.position = pos
    elif action == "rename":
        new_name = str(form.get("name", "")).strip()[:80]
        if new_name:
            audit.record(session, user, "stage.rename", "stage", stage.id, old=stage.name, new=new_name)
            stage.name = new_name
    elif action == "outcome":
        stage.outcome = form.get("outcome") if form.get("outcome") in ("won", "lost") else None
    elif action == "delete":
        in_use = session.scalar(select(func.count()).select_from(Business).where(Business.stage_id == stage.id))
        if in_use or len(stages) <= 1:
            flash(request, "Only an empty stage can be deleted, and at least one stage must remain.", "error")
            return _see_other("/settings")
        audit.record(session, user, "stage.delete", "stage", stage.id, name=stage.name)
        session.delete(stage)
    session.commit()
    return _see_other("/settings")


@router.post("/settings/icps")
async def save_icp(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    icp = session.get(IcpProfile, _int_or_none(form.get("id")) or 0) or IcpProfile()
    name = str(form.get("name", "")).strip()[:120]
    if not name:
        flash(request, "Profile name is required.", "error")
        return _see_other("/settings")
    clash = session.scalars(select(IcpProfile).where(IcpProfile.name == name, IcpProfile.id != (icp.id or 0))).first()
    if clash:
        flash(request, "Another profile already has that name.", "error")
        return _see_other("/settings")
    icp.name = name
    icp.description = str(form.get("description", "")).strip()
    icp.countries = [c.upper()[:2] for c in _csv_list(form.get("countries"))]
    icp.cities = _csv_list(form.get("cities"))
    icp.industries = _csv_list(form.get("industries"))
    icp.keywords = _csv_list(form.get("keywords"))
    icp.size_notes = str(form.get("size_notes", "")).strip()
    icp.active = form.get("active") == "1"
    session.add(icp)
    session.flush()
    audit.record(session, user, "icp.save", "icp", icp.id, name=name)
    rescore_all(session)  # industries/keywords/countries feed the score
    session.commit()
    flash(request, f"Saved profile {name}.")
    return _see_other("/settings")


@router.post("/settings/offers")
async def save_offer(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    offer = session.get(Offer, _int_or_none(form.get("id")) or 0) or Offer()
    name = str(form.get("name", "")).strip()[:120]
    try:
        price = Decimal(str(form.get("price")).replace(",", "")) if str(form.get("price", "")).strip() else None
    except InvalidOperation:
        flash(request, "Price must be a number.", "error")
        return _see_other("/settings")
    if not name:
        flash(request, "Offer name is required.", "error")
        return _see_other("/settings")
    offer.name, offer.price = name, price
    offer.currency = (str(form.get("currency", "QAR")).strip().upper() or "QAR")[:3]
    offer.description = str(form.get("description", "")).strip()
    offer.active = form.get("active") == "1"
    session.add(offer)
    session.flush()
    audit.record(session, user, "offer.save", "offer", offer.id, name=name)
    session.commit()
    flash(request, f"Saved offer {name}.")
    return _see_other("/settings")


@router.get("/audit")
def audit_log(request: Request, session: Session = SessionDep, user: str = UserDep):
    rows = list(session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)))
    return render(request, "audit.html", rows=rows)
