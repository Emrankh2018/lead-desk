import csv
import io

from openpyxl import load_workbook

from tests.conftest import TEST_PASSWORD, TEST_USER


def test_pages_require_login(client):
    for path in ["/leads", "/pipeline", "/import", "/settings", "/audit", "/export?format=csv", "/duplicates"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path


def test_login_rejects_wrong_password_and_throttles(client):
    for _ in range(5):
        assert client.post("/login", data={"username": TEST_USER, "password": "wrong"}).status_code == 401
    r = client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
    assert r.status_code == 429


def test_login_ignores_offsite_next(client):
    r = client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "//evil.example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/today"


def test_cross_origin_post_is_blocked(auth_client):
    r = auth_client.post("/leads", data={"name": "X"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_add_lead_view_and_duplicate_warning(auth_client):
    r = auth_client.post("/leads", data={"name": "Oud House", "website": "oudhouse.qa", "city": "Doha"}, follow_redirects=False)
    assert r.status_code == 303
    page = auth_client.get(r.headers["location"])
    assert "Oud House" in page.text and "Added from manual" in page.text
    dup = auth_client.post("/leads", data={"name": "Other", "website": "https://www.oudhouse.qa/"})
    assert dup.status_code == 409 and "already in your list" in dup.text


def test_stage_change_activity_and_pipeline(auth_client):
    lead_url = auth_client.post("/leads", data={"name": "Brand A"}, follow_redirects=False).headers["location"]
    settings = auth_client.get("/settings").text
    assert "Ready for outreach" in settings
    # stage ids are seeded in order; "contacted" is the 4th
    auth_client.post(f"{lead_url}/stage", data={"stage_id": "4"})
    auth_client.post(f"{lead_url}/activity", data={"kind": "outreach", "channel": "instagram", "summary": "Sent DM",
                                                    "follow_up_on": "2026-11-05"})
    page = auth_client.get(lead_url).text
    assert "Discovered → Contacted" in page and "Sent DM" in page
    assert "follow up 2026-11-05" in auth_client.get("/pipeline").text


def test_export_csv_escapes_formulas_and_xlsx_opens(auth_client):
    auth_client.post("/leads", data={"name": "=HYPERLINK(\"http://x\")", "website": "formula.qa"})
    data = auth_client.get("/export?format=csv").content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(data)))
    assert rows[0]["name"].startswith("'=")
    wb = load_workbook(io.BytesIO(auth_client.get("/export?format=xlsx").content))
    assert wb.active.cell(row=2, column=2).value.startswith("'=")
    assert "export" in auth_client.get("/audit").text


def test_import_preview_then_commit(auth_client):
    content = b"name,website,instagram\nBrand One,one.qa,\nBrand Two,,brandtwo\nBrand One Copy,one.qa,\n"
    preview = auth_client.post("/import", files={"file": ("list.csv", content, "text/csv")})
    assert preview.status_code == 200 and "2 new" in preview.text and "1 repeated in file" in preview.text
    token = preview.text.split('name="token" value="')[1].split('"')[0]
    done = auth_client.post("/import/commit", data={"token": token})
    assert "Imported 2 new leads" in done.text
    assert auth_client.post("/import/commit", data={"token": token}).text.count("expired") == 1  # single use


def test_import_rejects_path_tricks(auth_client):
    assert auth_client.post("/import/commit", data={"token": "../../secret"}).status_code == 400


def test_delete_requires_typed_name(auth_client):
    url = auth_client.post("/leads", data={"name": "Gone Soon"}, follow_redirects=False).headers["location"]
    auth_client.post(f"{url}/delete", data={"confirm_name": "wrong"})
    assert auth_client.get(url).status_code == 200
    auth_client.post(f"{url}/delete", data={"confirm_name": "Gone Soon"})
    assert auth_client.get(url).status_code == 404
    assert "lead.delete" in auth_client.get("/audit").text


def test_research_button_shows_score_and_evidence(auth_client):
    from tests.research_helpers import make_fetcher, oudnoir_routes
    auth_client.app.state.fetcher_factory = lambda: make_fetcher(oudnoir_routes())
    url = auth_client.post("/leads", data={"name": "Oud Noir", "website": "oudnoir.qa", "industry": "perfume", "icp_id": "1"},
                           follow_redirects=False).headers["location"]
    page = auth_client.post(f"{url}/research").text
    assert "Research finished with some pages unavailable" in page
    assert "Public Shopify catalogue lists 4 products" in page and "robots.txt does not allow" in page
    assert "Industry fit" in page and "91%" in page  # all but "independent brand" (needs a directory listing)
    assert "Oud Noir" in auth_client.get("/research").text
    weights = {"industry_fit": "0", "location_fit": "20", "video_gap": "20", "sells_online": "15", "social_presence": "10", "reachable": "10"}
    assert "rescored" in auth_client.post("/settings/scoring/1", data=weights).text
    assert "Industry fit" not in auth_client.get(url).text.split("Evidence")[0].split("Research &amp; score")[1]


def test_batch_skips_recently_failed_sites(auth_client):
    from tests.research_helpers import make_fetcher
    auth_client.app.state.fetcher_factory = lambda: make_fetcher({})  # every site unreachable
    url = auth_client.post("/leads", data={"name": "Down Site", "website": "down.example"}, follow_redirects=False).headers["location"]
    assert "Research 1 never-researched lead" in auth_client.get("/research").text
    assert "could not read the website" in auth_client.post(f"{url}/research").text
    assert "Nothing to research" in auth_client.post("/research/batch", data={"scope": "unresearched"}).text


def _use_fake_ai(client, *responses):
    from leadgen.ai.worker import AiWorker
    from tests.ai_helpers import FakeProvider
    fake = FakeProvider(*responses)
    client.app.state.ai_provider_factory = lambda: fake
    client.app.state.ai = AiWorker(client.app.state.db.sessionmaker, lambda: fake)
    return fake


def test_ai_insight_and_draft_approval_flow(auth_client):
    url = auth_client.post("/leads", data={"name": "Oud Noir", "website": "oudnoir.qa", "email": "info@oudnoir.qa"},
                           follow_redirects=False).headers["location"]
    lead_id = url.rsplit("/", 1)[1]
    auth_client.post(f"{url}/evidence", data={"text": "No reels on Instagram since August", "source_url": "https://www.instagram.com/oudnoir/"})
    _use_fake_ai(auth_client,
                 {"hypotheses": [{"text": "May benefit from short reels.", "evidence_ids": [1]}], "angle": "Reel idea.", "insufficient_evidence": False},
                 {"subject": "A reel idea for Oud Noir", "body": "Hi Oud Noir team, my pilot is QAR 450. Want an example?"})
    auth_client.post(f"{url}/insight")
    auth_client.app.state.ai.drain()
    page = auth_client.get(url).text
    assert "May benefit from short reels." in page and 'href="#ev-1"' in page
    auth_client.post(f"{url}/drafts", data={"channel": "email", "mode": "ai"})
    auth_client.app.state.ai.drain()
    page = auth_client.get(url).text
    assert "A reel idea for Oud Noir" in page and "Approve" in page
    draft_id = page.split('id="draft-')[1].split('"')[0]
    assert "Approve the draft" in auth_client.post(f"/drafts/{draft_id}", data={"action": "sent"}).text
    approved = auth_client.post(f"/drafts/{draft_id}", data={"action": "approve", "subject": "A reel idea for Oud Noir",
                                                             "body": "Hi Oud Noir team, my pilot is QAR 450. Want an example?"}).text
    assert "Open in my email app" in approved and "mailto:info@oudnoir.qa" in approved
    sent = auth_client.post(f"/drafts/{draft_id}", data={"action": "sent", "sent_channel": "email", "follow_up_on": "2026-11-08"}).text
    assert "Logged as sent; follow-up set for 2026-11-08" in sent
    assert "Discovered → Contacted" in sent and "Sent email message (draft #" in sent
    assert "draft.sent_manually" in auth_client.get("/audit").text
    assert lead_id


def test_editing_approved_draft_requires_new_approval(auth_client):
    url = auth_client.post("/leads", data={"name": "Rose Atelier", "instagram": "roseatelier"}, follow_redirects=False).headers["location"]
    page = auth_client.post(f"{url}/drafts", data={"channel": "instagram", "mode": "template"}).text
    draft_id = page.split('id="draft-')[1].split('"')[0]
    auth_client.post(f"/drafts/{draft_id}", data={"action": "approve"})
    page = auth_client.post(f"/drafts/{draft_id}", data={"action": "save", "body": "Hi Rose Atelier, changed text"}).text
    assert "Draft saved and re-checked" in page
    assert f'id="draft-{draft_id}"' in page and page.split(f'id="draft-{draft_id}"')[1].split("</strong>")[1].count("approved") == 0


def test_ai_unavailable_falls_back_and_dnc_blocks_drafts(auth_client):
    url = auth_client.post("/leads", data={"name": "Brand X", "website": "brandx.qa"}, follow_redirects=False).headers["location"]
    page = auth_client.post(f"{url}/drafts", data={"channel": "email", "mode": "ai"}).text
    assert "AI unavailable" in page and "Template draft" in page
    assert "Template draft ready" in auth_client.post(f"{url}/drafts", data={"channel": "email", "mode": "template"}).text
    auth_client.post(f"{url}/dnc", data={"value": "1"})
    assert "no drafts are prepared" in auth_client.post(f"{url}/drafts", data={"channel": "email", "mode": "template"}).text


def test_sender_profile_and_outreach_page(auth_client):
    auth_client.post("/settings/sender", data={"your_name": "Emran", "business_name": "Frame Studio"})
    assert 'value="Emran"' in auth_client.get("/settings").text
    url = auth_client.post("/leads", data={"name": "Gold Co"}, follow_redirects=False).headers["location"]
    auth_client.post(f"{url}/drafts", data={"channel": "instagram", "mode": "template"})
    assert "Gold Co" in auth_client.get("/outreach").text


def test_discover_search_preview_and_add(auth_client):
    from leadgen.discovery import osm
    from tests.test_discovery import ELEMENTS
    auth_client.app.state.osm_search = lambda country, types, city, cache_dir: (osm.parse(ELEMENTS), False)
    form = auth_client.get("/discover").text
    assert 'value="perfumery" checked' in form and 'value="QA"' in form and "OpenStreetMap contributors" in form
    preview = auth_client.post("/discover", data={"country": "QA", "types": ["perfumery", "jewelry"], "icp_id": "1"}).text
    assert "27 listings → 5 businesses" in preview and "2 listings without a name skipped" in preview
    token = preview.split('name="token" value="')[1].split('"')[0]
    oud_key = preview.split('value="node/')[1].split('"')[0]  # first row is a small shop (chains sort last)
    done = auth_client.post("/discover/add", data={"token": token, "keys": [f"node/{oud_key}"]}).text
    assert "Added 1 leads from OpenStreetMap" in done
    assert "osm" in auth_client.get("/audit").text and "1</td></tr>" in auth_client.get("/discover").text.replace(" ", "")


def test_discover_reports_osm_errors(auth_client):
    from leadgen.discovery import osm

    def busy(*a, **k):
        raise osm.OsmError("OpenStreetMap's free server is busy or rate-limiting (HTTP 429); wait a few minutes")
    auth_client.app.state.osm_search = busy
    r = auth_client.post("/discover", data={"country": "QA", "types": ["perfumery"]})
    assert r.status_code == 502 and "busy or rate-limiting" in r.text and "Nothing was added" in r.text


def test_paste_list_preview_and_commit(auth_client):
    preview = auth_client.post("/import/paste", data={"text": "@noorabayas.qa\nOud Noir, oudnoir.qa\n@noorabayas.qa"}).text
    assert "2 new" in preview and "1 repeated in file" in preview
    token = preview.split('name="token" value="')[1].split('"')[0]
    assert "Imported 2 new leads" in auth_client.post("/import/commit", data={"token": token}).text
    assert "source: paste" in auth_client.get("/leads/1").text


def test_add_as_lead_prefill(auth_client):
    page = auth_client.get("/leads/new?website=giftsdoha.qa&notes=Linked%20from%20Oud%20Noir&evil=x").text
    assert 'value="giftsdoha.qa"' in page and "Linked from Oud Noir" in page and "evil" not in page


def test_today_and_analytics_pages(auth_client):
    url = auth_client.post("/leads", data={"name": "Due Co", "follow_up_on": "2020-01-01"}, follow_redirects=False).headers["location"]
    assert auth_client.get("/", follow_redirects=False).headers["location"] == "/today"
    today = auth_client.get("/today").text
    assert "Follow-ups due" in today and "Due Co" in today and "overdue since 2020-01-01" in today
    auth_client.post(f"{url}/activity", data={"kind": "outreach", "channel": "email", "summary": "hello"})
    page = auth_client.get("/analytics?period=30").text
    assert "Contacted" in page and "Nothing is estimated" in page and "manual" in page
    assert "Logged." in auth_client.post("/effort", data={"minutes": "45", "category": "research", "note": "OSM review"}).text
    page = auth_client.get("/analytics").text
    assert "0.8 h" in page and "OSM review" in page and "45 min" in page  # 45 minutes per 1 contacted lead
    assert "Enter minutes or a cost" in auth_client.post("/effort", data={"category": "research"}).text
    assert "check the category" in auth_client.post("/effort", data={"minutes": "5000", "category": "research"}).text


def test_manual_evidence_form(auth_client):
    url = auth_client.post("/leads", data={"name": "Insta Brand", "instagram": "instabrand"}, follow_redirects=False).headers["location"]
    page = auth_client.post(f"{url}/evidence", data={"text": "Posts weekly, no reels", "source_url": "https://www.instagram.com/instabrand/"}).text
    assert "Observation saved" in page and "Posts weekly, no reels" in page and "your observation" in page


def test_settings_add_stage_and_icp(auth_client):
    auth_client.post("/settings/stages", data={"name": "Negotiation"})
    auth_client.post("/settings/icps", data={"name": "UAE jewelry", "countries": "ae", "industries": "jewelry, watches", "active": "1",
                                              "services": "product_ads"})
    page = auth_client.get("/settings").text
    assert "Negotiation" in page and "UAE jewelry" in page and "AE" in page
