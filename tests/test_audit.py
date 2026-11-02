"""Regression tests for defects found in the full audit of 2026-11-01. Each failed before its fix."""
from datetime import datetime

import pytest
from sqlalchemy import select

from leadgen import normalize
from leadgen.models import Activity
from leadgen.services.leads import LeadError, LeadInput, create_lead


# 1. Stored XSS: a non-http(s) "website" was accepted and rendered as a clickable href.
@pytest.mark.parametrize("url", ["javascript://x.com/%0aalert(1)", "data://x.com/text", "ftp://files.example.com/"])
def test_website_rejects_non_http_schemes(url):
    assert normalize.website_url(url) is None and normalize.domain_of(url) is None


def test_website_still_accepts_normal_input():
    assert normalize.website_url("Brand.qa") == "https://Brand.qa"
    assert normalize.website_url("http://brand.qa/shop") == "http://brand.qa/shop"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://evil.example/in/brand", "linkedin.com/company/x\" onclick=x"])
def test_linkedin_url_must_be_linkedin_https(session, url):
    with pytest.raises(LeadError, match="LinkedIn"):
        create_lead(session, LeadInput(name="X", linkedin_url=url), actor="t", source="manual", source_ref=None, default_region="QA")


def test_linkedin_url_normalised(session):
    b, _ = create_lead(session, LeadInput(name="X", linkedin_url="linkedin.com/company/brand-x/"), actor="t",
                       source="manual", source_ref=None, default_region="QA")
    assert b.linkedin_url == "https://linkedin.com/company/brand-x/"


def test_manual_evidence_source_must_be_http(session):
    from leadgen.research import service
    b, _ = create_lead(session, LeadInput(name="X"), actor="t", source="manual", source_ref=None, default_region="QA")
    with pytest.raises(ValueError):
        service.add_manual_evidence(session, b, text="obs", source_url="javascript://x.com/%0aalert(1)", actor="t")


# 2. Open redirect: "/\evil.example" passed the same-site check; browsers treat it as //evil.example.
@pytest.mark.parametrize("target", ["/\\evil.example", "/\\/evil.example", "//evil.example", "https://evil.example", "/%5Cevil.example"])
def test_safe_next_rejects_offsite(target):
    from leadgen.web.routes_leads import _safe_next
    assert _safe_next(target) == "/today"


# 3. Activity "When" was entered in Doha time but stored as if UTC, showing 3 hours late.
def test_activity_time_entered_in_doha_is_stored_as_utc(auth_client):
    url = auth_client.post("/leads", data={"name": "Time Co"}, follow_redirects=False).headers["location"]
    auth_client.post(f"{url}/activity", data={"kind": "note", "summary": "call", "occurred_at": "2026-11-01T10:00"})
    with auth_client.app.state.db.sessionmaker() as s:
        act = s.scalars(select(Activity).where(Activity.summary == "call")).one()
    assert act.occurred_at == datetime(2026, 11, 1, 7, 0)
    assert "2026-11-01 10:00" in auth_client.get(url).text


# 4. A misconfigured provider (openai_compatible without a URL) raised on every page that shows AI status.
def test_misconfigured_provider_degrades_instead_of_crashing():
    from leadgen.ai.provider import make_provider
    from leadgen.config import Settings
    p = make_provider(Settings(ai_provider="openai_compatible", ai_base_url=None, _env_file=None))
    ok, msg = p.status()
    assert ok is False and "LEADGEN_AI_BASE_URL" in msg


def test_templates_neutralise_unsafe_stored_links(auth_client):
    """Defence in depth: even a bad value already in the database (older data, direct edits) renders inert."""
    from leadgen.models import Business
    from leadgen.web.app import safe_href
    assert safe_href("javascript:alert(1)") == "#" and safe_href(" JavaScript:x") == "#" and safe_href(None) == "#"
    assert safe_href("https://brand.qa/") == "https://brand.qa/"
    url = auth_client.post("/leads", data={"name": "Legacy Co"}, follow_redirects=False).headers["location"]
    with auth_client.app.state.db.sessionmaker() as s:
        b = s.get(Business, int(url.rsplit("/", 1)[1]))
        b.website, b.domain, b.linkedin_url = "javascript:alert(1)", "legacy.qa", "javascript:alert(2)"
        s.commit()
    page = auth_client.get(url).text
    # The stored value may still appear as text in the edit form; it must never become a link.
    assert 'href="javascript:' not in page.lower() and "legacy.qa" in page


# 6. Merging deleted the removed record's outreach drafts (including approved ones) through the cascade.
def test_merge_keeps_drafts_of_the_removed_record(session):
    from leadgen.models import OutreachDraft
    from leadgen.services import leads
    keep, _ = create_lead(session, LeadInput(name="Al Reem Perfumes", city="Doha"), actor="t", source="manual", source_ref=None, default_region="QA")
    other, _ = create_lead(session, LeadInput(name="Al Reem Perfumes WLL", city="Doha"), actor="t", source="manual", source_ref=None, default_region="QA")
    session.add(OutreachDraft(business_id=other.id, channel="email", status="approved", body="Hi Al Reem"))
    session.commit()
    leads.merge(session, keep, other, actor="t")
    session.commit()
    drafts = session.scalars(select(OutreachDraft)).all()
    assert [(d.business_id, d.status) for d in drafts] == [(keep.id, "approved")]


# 7. Deleting a lead must remove everything about it (privacy: deletion on request).
def test_delete_removes_all_related_rows(session):
    from leadgen.models import AiInsight, DuplicateCandidate, Evidence, OutreachDraft, ResearchRun
    from leadgen.services import leads
    b, _ = create_lead(session, LeadInput(name="Gone Co", website="gone.qa"), actor="t", source="manual", source_ref=None, default_region="QA")
    create_lead(session, LeadInput(name="Gone Co LLC"), actor="t", source="manual", source_ref=None, default_region="QA")
    run = ResearchRun(business_id=b.id, status="ok")
    session.add(run)
    session.flush()
    session.add_all([Evidence(business_id=b.id, run_id=run.id, kind="page_title", text="t", value={}),
                     AiInsight(business_id=b.id, status="ready"), OutreachDraft(business_id=b.id, channel="email", body="x")])
    session.commit()
    leads.delete_lead(session, b, actor="t")
    session.commit()
    for model in (Evidence, ResearchRun, AiInsight, OutreachDraft, Activity):
        assert session.scalars(select(model).where(model.business_id == b.id)).all() == [], model.__name__
    assert session.scalars(select(DuplicateCandidate)).all() == []


# 8. Today's "best not-yet-contacted" ranked a 94 at 59% coverage above a researched 90 at 100%.
def test_today_ranking_accounts_for_evidence_coverage(session):
    from datetime import date
    from leadgen.services import analytics
    thin, _ = create_lead(session, LeadInput(name="Thin"), actor="t", source="manual", source_ref=None, default_region="QA")
    solid, _ = create_lead(session, LeadInput(name="Solid"), actor="t", source="manual", source_ref=None, default_region="QA")
    thin.score, thin.score_coverage = 94, 59
    solid.score, solid.score_coverage = 90, 100
    session.flush()
    top = analytics.today_view(session, date(2026, 11, 10), datetime(2026, 11, 10, 9)).top_uncontacted
    assert [b.name for b in top[:2]] == ["Solid", "Thin"]


# 9. Abandoned import/discovery previews piled up in data/imports forever.
def test_stale_previews_are_cleaned_up(auth_client):
    import os
    import time
    folder = auth_client.app.state.settings.data_dir / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    old = folder / ("x" * 30 + ".json")
    old.write_text("{}", encoding="utf-8")
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    auth_client.post("/import/paste", data={"text": "brand.qa"})
    assert not old.exists() and len(list(folder.glob("*.json"))) == 1


# 5. The lead page used <meta refresh> while AI jobs ran, discarding anything typed into a draft.
def test_lead_page_refresh_does_not_discard_typing(auth_client):
    from leadgen.models import AiInsight
    url = auth_client.post("/leads", data={"name": "Busy Co"}, follow_redirects=False).headers["location"]
    with auth_client.app.state.db.sessionmaker() as s:
        s.add(AiInsight(business_id=int(url.rsplit("/", 1)[1])))  # pending job
        s.commit()
    page = auth_client.get(url).text
    assert 'http-equiv="refresh"' not in page and "data-autorefresh" in page


# UI redesign audit (2026-11-01): pages could be framed by another site, and score points showed as "25.0".
def test_pages_send_basic_security_headers(auth_client):
    r = auth_client.get("/today")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"


def test_score_points_drop_trailing_zero():
    from leadgen.web.app import num
    assert num(25.0) == "25" and num(13.5) == "13.5" and num(7) == "7" and num(None) == ""


def test_every_page_renders_with_the_new_layout(auth_client):
    auth_client.post("/leads", data={"name": "Layout Co", "website": "layout.qa"})
    for path in ["/today", "/leads", "/leads/1", "/leads/new", "/pipeline", "/outreach", "/discover", "/import",
                 "/research", "/duplicates", "/analytics", "/settings", "/audit", "/leads/1/delete"]:
        r = auth_client.get(path)
        assert r.status_code == 200, path
        assert 'aria-label="Main"' in r.text and 'class="page-head"' in r.text, path
