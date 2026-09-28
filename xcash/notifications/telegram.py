"""Minimal Telegram Bot API client for operational alerts.

Only ``sendMessage`` is needed: alerts are push-only, so there is no polling,
no webhook registration and no update handling. Every call is bounded by a
timeout and swallows transport/API errors (returning False), because alerting
is a side channel — a broken bot must never break a deposit, sweep or scan.
"""

from __future__ import annotations

import structlog
import requests
from django.conf import settings

logger = structlog.get_logger()

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramClient:
    """Thin wrapper around the Bot API ``sendMessage`` endpoint."""

    def __init__(self, *, token: str | None = None, chat_id: str | None = None):
        self.token = token if token is not None else settings.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id if chat_id is not None else settings.TELEGRAM_CHAT_ID

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_message(self, text: str, *, disable_notification: bool = False) -> bool:
        """Send a message; returns True only on a confirmed API-level success.

        Never raises: network failures, timeouts, non-200 responses and
        ``{"ok": false}`` payloads all degrade to a warning log plus False.
        """
        if not self.configured:
            logger.debug("Telegram 未配置，跳过告警发送")
            return False

        url = f"{TELEGRAM_API_BASE}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            # HTML is the safest parse mode for operator-facing text: only the
            # three entities we deliberately emit need escaping, unlike Markdown
            # where arbitrary user data (uids, symbols) breaks formatting.
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if disable_notification:
            payload["disable_notification"] = True

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=settings.TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — alerting must never propagate
            logger.warning("Telegram 告警发送失败（网络异常）", error=str(exc))
            return False

        if response.status_code != 200:
            logger.warning(
                "Telegram 告警发送失败（HTTP 状态异常）",
                status_code=response.status_code,
                body=response.text[:200],
            )
            return False

        try:
            body = response.json()
        except ValueError:
            logger.warning("Telegram 告警发送失败（响应非 JSON）", body=response.text[:200])
            return False

        if not body.get("ok"):
            logger.warning("Telegram 告警发送失败（API 返回 ok=false）", body=body)
            return False

        return True

    def get_updates(self, *, offset: int, timeout: int = 0, limit: int = 10):
        """Fetch pending updates for command handling.

        Returns a list of updates, or None when the call failed (so callers can
        leave the offset untouched and retry without dropping messages).
        """
        if not self.configured:
            return None

        url = f"{TELEGRAM_API_BASE}/bot{self.token}/getUpdates"
        try:
            response = requests.post(
                url,
                json={"offset": offset, "timeout": timeout, "limit": limit},
                timeout=settings.TELEGRAM_REQUEST_TIMEOUT_SECONDS + timeout,
            )
        except Exception as exc:  # noqa: BLE001 — polling must never propagate
            logger.debug("Telegram 命令轮询失败（网络异常）", error=str(exc))
            return None

        if response.status_code != 200:
            logger.warning(
                "Telegram 命令轮询失败（HTTP 状态异常）", status_code=response.status_code
            )
            return None

        try:
            body = response.json()
        except ValueError:
            return None

        if not body.get("ok"):
            logger.warning("Telegram 命令轮询失败（API 返回 ok=false）", body=body)
            return None

        return body.get("result") or []


def escape_html(value: object) -> str:
    """Escape text for Telegram's HTML parse mode."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def mask_secret_in_url(url: str) -> str:
    """Hide the credential segment of an RPC URL before it reaches a chat.

    Providers carry the API key in the path (``/v3/<key>``) or in the last
    segment (``/bsc/<key>``). Chat history is long-lived and can be forwarded,
    so the key is replaced with ``***`` while the host stays visible for triage.
    """
    if not url:
        return "(not configured)"
    parts = url.split("/")
    if len(parts) <= 3:
        return url
    parts[-1] = "***"
    return "/".join(parts)
