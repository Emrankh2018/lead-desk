"""Today view, analytics and the hand-entered effort/cost log."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from leadgen.models import EffortLog, utcnow
from leadgen.services import analytics, audit
from leadgen.web.app import SessionDep, UserDep, flash, render, today_local
from leadgen.web.routes_leads import _int_or_none

router = APIRouter()
PERIODS = {"30": 30, "90": 90, "all": None}


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("/today")
def today_page(request: Request, session: Session = SessionDep, user: str = UserDep):
    view = analytics.today_view(session, today_local(), utcnow())
    return render(request, "today.html", v=view, today=today_local(), await_days=analytics.AWAIT_REPLY_DAYS,
                  ai_pending=request.app.state.ai.pending(), batch=request.app.state.batch.state)


@router.get("/analytics")
def analytics_page(request: Request, session: Session = SessionDep, user: str = UserDep):
    period = request.query_params.get("period", "all")
    days = PERIODS.get(period, None)
    since = today_local() - timedelta(days=days) if days else None
    total, by_source = analytics.funnel(session, since)
    eff = analytics.effort(session, since)
    return render(request, "analytics.html", period=period, since=since, total=total, by_source=by_source, eff=eff,
                  sent=analytics.outreach_sent(session, since), categories=analytics.EFFORT_CATEGORIES,
                  per_contacted=eff.per(total.contacted), per_qualified=eff.per(total.qualified), per_won=eff.per(total.won),
                  today=today_local())


@router.post("/effort")
async def add_effort(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    try:
        day = date.fromisoformat(str(form.get("day") or today_local().isoformat()))
        minutes = int(str(form.get("minutes") or 0))
        cost = Decimal(str(form.get("cost")).replace(",", "")) if str(form.get("cost") or "").strip() else None
    except (ValueError, InvalidOperation):
        flash(request, "Not saved: date must be YYYY-MM-DD, minutes a whole number and cost a number.", "error")
        return _see_other("/analytics#effort")
    category = str(form.get("category", "other"))
    if category not in analytics.EFFORT_CATEGORIES or minutes < 0 or minutes > 24 * 60 or (cost is not None and cost < 0):
        flash(request, "Not saved: check the category, minutes (0–1440) and cost.", "error")
        return _see_other("/analytics#effort")
    if not minutes and cost is None:
        flash(request, "Enter minutes or a cost.", "error")
        return _see_other("/analytics#effort")
    row = EffortLog(day=day, minutes=minutes, cost=cost, currency=(str(form.get("currency") or "QAR").upper()[:3]),
                    category=category, note=str(form.get("note", "")).strip()[:500], business_id=_int_or_none(form.get("business_id")))
    session.add(row)
    session.flush()
    audit.record(session, user, "effort.add", "effort", row.id, minutes=minutes, cost=str(cost) if cost is not None else None)
    session.commit()
    flash(request, "Logged.")
    return _see_other("/analytics#effort")


@router.post("/effort/{entry_id}/delete")
def delete_effort(entry_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    row = session.get(EffortLog, entry_id)
    if not row:
        raise HTTPException(404, "Entry not found")
    audit.record(session, user, "effort.delete", "effort", row.id, minutes=row.minutes, day=row.day.isoformat())
    session.delete(row)
    session.commit()
    flash(request, "Entry removed.")
    return _see_other("/analytics#effort")
