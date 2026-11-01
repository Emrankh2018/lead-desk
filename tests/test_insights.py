from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from leadgen.discovery import osm
from leadgen.models import EffortLog, OutreachDraft, PipelineStage
from leadgen.services import analytics, leads
from leadgen.services.leads import LeadInput, create_lead
from tests.test_discovery import ELEMENTS

TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 10, 9, 0)


def make(session, name, source="manual", **kw):
    b, _ = create_lead(session, LeadInput(name=name, **kw), actor="t", source=source, source_ref=None, default_region="QA")
    session.flush()
    return b


def stage(session, key):
    return session.scalars(select(PipelineStage).where(PipelineStage.key == key)).one()


def log(session, b, kind, when, channel="instagram"):
    leads.add_activity(session, b, kind=kind, channel=channel, summary=f"{kind} test", occurred_at=when, actor="t")


def test_today_view_buckets(session):
    due = make(session, "Due Brand", follow_up_on="2026-11-09")
    soon = make(session, "Soon Brand", follow_up_on="2026-11-14")
    replied = make(session, "Replied Brand")
    log(session, replied, "outreach", NOW - timedelta(days=5))
    log(session, replied, "reply", NOW - timedelta(days=1))
    waiting = make(session, "Quiet Brand")
    log(session, waiting, "outreach", NOW - timedelta(days=4))
    fresh = make(session, "Fresh Brand")
    log(session, fresh, "outreach", NOW - timedelta(days=1))
    won = make(session, "Won Brand", follow_up_on="2026-11-01")
    leads.change_stage(session, won, stage(session, "won"), actor="t")
    blocked = make(session, "Blocked Brand", follow_up_on="2026-11-01")
    leads.set_do_not_contact(session, blocked, True, actor="t")
    top = make(session, "Top Brand")
    top.score, top.score_coverage = 90, 80
    d = OutreachDraft(business_id=top.id, channel="email", status="approved", body="hi", approved_at=NOW)
    session.add(d)
    session.flush()
    v = analytics.today_view(session, TODAY, NOW)
    assert [b.name for b in v.due] == ["Due Brand"]  # won and do-not-contact leads never nag
    assert [b.name for b in v.upcoming] == ["Soon Brand"]
    assert [b.name for b, _ in v.replied] == ["Replied Brand"]
    assert [(b.name, days) for b, _, days in v.awaiting] == [("Quiet Brand", 4)]  # fresh outreach not yet "awaiting"
    assert [x.business.name for x in v.approved_drafts] == ["Top Brand"]
    assert v.top_uncontacted[0].name == "Top Brand"
    assert due and soon


def test_funnel_counts_only_recorded_events(session):
    a = make(session, "A", source="osm")
    b = make(session, "B", source="osm")
    c = make(session, "C", source="paste")
    make(session, "D", source="paste")
    log(session, a, "outreach", NOW)
    log(session, a, "reply", NOW)
    log(session, a, "meeting", NOW)
    log(session, b, "outreach", NOW)
    leads.change_stage(session, c, stage(session, "qualified"), actor="t")
    a.deal_value = Decimal("450")
    leads.change_stage(session, a, stage(session, "won"), actor="t")
    leads.change_stage(session, b, stage(session, "won"), actor="t")  # won but no value entered
    session.flush()
    total, by_source = analytics.funnel(session, None)
    assert (total.leads, total.qualified, total.contacted, total.responded, total.meetings, total.won) == (4, 3, 2, 1, 1, 2)
    assert total.response_rate == 50.0 and total.revenue == {"QAR": Decimal("450")} and total.won_without_value == 1
    assert by_source["osm"].contacted == 2 and by_source["paste"].contacted == 0 and by_source["paste"].response_rate is None


def test_effort_summary_and_per_unit(session):
    session.add_all([EffortLog(day=TODAY, minutes=90, category="research"),
                     EffortLog(day=TODAY, minutes=30, cost=Decimal("20"), category="tools"),
                     EffortLog(day=TODAY - timedelta(days=100), minutes=600, category="research")])
    session.flush()
    eff = analytics.effort(session, TODAY - timedelta(days=30))
    assert eff.minutes == 120 and eff.cost == {"QAR": Decimal("20")} and eff.by_category == {"research": 90, "tools": 30}
    assert eff.per(4) == {"minutes": 30, "cost": Decimal("5.00")} and eff.per(0) == {"minutes": None, "cost": None}


def test_benchmark_runs_isolated_and_applies_criteria(settings, tmp_path):
    from leadgen import benchmark
    from tests.research_helpers import make_fetcher, oudnoir_routes
    els = ELEMENTS + [{"type": "node", "id": 50, "tags": {"shop": "cosmetics", "name": "Loret Beauty Salon & Spa", "phone": "44319880"}}]
    report = benchmark.run(settings, country="QA", search=lambda *a, **k: (osm.parse(els), True),
                           fetcher_factory=lambda: make_fetcher(oudnoir_routes()), out_dir=tmp_path)
    s = report["summary"]
    # contactable, relevant businesses: Ahmed Al Maghribi (chain, website), Oud Noir, Existing Gold; salon excluded,
    # Rose Atelier is name-only, 2 unnamed listings skipped
    assert (s["unnamed_listings"], s["service_businesses_excluded"], s["name_only"]) == (2, 2, 1)
    assert s["benchmark_prospects"] == 3 and s["independent_prospects"] == 2 and s["target_met"] is False
    from pathlib import Path
    assert "NOT MET" in Path(report["path"]).read_text(encoding="utf-8")
    # isolated: its own database under data/benchmark/, never the app's main database
    assert (settings.data_dir / "benchmark").as_posix() in s["database"] and "leadgen.sqlite" in s["database"]
    assert settings.db_url != s["database"]
