"""Thin Telegram Bot API client. Only the calls the lead bot needs; messages use HTML formatting."""
import html
import logging

import httpx

log = logging.getLogger("leadgen.telegram")
MAX_TEXT = 4000  # Telegram's limit is 4096 characters per message

# A button is {"text": ..., "callback_data": ...} or {"text": ..., "url": ...}; buttons are given as rows.
Buttons = list[list[dict]]


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def link(text: str, url: str) -> dict:
    return {"text": text, "url": url}


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.Client(timeout=20)

    def _call(self, method: str, **payload) -> dict:
        try:
            resp = self._client.post(f"{self._base}/{method}", json=payload)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The URL holds the bot token, so log only the exception type.
            raise TelegramError(f"Telegram {method} failed ({type(exc).__name__})") from exc
        if not data.get("ok"):
            raise TelegramError(f"Telegram {method} failed: {str(data.get('description', ''))[:200]}")
        return data.get("result") or {}

    def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> int:
        payload = {"chat_id": chat_id, "text": text[:MAX_TEXT], "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("sendMessage", **payload).get("message_id", 0)

    def edit(self, chat_id: int, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_TEXT], "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}, "reply_markup": {"inline_keyboard": buttons or []}}
        try:
            self._call("editMessageText", **payload)
        except TelegramError as exc:
            if "not modified" not in str(exc):  # tapping the same button twice is harmless
                raise

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except TelegramError:
            log.warning("could not answer a button tap (it may be too old)")

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        """Long-poll for updates (running the bot on this computer, where Telegram cannot reach a webhook)."""
        try:
            resp = self._client.post(f"{self._base}/getUpdates", json={"offset": offset, "timeout": timeout},
                                     timeout=timeout + 10)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"Telegram getUpdates failed ({type(exc).__name__})") from exc
        if not data.get("ok"):
            raise TelegramError(f"Telegram getUpdates failed: {str(data.get('description', ''))[:200]}")
        return data.get("result") or []

    def delete_webhook(self) -> None:
        self._call("deleteWebhook")

    def set_webhook(self, url: str, secret: str) -> None:
        self._call("setWebhook", url=url, secret_token=secret, allowed_updates=["message", "callback_query"],
                   drop_pending_updates=False)
