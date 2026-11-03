"""Machine endpoints: the Telegram webhook and the morning job trigger. Each is protected by its own secret, not the
login session, and answers 404 when its feature is not configured."""
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from leadgen.web.app import today_local

log = logging.getLogger("leadgen.telegram")
router = APIRouter()


def _matches(given: str | None, expected: str | None) -> bool:
    return bool(expected) and hmac.compare_digest((given or "").encode(), expected.encode())


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    bot, settings = request.app.state.telegram, request.app.state.settings
    if not bot:
        raise HTTPException(404)
    if not _matches(request.headers.get("x-telegram-bot-api-secret-token"), settings.telegram_webhook_secret):
        raise HTTPException(403)
    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(400) from None
    try:
        await run_in_threadpool(bot.handle_update, update)
    except Exception:
        # Answer 200 anyway: Telegram would otherwise resend the same update again and again.
        log.exception("Telegram update failed")
    return JSONResponse({"ok": True})


@router.get("/cron/daily")
def cron_daily(request: Request, key: str = ""):
    if not _matches(key, request.app.state.settings.cron_key):
        raise HTTPException(404)
    started = request.app.state.daily.start(today_local())
    return PlainTextResponse("started" if started else "already running", status_code=202 if started else 200)
