"""AI insights and outreach drafts. Nothing here sends a message: the user copies/opens it and marks it sent."""
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from leadgen.ai import tasks
from leadgen.models import AiInsight, OutreachDraft, PipelineStage, utcnow
from leadgen.offerings import SERVICES
from leadgen.services import audit, leads
from leadgen.services.leads import LeadError
from leadgen.web.app import SessionDep, UserDep, flash, render
from leadgen.web.routes_leads import _get_lead

router = APIRouter()
EARLY_STAGES = {"discovered", "qualified", "ready"}


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.post("/leads/{lead_id}/insight")
def request_insight(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    biz = _get_lead(session, lead_id)
    ok, msg = request.app.state.ai.status()
    if not ok:
        flash(request, f"AI unavailable: {msg}", "error")
        return _see_other(f"/leads/{lead_id}#ai")
    insight = AiInsight(business_id=biz.id)
    session.add(insight)
    session.commit()
    request.app.state.ai.submit("insight", insight.id, user)
    flash(request, "Analysing evidence with the local model (usually 1–2 minutes). This page refreshes itself.")
    return _see_other(f"/leads/{lead_id}#ai")


@router.post("/leads/{lead_id}/drafts")
async def request_draft(lead_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    biz = _get_lead(session, lead_id)
    form = await request.form()
    channel = str(form.get("channel", ""))
    if channel not in tasks.CHANNELS:
        raise HTTPException(422, "Unknown channel")
    if biz.do_not_contact:
        flash(request, "This business is marked do-not-contact; no drafts are prepared.", "error")
        return _see_other(f"/leads/{lead_id}#ai")
    service = str(form.get("service", "")) or None
    if service and service not in SERVICES:
        raise HTTPException(422, "Unknown service")
    draft = OutreachDraft(business_id=biz.id, channel=channel, service=service)
    session.add(draft)
    if form.get("mode") == "template":
        tasks.template_draft(session, draft)
        session.flush()
        audit.record(session, user, "draft.template", "business", biz.id, draft_id=draft.id, channel=channel)
        session.commit()
        flash(request, "Template draft ready. Personalise it before approving.")
        return _see_other(f"/leads/{lead_id}#draft-{draft.id}")
    ok, msg = request.app.state.ai.status()
    if not ok:
        session.rollback()
        flash(request, f"AI unavailable: {msg}. Use a template draft instead.", "error")
        return _see_other(f"/leads/{lead_id}#ai")
    session.commit()
    request.app.state.ai.submit("draft", draft.id, user)
    label = {"whatsapp": "a WhatsApp", "email": "an email", "instagram": "an Instagram", "linkedin": "a LinkedIn",
             "followup": "a follow-up"}[channel]
    flash(request, f"Writing {label} draft with the local model (usually under a minute). This page refreshes itself.")
    return _see_other(f"/leads/{lead_id}#ai")


@router.post("/drafts/{draft_id}")
async def update_draft(draft_id: int, request: Request, session: Session = SessionDep, user: str = UserDep):
    draft = session.get(OutreachDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    biz = draft.business
    form = await request.form()
    action = str(form.get("action", "save"))
    back = f"/leads/{biz.id}#draft-{draft.id}"
    if draft.status in ("sent", "discarded", "pending", "failed") and action != "discard":
        flash(request, f"This draft is {draft.status} and can no longer be changed.", "error")
        return _see_other(back)
    if "body" in form:
        edited_body, edited_subject = str(form.get("body", "")), str(form.get("subject", ""))
        if edited_body != draft.body or edited_subject != (draft.subject or ""):
            if draft.status == "approved":
                draft.status, draft.approved_at = "draft", None  # edits need a fresh approval
            tasks.finish_draft(session, draft, edited_subject, edited_body)
            if action == "save":
                flash(request, "Draft saved and re-checked.")

    if action == "approve":
        if biz.do_not_contact:
            flash(request, "Cannot approve: business is do-not-contact.", "error")
            return _see_other(back)
        draft.status, draft.approved_at = "approved", utcnow()
        audit.record(session, user, "draft.approve", "business", biz.id, draft_id=draft.id, channel=draft.channel,
                     warnings=len(draft.warnings))
        flash(request, "Approved. Send it yourself from your own account, then click “Mark as sent”.")
    elif action == "discard":
        draft.status = "discarded"
        audit.record(session, user, "draft.discard", "business", biz.id, draft_id=draft.id)
        flash(request, "Draft discarded.")
    elif action == "sent":
        if draft.status != "approved":
            flash(request, "Approve the draft before marking it as sent.", "error")
            return _see_other(back)
        channel = str(form.get("sent_channel") or ("email" if draft.channel in ("email", "followup") else draft.channel))
        follow = None
        if form.get("follow_up_on"):
            try:
                follow = date.fromisoformat(str(form["follow_up_on"]))
            except ValueError:
                flash(request, "Follow-up date must be YYYY-MM-DD.", "error")
                return _see_other(back)
        try:
            leads.add_activity(session, biz, kind="outreach", channel=channel,
                               summary=f"Sent {draft.channel} message (draft #{draft.id}):\n{draft.body[:1500]}",
                               follow_up_on=follow, actor=user)
        except LeadError as exc:
            session.rollback()
            flash(request, f"Not marked as sent: {exc}", "error")
            return _see_other(back)
        draft.status, draft.sent_at = "sent", utcnow()
        if biz.stage.key in EARLY_STAGES:
            contacted = session.scalars(select(PipelineStage).where(PipelineStage.key == "contacted")).first()
            if contacted:
                leads.change_stage(session, biz, contacted, actor=user)
        audit.record(session, user, "draft.sent_manually", "business", biz.id, draft_id=draft.id, channel=channel)
        flash(request, "Logged as sent" + (f"; follow-up set for {follow}." if follow else "."))
    session.commit()
    return _see_other(back)


@router.get("/outreach")
def outreach_page(request: Request, session: Session = SessionDep, user: str = UserDep):
    rows = list(session.scalars(select(OutreachDraft).where(OutreachDraft.status.in_(("draft", "approved", "pending")))
                                .order_by(OutreachDraft.status, OutreachDraft.created_at.desc())))
    ok, msg = request.app.state.ai.status()
    return render(request, "outreach.html", rows=rows, ai_ok=ok, ai_msg=msg, ai_pending=request.app.state.ai.pending())


@router.post("/settings/sender")
async def save_sender(request: Request, session: Session = SessionDep, user: str = UserDep):
    form = await request.form()
    tasks.save_sender(session, dict(form))
    audit.record(session, user, "settings.sender", "setting", None)
    session.commit()
    flash(request, "Sender profile saved.")
    return _see_other("/settings#sender")
