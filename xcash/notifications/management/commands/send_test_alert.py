"""Send a test Telegram alert to verify bot configuration.

Usage:
    python manage.py send_test_alert
    python manage.py send_test_alert --message "custom text"
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from notifications.commands import alert_brand
from notifications.service import AlertService
from notifications.telegram import TelegramClient


class Command(BaseCommand):
    help = "Send a test alert to the configured Telegram chat (verifies token + chat id)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--message",
            default=None,
            help="Custom message body; defaults to a configuration self-check.",
        )

    def handle(self, *args, **options):
        client = TelegramClient()
        if not client.configured:
            self.stderr.write(
                self.style.ERROR(
                    "Telegram is not configured: set TELEGRAM_BOT_TOKEN and "
                    "TELEGRAM_CHAT_ID in .env (or the environment) and retry."
                )
            )
            return

        text = options["message"] or (
            f"✅ {alert_brand()} alert channel test\n"
            f"chat_id={settings.TELEGRAM_CHAT_ID}\n"
            "If you can read this, deposit/sweep/RPC alerts will arrive here."
        )

        # Deliver synchronously so the operator sees the API verdict immediately
        # instead of a queued task that may fail silently later.
        if AlertService.deliver(text):
            self.stdout.write(self.style.SUCCESS("Test alert delivered."))
        else:
            self.stderr.write(
                self.style.ERROR(
                    "Delivery failed — check the bot token, chat id, and that the "
                    "bot has been started in that chat (see worker logs for the reason)."
                )
            )
