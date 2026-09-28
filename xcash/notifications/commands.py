"""On-demand Telegram commands ("/status", "/rpcs", "/deposits").

The bot is push-first; these handlers add a read-only query surface so operators
can ask "how are the RPCs doing right now?" instead of waiting for the watchdog.
Design constraints:

* **Read-only.** Commands can only read state — no command may mutate money or
  chain state, so a leaked chat id cannot be leveraged into an action.
* **Chat-scoped.** Only updates from the configured chat are answered; anything
  else is ignored silently (a bot's username is discoverable, its answers are
  not public data).
* **Bounded work.** Each handler touches a handful of rows and at most one RPC
  call per chain, with timeouts, so the poll task stays cheap.
"""

from __future__ import annotations

import structlog
from django.conf import settings
from django.utils import timezone

from chains.models import Chain
from chains.models import ChainType
from deposits.models import Deposit
from evm.models import EvmScanCursor
from notifications.telegram import escape_html
from notifications.telegram import mask_secret_in_url
from notifications.watchdog import looks_like_auth_error
from notifications.watchdog import scan_stall_seconds

logger = structlog.get_logger()

STALE_SCAN_MULTIPLIER = 10  # a chain is "stalled" well before the watchdog threshold

HELP_TEXT = (
    "🤖 <b>xcash bot commands</b>\n"
    "/status — chain &amp; RPC health at a glance\n"
    "/rpcs — per-chain detail (cursor, errors, gas, endpoint)\n"
    "/deposits — last 5 credited deposits\n"
    "/help — this message"
)


def _format_timedelta(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def chain_health_rows() -> list[dict]:
    """Collect one row per active chain; shared by /status and /rpcs."""
    now = timezone.now()
    rows: list[dict] = []
    for chain in Chain.objects.filter(active=True).order_by("id"):
        cursor = None
        if chain.type == ChainType.EVM:
            cursor = EvmScanCursor.objects.filter(chain=chain).first()

        cursor_block = int(getattr(cursor, "last_scanned_block", 0) or 0)
        head_block = int(chain.latest_block_number or 0)
        lag = max(0, head_block - cursor_block) if cursor else None

        last_scan = chain.last_scanned_at
        scanned_ago = int((now - last_scan).total_seconds()) if last_scan else None

        error = (getattr(cursor, "last_error", "") or "").strip()
        expected_gap = max(60, chain.spec.scan_interval_seconds * STALE_SCAN_MULTIPLIER)

        if error and looks_like_auth_error(error):
            state = "AUTH FAIL"
        elif scanned_ago is not None and scanned_ago > expected_gap:
            state = "STALLED"
        elif lag is not None and lag > 0 and lag > (chain.spec.scan_interval_seconds * 60):
            state = "LAGGING"
        elif error:
            state = "ERROR"
        else:
            state = "OK"

        rows.append(
            {
                "chain": chain,
                "state": state,
                "lag": lag,
                "scanned_ago": scanned_ago,
                "cursor_block": cursor_block,
                "head_block": head_block,
                "error": error,
            }
        )
    return rows


def build_status_text() -> str:
    rows = chain_health_rows()
    lines = [f"📊 <b>xcash status</b> — {timezone.now():%Y-%m-%d %H:%M} UTC", ""]
    icon = {
        "OK": "✅",
        "LAGGING": "⚠️",
        "STALLED": "🛑",
        "AUTH FAIL": "🔑",
        "ERROR": "❗",
    }
    for row in rows:
        lag = "n/a" if row["lag"] is None else f"{row['lag']:,}"
        lines.append(
            f"{icon[row['state']]} <b>{escape_html(row['chain'].code)}</b> "
            f"{row['state']} · lag {lag} · scanned {_format_timedelta(row['scanned_ago'])} ago"
        )
    if not rows:
        lines.append("(no active chains)")
    return "\n".join(lines)


def build_rpcs_text() -> str:
    """Per-chain detail. The only handler allowed to hit RPCs (gas balances)."""
    rows = chain_health_rows()
    blocks: list[str] = []
    for row in rows:
        chain = row["chain"]
        head = [f"🔗 <b>{escape_html(chain.code)}</b> — {row['state']}"]
        head.append(f"cursor {row['cursor_block']:,} / head {row['head_block']:,}")
        if row["scanned_ago"] is not None:
            head.append(f"last scan {_format_timedelta(row['scanned_ago'])} ago")
        head.append(f"endpoint {escape_html(mask_secret_in_url(chain.rpc))}")
        if row["error"]:
            head.append(f"last error: {escape_html(row['error'][:200])}")
        blocks.append("\n".join(head))
    if not blocks:
        blocks.append("(no active chains)")
    return "🛠 <b>RPC detail</b>\n\n" + "\n\n".join(blocks)


def build_deposits_text(*, limit: int = 5) -> str:
    deposits = (
        Deposit.objects.select_related("transfer__chain", "transfer__crypto", "customer")
        .order_by("-id")[:limit]
    )
    lines = [f"💰 <b>last {limit} deposits</b>"]
    if not deposits:
        lines.append("(none yet)")
        return "\n".join(lines)
    for deposit in deposits:
        transfer = deposit.transfer
        amount = f"{transfer.amount.normalize():f}" if hasattr(transfer.amount, "normalize") else str(transfer.amount)
        lines.append(
            f"\n• {escape_html(amount)} {escape_html(transfer.crypto.symbol)} "
            f"on {escape_html(transfer.chain.code)} — ${escape_html(deposit.worth)}\n"
            f"  user {escape_html(deposit.customer.uid)} · {deposit.created_at:%m-%d %H:%M} UTC"
        )
    return "\n".join(lines)


_COMMANDS = {
    "/status": build_status_text,
    "/rpcs": build_rpcs_text,
    "/deposits": build_deposits_text,
    "/help": lambda: HELP_TEXT,
    "/start": lambda: HELP_TEXT,
}


def command_from_update(update: dict) -> tuple[str, str] | None:
    """Extract (command, chat_id) from an update, or None when it is not a
    command message we should answer.

    Commands may arrive as ``/status`` or ``/status@botname`` (the ``@`` form is
    what Telegram sends from groups), and unknown commands are ignored so typos
    do not produce noise.
    """
    message = update.get("message") or update.get("channel_post") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text.startswith("/"):
        return None
    command = text.split()[0].split("@")[0].lower()
    if command not in _COMMANDS:
        return None
    return command, str(chat_id)


def handle_update(update: dict) -> str | None:
    """Return the reply text for an update, or None when nothing to send."""
    parsed = command_from_update(update)
    if parsed is None:
        return None
    command, chat_id = parsed
    if chat_id != str(settings.TELEGRAM_CHAT_ID):
        # Only the operator's own chat gets answers; a bot username is public.
        logger.warning(
            "忽略非配置会话的 Telegram 命令", chat_id=chat_id, command=command
        )
        return None
    try:
        return _COMMANDS[command]()
    except Exception as exc:  # noqa: BLE001 — a broken handler must not kill polling
        logger.exception("Telegram 命令处理失败", command=command, error=str(exc))
        return f"⚠️ command failed: {escape_html(exc)}"


def scan_stall_threshold_seconds() -> int:
    """Expose the watchdog threshold for tests/tools."""
    return scan_stall_seconds()
