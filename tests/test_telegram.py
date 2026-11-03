from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from leadgen.ai.provider import GeminiProvider, ProviderError, make_provider
from leadgen.config import Settings
from leadgen.discovery import osm
from leadgen.models import Activity, AuditLog, BotPick, Business, OutreachDraft
from leadgen.services.leads import LeadInput, create_lead
from leadgen.web.app import create_app
from tests.ai_helpers import FakeProvider
from tests.research_helpers import make_fetcher, oudnoir_routes
from tests.test_discovery import ELEMENTS

CHAT, OTHER, SECRET, CRON = 4242, 999, "hook-secret", "cron-key"


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str, list]] = []
        self.edits: list[tuple[int, int, str, list]] = []
        self.answers: list[str] = []
        self.webhooks: list[str] = []

    def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons or []))
        return len(self.sent)

    def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append((chat_id, message_id, text, buttons or []))

    def answer(self, callback_id, text=""):
        self.answers.append(text)

    def set_webhook(self, url, secret):
        self.webhooks.append(url)


def data_of(buttons) -> list[str]:
    return [b.get("callback_data") or b.get("url") for row in buttons for b in row]


@pytest.fixture
def bot_settings(tmp_path):
    return Settings(data_dir=tmp_path, secret_key="k", require_login=True, _env_file=None, telegram_bot_token="123:abc",
                    telegram_chat_id=CHAT, telegram_webhook_secret=SECRET, cron_key=CRON, digest_size=3)


@pytest.fixture
def app_ctx(bot_settings):
    app = create_app(bot_settings)
    fake = FakeTelegram()
    app.state.telegram.client = fake
    with TestClient(app, base_url="http://testserver") as c:
        yield app, c, fake


def add(app, name, **kw):
    with app.state.db.sessionmaker() as s:
        biz, _ = create_lead(s, LeadInput(name=name, country="QA", **kw), actor="t", source="manual", source_ref=None,
                             default_region="QA")
        s.commit()
        return biz.id


def tap(client, data, chat=CHAT, message_id=7):
    update = {"callback_query": {"id": "cb", "data": data, "message": {"message_id": message_id, "chat": {"id": chat}}}}
    return client.post("/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})


def test_digest_offers_reachable_leads_once(app_ctx):
    app, _c, fake = app_ctx
    a = add(app, "Cafe One", phone="+974 5555 0001")
    add(app, "No Contact Shop")  # nothing published to reach it: never offered
    b = add(app, "Studio Two", instagram="studiotwo.qa")
    assert app.state.telegram.send_digest(date(2026, 11, 3)) == 2
    texts = [t for _, t, _ in fake.sent]
    assert "2 leads" in texts[0] and any("Cafe One" in t for t in texts) and not any("No Contact" in t for t in texts)
    assert {f"p:{a}", f"s:{a}", f"p:{b}"} <= {d for _, _, btn in fake.sent for d in data_of(btn)}
    fake.sent.clear()
    assert app.state.telegram.send_digest(date(2026, 11, 4)) == 0  # already offered: not repeated
    assert "No new leads" in fake.sent[0][1]


def test_webhook_needs_the_secret_and_ignores_other_chats(app_ctx):
    app, c, fake = app_ctx
    lead = add(app, "Cafe One", phone="+974 5555 0001")
    r = c.post("/telegram/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    assert r.status_code == 403
    assert tap(c, f"s:{lead}", chat=OTHER).status_code == 200
    with app.state.db.sessionmaker() as s:
        assert s.scalars(select(BotPick)).first() is None  # a stranger's tap changed nothing
    tap(c, f"s:{lead}")
    with app.state.db.sessionmaker() as s:
        assert s.scalars(select(BotPick)).one().status == "skipped"
    assert "Skipped" in fake.edits[-1][2]


def test_pursue_draft_approve_and_log_sent(app_ctx):
    app, c, fake = app_ctx
    lead = add(app, "Cafe One", phone="+974 5555 0001", instagram="cafeone.qa")
    app.state.ai_provider_factory = lambda: FakeProvider(
        {"subject": "", "body": "Hello Cafe One, this is Emran. I build websites. Would a short call help?"})
    app.state.ai._status = None
    tap(c, f"p:{lead}")
    channels = data_of(fake.edits[-1][3])
    assert channels == [f"c:{lead}:instagram", f"c:{lead}:whatsapp"]
    tap(c, f"c:{lead}:whatsapp")
    app.state.ai.drain()
    draft_msg = fake.sent[-1]
    assert "WhatsApp draft for Cafe One" in draft_msg[1] and "Hello Cafe One" in draft_msg[1]
    with app.state.db.sessionmaker() as s:
        draft = s.scalars(select(OutreachDraft)).one()
    assert data_of(draft_msg[2]) == [f"a:{draft.id}", f"r:{draft.id}", f"d:{draft.id}"]
    tap(c, f"a:{draft.id}")
    links = data_of(fake.sent[-1][2])
    assert links[0].startswith("https://wa.me/97455550001?text=Hello%20Cafe%20One") and links[1] == f"x:{draft.id}"
    tap(c, f"x:{draft.id}")
    with app.state.db.sessionmaker() as s:
        biz = s.get(Business, lead)
        assert s.get(OutreachDraft, draft.id).status == "sent"
        assert biz.stage.key == "contacted" and biz.follow_up_on is not None
        assert s.scalars(select(Activity).where(Activity.kind == "outreach")).one().channel == "whatsapp"
    assert "Logged as sent" in fake.edits[-1][2]


def test_ai_unavailable_falls_back_to_template(app_ctx):
    app, c, fake = app_ctx
    lead = add(app, "Studio Two", instagram="studiotwo.qa")
    app.state.ai_provider_factory = lambda: FakeProvider(available=False)
    app.state.ai._status = None
    tap(c, f"c:{lead}:instagram")
    assert "template" in fake.sent[-1][1] and "Studio Two" in fake.sent[-1][1]


def test_do_not_contact_blocks_drafts(app_ctx):
    app, c, fake = app_ctx
    lead = add(app, "Cafe One", phone="+974 5555 0001")
    with app.state.db.sessionmaker() as s:
        s.get(Business, lead).do_not_contact = True
        s.commit()
    tap(c, f"c:{lead}:whatsapp")
    assert fake.answers[-1] == "This business is marked do-not-contact"
    with app.state.db.sessionmaker() as s:
        assert s.scalars(select(OutreachDraft)).first() is None


def test_unlinked_bot_tells_start_its_chat_id(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key="k", require_login=True, _env_file=None, telegram_bot_token="1:a",
                        telegram_webhook_secret=SECRET)
    app = create_app(settings)
    fake = FakeTelegram()
    app.state.telegram.client = fake
    with TestClient(app) as c:
        c.post("/telegram/webhook", json={"message": {"chat": {"id": 55}, "text": "/start"}},
               headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert "LEADGEN_TELEGRAM_CHAT_ID=55" in fake.sent[0][1]


def test_cron_needs_key_and_daily_job_finds_researches_and_sends(app_ctx):
    app, c, fake = app_ctx
    assert c.get("/cron/daily?key=wrong").status_code == 404
    app.state.osm_search = lambda country, types, city, cache_dir: (osm.parse(ELEMENTS), False)
    app.state.fetcher_factory = lambda: make_fetcher(oudnoir_routes())
    result = app.state.daily.run_now(date(2026, 11, 3))
    assert result["found"] >= 1 and result["researched"] == 1 and result["offered"] >= 1
    with app.state.db.sessionmaker() as s:
        oud = s.scalars(select(Business).where(Business.name == "Oud Noir")).one()
        assert oud.researched_at is not None and oud.source == "osm"
        assert s.scalars(select(AuditLog).where(AuditLog.action == "daily.run")).one()
    assert "Found" in fake.sent[0][1] and any("Oud Noir" in t for _, t, _ in fake.sent)


def test_no_token_means_no_bot_endpoints(client):
    assert client.post("/telegram/webhook", json={}).status_code == 404
    assert client.get("/cron/daily?key=").status_code == 404


# --- configuration ------------------------------------------------------------------------------------------

def test_postgres_urls_get_the_driver(tmp_path):
    s = Settings(data_dir=tmp_path, database_url="postgresql://u:p@host:5432/postgres", _env_file=None)
    assert s.db_url == "postgresql+psycopg://u:p@host:5432/postgres"
    assert Settings(data_dir=tmp_path, database_url="postgres://u:p@h/db", _env_file=None).db_url.startswith("postgresql+psycopg://")


def test_gemini_provider_defaults_and_free_limit(tmp_path):
    settings = Settings(data_dir=tmp_path, ai_provider="gemini", ai_api_key="key", _env_file=None)
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(429, json={"error": "quota"})

    p = GeminiProvider(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    assert p.model == "gemini-2.5-flash" and p.url.startswith("https://generativelanguage.googleapis.com/")
    with pytest.raises(ProviderError, match="free limit"):
        p.complete_json("s", "u", {"type": "object"})
    assert seen[0].url.path.endswith("/chat/completions")
    no_key = make_provider(Settings(data_dir=tmp_path, ai_provider="gemini", _env_file=None))
    assert no_key.name == "none" and "AI Studio" in no_key.status()[1]
