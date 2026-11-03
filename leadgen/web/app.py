"""FastAPI application factory, shared dependencies, auth gate and same-origin protection."""
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import jinja2
from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from leadgen.ai.provider import make_provider
from leadgen.ai.worker import AiWorker, fail_interrupted
from leadgen.auth import LoginThrottle
from leadgen.discovery.osm import search as osm_search
from leadgen.config import Settings, get_settings
from leadgen.db import Database
from leadgen.migrate import upgrade
from leadgen.research.fetcher import Fetcher
from leadgen.research.service import BatchRunner, rescore_all
from leadgen import normalize, offerings
from leadgen.research.scoring import best_service
from leadgen.services.seed import seed_defaults
from leadgen.telegram.bot import LeadBot
from leadgen.telegram.client import TelegramClient, TelegramError
from leadgen.telegram.daily import DailyJob

log = logging.getLogger("leadgen")
WEB_DIR = Path(__file__).parent
DISPLAY_TZ = ZoneInfo("Asia/Qatar")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class LoginRequired(Exception):
    pass


def local_time(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not value:
        return ""
    return value.replace(tzinfo=ZoneInfo("UTC")).astimezone(DISPLAY_TZ).strftime(fmt)


def safe_href(url: str | None) -> str:
    """Last line of defence for stored external links: anything but http(s) becomes an inert '#'."""
    return url if url and urlsplit(url.strip()).scheme.lower() in ("http", "https") else "#"


def num(value: float | int | None) -> str:
    """Score points for display: 25.0 -> '25', 13.5 -> '13.5'."""
    if value is None:
        return ""
    return f"{value:g}" if isinstance(value, float) else str(value)


def today_local() -> date:
    return datetime.now(DISPLAY_TZ).date()


LOCAL_OWNER = "owner"  # who the audit log records when login is off


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests whose Origin/Referer points at another site (CSRF defence).

    Without a login, also refuse any request not addressed to this computer by name. Otherwise a web page could
    point its own domain at 127.0.0.1 (DNS rebinding) and read the lead list through the visitor's browser."""

    async def dispatch(self, request: Request, call_next):
        settings = request.app.state.settings
        if not settings.require_login:
            host = urlsplit("//" + (request.headers.get("host") or "")).hostname
            if host not in settings.local_hosts:
                log.warning("blocked request for host %r while login is off", host)
                return PlainTextResponse("Lead Desk only answers on localhost / 127.0.0.1", status_code=403)
        if request.method in UNSAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            if source and source != "null" and urlsplit(source).netloc != request.headers.get("host"):
                log.warning("blocked cross-origin %s %s from %s", request.method, request.url.path, source)
                return PlainTextResponse("Cross-origin request blocked", status_code=403)
        response = await call_next(request)
        # No framing (clickjacking), no MIME sniffing, and lead page URLs are not sent to the sites we link to.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        upgrade(settings.db_url)
        with db.sessionmaker() as s:
            seed_defaults(s)
            rescore_all(s)  # offline and cheap; keeps stored scores in step with the current rubric code
            fail_interrupted(s)
            s.commit()
        if _app.state.telegram and settings.public_url and settings.telegram_webhook_secret:
            try:  # tell Telegram where to deliver button taps; harmless to repeat on every start
                _app.state.telegram.client.set_webhook(settings.public_url.rstrip("/") + "/telegram/webhook",
                                                       settings.telegram_webhook_secret)
            except TelegramError as exc:
                log.error("could not register the Telegram webhook: %s", exc)
        yield
        db.engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.throttle = LoginThrottle()
    app.state.fetcher_factory = Fetcher  # tests replace this with an offline fetcher
    app.state.osm_search = osm_search  # tests replace this with canned OpenStreetMap results
    app.state.batch = BatchRunner(db.sessionmaker, lambda: app.state.fetcher_factory())
    # One provider for the app's lifetime: creating one per call leaked an open HTTP client each time.
    provider = make_provider(settings)
    app.state.ai_provider_factory = lambda: provider  # tests replace this with a scripted provider
    app.state.ai = AiWorker(db.sessionmaker, lambda: app.state.ai_provider_factory())
    app.state.telegram = None
    if settings.telegram_bot_token:
        app.state.telegram = LeadBot(settings, db.sessionmaker, TelegramClient(settings.telegram_bot_token), app.state.ai)
    app.state.daily = DailyJob(app.state)

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB_DIR / "templates"), autoescape=True)
    env.filters["local_time"] = local_time
    env.filters["safe_href"] = safe_href
    env.filters["num"] = num
    env.globals.update(ui_lang=settings.ui_lang, ui_dir=settings.ui_dir, require_login=settings.require_login, service_label=offerings.label, listing_host=normalize.listing_host,
                       best_service=lambda biz: best_service(biz.score_breakdown, biz.icp))
    app.state.templates = Jinja2Templates(env=env)

    app.add_middleware(SameOriginMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret(), session_cookie="leadgen_session",
                       max_age=settings.session_max_age, same_site="strict", https_only=settings.https_only_cookies)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, _exc: LoginRequired):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    from leadgen.web import routes_admin, routes_ai, routes_bot, routes_discovery, routes_insights, routes_leads, routes_research
    app.include_router(routes_bot.router)
    app.include_router(routes_insights.router)
    app.include_router(routes_leads.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_research.router)
    app.include_router(routes_ai.router)
    app.include_router(routes_discovery.router)
    return app


def get_session(request: Request):
    yield from request.app.state.db.session()


def current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user and not request.app.state.settings.require_login:
        return LOCAL_OWNER
    if not user:
        raise LoginRequired()
    return user


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = request.session.get("flash", []) + [[kind, message]]


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["user"] = request.session.get("user") or (None if request.app.state.settings.require_login else LOCAL_OWNER)
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status_code)


SessionDep = Depends(get_session)
UserDep = Depends(current_user)
DbSession = Session
