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
from leadgen.config import Settings, get_settings
from leadgen.db import Database
from leadgen.migrate import upgrade
from leadgen.research.fetcher import Fetcher
from leadgen.research.service import BatchRunner, rescore_all
from leadgen.services.seed import seed_defaults

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


def today_local() -> date:
    return datetime.now(DISPLAY_TZ).date()


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests whose Origin/Referer points at another site (CSRF defence)."""

    async def dispatch(self, request: Request, call_next):
        if request.method in UNSAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            if source and source != "null" and urlsplit(source).netloc != request.headers.get("host"):
                log.warning("blocked cross-origin %s %s from %s", request.method, request.url.path, source)
                return PlainTextResponse("Cross-origin request blocked", status_code=403)
        return await call_next(request)


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
        yield
        db.engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.throttle = LoginThrottle()
    app.state.fetcher_factory = Fetcher  # tests replace this with an offline fetcher
    app.state.batch = BatchRunner(db.sessionmaker, lambda: app.state.fetcher_factory())
    app.state.ai_provider_factory = lambda: make_provider(settings)  # tests replace this with a scripted provider
    app.state.ai = AiWorker(db.sessionmaker, lambda: app.state.ai_provider_factory())

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(WEB_DIR / "templates"), autoescape=True)
    env.filters["local_time"] = local_time
    env.globals.update(ui_lang=settings.ui_lang, ui_dir=settings.ui_dir)
    app.state.templates = Jinja2Templates(env=env)

    app.add_middleware(SameOriginMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret(), session_cookie="leadgen_session",
                       max_age=settings.session_max_age, same_site="strict", https_only=settings.https_only_cookies)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, _exc: LoginRequired):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    from leadgen.web import routes_admin, routes_ai, routes_leads, routes_research
    app.include_router(routes_leads.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_research.router)
    app.include_router(routes_ai.router)
    return app


def get_session(request: Request):
    yield from request.app.state.db.session()


def current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise LoginRequired()
    return user


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = request.session.get("flash", []) + [[kind, message]]


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx["flashes"] = request.session.pop("flash", [])
    ctx["user"] = request.session.get("user")
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status_code)


SessionDep = Depends(get_session)
UserDep = Depends(current_user)
DbSession = Session
