import httpx
import pytest
from sqlalchemy import select

from leadgen.discovery import osm
from leadgen.discovery import service as discovery
from leadgen.models import Business, DiscoveryRun, Evidence, IcpProfile
from leadgen.research import extract
from leadgen.services import importer
from leadgen.services.leads import LeadInput, create_lead
from tests.research_helpers import site


def el(i, name=None, shop="perfumery", **tags):
    t = {"shop": shop, **tags}
    if name:
        t["name"] = name
    return {"type": "node", "id": i, "tags": t}


# Shaped like the real Qatar results: a 20-branch chain, small shops, a salon and unnamed listings.
ELEMENTS = (
    [el(100 + i, "Ahmed Al Maghribi", brand="Ahmed Al Maghribi", **{"brand:wikidata": "Q123"}, website="ahmedalmaghribi.com")
     for i in range(20)]
    + [el(1, "Oud Noir", website="https://oudnoir.qa/", phone="+974 5555 1234", **{"contact:instagram": "https://instagram.com/oudnoir.qa"}),
       el(2, "Rose Atelier", shop="cosmetics", **{"addr:city": "Lusail"}),
       el(3, "Rose Atelier", shop="cosmetics", **{"addr:city": "Lusail"}),
       el(4, "Glow Salon", shop="beauty"),
       el(5, None), el(6, None),
       el(7, "Existing Gold", shop="jewelry", website="existing-gold.qa")]
)


# --- query building & fetching ------------------------------------------------------------------------------

def test_build_query_country_city_and_types():
    q = osm.build_query("QA", ["perfumery", "jewelry", "not-a-type"], "Al Rayyan")
    assert 'area["ISO3166-1"="QA"]' in q and '"^(perfumery|jewelry)$"' in q and '"^Al Rayyan$",i' in q and "(area.place)" in q


@pytest.mark.parametrize("country,types,city,msg", [
    ("Qatar", ["perfumery"], None, "two-letter"), ("QA", [], None, "at least one"),
    ("QA", ["perfumery"], 'Doha"];out;', "letters, spaces"), ("QA", ["perfumery"], "Doha.*", "letters, spaces"),
])
def test_build_query_rejects_bad_input(country, types, city, msg):
    with pytest.raises(osm.OsmError, match=msg):
        osm.build_query(country, types, city)


def test_categories_from_profile():
    assert osm.categories_for(["perfume", "skincare", "jewelry"], ["abaya"]) == ["perfumery", "cosmetics", "jewelry", "clothes", "boutique"]


def test_search_caches_and_reports_rate_limit(tmp_path):
    calls = []

    def ok(req):
        calls.append(req)
        assert req.headers["user-agent"].startswith("LeadDesk/")
        return httpx.Response(200, json={"elements": ELEMENTS[:3]})

    client = httpx.Client(transport=httpx.MockTransport(ok), headers={"User-Agent": osm.USER_AGENT})
    places, cached = osm.search("QA", ["perfumery"], None, cache_dir=tmp_path, client=client)
    again, cached2 = osm.search("QA", ["perfumery"], None, cache_dir=tmp_path, client=client)
    assert len(places) == len(again) == 3 and (cached, cached2) == (False, True) and len(calls) == 1
    busy = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(osm.OsmError, match="busy or rate-limiting"):
        osm.search("QA", ["jewelry"], None, cache_dir=tmp_path, client=busy)


# --- grouping, classification, adding -----------------------------------------------------------------------

def test_group_places_merges_branches_and_skips_unnamed():
    candidates, unnamed = discovery.group_places(osm.parse(ELEMENTS), None)
    by = {c.name: c for c in candidates}
    assert unnamed == 2 and len(candidates) == 5
    chain = by["Ahmed Al Maghribi"]
    assert chain.branches == 20 and "recognised brand (Wikidata)" in chain.notes and len(chain.osm_urls) == 10
    assert by["Rose Atelier"].branches == 2 and by["Rose Atelier"].city == "Lusail"
    assert by["Oud Noir"].instagram == "oudnoir.qa" and by["Oud Noir"].website == "https://oudnoir.qa/"
    assert any("beauty salon" in n for n in by["Glow Salon"].notes)
    assert candidates[-1].name == "Ahmed Al Maghribi"  # chains sort last


def _icp(session):
    return session.scalars(select(IcpProfile)).first()


def test_classify_and_add_selected(session):
    create_lead(session, LeadInput(name="Existing Gold", website="existing-gold.qa"), actor="t", source="manual",
                source_ref=None, default_region="QA")
    session.commit()
    candidates, _ = discovery.group_places(osm.parse(ELEMENTS), "Doha")
    discovery.classify(session, candidates, "QA")
    by = {c.name: c for c in candidates}
    assert by["Existing Gold"].status == "in_list" and not by["Existing Gold"].selected
    assert by["Oud Noir"].selected and not by["Ahmed Al Maghribi"].selected and not by["Glow Salon"].selected
    run = DiscoveryRun(source="osm", params={}, places_found=len(ELEMENTS), candidates=len(candidates))
    session.add(run)
    session.flush()
    keys = {c.key for c in candidates}  # user ticks everything, including the one already in the list
    result = discovery.add_candidates(session, candidates, keys, run=run, country="QA", icp_id=_icp(session).id,
                                      actor="t", default_region="QA")
    session.commit()
    assert result["created"] == 4 and run.added == 4 and len(result["ids"]) == 4  # explicit ticks override defaults
    oud = session.scalars(select(Business).where(Business.name == "Oud Noir")).one()
    assert (oud.source, oud.phone_e164, oud.instagram_handle, oud.city) == ("osm", "+97455551234", "oudnoir.qa", "Doha")
    assert oud.source_ref == "https://www.openstreetmap.org/node/1"
    listing = session.scalars(select(Evidence).where(Evidence.business_id == oud.id, Evidence.kind == "directory_listing")).one()
    assert listing.value["branches"] == 1
    indep = {r["id"]: r for r in oud.score_breakdown}["independent"]
    assert indep["fraction"] == 1.0
    chain = session.scalars(select(Business).where(Business.name == "Ahmed Al Maghribi")).one()
    assert {r["id"]: r for r in chain.score_breakdown}["independent"]["fraction"] == 0.2


def test_service_businesses_tagged_as_shops_not_preselected(session):
    places = osm.parse([el(10, "Loret Beauty Salon & Spa", shop="cosmetics", phone="44319880"),
                        el(11, "al riyada massage center", shop="boutique", phone="55736093"),
                        el(12, "Rieman Jewelry", shop="jewelry", phone="+974 33334165")])
    candidates, _ = discovery.group_places(places, None)
    discovery.classify(session, candidates, "QA")
    assert {c.name: c.selected for c in candidates} == {"Loret Beauty Salon & Spa": False, "al riyada massage center": False,
                                                         "Rieman Jewelry": True}


def test_name_only_listings_not_preselected(session):
    candidates, _ = discovery.group_places(osm.parse(ELEMENTS), None)
    discovery.classify(session, candidates, "QA")
    rose = next(c for c in candidates if c.name == "Rose Atelier")
    assert not rose.selected and any("name only" in n for n in rose.notes)


def test_possible_duplicate_by_spacing_and_website_vs_instagram(session):
    """Seen live: 'Alsugha Jewelry & Watches' (alsughajewelry.com) vs existing 'Al Sugha Jewelry' (@alsughajewelry)."""
    create_lead(session, LeadInput(name="Al Sugha Jewelry", instagram="alsughajewelry", city="Doha"), actor="t",
                source="csv", source_ref=None, default_region="QA")
    session.commit()
    places = osm.parse([el(9, "Alsugha Jewelry & Watches", shop="jewelry", website="https://www.alsughajewelry.com/")])
    candidates, _ = discovery.group_places(places, None)
    discovery.classify(session, candidates, "QA")
    assert candidates[0].status == "possible_duplicate" and "Instagram handle" in candidates[0].message
    from leadgen.services.dedupe import fuzzy_matches
    assert fuzzy_matches(session, name_key="alsugha jewelry watches", city=None)  # spacing variant alone
    assert not fuzzy_matches(session, name_key="gold", city=None)  # short generic names don't match by containment


def test_directory_evidence_survives_rerunning_research(session):
    from leadgen.research import service as research
    from tests.research_helpers import make_fetcher
    candidates, _ = discovery.group_places(osm.parse(ELEMENTS[20:21]), None)
    run = DiscoveryRun(source="osm", params={})
    session.add(run)
    session.flush()
    discovery.add_candidates(session, candidates, {candidates[0].key}, run=run, country="QA", icp_id=None, actor="t", default_region="QA")
    biz = session.scalars(select(Business)).one()
    research.research_lead(session, biz, actor="t", fetcher=make_fetcher({}))
    kinds = {e.kind for e in session.scalars(select(Evidence).where(Evidence.business_id == biz.id, Evidence.is_current.is_(True)))}
    assert "directory_listing" in kinds


# --- paste and outbound links -------------------------------------------------------------------------------

def test_parse_lines_understands_common_formats():
    rows = importer.parse_lines("@noorabayas.qa\nOud Noir, oudnoir.qa\n• https://www.instagram.com/rose.atelier/\n\nJust A Name\nGold Co\thttps://goldco.qa")
    assert rows == [{"name": "", "instagram": "@noorabayas.qa"}, {"name": "Oud Noir", "website": "oudnoir.qa"},
                    {"name": "", "instagram": "https://www.instagram.com/rose.atelier/"}, {"name": "Just A Name"},
                    {"name": "Gold Co", "website": "https://goldco.qa"}]


def test_outbound_links_skip_services_and_own_site():
    signals, _ = extract.extract_page(site("oudnoir_home.html"), "https://oudnoir.qa/", site_domain="oudnoir.qa")
    out = next(s for s in signals if s.kind == "outbound_links")
    assert out.value["domains"] == ["giftsdoha.qa"]
