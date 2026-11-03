"""Runtime configuration. Values come from environment variables or a git-ignored .env file."""
import re
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # env_ignore_empty: a blank line in .env (e.g. "LEADGEN_TELEGRAM_CHAT_ID=") means "not set yet".
    model_config = SettingsConfigDict(env_prefix="LEADGEN_", env_file=PROJECT_ROOT / ".env", extra="ignore", env_ignore_empty=True)

    data_dir: Path = PROJECT_ROOT / "data"
    database_url: str | None = None  # defaults to sqlite file inside data_dir; a postgres:// URL (e.g. Supabase) also works
    secret_key: str | None = None  # defaults to a generated key persisted in data_dir
    https_only_cookies: bool = False  # set true when served behind HTTPS
    session_max_age: int = 60 * 60 * 12
    # Off for a single owner on this computer (the app only listens on 127.0.0.1). Turn on before hosting it anywhere
    # others can reach, e.g. a VPS; the login page, password hashing and throttling are all still there.
    require_login: bool = False
    local_hosts: list[str] = ["127.0.0.1", "localhost", "::1"]
    default_country: str = "QA"  # ISO region used to parse phone numbers without a country code
    ui_lang: str = "en"
    ui_dir: str = "ltr"  # "rtl" once Arabic strings are added

    # AI provider. "ollama" (local, default), "gemini" (Google AI Studio key; free tier), "openai_compatible" (any /v1/chat/completions server; needs explicit
    # owner approval if it is a paid service), or "none" (template drafts only).
    ai_provider: str = "ollama"
    ai_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    ollama_url: str = "http://127.0.0.1:11434"
    ai_num_gpu: int | None = 0  # GPU layers; 0 = CPU only (reliable on a 4 GB GPU shared with the browser)
    ai_timeout_seconds: float = 300
    ai_base_url: str | None = None  # openai_compatible only
    ai_api_key: str | None = None  # openai_compatible only; keep in .env, never in code

    # Telegram bot (optional). The bot only talks to telegram_chat_id; Telegram proves webhook calls with the secret.
    telegram_bot_token: str | None = None
    telegram_chat_id: int | None = None
    telegram_webhook_secret: str | None = None
    public_url: str | None = None  # e.g. https://lead-desk.onrender.com, used to register the webhook
    cron_key: str | None = None  # protects /cron/daily, which a free scheduler (cron-job.org) opens each morning
    digest_size: int = 8

    @property
    def db_url(self) -> str:
        url = self.database_url or f"sqlite:///{(self.data_dir / 'leadgen.sqlite').as_posix()}"
        # Supabase and Render hand out postgres:// URLs; SQLAlchemy needs the driver named.
        return re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", url)

    def session_secret(self) -> str:
        if self.secret_key:
            return self.secret_key
        key_file = self.data_dir / "secret.key"
        if not key_file.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            key_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        return key_file.read_text(encoding="utf-8").strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
