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
    assert r.headers["location"] == "/leads"


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


def test_settings_add_stage_and_icp(auth_client):
    auth_client.post("/settings/stages", data={"name": "Negotiation"})
    auth_client.post("/settings/icps", data={"name": "UAE jewelry", "countries": "ae", "industries": "jewelry, watches", "active": "1"})
    page = auth_client.get("/settings").text
    assert "Negotiation" in page and "UAE jewelry" in page and "AE" in page
