import pytest
from sqlalchemy import select

from leadgen.models import Activity, AuditLog, Business, DuplicateCandidate, PipelineStage
from leadgen.services import leads
from leadgen.services.leads import DuplicateLead, LeadError, LeadInput


def make(session, **kw):
    biz, fuzzy = leads.create_lead(session, LeadInput(**kw), actor="t", source="manual", source_ref=None, default_region="QA")
    session.commit()
    return biz, fuzzy


def test_create_normalises_and_records_history(session):
    biz, _ = make(session, name="Oud House", website="www.oudhouse.qa/", instagram="@OudHouse", phone="5555 1234", notes="met at expo")
    assert (biz.domain, biz.instagram_handle, biz.phone_e164, biz.country) == ("oudhouse.qa", "oudhouse", "+97455551234", "QA")
    assert biz.stage.key == "discovered"
    kinds = {a.kind for a in session.scalars(select(Activity).where(Activity.business_id == biz.id))}
    assert kinds == {"created", "note"}
    assert session.scalars(select(AuditLog).where(AuditLog.action == "lead.create")).one()


def test_social_url_in_website_field_becomes_handle(session):
    biz, _ = make(session, website="https://instagram.com/rose.atelier")
    assert biz.website is None and biz.instagram_handle == "rose.atelier" and biz.name == "@rose.atelier"


@pytest.mark.parametrize("dupe", [{"website": "https://oudhouse.qa/about"}, {"instagram": "oudhouse"}, {"phone": "+974 5555 1234"}])
def test_exact_identifier_duplicates_are_blocked(session, dupe):
    make(session, name="Oud House", website="oudhouse.qa", instagram="oudhouse", phone="55551234")
    with pytest.raises(DuplicateLead):
        leads.create_lead(session, LeadInput(name="Something else", **dupe), actor="t", source="manual", source_ref=None, default_region="QA")


def test_similar_name_creates_review_candidate_not_block(session):
    a, _ = make(session, name="Al Reem Perfumes", city="Doha")
    b, fuzzy = make(session, name="Al Reem Perfumes W.L.L", city="Doha")
    assert [m.business.id for m in fuzzy] == [a.id]
    assert session.scalars(select(DuplicateCandidate)).one().status == "open"
    _, none = make(session, name="Al Reem Perfumes", city="Dubai")  # different city: not flagged
    assert none == []


def test_invalid_input_is_rejected_without_inventing_data(session):
    with pytest.raises(LeadError, match="name, website or Instagram"):
        make(session, city="Doha")
    with pytest.raises(LeadError, match="phone"):
        make(session, name="X", phone="123")
    with pytest.raises(LeadError, match="email"):
        make(session, name="X", email="not-an-email")


def test_merge_fills_gaps_moves_history_and_deletes_other(session):
    keep, _ = make(session, name="Al Reem Perfumes", city="Doha")
    other, _ = make(session, name="Al Reem Perfumes WLL", city="Doha", website="alreem.qa", instagram="alreem")
    leads.add_activity(session, other, kind="note", channel=None, summary="old note", actor="t")
    session.commit()
    leads.merge(session, keep, other, actor="t")
    session.commit()
    assert session.get(Business, other.id) is None
    assert (keep.domain, keep.instagram_handle) == ("alreem.qa", "alreem")
    summaries = [a.summary for a in session.scalars(select(Activity).where(Activity.business_id == keep.id))]
    assert "old note" in summaries and any(s.startswith("Merged duplicate") for s in summaries)
    assert session.scalars(select(DuplicateCandidate)).all() == []


def test_do_not_contact_blocks_outreach_logging(session):
    biz, _ = make(session, name="Quiet Brand")
    leads.set_do_not_contact(session, biz, True, actor="t")
    with pytest.raises(LeadError, match="do-not-contact"):
        leads.add_activity(session, biz, kind="outreach", channel="email", summary="hi", actor="t")
    leads.add_activity(session, biz, kind="note", channel=None, summary="asked us to stop", actor="t")


def test_outreach_sets_last_contacted_and_stage_change_logs(session):
    biz, _ = make(session, name="Brand")
    leads.add_activity(session, biz, kind="outreach", channel="instagram", summary="sent DM", actor="t")
    assert biz.last_contacted_at is not None
    contacted = session.scalars(select(PipelineStage).where(PipelineStage.key == "contacted")).one()
    leads.change_stage(session, biz, contacted, actor="t")
    session.commit()
    assert any(a.kind == "stage_change" and "Contacted" in a.summary for a in biz.activities)


def test_filter_follow_up_due_and_dnc(session):
    from datetime import date
    a, _ = make(session, name="Due", follow_up_on="2026-10-30")
    make(session, name="Later", follow_up_on="2026-12-30")
    c, _ = make(session, name="Blocked")
    leads.set_do_not_contact(session, c, True, actor="t")
    session.commit()
    today = date(2026, 11, 1)
    assert [b.name for b in leads.query_leads(session, leads.LeadFilter(follow_up_due=True), today)] == ["Due"]
    assert "Blocked" not in [b.name for b in leads.query_leads(session, leads.LeadFilter(), today)]
    assert [b.name for b in leads.query_leads(session, leads.LeadFilter(dnc="only"), today)] == ["Blocked"]
