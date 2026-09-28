"""Periodic health watchdog that feeds the Telegram alert channel.

Two families of checks, both driven from state the system already maintains —
no extra RPC calls are made for the scanner checks, because a dead RPC is
exactly the situation where extra calls are unavailable:

* scanner state per active EVM chain: scan stalled, cursor lag, cursor error,
  RPC key rejected. ``Chain.latest_block_number`` is refreshed by the scanner on
  every successful tick, so a frozen value plus a stale ``last_scanned_at`` is
  itself the outage signal.
* resource/settlement risks reused from ``core.monitoring``: low native gas for
  queued tasks, low Tron resources, stale price feeds, stalled webhook events.

Every check is fail-safe: exceptions are logged and the loop continues, so one
broken check can never silence the others.
"""

from __future__ import annotations

from datetime import timedelta

import structlog
from django.conf import settings
from django.utils import timezone

from chains.models import Chain
from chains.models import ChainType
from chains.models import Transfer
from chains.models import TransferType
from core.monitoring import OperationalRiskService
from evm.models import EvmScanCursor
from notifications import events

logger = structlog.get_logger()

# Substrings that mean "the provider rejected our credentials" — the silent
# killer that stalled Base for two days. Kept deliberately broad because every
# provider words it differently ("403 Forbidden", "401 Unauthorized",
# "API key disabled", "invalid api key").
_RPC_AUTH_ERROR_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "api key disabled",
    "invalid api key",
    "invalid key",
    "forbidden",
)


def scan_stall_seconds() -> int:
    return int(getattr(settings, "TELEGRAM_SCAN_STALL_SECONDS", 300))


def chain_lag_alert_blocks() -> int:
    return int(getattr(settings, "TELEGRAM_CHAIN_LAG_ALERT_BLOCKS", 1000))


def looks_like_auth_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _RPC_AUTH_ERROR_MARKERS)


class AlertWatchdog:
    """Runs the health checks; each check method is safe to call independently."""

    CHECK_NAMES = ("scanner", "gas", "tron", "prices", "webhooks", "uncredited")

    @classmethod
    def run(cls) -> dict[str, int]:
        checks = {
            "scanner": cls.check_scanner_health,
            "gas": cls.check_evm_gas,
            "tron": cls.check_tron_resources,
            "prices": cls.check_stale_prices,
            "webhooks": cls.check_stalled_webhooks,
            "uncredited": cls.check_uncredited_transfers,
        }
        counts: dict[str, int] = {}
        for name, check in checks.items():
            try:
                counts[name] = check()
            except Exception as exc:  # noqa: BLE001 — one check must not hide others
                counts[name] = 0
                logger.exception("告警巡检子任务失败", check=name, error=str(exc))
        return counts

    # ------------------------------------------------------------------
    # Scanner / RPC health
    # ------------------------------------------------------------------

    @classmethod
    def check_scanner_health(cls) -> int:
        alerted = 0
        now = timezone.now()
        stall_after = timedelta(seconds=scan_stall_seconds())
        lag_threshold = chain_lag_alert_blocks()

        for chain in Chain.objects.filter(active=True, type=ChainType.EVM):
            cursor = EvmScanCursor.objects.filter(chain=chain).first()
            last_scan = chain.last_scanned_at

            # 1. Scanner stalled: no completed scan for too long. This is the
            #    signal that survives a dead RPC (lag cannot be computed then).
            seconds_since_scan = (
                int((now - last_scan).total_seconds()) if last_scan else None
            )
            if seconds_since_scan is not None and seconds_since_scan > (
                stall_after.total_seconds()
            ):
                alerted += int(
                    events.scanner_stalled(
                        chain=chain, seconds_since_scan=seconds_since_scan
                    )
                )
            else:
                events.scanner_stalled_recovered(chain=chain)

            if cursor is None:
                continue

            # 2. Cursor error: auth failures get the sharper message.
            error = cursor.last_error or ""
            if error:
                if looks_like_auth_error(error):
                    alerted += int(events.rpc_auth_error(chain=chain, error=error))
                else:
                    alerted += int(events.cursor_error(chain=chain, error=error))
            else:
                events.rpc_auth_recovered(chain=chain)
                events.cursor_error_recovered(chain=chain)

            # 3. Lag while still scanning (rate-limited or overloaded node).
            cursor_block = int(cursor.last_scanned_block or 0)
            head_block = int(chain.latest_block_number or 0)
            lag = head_block - cursor_block
            if lag > lag_threshold:
                alerted += int(
                    events.chain_lag(
                        chain=chain,
                        lag_blocks=lag,
                        cursor_block=cursor_block,
                        head_block=head_block,
                        scanned_ago_seconds=seconds_since_scan,
                    )
                )
            elif lag >= 0:
                events.chain_lag_recovered(chain=chain, lag_blocks=lag)

        return alerted

    # ------------------------------------------------------------------
    # Resources / settlement risks (reuses existing risk calculators)
    # ------------------------------------------------------------------

    @classmethod
    def check_evm_gas(cls) -> int:
        alerts = OperationalRiskService.evm_low_native_balance_alerts(limit=8)
        alerted = 0
        alerted_chains: set[str] = set()
        for alert in alerts:
            chain = alert["chain"]
            alerted_chains.add(chain.code)
            balance = alert.get("current_balance")
            required = alert.get("required_balance") or 0
            if alert.get("error"):
                warning = f"Balance check failed: {alert['error']}"
            else:
                warning = (
                    f"Needs ~{events.format_native_amount(chain=chain, wei=required)}"
                    f" for {alert.get('task_count', 0)} queued task(s)"
                )
            alerted += int(
                events.low_gas(
                    chain=chain,
                    address=alert["sender"].address,
                    balance_display=events.format_native_amount(
                        chain=chain, wei=balance
                    ),
                    warning=warning,
                )
            )

        for chain in Chain.objects.filter(active=True, type=ChainType.EVM):
            if chain.code not in alerted_chains:
                events.low_gas_recovered(chain=chain)
        return alerted

    @classmethod
    def check_tron_resources(cls) -> int:
        alerts = OperationalRiskService.tron_low_resource_alerts(limit=8)
        if alerts:
            warnings = []
            for alert in alerts:
                chain = alert.get("chain")
                code = getattr(chain, "code", "tron")
                warnings.append(f"{code}: {alert.get('warning', 'low resources')}")
            return int(events.tron_low_resources(warning="; ".join(warnings)[:400]))
        events.tron_resources_recovered()
        return 0

    @classmethod
    def check_stale_prices(cls) -> int:
        stale = OperationalRiskService.stale_price_cryptos(limit=8)
        if stale:
            return int(events.stale_prices(symbols=[row["symbol"] for row in stale]))
        events.prices_recovered()
        return 0

    @classmethod
    def check_stalled_webhooks(cls) -> int:
        stalled = OperationalRiskService.stalled_webhook_events()
        count = stalled.count() if hasattr(stalled, "count") else len(list(stalled))
        if count:
            return int(events.stalled_webhook_events(count=count))
        events.stalled_webhooks_recovered()
        return 0

    # ------------------------------------------------------------------
    # Integrity: confirmed deposits that never produced a deposit row
    # ------------------------------------------------------------------

    @classmethod
    def check_uncredited_transfers(cls) -> int:
        """A confirmed DEPOSIT transfer without a deposit row means on-chain money
        credited to nobody. Rare enough to alert unconditionally."""
        alerted = 0
        orphans = (
            Transfer.objects.filter(
                type=TransferType.Deposit,
                status="confirmed",
                deposit__isnull=True,
            )
            .select_related("chain", "crypto")
            .order_by("-id")[:5]
        )
        for transfer in orphans:
            alerted += int(events.confirmed_transfer_without_slot(transfer))
        return alerted
