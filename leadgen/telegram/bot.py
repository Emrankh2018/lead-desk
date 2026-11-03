"""Telegram lead bot: a daily list of leads, the owner picks which to pursue, AI writes the message, the owner approves.

Nothing is sent to a business by the bot. Approving gives the owner a link (Instagram, WhatsApp) or the text to send
from their own account; "I sent it" then logs the outreach exactly like the web app's "Mark as sent".
Only the configured chat can use the bot; every other chat is ignored.
"""
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadgen.ai import tasks
from leadgen.ai.worker import AiWorker
from leadgen.config import Settings
from leadgen.models import BotPick, Business, OutreachDraft, PipelineStage, utcnow
from leadgen.offerings import label as service_label
from leadgen.research.scoring import service_fits
from leadgen.services import audit, leads
from leadgen.services.analytics import priority
from leadgen.services.leads import LeadError
from leadgen.telegram.client import Buttons, TelegramClient, TelegramError, button, esc, link

log = logging.getLogger("leadgen.telegram")
ACTOR = "telegram"
FOLLOW_UP_DAYS = 3
CHANNEL_NAMES = {"email": "Email", "instagram": "Instagram DM", "whatsapp": "WhatsApp"}


def channels_for(biz: Business) -> list[str]:
    """Channels the business itself published a way to reach it on."""
    out = []
    if biz.email_public:
        out.append("email")
    if biz.instagram_handle:
        out.append("instagram")
    if biz.phone_e164:
        out.append("whatsapp")
    return out


def lead_summary(session: Session, biz: Business) -> str:
    where = " · ".join(v for v in (biz.industry, biz.city) if v)
    lines = [f"<b>{esc(biz.name)}</b>" + (f" — {esc(where)}" if where else "")]
    if biz.score is not None:
        lines.append(f"Score {biz.score}/100 (based on {biz.score_coverage or 0}% of the checks)")
    fits = service_fits(biz.score_breakdown, biz.icp)
    if fits and fits[0][1] >= 0.6:
        lines.append(f"Could use: <b>{esc(service_label(fits[0][0]))}</b> — {esc(fits[0][2])}")
    insight = tasks.latest_insight(session, biz)
    if insight and insight.content.get("angle"):
        lines.append(f"Idea: {esc(insight.content['angle'])}")
    reach = []
    if biz.domain:
        reach.append(f"🌐 {esc(biz.domain)}")
    if biz.instagram_handle:
        reach.append(f"📸 @{esc(biz.instagram_handle)}")
    if biz.phone_e164:
        reach.append("💬 WhatsApp")
    if biz.email_public:
        reach.append("📧 email")
    lines.append("Reach: " + (" · ".join(reach) if reach else "no published contact"))
    return "\n".join(lines)


def qatar_today() -> date:
    return datetime.now(ZoneInfo("Asia/Qatar")).date()


def _wa_number(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


class LeadBot:
    def __init__(self, settings: Settings, sessionmaker: Callable[[], Session], client: TelegramClient, ai: AiWorker):
        self.chat_id = settings.telegram_chat_id
        self.digest_size = settings.digest_size
        self._sessionmaker = sessionmaker
        self.client = client
        self.ai = ai
        ai.on_done.append(self._on_ai_done)

    # --- daily list ----------------------------------------------------------------------------------------------

    def candidates(self, session: Session) -> list[Business]:
        """Open, reachable, never-contacted leads the bot has not offered before, best first."""
        offered = select(BotPick.business_id)
        rows = session.scalars(select(Business).join(Business.stage).where(
            Business.do_not_contact.is_(False), PipelineStage.outcome.is_(None), Business.last_contacted_at.is_(None),
            Business.id.not_in(offered)))
        reachable = [b for b in rows if channels_for(b)]
        return sorted(reachable, key=lambda b: (-priority(b), b.id))[:self.digest_size]

    def follow_ups_due(self, session: Session, today: date) -> list[Business]:
        return list(session.scalars(select(Business).join(Business.stage).where(
            Business.do_not_contact.is_(False), PipelineStage.outcome.is_(None), Business.follow_up_on <= today)
            .order_by(Business.follow_up_on)))

    def send_digest(self, today: date, note: str = "") -> int:
        if not self.chat_id:
            return 0
        with self._sessionmaker() as s:
            due = self.follow_ups_due(s, today)
            if due:
                self.client.send(self.chat_id, "⏰ <b>Follow-ups due</b>\n" + "\n".join(
                    f"• {esc(b.name)} (since {b.follow_up_on:%d %b})" for b in due[:15]))
            picks = self.candidates(s)
            if not picks:
                self.client.send(self.chat_id, "No new leads to suggest today." + (f"\n{esc(note)}" if note else ""))
                return 0
            header = f"☀️ <b>{len(picks)} leads for {today:%a %d %b}</b>" + (f"\n{esc(note)}" if note else "")
            header += "\nTap ✅ on the ones you want to contact."
            self.client.send(self.chat_id, header, [[button("✅ Pursue all", f"pa:{today.isoformat()}")]])
            for biz in picks:
                s.add(BotPick(business_id=biz.id, offered_on=today))
                self.client.send(self.chat_id, lead_summary(s, biz),
                                 [[button("✅ Pursue", f"p:{biz.id}"), button("⏭ Skip", f"s:{biz.id}")]])
            audit.record(s, ACTOR, "bot.digest", "bot", None, leads=[b.id for b in picks])
            s.commit()
            return len(picks)

    # --- incoming updates --------------------------------------------------------------------------------------

    def handle_update(self, update: dict) -> None:
        if cb := update.get("callback_query"):
            chat = (cb.get("message") or {}).get("chat", {}).get("id")
            if not self.chat_id or chat != self.chat_id:
                return  # not the owner's chat: ignore silently
            try:
                reply = self._on_button(cb.get("data") or "", chat, (cb.get("message") or {}).get("message_id"))
            except TelegramError:
                log.exception("Telegram call failed while handling a button")
                reply = "Something went wrong; try again."
            self.client.answer(cb["id"], reply or "")
            return
        msg = update.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat or not text:
            return
        if not self.chat_id:
            if text.startswith("/start"):  # first-time setup: tell the owner which id to configure
                self.client.send(chat, f"Lead Desk bot is not linked yet. Set LEADGEN_TELEGRAM_CHAT_ID={chat} and restart.")
            return
        if chat != self.chat_id:
            return
        if text.startswith("/more"):
            self.send_digest(qatar_today(), "Extra list on request.")
        else:
            self.client.send(chat, "Each morning I send a list of leads.\n/more — send more leads now")

    def _on_button(self, data: str, chat: int, message_id: int | None) -> str:
        kind, _, rest = data.partition(":")
        with self._sessionmaker() as s:
            if kind == "p":
                return self._pursue(s, int(rest), chat, message_id)
            if kind == "s":
                return self._set_pick(s, int(rest), "skipped", chat, message_id)
            if kind == "pa":
                picks = list(s.scalars(select(BotPick).where(BotPick.offered_on == date.fromisoformat(rest), BotPick.status == "offered")))
                for pick in picks:
                    self._pursue(s, pick.business_id, chat, None)
                return f"Pursuing {len(picks)} leads" if picks else "Nothing left to pick from that list"
            if kind == "c":
                biz_id, _, channel = rest.partition(":")
                return self._start_draft(s, int(biz_id), channel, chat, message_id)
            if kind in ("a", "r", "d", "x"):
                draft = s.get(OutreachDraft, int(rest))
                if not draft:
                    return "That draft no longer exists"
                return {"a": self._approve, "r": self._rewrite, "d": self._discard, "x": self._sent}[kind](s, draft, chat, message_id)
        return "Unknown button"

    def _set_pick(self, s: Session, biz_id: int, status: str, chat: int, message_id: int | None) -> str:
        biz = s.get(Business, biz_id)
        if not biz:
            return "That lead was deleted"
        pick = s.scalars(select(BotPick).where(BotPick.business_id == biz_id)).first() or BotPick(business_id=biz_id, offered_on=qatar_today())
        pick.status = status
        s.add(pick)
        audit.record(s, ACTOR, f"bot.{status}", "business", biz_id)
        s.commit()
        if message_id and status == "skipped":
            self.client.edit(chat, message_id, f"⏭ Skipped: {esc(biz.name)}")
        return "Skipped" if status == "skipped" else "Noted"

    def _pursue(self, s: Session, biz_id: int, chat: int, message_id: int | None) -> str:
        self._set_pick(s, biz_id, "pursue", chat, None)
        biz = s.get(Business, biz_id)
        if not biz:
            return "That lead was deleted"
        channels = channels_for(biz)
        text = lead_summary(s, biz) + "\n\n<b>How should I reach them?</b>"
        buttons: Buttons = [[button(CHANNEL_NAMES[c], f"c:{biz.id}:{c}") for c in channels]]
        if not channels:
            text = lead_summary(s, biz) + "\n\nNo published way to reach them. Add one on the lead page."
            buttons = []
        if message_id:
            self.client.edit(chat, message_id, text, buttons)
        else:
            self.client.send(chat, text, buttons)
        return "Pick a channel"

    def _start_draft(self, s: Session, biz_id: int, channel: str, chat: int, message_id: int | None) -> str:
        biz = s.get(Business, biz_id)
        if not biz or channel not in CHANNEL_NAMES:
            return "That lead was deleted"
        if biz.do_not_contact:
            return "This business is marked do-not-contact"
        draft = OutreachDraft(business_id=biz.id, channel=channel)
        s.add(draft)
        ok, msg = self.ai.status()
        if not ok:
            tasks.template_draft(s, draft)
            s.flush()
            audit.record(s, ACTOR, "draft.template", "business", biz.id, draft_id=draft.id, channel=channel)
            s.commit()
            self.send_draft(draft.id, note=f"AI unavailable ({msg}); this is a template. Edit it before sending.")
            return "Template draft ready"
        s.commit()
        if message_id:
            self.client.edit(chat, message_id, f"✍️ Writing a {CHANNEL_NAMES[channel]} message for <b>{esc(biz.name)}</b>…")
        self.ai.submit("draft", draft.id, ACTOR)
        return "Writing the draft"

    def _on_ai_done(self, kind: str, row_id: int, actor: str) -> None:
        if kind == "draft" and actor == ACTOR:
            self.send_draft(row_id)

    def send_draft(self, draft_id: int, note: str = "") -> None:
        with self._sessionmaker() as s:
            draft = s.get(OutreachDraft, draft_id)
            if not draft or not self.chat_id:
                return
            biz = draft.business
            if draft.status == "failed":
                self.client.send(self.chat_id, f"⚠️ Could not write the message for <b>{esc(biz.name)}</b>: {esc(draft.error or '')}",
                                 [[button("Try again", f"c:{biz.id}:{draft.channel}")]])
                return
            lines = [f"<b>{esc(CHANNEL_NAMES.get(draft.channel, draft.channel))} draft for {esc(biz.name)}</b>"
                     f" — {esc(service_label(draft.service))}"]
            if note:
                lines.append(f"<i>{esc(note)}</i>")
            if draft.subject:
                lines.append(f"Subject: {esc(draft.subject)}")
            lines.append("")
            lines.append(esc(draft.body))
            for w in draft.warnings or []:
                lines.append(f"⚠️ {esc(w)}")
            self.client.send(self.chat_id, "\n".join(lines), [[button("👍 Approve", f"a:{draft.id}"),
                                                              button("🔄 Rewrite", f"r:{draft.id}"),
                                                              button("🗑 Discard", f"d:{draft.id}")]])

    def _approve(self, s: Session, draft: OutreachDraft, chat: int, message_id: int | None) -> str:
        biz = draft.business
        if draft.status != "draft":
            return f"This draft is already {draft.status}"
        if biz.do_not_contact:
            return "This business is marked do-not-contact"
        draft.status, draft.approved_at = "approved", utcnow()
        audit.record(s, ACTOR, "draft.approve", "business", biz.id, draft_id=draft.id, channel=draft.channel,
                     warnings=len(draft.warnings))
        s.commit()
        rows: Buttons = []
        how = "Send it from your own account, then tap “I sent it”."
        if draft.channel == "instagram" and biz.instagram_handle:
            rows.append([link("Open Instagram chat", f"https://ig.me/m/{biz.instagram_handle}")])
            how = "Copy the message above, open the chat, paste and send. Then tap “I sent it”."
        elif draft.channel == "whatsapp" and biz.phone_e164:
            rows.append([link("Open WhatsApp with this message", f"https://wa.me/{_wa_number(biz.phone_e164)}?text={quote(draft.body)}")])
        elif draft.channel == "email" and biz.email_public:
            how = f"Email it to {esc(biz.email_public)} from your own mailbox, then tap “I sent it”."
        rows.append([button("✔️ I sent it", f"x:{draft.id}")])
        self.client.send(chat, f"👍 Approved for <b>{esc(biz.name)}</b>. {how}", rows)
        return "Approved"

    def _rewrite(self, s: Session, draft: OutreachDraft, chat: int, message_id: int | None) -> str:
        if draft.status in ("sent", "pending"):
            return f"This draft is {draft.status}"
        draft.status = "discarded"
        audit.record(s, ACTOR, "draft.discard", "business", draft.business_id, draft_id=draft.id)
        s.commit()
        return self._start_draft(s, draft.business_id, draft.channel, chat, message_id)

    def _discard(self, s: Session, draft: OutreachDraft, chat: int, message_id: int | None) -> str:
        if draft.status == "sent":
            return "Already sent"
        draft.status = "discarded"
        audit.record(s, ACTOR, "draft.discard", "business", draft.business_id, draft_id=draft.id)
        s.commit()
        if message_id:
            self.client.edit(chat, message_id, f"🗑 Discarded the draft for {esc(draft.business.name)}")
        return "Discarded"

    def _sent(self, s: Session, draft: OutreachDraft, chat: int, message_id: int | None) -> str:
        if draft.status != "approved":
            return f"This draft is {draft.status}"
        follow = qatar_today() + timedelta(days=FOLLOW_UP_DAYS)
        try:
            leads.mark_draft_sent(s, draft, channel=draft.channel, follow_up_on=follow, actor=ACTOR)
        except LeadError as exc:
            s.rollback()
            return f"Not logged: {exc}"
        s.commit()
        if message_id:
            self.client.edit(chat, message_id, f"✔️ Logged as sent to <b>{esc(draft.business.name)}</b>. "
                                               f"I'll remind you to follow up on {follow:%a %d %b}.")
        return "Logged"
