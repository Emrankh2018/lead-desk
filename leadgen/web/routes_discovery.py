"""Free discovery: OpenStreetMap search and pasted lists. Every source goes through a preview before anything is added."""
import json
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from leadgen import normalize
from leadgen.discovery import osm
from leadgen.discovery import service as discovery
from leadgen.models import Business, DiscoveryRun, IcpProfile
from leadgen.offerings import services_of
from leadgen.services import audit, importer
from leadgen.web.app import SessionDep, UserDep, flash, render
from leadgen.web.routes_admin import _import_path
from leadgen.web.routes_leads import _int_or_none

router = APIRouter()


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _discover_form(request: Request, session: Session, **ctx):
    icps = list(session.scalars(select(IcpProfile).where(IcpProfile.active.is_(True)).order_by(IcpProfile.id)))
    icp = ctx.pop("icp", None) or (icps[0] if icps else None)
    defaults = {"country": (icp.countries[0] if icp and icp.countries else request.app.state.settings.default_country),
                "city": "", "types": osm.categories_for(icp.industries, icp.keywords, services_of(icp)) if icp else ["perfumery"],
                "icp_id": icp.id if icp else None}
    runs = list(session.scalars(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()).limit(15)))
    return render(request, "discover.html", icps=icps, groups=osm.CATEGORY_GROUPS, label_of=osm.label_of, form=defaults | ctx.pop("form", {}),
                  runs=runs, **ctx)


@router.get("/discover")
def discover_page(request: Request, session: Session = SessionDep, user: str = UserDep):
    icp = session.get(IcpProfile, _int_or_none(request.query_params.get("icp_id")) or 0)
    return _discover_form(request, session, icp=icp)


@router.post("/discover")
async def discover_search(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    country = str(form.get("country", "")).strip().upper()
    city = str(form.get("city", "")).strip() or None
    types = [t for t in form.getlist("types") if t in osm.SHOP_TYPES]
    icp_id = _int_or_none(form.get("icp_id"))
    entered = {"country": country, "city": city or "", "types": types, "icp_id": icp_id}
    settings = request.app.state.settings
    try:
        # Overpass can take up to two minutes; run it off the event loop so the rest of the app stays responsive.
        places, cached = await run_in_threadpool(request.app.state.osm_search, country, types, city,
                                                 cache_dir=settings.data_dir / "cache")
    except osm.OsmError as exc:
        return render(request, "discover_error.html", status_code=502, error=str(exc), form=entered)
    candidates, unnamed = discovery.group_places(places, city)
    icp = session.get(IcpProfile, icp_id or 0)
    discovery.classify(session, candidates, settings.default_country, services_of(icp) if icp else None)
    run = DiscoveryRun(source="osm", params=entered, places_found=len(places), candidates=len(candidates),
                       new_candidates=sum(1 for c in candidates if c.status == "new"), from_cache=cached)
    session.add(run)
    session.flush()
    audit.record(session, user, "discovery.search", "discovery", run.id, **entered, found=len(places), cached=cached)
    session.commit()
    token = secrets.token_urlsafe(24)
    _import_path(request, token).write_text(json.dumps(
        {"kind": "osm", "run_id": run.id, "country": country, "icp_id": icp_id, "candidates": discovery.to_json(candidates)},
        ensure_ascii=False), encoding="utf-8")
    counts = {s: sum(1 for c in candidates if c.status == s) for s in ("new", "possible_duplicate", "in_list")}
    return render(request, "discover_preview.html", candidates=candidates, counts=counts, token=token, run=run,
                  unnamed=unnamed, cached=cached, label_of=osm.label_of, params=entered,
                  product_ads=not icp or services_of(icp) == ["product_ads"])


@router.post("/discover/add")
async def discover_add(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    path = _import_path(request, str(form.get("token", "")))
    if not path.exists():
        flash(request, "That search preview has expired. Search again.", "error")
        return _see_other("/discover")
    payload = json.loads(path.read_text(encoding="utf-8"))
    run = session.get(DiscoveryRun, payload["run_id"])
    keys = set(form.getlist("keys"))
    if not keys:
        flash(request, "Select at least one business to add.", "error")
        return _see_other("/discover")
    result = discovery.add_candidates(session, discovery.from_json(payload["candidates"]), keys, run=run,
                                      country=payload["country"], icp_id=payload["icp_id"], actor=user,
                                      default_region=request.app.state.settings.default_country)
    session.commit()
    path.unlink(missing_ok=True)
    message = f"Added {result['created']} leads from OpenStreetMap" + (f"; skipped {result['skipped']}" if result["skipped"] else "") + "."
    if form.get("research") == "1":
        with_site = [b.id for b in session.scalars(select(Business).where(Business.id.in_(result["ids"]), Business.website.is_not(None)))
                     if not normalize.listing_host(b.website)]  # a Zomato or Google page is not their site
        if with_site and request.app.state.batch.start(with_site[:25], user):
            message += f" Researching the {min(len(with_site), 25)} with a website in the background."
        elif with_site:
            message += " A research batch is already running; start this one from the Research page later."
    flash(request, message)
    return _see_other("/leads?sort=created")


@router.post("/import/paste")
async def import_paste(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    rows = importer.parse_lines(str(form.get("text", "")))
    if not rows:
        flash(request, "Paste at least one website, Instagram handle or name.", "error")
        return _see_other("/import")
    icp_id = _int_or_none(form.get("icp_id"))
    preview = importer.preview(session, rows, icp_id=icp_id, default_region=request.app.state.settings.default_country)
    token = secrets.token_urlsafe(24)
    _import_path(request, token).write_text(json.dumps(
        {"filename": "pasted list", "icp_id": icp_id, "source": "paste", "rows": importer.rows_to_json(preview)},
        ensure_ascii=False), encoding="utf-8")
    counts = {s: sum(1 for r in preview if r.status == s) for s in ("new", "duplicate", "duplicate_in_file", "invalid")}
    mapping = {"name": "name", "website": "website", "instagram": "instagram"}
    return render(request, "import_preview.html", rows=preview, counts=counts, mapping=mapping, token=token, filename="pasted list")
