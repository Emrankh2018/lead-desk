import pytest
from fastapi.testclient import TestClient

from leadgen.auth import set_password
from leadgen.config import Settings
from leadgen.db import Database
from leadgen.migrate import upgrade
from leadgen.services.seed import seed_defaults
from leadgen.web.app import create_app
from tests.ai_helpers import FakeProvider

TEST_USER, TEST_PASSWORD = "owner", "test-password-123"


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, secret_key="test-secret-key", _env_file=None)


@pytest.fixture
def session(settings):
    upgrade(settings.db_url)
    db = Database(settings.db_url)
    with db.sessionmaker() as s:
        seed_defaults(s)
        yield s
    db.engine.dispose()


@pytest.fixture
def client(settings):
    app = create_app(settings)
    app.state.ai_provider_factory = lambda: FakeProvider(available=False)  # tests never call a real model
    with TestClient(app, base_url="http://testserver") as c:
        with app.state.db.sessionmaker() as s:
            set_password(s, TEST_USER, TEST_PASSWORD)
        yield c


@pytest.fixture
def auth_client(client):
    r = client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/leads"}, follow_redirects=False)
    assert r.status_code == 303
    return client
