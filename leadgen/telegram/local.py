"""Run the bot and the morning job from the command line, without the web server (testing on this computer)."""
import logging
import time
from types import SimpleNamespace

from leadgen.ai.provider import make_provider
from leadgen.ai.worker import AiWorker
from leadgen.config import Settings
from leadgen.db import Database
from leadgen.discovery.osm import search as osm_search
from leadgen.migrate import upgrade
from leadgen.research.fetcher import Fetcher
from leadgen.services.seed import seed_defaults
from leadgen.telegram.bot import LeadBot
from leadgen.telegram.client import TelegramClient, TelegramError
from leadgen.telegram.daily import DailyJob

log = logging.getLogger("leadgen.telegram")


def build_state(settings: Settings) -> SimpleNamespace:
    upgrade(settings.db_url)
    db = Database(settings.db_url)
    with db.sessionmaker() as s:
        seed_defaults(s)
        s.commit()
    provider = make_provider(settings)
    ai = AiWorker(db.sessionmaker, lambda: provider)
    bot = LeadBot(settings, db.sessionmaker, TelegramClient(settings.telegram_bot_token), ai) if settings.telegram_bot_token else None
    state = SimpleNamespace(settings=settings, db=db, ai=ai, telegram=bot, osm_search=osm_search, fetcher_factory=Fetcher)
    state.daily = DailyJob(state)
    return state


def poll(state: SimpleNamespace) -> None:
    """Answer Telegram until Ctrl+C. Removes any webhook first: Telegram allows one delivery method at a time."""
    client = state.telegram.client
    client.delete_webhook()
    offset = None
    print("Bot is running on this computer. Press Ctrl+C to stop.")
    while True:
        try:
            updates = client.get_updates(offset)
        except TelegramError as exc:
            log.warning("%s; retrying in 5 s", exc)
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                state.telegram.handle_update(update)
            except Exception:
                log.exception("update failed")
