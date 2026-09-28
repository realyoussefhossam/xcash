"""Typed alert events.

Every business/infra event funnels through one small function here so call sites
stay one-liners and message formatting has a single owner. Event keys are stable
strings because they double as dedup keys and as the future filter vocabulary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.service import AlertLevel
from notifications.service import AlertService
from notifications.service import cooldown_seconds

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids import cycles
    from chains.models import Chain
    from chains.models import Transfer
    from chains.models import TxTask
    from deposits.models import Deposit
    from webhooks.models import WebhookEvent

_TX_TYPE_LABEL = {
    "vault_slot_collect": "Sweep",
    "vault_slot_deploy": "Contract deploy",
}


def format_amount(value, symbol: str) -> str:
    """Trim trailing zeros so 10.00000000 reads as 10 USDC."""
    text = f"{value:f}" if hasattr(value, "f") else str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} {symbol}"


def _task_lines(task: TxTask) -> list[str]:
    lines = [
        f"Chain: {task.chain.code}",
        f"Type: {_TX_TYPE_LABEL.get(task.tx_type, task.tx_type)}",
    ]
    if task.tx_hash:
        lines.append(f"Tx: {task.tx_hash}")
    return lines


# ---------------------------------------------------------------------------
# Money events — never deduplicated, each one is a distinct business fact
# ---------------------------------------------------------------------------


def deposit_credited(deposit: Deposit) -> bool:
    transfer = deposit.transfer
    lines = [
        format_amount(transfer.amount, transfer.crypto.symbol)
        + f" on {transfer.chain.code}",
        f"USD: {deposit.worth}",
        f"Customer: {deposit.customer.uid}",
        f"sys_no: {deposit.sys_no}",
        f"Tx: {transfer.hash}",
    ]
    return AlertService.notify(
        event_key=f"deposit_credited:{deposit.pk}",
        title="Deposit credited",
        level=AlertLevel.MONEY,
        lines=lines,
    )


def sweep_succeeded(task: TxTask) -> bool:
    return AlertService.notify(
        event_key=f"sweep_succeeded:{task.pk}",
        title="Sweep completed",
        level=AlertLevel.MONEY,
        lines=_task_lines(task),
    )


def sweep_failed(task: TxTask, *, reason: str | None = None) -> bool:
    lines = _task_lines(task)
    if reason:
        lines.append(f"Reason: {reason[:300]}")
    return AlertService.notify(
        event_key=f"sweep_failed:{task.pk}",
        title="SWEEP FAILED — funds still at deposit address",
        level=AlertLevel.CRITICAL,
        lines=lines,
    )


def deploy_succeeded(task: TxTask) -> bool:
    return AlertService.notify(
        event_key=f"deploy_succeeded:{task.pk}",
        title="Deposit contract deployed",
        level=AlertLevel.INFO,
        lines=_task_lines(task),
    )


def deploy_failed(task: TxTask, *, reason: str | None = None) -> bool:
    lines = _task_lines(task)
    if reason:
        lines.append(f"Reason: {reason[:300]}")
    return AlertService.notify(
        event_key=f"deploy_failed:{task.pk}",
        title="Deposit contract deploy FAILED",
        level=AlertLevel.CRITICAL,
        lines=lines,
    )


def confirmed_transfer_without_slot(transfer: Transfer) -> bool:
    """Confirmed inbound transfer that matched no DEPOSIT vault slot.

    On-chain money is credited to nobody in this state, so it always alerts.
    """
    return AlertService.notify(
        event_key=f"transfer_without_slot:{transfer.pk}",
        title="CONFIRMED TRANSFER HAS NO DEPOSIT SLOT — manual credit needed",
        level=AlertLevel.CRITICAL,
        lines=[
            format_amount(transfer.amount, transfer.crypto.symbol)
            + f" on {transfer.chain.code}",
            f"To: {transfer.to_address}",
            f"Tx: {transfer.hash}",
        ],
    )


def webhook_delivery_failed(event: WebhookEvent, *, reason: str | None = None) -> bool:
    lines = [
        f"Attempts: {event.attempt_count}",
        f"URL: {event.delivery_url}",
    ]
    if reason:
        lines.append(f"Reason: {reason[:300]}")
    return AlertService.notify(
        event_key=f"webhook_failed:{event.pk}",
        title="Webhook delivery failed (retries exhausted)",
        level=AlertLevel.CRITICAL,
        lines=lines,
    )


def stalled_events_reaped(*, count: int) -> bool:
    """Janitor reaped events that never got delivered — merchant notifications
    lost, so this fires regardless of any cooldown."""
    return AlertService.notify(
        event_key=f"webhooks_reaped:{count}",
        title="Webhook events reaped as permanently undelivered",
        level=AlertLevel.CRITICAL,
        lines=[f"Events dropped: {count}"],
    )


# ---------------------------------------------------------------------------
# Repeating conditions — deduplicated, with recovery announcements
# ---------------------------------------------------------------------------


def tx_task_stuck(task: TxTask, *, minutes: int) -> bool:
    return AlertService.notify(
        event_key=f"tx_task_stuck:{task.pk}",
        title="Tx task stuck at nonce head",
        level=AlertLevel.CRITICAL,
        lines=_task_lines(task) + [f"Queued for: {minutes} min"],
        cooldown_seconds=cooldown_seconds(),
    )


def chain_lag(
    *,
    chain: Chain,
    lag_blocks: int,
    cursor_block: int,
    head_block: int,
    scanned_ago_seconds: int | None,
) -> bool:
    lines = [
        f"Behind by: {lag_blocks:,} blocks",
        f"Cursor: {cursor_block:,} / head: {head_block:,}",
    ]
    if scanned_ago_seconds is not None:
        lines.append(f"Last scan completed: {scanned_ago_seconds}s ago")
    return AlertService.notify(
        event_key=f"chain_lag:{chain.code}",
        title=f"Chain lagging: {chain.code}",
        level=AlertLevel.CRITICAL,
        lines=lines,
        cooldown_seconds=cooldown_seconds(),
    )


def chain_lag_recovered(*, chain: Chain, lag_blocks: int) -> bool:
    return AlertService.notify_recovery(
        event_key=f"chain_lag:{chain.code}",
        title=f"Chain caught up: {chain.code}",
        lines=[f"Behind by: {lag_blocks:,} blocks"],
    )


def scanner_stalled(*, chain: Chain, seconds_since_scan: int) -> bool:
    """No completed scan for too long — scanning is dead (dead RPC, worker
    down, or a permanently failing job). This signal does not depend on being
    able to reach the chain, which is why it is the primary outage detector."""
    return AlertService.notify(
        event_key=f"scanner_stalled:{chain.code}",
        title=f"SCANNER STALLED: {chain.code} — deposits not being detected",
        level=AlertLevel.CRITICAL,
        lines=[
            f"No completed scan for {seconds_since_scan}s",
            f"Expected cadence: every {chain.spec.scan_interval_seconds}s",
        ],
        cooldown_seconds=cooldown_seconds(),
    )


def scanner_stalled_recovered(*, chain: Chain) -> bool:
    return AlertService.notify_recovery(
        event_key=f"scanner_stalled:{chain.code}",
        title=f"Scanner resumed: {chain.code}",
    )


def cursor_error(*, chain: Chain, error: str) -> bool:
    return AlertService.notify(
        event_key=f"cursor_error:{chain.code}",
        title=f"Scan error: {chain.code}",
        level=AlertLevel.CRITICAL,
        lines=[f"Error: {error[:300]}"],
        cooldown_seconds=cooldown_seconds(),
    )


def cursor_error_recovered(*, chain: Chain) -> bool:
    return AlertService.notify_recovery(
        event_key=f"cursor_error:{chain.code}",
        title=f"Scan errors cleared: {chain.code}",
    )


def rpc_auth_error(*, chain: Chain, error: str) -> bool:
    """RPC key rejected (401/403/disabled) — scanning is dead until fixed."""
    return AlertService.notify(
        event_key=f"rpc_auth:{chain.code}",
        title=f"RPC KEY REJECTED: {chain.code} — scanning stopped",
        level=AlertLevel.CRITICAL,
        lines=[f"Error: {error[:300]}"],
        cooldown_seconds=cooldown_seconds(),
    )


def rpc_auth_recovered(*, chain: Chain) -> bool:
    return AlertService.notify_recovery(
        event_key=f"rpc_auth:{chain.code}",
        title=f"RPC working again: {chain.code}",
    )


def low_gas(*, chain: Chain, address: str, balance_display: str, warning: str) -> bool:
    return AlertService.notify(
        event_key=f"low_gas:{chain.code}",
        title=f"Low gas on {chain.code}",
        level=AlertLevel.CRITICAL,
        lines=[warning, f"Wallet: {address}", f"Balance: {balance_display}"],
        cooldown_seconds=cooldown_seconds(),
    )


def low_gas_recovered(*, chain: Chain) -> bool:
    return AlertService.notify_recovery(
        event_key=f"low_gas:{chain.code}",
        title=f"Gas topped up: {chain.code}",
    )


def tron_low_resources(*, warning: str) -> bool:
    return AlertService.notify(
        event_key="tron_low_resources",
        title="Low Tron resources",
        level=AlertLevel.CRITICAL,
        lines=[warning],
        cooldown_seconds=cooldown_seconds(),
    )


def tron_resources_recovered() -> bool:
    return AlertService.notify_recovery(
        event_key="tron_low_resources",
        title="Tron resources restored",
    )


def stale_prices(*, symbols: list[str]) -> bool:
    return AlertService.notify(
        event_key="stale_prices",
        title="Price feed stale — billing would use old rates",
        level=AlertLevel.CRITICAL,
        lines=[f"Coins: {', '.join(symbols)}"],
        cooldown_seconds=cooldown_seconds(),
    )


def prices_recovered() -> bool:
    return AlertService.notify_recovery(
        event_key="stale_prices",
        title="Price feed fresh again",
    )


def stalled_webhook_events(*, count: int) -> bool:
    return AlertService.notify(
        event_key="stalled_webhooks",
        title="Webhook events stalled",
        level=AlertLevel.CRITICAL,
        lines=[f"Stalled events: {count}"],
        cooldown_seconds=cooldown_seconds(),
    )


def stalled_webhooks_recovered() -> bool:
    return AlertService.notify_recovery(
        event_key="stalled_webhooks",
        title="Webhook backlog cleared",
    )
