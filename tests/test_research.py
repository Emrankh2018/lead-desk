from types import SimpleNamespace

import pytest
from sqlalchemy import select

from leadgen.models import Evidence, ResearchRun
from leadgen.research import extract, scoring, service
from leadgen.research.fetcher import Fetcher
from leadgen.services.leads import LeadInput, create_lead
from tests.research_helpers import HTML, make_fetcher, oudnoir_routes, site


def kinds(signals):
    return {s.kind for s in signals}


# --- extraction ---------------------------------------------------------------------------------------------

def test_extract_qatar_shopify_store():
    signals, extra = extract.extract_page(site("oudnoir_home.html"), "https://oudnoir.qa/", site_domain="oudnoir.qa")
    by = {s.kind: s for s in signals}
    assert by["platform"].value["platforms"] == ["Shopify"]
    assert by["structured_products"].value["count"] == 2
    assert by["video_presence"].value == {"video_tags": 0, "video_embeds": 0, "video_files": 0}
    assert set(by["qatar_mentions"].value["markers"]) >= {"QAR", "Doha", "Qatar", "ر.ق"}
    assert by["copyright_year"].value["year"] == 2025
    socials = [s.value for s in signals if s.kind == "social_link"]
    assert {"network": "instagram", "url": "https://www.instagram.com/oudnoir.qa/", "handle": "oudnoir.qa"} in socials
    assert sum(1 for s in socials if s["network"] == "instagram") == 1  # bare instagram.com link ignored
    emails = [s.value["email"] for s in signals if s.kind == "published_email"]
    assert emails == ["info@oudnoir.qa"]  # script text ("help@cdn-assets.png") is not page content
    assert extra == ["https://oudnoir.qa/pages/contact", "https://oudnoir.qa/pages/about-us"]


def test_extract_video_heavy_site_and_redirect_country():
    signals, _ = extract.extract_page(site("goldline_ae.html"), "https://ae.goldline.com/", site_domain="goldline.com")
    video = next(s for s in signals if s.kind == "video_presence").value
    assert video["video_tags"] == 1 and video["video_embeds"] == 1 and video["video_files"] >= 1
    assert "qatar_mentions" not in kinds(signals)
    r = extract.redirect_signal("https://goldline.com", "https://ae.goldline.com/en")
    assert r.value["country_hint"] == "AE"
    assert extract.redirect_signal("https://brand.qa", "https://www.brand.qa/") is None
    assert extract.redirect_signal("https://brand.com", "https://brand.com/ae/").value["country_hint"] == "AE"


def test_thin_javascript_page_is_flagged_unverified():
    signals, _ = extract.extract_page(site("js_app.html"), "https://app.example/", site_domain="app.example")
    thin = next(s for s in signals if s.kind == "thin_content")
    assert thin.verified is False


# --- fetcher ------------------------------------------------------------------------------------------------

def test_fetcher_respects_robots_and_stops_on_rate_limit():
    log = []
    routes = oudnoir_routes() | {
        "https://slow.example/robots.txt": (200, {}, ""),
        "https://slow.example/": (429, {}, "slow down"),
    }
    f = make_fetcher(routes, log)
    assert "robots.txt does not allow" in f.get("https://oudnoir.qa/pages/about-us").issue
    assert "https://oudnoir.qa/pages/about-us" not in log
    first = f.get("https://slow.example/")
    assert first.status == 429 and "rate-limited" in first.issue
    again = f.get("https://slow.example/other")
    assert "not retried" in again.issue
    assert log.count("https://slow.example/") == 1 and "https://slow.example/other" not in log


def test_fetcher_follows_redirects_safely_and_robots_redirect():
    routes = {
        "http://brand.qa/robots.txt": (301, {"location": "https://www.brand.qa/robots.txt"}, ""),
        "https://www.brand.qa/robots.txt": (200, {}, "User-agent: *\nDisallow: /\n"),
        "http://brand.qa/": (301, {"location": "https://www.brand.qa/"}, ""),
    }
    res = make_fetcher(routes).get("http://brand.qa/")
    assert not res.ok and "robots.txt does not allow" in res.issue  # disallow behind a robots redirect is honoured


def test_fetcher_refuses_private_addresses_and_non_http():
    f = Fetcher(delay_seconds=0)
    try:
        assert "private or local" in f.get("http://127.0.0.1:8765/").issue
        assert "only public http" in f.get("file:///C:/Windows/win.ini").issue
    finally:
        f.close()


def test_fetcher_size_limit(monkeypatch):
    import leadgen.research.fetcher as fetcher_mod
    monkeypatch.setattr(fetcher_mod, "MAX_BYTES", 100)
    routes = {"https://big.example/robots.txt": (404, {}, ""), "https://big.example/": (200, HTML, "x" * 500)}
    assert "larger than" in make_fetcher(routes).get("https://big.example/").issue


# --- scoring ------------------------------------------------------------------------------------------------

ICP = SimpleNamespace(industries=["perfume", "oud"], keywords=["bakhoor"], countries=["QA"], scoring_weights={})


def biz(**kw):
    base = dict(industry=None, country=None, instagram_handle=None, email_public=None, phone_e164=None,
                linkedin_url=None, do_not_contact=False)
    return SimpleNamespace(**(base | kw))


def test_score_without_evidence_is_unknown_not_zero():
    value, coverage, rows = scoring.score(biz(), [], ICP)
    assert value == 0 and coverage == 10  # only "reachable" is decidable (no contact route → 0)
    assert all(r["fraction"] is None for r in rows if r["id"] != "reachable")


def test_score_explains_each_criterion_with_evidence_ids():
    ev = [scoring.Ev(1, "page_title", {}, True, "Page title: “Oud Noir | Luxury Oud & Perfumes in Doha”"),
          scoring.Ev(2, "qatar_mentions", {"markers": ["QAR", "Doha"]}, True, "mentions"),
          scoring.Ev(3, "video_presence", {"video_tags": 0, "video_embeds": 0, "video_files": 0}, True, "no video"),
          scoring.Ev(4, "product_count", {"count": 40}, True, "40 products"),
          scoring.Ev(5, "social_link", {"network": "instagram"}, True, "ig"),
          scoring.Ev(6, "published_email", {"email": "info@x.qa"}, True, "email")]
    value, coverage, rows = scoring.score(biz(), ev, ICP)
    assert value == 100 and coverage == 100
    by = {r["id"]: r for r in rows}
    assert by["industry_fit"]["evidence_ids"] == [1] and "oud" in by["industry_fit"]["explanation"]
    assert by["video_gap"]["evidence_ids"] == [3]


def test_score_penalises_other_country_redirect_and_existing_video():
    ev = [scoring.Ev(1, "redirect", {"country_hint": "AE"}, True, "redirect"),
          scoring.Ev(2, "video_presence", {"video_tags": 3, "video_embeds": 0, "video_files": 2}, True, "video")]
    _, _, rows = scoring.score(biz(country="QA"), ev, ICP)
    by = {r["id"]: r for r in rows}
    assert by["location_fit"]["fraction"] == 0.3 and by["video_gap"]["fraction"] == 0.3


def test_zero_weight_disables_criterion():
    icp = SimpleNamespace(**(vars(ICP) | {"scoring_weights": {"video_gap": 0}}))
    _, _, rows = scoring.score(biz(), [], icp)
    assert "video_gap" not in {r["id"] for r in rows}


# --- service (end to end, offline) --------------------------------------------------------------------------

def _lead(session, **kw):
    from leadgen.models import IcpProfile
    icp = session.scalars(select(IcpProfile)).first()
    b, _ = create_lead(session, LeadInput(icp_id=icp.id, **kw), actor="t", source="manual", source_ref=None, default_region="QA")
    session.commit()
    return b


def test_research_lead_stores_evidence_fills_contacts_and_scores(session):
    b = _lead(session, name="Oud Noir", website="oudnoir.qa", industry="perfume")
    run = service.research_lead(session, b, actor="t", fetcher=make_fetcher(oudnoir_routes()))
    session.commit()
    assert run.status == "partial"  # about-us page blocked by robots
    assert any("robots.txt does not allow" in i for i in run.issues)
    ev = list(session.scalars(select(Evidence).where(Evidence.business_id == b.id, Evidence.is_current.is_(True))))
    by_kind = {e.kind for e in ev}
    assert {"platform", "product_count", "published_email", "published_phone", "social_link", "fetch_issue"} <= by_kind
    assert all(e.source_url for e in ev if e.kind != "fetch_issue")
    assert b.email_public == "info@oudnoir.qa" and b.email_source_url.rstrip("/") == "https://oudnoir.qa"
    assert b.instagram_handle == "oudnoir.qa"
    assert b.phone_e164 == "+97455551234"  # from the contact page's tel: link
    assert b.score is not None and b.score >= 85 and b.score_coverage == 100
    # injected instructions on the contact page are not turned into evidence
    assert not any("50,000" in e.text for e in ev)


def test_rerun_replaces_current_evidence_and_keeps_history(session):
    b = _lead(session, name="Oud Noir", website="oudnoir.qa")
    service.research_lead(session, b, actor="t", fetcher=make_fetcher(oudnoir_routes()))
    first_count = len(session.scalars(select(Evidence).where(Evidence.business_id == b.id)).all())
    service.research_lead(session, b, actor="t", fetcher=make_fetcher(oudnoir_routes()))
    session.commit()
    total = session.scalars(select(Evidence).where(Evidence.business_id == b.id)).all()
    current = [e for e in total if e.is_current]
    assert len(total) == 2 * first_count and len(current) == first_count


def test_unreachable_site_fails_honestly_and_keeps_user_data(session):
    b = _lead(session, name="Gone Brand", website="gone.example", email="owner@gone.example")
    run = service.research_lead(session, b, actor="t", fetcher=make_fetcher({}))
    session.commit()
    assert run.status == "failed" and run.pages_fetched == 0
    assert b.email_public == "owner@gone.example" and b.researched_at is None
    assert session.scalars(select(ResearchRun)).one().issues


def test_no_website_is_skipped_but_scored_from_known_details(session):
    b = _lead(session, name="Insta Only", instagram="instaonly.qa", industry="oud perfume")
    run = service.research_lead(session, b, actor="t", fetcher=make_fetcher({}))
    assert run.status == "skipped"
    by = {r["id"]: r for r in b.score_breakdown}
    # website-only criteria stay unknown (35% of default weight), so coverage shows it is under-researched
    assert by["video_gap"]["fraction"] is None and by["sells_online"]["fraction"] is None
    assert b.score_coverage == 65


def test_manual_observation_requires_text_and_web_source(session):
    b = _lead(session, name="Brand")
    with pytest.raises(ValueError):
        service.add_manual_evidence(session, b, text=" ", source_url="", actor="t")
    with pytest.raises(ValueError):
        service.add_manual_evidence(session, b, text="posts weekly", source_url="javascript:alert(1)", actor="t")
    ev = service.add_manual_evidence(session, b, text="Posts 3x/week, no reels", source_url="https://www.instagram.com/brand/", actor="t")
    assert ev.created_by == "user" and ev.verified
