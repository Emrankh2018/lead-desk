"""Login off (the default on this computer): pages open directly, but only when addressed to localhost."""
import pytest
from fastapi.testclient import TestClient

from leadgen.config import Settings
from leadgen.web.app import create_app
from tests.ai_helpers import FakeProvider


@pytest.fixture
def local(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, secret_key="k", _env_file=None))  # require_login defaults to off
    app.state.ai_provider_factory = lambda: FakeProvider(available=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c


def test_login_is_off_by_default():
    assert Settings(_env_file=None).require_login is False


def test_pages_open_without_signing_in(local):
    r = local.get("/today")
    assert r.status_code == 200 and 'aria-label="Main"' in r.text and "Sign out" not in r.text
    assert local.get("/login", follow_redirects=False).headers["location"] == "/today"


def test_changes_are_recorded_as_the_owner(local):
    url = local.post("/leads", data={"name": "Karak House"}, follow_redirects=False).headers["location"]
    assert local.get(url).status_code == 200
    assert "owner" in local.get("/audit").text


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1", "[::1]:8765"])
def test_local_names_are_allowed(local, host):
    assert local.get("/today", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8765", "192.168.1.20:8765"])
def test_other_host_names_are_refused_without_login(local, host):
    # A rebinding attack points the attacker's own domain at 127.0.0.1; the browser then sends that name as Host.
    r = local.get("/leads", headers={"host": host})
    assert r.status_code == 403 and "localhost" in r.text


def test_cross_site_posts_are_still_blocked(local):
    r = local.post("/leads", data={"name": "X"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
