"""Celery tasks for Telegram alerting."""

from __future__ import annotations

import structlog
from celery import shared_task
from django.core.cache import cache

from common.decorators import singleton_task
from notifications.commands import handle_update
from notifications.service import AlertService
from notifications.telegram import TelegramClient
from notifications.watchdog import AlertWatchdog

logger = structlog.get_logger()

COMMAND_OFFSET_CACHE_KEY = "notifications:telegram:update_offset"


@shared_task(ignore_result=True, soft_time_limit=20, time_limit=25)
def send_alert(text: str) -> bool:
    """Deliver one pre-rendered alert message.

    Isolated as a task so business logic only pays the cost of building a string
    and enqueueing; network latency and Telegram outages stay out of the money
    path. Failures are logged inside the client and never retried here — the
    next occurrence of the condition will alert anyway.
    """
    return AlertService.deliver(text)


@shared_task(ignore_result=True, soft_time_limit=100, time_limit=110)
@singleton_task(timeout=115)
def scan_operational_alerts() -> dict[str, int]:
    """Periodic health sweep feeding the Telegram alert channel."""
    counts = AlertWatchdog.run()
    if any(counts.values()):
        logger.warning("告警巡检已推送", **counts)
    return counts


@shared_task(ignore_result=True, soft_time_limit=20, time_limit=25)
@singleton_task(timeout=25)
def poll_telegram_commands() -> int:
    """Answer on-demand bot commands (/status, /rpcs, /deposits).

    Short polling on the beat schedule rather than long polling: each tick is a
    single cheap API call when idle, and holding a worker open for a long-poll
    window would starve scan dispatch. The update offset is persisted in cache
    so restarts never replay old commands, and a failed fetch leaves the offset
    untouched so pending commands are consumed on the next tick.
    """
    client = TelegramClient()
    if not client.configured:
        return 0

    offset = cache.get(COMMAND_OFFSET_CACHE_KEY)
    updates = client.get_updates(offset=int(offset) if offset else 0)
    if updates is None:
        return 0

    handled = 0
    for update in updates:
        reply = handle_update(update)
        if reply:
            client.send_message(reply)
            handled += 1
        else:
            # Also advance for messages we intentionally ignore (other chats,
            # non-commands) so they are not re-fetched forever.
            handled += 0
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            cache.set(COMMAND_OFFSET_CACHE_KEY, update_id + 1, timeout=None)
    return handled
