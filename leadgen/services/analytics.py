"""Today view and performance metrics, computed only from recorded data.

Nothing is projected or estimated: a lead counts as "contacted" only if an outreach was logged, "won" only if it sits
in a stage marked won, and revenue is the sum of deal values entered on won leads.
Funnel cohorts are the leads *added* in the selected period and everything that has happened to them since.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leadgen.models import Activity, Business, EffortLog, OutreachDraft, PipelineStage

AWAIT_REPLY_DAYS = 3
UPCOMING_DAYS = 7
EFFORT_CATEGORIES = ["research", "outreach", "follow_up", "meetings", "tools", "other"]


@dataclass
class TodayView:
    due: list[Business]
    upcoming: list[Business]
    replied: list[tuple[Business, Activity]]
    awaiting: list[tuple[Business, Activity, int]]
    approved_drafts: list[OutreachDraft]
    top_uncontacted: list[Business]


def priority(b: Business) -> float:
    """Score discounted by missing evidence: the score is an average over evidenced criteria only, so a 94 at 59%
    coverage is less certain than a 90 at 100% (benchmark 2026-11-01 showed unresearched leads topping the list)."""
    return (b.score or 0) * (0.5 + 0.5 * (b.score_coverage or 0) / 100)


def _last_by_kind(session: Session) -> dict[int, dict[str, Activity]]:
    """Latest outreach and reply activity per lead."""
    out: dict[int, dict[str, Activity]] = defaultdict(dict)
    rows = session.scalars(select(Activity).where(Activity.kind.in_(("outreach", "reply"))).order_by(Activity.occurred_at))
    for a in rows:
        out[a.business_id][a.kind] = a
    return out


def today_view(session: Session, today: date, now: datetime) -> TodayView:
    open_leads = list(session.scalars(select(Business).join(Business.stage).where(
        Business.do_not_contact.is_(False), PipelineStage.outcome.is_(None))))
    last = _last_by_kind(session)
    due = sorted((b for b in open_leads if b.follow_up_on and b.follow_up_on <= today), key=lambda b: b.follow_up_on)
    upcoming = sorted((b for b in open_leads if b.follow_up_on and today < b.follow_up_on <= today + timedelta(days=UPCOMING_DAYS)),
                      key=lambda b: b.follow_up_on)
    replied, awaiting = [], []
    for b in open_leads:
        acts = last.get(b.id, {})
        o, r = acts.get("outreach"), acts.get("reply")
        if r and (not o or r.occurred_at >= o.occurred_at):
            replied.append((b, r))  # their reply is the latest word: your move
        elif o and (now - o.occurred_at).days >= AWAIT_REPLY_DAYS and b not in due and not (b.follow_up_on and b.follow_up_on > today):
            awaiting.append((b, o, (now - o.occurred_at).days))
    contacted_ids = set(last)
    top = sorted((b for b in open_leads if b.id not in contacted_ids and b.score is not None),
                 key=lambda b: -priority(b))[:5]
    drafts = list(session.scalars(select(OutreachDraft).where(OutreachDraft.status == "approved").order_by(OutreachDraft.approved_at)))
    return TodayView(due, upcoming, sorted(replied, key=lambda x: x[1].occurred_at, reverse=True),
                     sorted(awaiting, key=lambda x: -x[2]), [d for d in drafts if not d.business.do_not_contact], top)


@dataclass
class Funnel:
    leads: int = 0
    qualified: int = 0
    contacted: int = 0
    responded: int = 0
    meetings: int = 0
    proposals: int = 0
    won: int = 0
    lost: int = 0
    revenue: dict[str, Decimal] = field(default_factory=dict)
    won_without_value: int = 0

    @property
    def response_rate(self) -> float | None:
        return round(100 * self.responded / self.contacted, 1) if self.contacted else None


def funnel(session: Session, since: date | None) -> tuple[Funnel, dict[str, Funnel]]:
    stages = list(session.scalars(select(PipelineStage).order_by(PipelineStage.position)))
    qualified_pos = next((s.position for s in stages if s.key == "qualified"), 1)
    stmt = select(Business)
    if since:
        stmt = stmt.where(Business.created_at >= datetime.combine(since, datetime.min.time()))
    leads = list(session.scalars(stmt))
    kinds: dict[int, set[str]] = defaultdict(set)
    for bid, kind in session.execute(select(Activity.business_id, Activity.kind).distinct()):
        kinds[bid].add(kind)
    total, by_source = Funnel(), defaultdict(Funnel)
    for b in leads:
        k = kinds.get(b.id, set())
        for f in (total, by_source[b.source]):
            f.leads += 1
            f.qualified += int(b.stage.position >= qualified_pos or "outreach" in k)
            f.contacted += int("outreach" in k)
            f.responded += int("reply" in k)
            f.meetings += int("meeting" in k)
            f.proposals += int("proposal" in k)
            if b.stage.outcome == "won":
                f.won += 1
                if b.deal_value is None:
                    f.won_without_value += 1
                else:
                    f.revenue[b.deal_currency] = f.revenue.get(b.deal_currency, Decimal(0)) + b.deal_value
            f.lost += int(b.stage.outcome == "lost")
    return total, dict(sorted(by_source.items()))


@dataclass
class EffortSummary:
    minutes: int
    cost: dict[str, Decimal]
    by_category: dict[str, int]
    entries: list[EffortLog]

    def per(self, count: int) -> dict:
        """Minutes and QAR cost per unit (e.g. per contacted lead), or None when there is nothing to divide by."""
        if not count:
            return {"minutes": None, "cost": None}
        qar = self.cost.get("QAR")
        return {"minutes": round(self.minutes / count), "cost": round(qar / count, 2) if qar is not None else None}


def effort(session: Session, since: date | None) -> EffortSummary:
    stmt = select(EffortLog).order_by(EffortLog.day.desc(), EffortLog.id.desc())
    if since:
        stmt = stmt.where(EffortLog.day >= since)
    rows = list(session.scalars(stmt))
    cost: dict[str, Decimal] = {}
    by_cat: dict[str, int] = defaultdict(int)
    for r in rows:
        by_cat[r.category] += r.minutes
        if r.cost is not None:
            cost[r.currency] = cost.get(r.currency, Decimal(0)) + r.cost
    return EffortSummary(sum(r.minutes for r in rows), cost, dict(by_cat), rows)


def outreach_sent(session: Session, since: date | None) -> int:
    stmt = select(func.count()).select_from(Activity).where(Activity.kind == "outreach")
    if since:
        stmt = stmt.where(Activity.occurred_at >= datetime.combine(since, datetime.min.time()))
    return session.scalar(stmt) or 0
