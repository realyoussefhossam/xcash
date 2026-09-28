"""Celery tasks for Telegram alerting."""

from __future__ import annotations

import structlog
from celery import shared_task

from common.decorators import singleton_task
from notifications.service import AlertService
from notifications.watchdog import AlertWatchdog

logger = structlog.get_logger()


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
