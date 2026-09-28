"""Behavioral tests for the Telegram alerting layer.

Covered contracts (things that would silently break alerting or, worse, break a
money path):
* the client never raises and reports failure for any transport/API failure
* alerts are inert when unconfigured (no bot/chat configured)
* repeating conditions deduplicate inside the cooldown
* recovery messages only fire for conditions that were actually alerted, and
  release the cooldown for the next breach
* the watchdog detects a stalled scanner and a rejected RPC key from state alone
* deposit alerts carry the fields an operator needs to act
"""

from datetime import timedelta
from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone

from chains.models import Chain
from chains.models import ChainType
from evm.models import EvmScanCursor
from notifications import events
from notifications.service import AlertLevel
from notifications.service import AlertService
from notifications.service import build_message
from notifications.telegram import TelegramClient
from notifications.watchdog import AlertWatchdog
from notifications.watchdog import looks_like_auth_error


class TelegramClientTests(TestCase):
    def test_reports_failure_on_http_error_without_raising(self):
        response = Mock(status_code=500, text="boom")
        with patch("notifications.telegram.requests.post", return_value=response):
            with override_settings(
                TELEGRAM_BOT_TOKEN="token", TELEGRAM_CHAT_ID="chat"
            ):
                self.assertFalse(TelegramClient().send_message("hi"))

    def test_reports_failure_on_network_exception_without_raising(self):
        with patch(
            "notifications.telegram.requests.post",
            side_effect=TimeoutError("no route"),
        ):
            with override_settings(
                TELEGRAM_BOT_TOKEN="token", TELEGRAM_CHAT_ID="chat"
            ):
                self.assertFalse(TelegramClient().send_message("hi"))

    def test_reports_failure_when_api_returns_not_ok(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ok": False, "description": "chat not found"}
        with patch("notifications.telegram.requests.post", return_value=response):
            with override_settings(
                TELEGRAM_BOT_TOKEN="token", TELEGRAM_CHAT_ID="chat"
            ):
                self.assertFalse(TelegramClient().send_message("hi"))

    def test_reports_success_on_ok_payload(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True}
        with patch("notifications.telegram.requests.post", return_value=response) as post:
            with override_settings(
                TELEGRAM_BOT_TOKEN="token", TELEGRAM_CHAT_ID="chat"
            ):
                self.assertTrue(TelegramClient().send_message("hi"))
        # Timeout must be set — an unbounded alert call could hang a worker.
        self.assertIn("timeout", post.call_args.kwargs)

    def test_unconfigured_client_is_inert(self):
        with override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID=""):
            self.assertFalse(TelegramClient().configured)
            self.assertFalse(TelegramClient().send_message("hi"))


@override_settings(
    TELEGRAM_BOT_TOKEN="token",
    TELEGRAM_CHAT_ID="chat",
    TELEGRAM_ALERT_DEDUP_SECONDS=1800,
)
class AlertServiceTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_unconfigured_service_does_not_send(self):
        with override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID=""):
            with patch.object(AlertService, "send_now") as send:
                self.assertFalse(
                    AlertService.notify(event_key="k", title="t", level=AlertLevel.INFO)
                )
        send.assert_not_called()

    def test_repeating_condition_alerts_once_per_cooldown(self):
        with patch.object(AlertService, "send_now", return_value=True) as send:
            first = AlertService.notify(
                event_key="chain_lag:base", title="lag", cooldown_seconds=1800
            )
            second = AlertService.notify(
                event_key="chain_lag:base", title="lag", cooldown_seconds=1800
            )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(send.call_count, 1)

    def test_distinct_conditions_do_not_share_cooldown(self):
        with patch.object(AlertService, "send_now", return_value=True) as send:
            AlertService.notify(event_key="a", title="a", cooldown_seconds=1800)
            AlertService.notify(event_key="b", title="b", cooldown_seconds=1800)
        self.assertEqual(send.call_count, 2)

    def test_recovery_only_fires_after_an_alert_and_releases_cooldown(self):
        with patch.object(AlertService, "send_now", return_value=True) as send:
            # No prior alert: recovery stays silent (no noise for conditions
            # the operator never heard about).
            self.assertFalse(
                AlertService.notify_recovery(event_key="chain_lag:base", title="ok")
            )
            AlertService.notify(
                event_key="chain_lag:base", title="lag", cooldown_seconds=1800
            )
            self.assertTrue(
                AlertService.notify_recovery(event_key="chain_lag:base", title="ok")
            )
            # Cooldown released: the next breach alerts immediately.
            self.assertTrue(
                AlertService.notify(
                    event_key="chain_lag:base", title="lag", cooldown_seconds=1800
                )
            )
        self.assertEqual(send.call_count, 3)

    def test_notify_swallows_internal_errors(self):
        with patch.object(
            AlertService, "send_now", side_effect=RuntimeError("telegram exploded")
        ):
            self.assertFalse(
                AlertService.notify(event_key="k", title="t", level=AlertLevel.INFO)
            )

    def test_build_message_escapes_html(self):
        text = build_message(
            level=AlertLevel.MONEY,
            title="Deposit <credited>",
            lines=["user & co"],
        )
        self.assertIn("&lt;credited&gt;", text)
        self.assertIn("user &amp; co", text)


class AlertEventFormattingTests(TestCase):
    """Deposit alerts must carry the fields an operator needs to act on."""

    def test_deposit_alert_contains_amount_symbol_user_and_tx(self):
        deposit = Mock(
            pk=1,
            worth="50.105381",
            sys_no="DXC1",
            customer=Mock(uid="USER-42"),
            transfer=Mock(
                amount=__import__("decimal").Decimal("0.0006127"),
                crypto=Mock(symbol="cbBTC"),
                chain=Mock(code="base"),
                hash="0xdeadbeef",
            ),
        )
        with patch.object(AlertService, "notify", return_value=True) as notify:
            events.deposit_credited(deposit)
        lines = notify.call_args.kwargs["lines"]
        body = "\n".join(lines)
        self.assertIn("0.0006127 cbBTC", body)
        self.assertIn("base", body)
        self.assertIn("USER-42", body)
        self.assertIn("0xdeadbeef", body)
        self.assertEqual(notify.call_args.kwargs["level"], AlertLevel.MONEY)
        # Money events must never be deduplicated away.
        self.assertIsNone(notify.call_args.kwargs.get("cooldown_seconds"))


class WatchdogScannerHealthTests(TestCase):
    def setUp(self):
        cache.clear()
        # Create inert first, then flip to active via queryset update: an active
        # chain must satisfy the runtime-config constraint validated on save(),
        # and update() deliberately skips that validation like the project's own
        # chain fixtures do.
        chain = Chain.objects.create(code="anvil", rpc="", active=False)
        Chain.objects.filter(pk=chain.pk).update(
            rpc="http://evm-test.invalid",
            active=True,
            latest_block_number=10_000,
            last_scanned_at=timezone.now(),
        )
        self.chain = Chain.objects.get(pk=chain.pk)
        self.cursor = EvmScanCursor.objects.create(
            chain=self.chain, last_scanned_block=10_000, enabled=True
        )

    def test_auth_error_detection_variants(self):
        for message in (
            "401 Client Error: Unauthorized",
            "403 Client Error: Forbidden",
            "message: API key disabled",
            "invalid api key supplied",
        ):
            self.assertTrue(looks_like_auth_error(message), message)
        self.assertFalse(looks_like_auth_error("query returned more than 10000 results"))

    def test_stalled_scanner_alerts_from_staleness_alone(self):
        Chain.objects.filter(pk=self.chain.pk).update(
            last_scanned_at=timezone.now() - timedelta(seconds=4000)
        )
        with patch.object(AlertService, "notify", return_value=True) as notify:
            AlertWatchdog.check_scanner_health()
        titles = [call.kwargs["title"] for call in notify.call_args_list]
        self.assertTrue(
            any("SCANNER STALLED" in title for title in titles),
            f"expected a stalled-scanner alert, got {titles}",
        )

    def test_rejected_rpc_key_alerts_as_critical(self):
        EvmScanCursor.objects.filter(pk=self.cursor.pk).update(
            last_error="HTTPError: 403 Client Error: Forbidden for url: https://rpc"
        )
        with patch.object(AlertService, "notify", return_value=True) as notify:
            AlertWatchdog.check_scanner_health()
        titles = [call.kwargs["title"] for call in notify.call_args_list]
        self.assertTrue(
            any("RPC KEY REJECTED" in title for title in titles),
            f"expected a rejected-key alert, got {titles}",
        )

    def test_lag_above_threshold_alerts(self):
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=50_000)
        with patch.object(AlertService, "notify", return_value=True) as notify:
            AlertWatchdog.check_scanner_health()
        titles = [call.kwargs["title"] for call in notify.call_args_list]
        self.assertTrue(any("lagging" in title.lower() for title in titles), titles)

    def test_healthy_chain_sends_no_alerts(self):
        with patch.object(AlertService, "notify", return_value=True) as notify:
            AlertWatchdog.check_scanner_health()
        self.assertEqual(notify.call_count, 0)

    def test_evm_chain_type_is_required_for_health_check(self):
        # Guard the filter itself: only EVM chains participate in scanner checks.
        self.assertEqual(Chain.objects.get(pk=self.chain.pk).type, ChainType.EVM)
