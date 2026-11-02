"""Leads for websites, WhatsApp reply bots and website AI chatbots, for any kind of business (2026-11-02)."""
from decimal import Decimal

from sqlalchemy import select

from leadgen.ai import tasks
from leadgen.discovery import osm
from leadgen.discovery import service as discovery
from leadgen.models import AppSetting, Evidence, IcpProfile, Offer, OutreachDraft
from leadgen.research import extract, scoring
from leadgen.research import service as research
from leadgen.services.leads import LeadInput, create_lead
from leadgen.services.seed import DIGITAL_ICP, seed_defaults
from tests.research_helpers import HTML, make_fetcher


def _digital(session) -> IcpProfile:
    return session.scalars(select(IcpProfile).where(IcpProfile.name == DIGITAL_ICP["name"])).one()


def _lead(session, icp, **kw):
    b, _ = create_lead(session, LeadInput(icp_id=icp.id, country="QA", **kw), actor="t", source="manual",
                       source_ref=None, default_region="QA")
    session.commit()
    return b


def _listing(session, biz, category, branches=1):
    session.add(Evidence(business_id=biz.id, kind="directory_listing", created_by="directory", text="listed",
                         value={"branches": branches, "shop_types": [category]}))
    session.flush()
    session.expire(biz, ["evidence"])
    research.rescore(session, biz)


def _rows(biz):
    return {r["id"]: r for r in biz.score_breakdown}


OLD_SITE = """<html><head><title>Al Noor Dental</title></head><body>
<p>Call us on <a href="tel:+97444001122">4400 1122</a> or <a href="https://wa.me/97455001122">WhatsApp</a>.</p>
<p>© 2019 Al Noor Dental, Doha, Qatar. We look after the whole family with gentle dental care.</p></body></html>"""
MODERN_SITE = """<html><head><title>Bloom Clinic</title><meta name="viewport" content="width=device-width">
<script src="https://embed.tawk.to/abc/default"></script></head><body>
<p>Bloom Clinic in Doha, Qatar. <a href="https://www.fresha.com/book/bloom">Book now</a> for skin and laser care.</p>
<p>© 2026 Bloom Clinic</p></body></html>"""


# --- extraction -------------------------------------------------------------------------------------------

def test_extract_flags_old_insecure_site_without_chat_or_booking():
    signals, _ = extract.extract_page(OLD_SITE, "http://alnoordental.qa/", site_domain="alnoordental.qa")
    by = {s.kind: s for s in signals}
    assert by["mobile_viewport"].value == {"present": False}
    assert by["https"].value == {"secure": False}
    assert by["chat_widget"].value == {"tools": []} and not by["chat_widget"].verified  # absence is not proof
    assert by["online_booking"].value["tools"] == []


def test_extract_detects_chat_tool_and_booking_platform():
    signals, _ = extract.extract_page(MODERN_SITE, "https://bloom.qa/", site_domain="bloom.qa")
    by = {s.kind: s for s in signals}
    assert by["mobile_viewport"].value["present"] and by["https"].value["secure"]
    assert by["chat_widget"].value["tools"] == ["Tawk.to"] and by["chat_widget"].verified
    assert by["online_booking"].value["tools"] == ["Fresha"] and "book now" in by["online_booking"].value["wording"]


# --- scoring ----------------------------------------------------------------------------------------------

def test_name_and_phone_restaurant_is_a_website_lead(session):
    icp = _digital(session)
    b = _lead(session, icp, name="Karak House", phone="+974 5500 1234", city="Doha")
    _listing(session, b, "amenity=restaurant")
    rows = _rows(b)
    assert rows["website_gap"]["fraction"] == 0.9 and "not proof" in rows["website_gap"]["explanation"]
    assert rows["messaging_gap"]["fraction"] == 0.8
    assert rows["customer_questions"]["fraction"] == 1.0
    assert "chat_gap" not in rows  # no website: a site chatbot does not apply, and does not count as missing evidence
    assert not {"video_gap", "sells_online", "social_presence", "industry_fit"} & rows.keys()
    assert scoring.best_service(b.score_breakdown, icp) == "website"
    assert b.score_coverage == 100 and b.score >= 80


def test_old_site_with_whatsapp_link_suggests_whatsapp_bot(session):
    icp = _digital(session)
    b = _lead(session, icp, name="Al Noor Dental", website="alnoordental.qa")
    fetcher = make_fetcher({"https://alnoordental.qa/robots.txt": (404, {}, ""),
                            "https://alnoordental.qa/": (200, HTML, OLD_SITE.replace("© 2019", "© 2025"))})
    research.research_lead(session, b, actor="t", fetcher=fetcher)
    rows = _rows(b)
    assert rows["messaging_gap"]["fraction"] == 1.0
    assert rows["website_gap"]["fraction"] == 0.5 and "not set up for phones" in rows["website_gap"]["explanation"]
    assert rows["chat_gap"]["fraction"] == 0.9
    assert [s for s, _, _ in scoring.service_fits(b.score_breakdown, icp)] == ["whatsapp_bot", "ai_chatbot", "website"]
    assert tasks.pick_service(b) == "whatsapp_bot"


def test_modern_site_with_chat_and_booking_is_a_weak_prospect(session):
    icp = _digital(session)
    b = _lead(session, icp, name="Bloom Clinic", website="bloom.qa")
    fetcher = make_fetcher({"https://bloom.qa/robots.txt": (404, {}, ""), "https://bloom.qa/": (200, HTML, MODERN_SITE)})
    research.research_lead(session, b, actor="t", fetcher=fetcher)
    _listing(session, b, "amenity=clinic")
    rows = _rows(b)
    assert rows["website_gap"]["fraction"] == 0.15 and rows["chat_gap"]["fraction"] == 0.2
    assert rows["customer_questions"]["fraction"] == 0.7  # busy, but already books online
    assert scoring.best_service(b.score_breakdown, icp) is None  # nothing strong enough to pitch


def test_dead_website_is_recorded_as_a_website_opportunity(session):
    icp = _digital(session)
    b = _lead(session, icp, name="Gone Garage", website="gonegarage.qa")
    run = research.research_lead(session, b, actor="t", fetcher=make_fetcher({}))
    assert run.status == "failed" and b.researched_at is None
    assert _rows(b)["website_gap"]["fraction"] == 0.9 and "did not load" in _rows(b)["website_gap"]["explanation"]
    assert "chat_gap" not in _rows(b)


def test_product_ads_profile_keeps_its_original_rubric(session):
    product = session.scalars(select(IcpProfile).where(IcpProfile.name != DIGITAL_ICP["name"])).first()
    b = _lead(session, product, name="Oud Corner", industry="perfume", phone="+974 5500 9999")
    research.rescore(session, b)
    rows = _rows(b)
    assert {"industry_fit", "video_gap", "sells_online"} <= rows.keys()
    assert not {"website_gap", "messaging_gap", "chat_gap", "customer_questions"} & rows.keys()


# --- discovery --------------------------------------------------------------------------------------------

def test_query_mixes_shop_and_other_osm_tags():
    q = osm.build_query("QA", ["perfumery", "amenity=restaurant", "amenity=dentist", "leisure=fitness_centre"], None)
    assert 'nwr["shop"~"^(perfumery)$"](area.country);' in q
    assert 'nwr["amenity"~"^(restaurant|dentist)$"](area.country);' in q
    assert 'nwr["leisure"~"^(fitness_centre)$"](area.country);' in q


def test_parse_records_the_non_shop_category():
    places = osm.parse([{"type": "node", "id": 1, "tags": {"name": "Karak House", "amenity": "restaurant"}},
                        {"type": "node", "id": 2, "tags": {"name": "Oud Noir", "shop": "perfumery"}}])
    assert [p.shop for p in places] == ["amenity=restaurant", "perfumery"]
    assert osm.label_of("amenity=restaurant") == "Restaurants" and osm.label_of("perfumery") == "Perfume shops"


def test_service_profile_preselects_busy_local_businesses():
    picked = osm.categories_for([], [], ["website", "whatsapp_bot"])
    assert "amenity=restaurant" in picked and "amenity=clinic" in picked and "perfumery" not in picked
    assert osm.categories_for([], [], ["product_ads"]) == []


def test_salons_are_selected_for_service_profiles(session):
    places = osm.parse([{"type": "node", "id": 5, "tags": {"name": "Glow Salon", "shop": "beauty", "phone": "+974 5511 2233"}}])
    product, = discovery.group_places(places, "Doha")[0]
    discovery.classify(session, [product], "QA")
    assert not product.selected
    digital, = discovery.group_places(places, "Doha")[0]
    discovery.classify(session, [digital], "QA", ["website", "whatsapp_bot"])
    assert digital.selected and "no website listed" in digital.notes
    assert not any(n.startswith("beauty salon") for n in digital.notes)


# --- defaults ---------------------------------------------------------------------------------------------

def test_seed_adds_service_profile_and_offers_once(session):
    assert _digital(session).services == ["website", "whatsapp_bot", "ai_chatbot"]
    services = {o.service for o in session.scalars(select(Offer))}
    assert {"website", "whatsapp_bot", "ai_chatbot", "product_ads"} <= services
    session.delete(_digital(session))
    session.commit()
    seed_defaults(session)  # the owner deleted it: it must not come back
    assert session.scalars(select(IcpProfile).where(IcpProfile.name == DIGITAL_ICP["name"])).first() is None
    assert session.get(AppSetting, "seed_version").value == {"version": 2}


# --- drafts -----------------------------------------------------------------------------------------------

def test_whatsapp_template_pitches_the_service_without_inventing_a_price(session):
    icp = _digital(session)
    b = _lead(session, icp, name="Karak House", phone="+974 5500 1234")
    _listing(session, b, "amenity=restaurant")
    draft = OutreachDraft(business_id=b.id, channel="whatsapp")
    session.add(draft)
    tasks.template_draft(session, draft)
    assert draft.service == "website" and draft.subject is None
    assert draft.body.startswith("Hello Karak House") and "website" in draft.body and "quote" in draft.body
    assert "QAR" not in draft.body and not draft.warnings


def test_template_uses_the_price_set_for_that_service(session):
    icp = _digital(session)
    session.scalars(select(Offer).where(Offer.service == "whatsapp_bot")).one().price = Decimal("800")
    b = _lead(session, icp, name="Smile Dental", email="hello@smiledental.qa")
    draft = OutreachDraft(business_id=b.id, channel="email", service="whatsapp_bot")
    session.add(draft)
    tasks.template_draft(session, draft)
    assert "QAR 800" in draft.body and draft.subject == "Faster WhatsApp replies for Smile Dental"
    assert not any("price" in w.lower() for w in draft.warnings)


def test_lead_page_shows_what_to_pitch_and_drafts_for_a_chosen_service(auth_client):
    with auth_client.app.state.db.sessionmaker() as s:
        icp_id = _digital(s).id
    url = auth_client.post("/leads", data={"name": "Karak House", "phone": "+974 5500 1234", "icp_id": str(icp_id)},
                           follow_redirects=False).headers["location"]
    page = auth_client.get(url).text
    assert "What to pitch" in page and "best fit" in page and '<option value="website" selected>' in page
    auth_client.post(f"{url}/drafts", data={"channel": "whatsapp", "service": "whatsapp_bot", "mode": "template"})
    page = auth_client.get(url).text
    assert "WhatsApp draft" in page and "(WhatsApp reply bot)" in page and "reply bots" in page
    assert auth_client.post(f"{url}/drafts", data={"channel": "whatsapp", "service": "nope", "mode": "template"}).status_code == 422


def test_discover_suggests_service_categories_and_keeps_a_clean_title(auth_client):
    with auth_client.app.state.db.sessionmaker() as s:
        icp_id = _digital(s).id
    page = auth_client.get(f"/discover?icp_id={icp_id}").text
    assert "<title>Discover · Lead Desk</title>" in page
    assert 'value="amenity=restaurant" checked' in page and 'value="perfumery" checked' not in page


def test_a_zomato_or_google_page_is_not_their_own_website(session):
    icp = _digital(session)
    b = _lead(session, icp, name="Taco Spot", website="https://www.zomato.com/doha/taco-spot", phone="+974 5500 4321")
    run = research.research_lead(session, b, actor="t", fetcher=make_fetcher({}))
    assert run.status == "skipped"  # nothing fetched from Zomato
    rows = _rows(b)
    assert rows["website_gap"]["fraction"] == 1.0 and "zomato.com" in rows["website_gap"]["explanation"]
    assert "chat_gap" not in rows


def test_service_profile_lists_reachable_businesses_without_a_site_first(session):
    places = osm.parse([
        {"type": "node", "id": 1, "tags": {"name": "A Has Site", "amenity": "cafe", "website": "https://asite.qa", "phone": "+974 5511 0001"}},
        {"type": "node", "id": 2, "tags": {"name": "B Name Only", "amenity": "cafe"}},
        {"type": "node", "id": 3, "tags": {"name": "C Phone Only", "amenity": "cafe", "phone": "+974 5511 0003"}},
        {"type": "node", "id": 4, "tags": {"name": "D Google Page", "amenity": "cafe", "website": "https://d.business.site", "phone": "+974 5511 0004"}},
    ])
    candidates, _ = discovery.group_places(places, "Doha")
    discovery.classify(session, candidates, "QA", ["website", "whatsapp_bot"])
    assert [c.name for c in candidates] == ["C Phone Only", "D Google Page", "A Has Site", "B Name Only"]
    assert any("business.site page" in n for n in candidates[1].notes)
