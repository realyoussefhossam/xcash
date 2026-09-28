"""Alert service: severity levels, dedup windows and recovery messages.

Alerting rules encoded here:

* Alerts are a side channel. Every entry point degrades to a log line instead of
  raising, so a Telegram outage can never stall a deposit, a sweep or a scan.
* Repeating conditions (chain lag, low gas, stuck task) are deduplicated with a
  cooldown: the first breach alerts immediately, the condition re-alerts once per
  cooldown window, and a recovery message is sent when it clears. Without this,
  "alert on everything" mode becomes a wall of identical messages.
* One-off money events (deposit credited, sweep result) are never deduplicated —
  each one is a distinct business fact.
"""

from __future__ import annotations

from enum import Enum

import structlog
from django.conf import settings
from django.core.cache import cache

from notifications.telegram import TelegramClient
from notifications.telegram import escape_html

logger = structlog.get_logger()

ALERT_DEDUP_KEY_TEMPLATE = "notifications:alert:{event_key}"
ALERT_ACTIVE_KEY_TEMPLATE = "notifications:active:{event_key}"


class AlertLevel(str, Enum):
    """Severity tiers; the operator picked "alert on everything", so the tier is
    informational only — it drives the emoji prefix and lets filtering be added
    later without touching call sites."""

    CRITICAL = "critical"  # money path broken or money at risk
    MONEY = "money"  # deposit/sweep business events
    INFO = "info"  # everything else


_LEVEL_EMOJI = {
    AlertLevel.CRITICAL: "🚨",
    AlertLevel.MONEY: "💰",
    AlertLevel.INFO: "ℹ️",
}


def build_message(
    *,
    level: AlertLevel,
    title: str,
    lines: tuple[str, ...] | list[str] = (),
) -> str:
    """Render an alert message for Telegram's HTML parse mode."""
    parts = [f"{_LEVEL_EMOJI[level]} <b>{escape_html(title)}</b>"]
    parts.extend(escape_html(line) for line in lines if line)
    return "\n".join(parts)


def claim_dedup_slot(*, event_key: str, cooldown_seconds: int) -> bool:
    """Return True when this condition is allowed to alert right now.

    Cache failures fail open (True): a broken cache must not silence alerts,
    a duplicate message is the cheaper failure mode.
    """
    try:
        return bool(
            cache.add(
                ALERT_DEDUP_KEY_TEMPLATE.format(event_key=event_key),
                "1",
                timeout=cooldown_seconds,
            )
        )
    except Exception as exc:  # noqa: BLE001 — alerting must never propagate
        logger.warning("告警去重缓存不可用，按未去重处理", event_key=event_key, error=str(exc))
        return True


def clear_dedup_slot(*, event_key: str) -> None:
    """Clear the cooldown so the next breach alerts immediately again."""
    try:
        cache.delete(ALERT_DEDUP_KEY_TEMPLATE.format(event_key=event_key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("告警去重缓存清理失败", event_key=event_key, error=str(exc))


def mark_condition_active(*, event_key: str) -> None:
    """Record that a repeating condition is currently alerting.

    The dedup key expires with the cooldown, so recovery signalling needs its
    own long-lived marker: it answers "was the operator told about this and has
    it since cleared?" without sending recovery messages for conditions nobody
    ever alerted on.
    """
    try:
        cache.set(
            ALERT_ACTIVE_KEY_TEMPLATE.format(event_key=event_key),
            "1",
            timeout=None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("告警活跃标记写入失败", event_key=event_key, error=str(exc))


def condition_is_active(*, event_key: str) -> bool:
    try:
        return bool(cache.get(ALERT_ACTIVE_KEY_TEMPLATE.format(event_key=event_key)))
    except Exception:  # noqa: BLE001 — treat unknown as inactive to avoid noise
        return False


def clear_condition_active(*, event_key: str) -> None:
    try:
        cache.delete(ALERT_ACTIVE_KEY_TEMPLATE.format(event_key=event_key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("告警活跃标记清理失败", event_key=event_key, error=str(exc))


class AlertService:
    """Entry point used by event hooks and the watchdog.

    Every public method is wrapped so alerting can never raise into a business
    path: the worst case is a warning log line and a missing notification.
    """

    @classmethod
    def notify(
        cls,
        *,
        event_key: str,
        title: str,
        level: AlertLevel = AlertLevel.INFO,
        lines: tuple[str, ...] | list[str] = (),
        cooldown_seconds: int | None = None,
    ) -> bool:
        """Send an alert, honouring the cooldown for repeating conditions.

        Returns True when a message was actually dispatched.
        """
        try:
            if not TelegramClient().configured:
                return False

            if cooldown_seconds is not None and not claim_dedup_slot(
                event_key=event_key, cooldown_seconds=cooldown_seconds
            ):
                return False

            if cooldown_seconds is not None:
                mark_condition_active(event_key=event_key)

            return cls.send_now(
                build_message(level=level, title=title, lines=lines),
                event_key=event_key,
            )
        except Exception as exc:  # noqa: BLE001 — alerting must never propagate
            logger.exception("告警发送异常（已忽略）", event_key=event_key, error=str(exc))
            return False

    @classmethod
    def notify_recovery(
        cls,
        *,
        event_key: str,
        title: str,
        lines: tuple[str, ...] | list[str] = (),
    ) -> bool:
        """Announce that a previously alerted condition cleared.

        Only sends when the condition was actually alerted on (active marker
        set), then releases both markers so a new breach alerts immediately.
        """
        try:
            if not condition_is_active(event_key=event_key):
                return False
            clear_dedup_slot(event_key=event_key)
            clear_condition_active(event_key=event_key)
            if not TelegramClient().configured:
                return False
            return cls.send_now(
                build_message(level=AlertLevel.INFO, title=title, lines=lines),
                event_key=event_key,
            )
        except Exception as exc:  # noqa: BLE001 — alerting must never propagate
            logger.exception("告警恢复通知异常（已忽略）", event_key=event_key, error=str(exc))
            return False

    @classmethod
    def send_now(cls, text: str, *, event_key: str | None = None) -> bool:
        """Dispatch a pre-rendered message through the async send task.

        Falls back to an inline send when the broker is unreachable, so alerts
        still go out (slower) instead of being dropped when Redis is unhealthy
        — the very situation operators most want to hear about.
        """
        from notifications.tasks import send_alert  # noqa: PLC0415 — avoid Celery import cycle

        try:
            send_alert.delay(text)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("告警入队失败，改为同步发送", event_key=event_key, error=str(exc))
            return cls.deliver(text)

    @classmethod
    def deliver(cls, text: str) -> bool:
        """Synchronously POST the message to Telegram (used by the Celery task,
        the management command and the broker-down fallback)."""
        try:
            return TelegramClient().send_message(text)
        except Exception as exc:  # noqa: BLE001 — client already guards, belt and braces
            logger.warning("Telegram 同步发送异常", error=str(exc))
            return False


def cooldown_seconds() -> int:
    return int(settings.TELEGRAM_ALERT_DEDUP_SECONDS)
