from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace


def load_verification_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").strip()
            key, separator, value = line.partition("=")
            if not separator:
                continue
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value

    os.environ.setdefault("TRON_VAULT_SLOT_FEE_LIMIT", "300000000")
    os.environ.setdefault("TRON_VAULT_SLOT_DEPLOY_FEE_LIMIT", "1500000000")


def setup_django() -> None:
    load_verification_env()
    repo_root = Path(__file__).resolve().parents[3]
    source_root = repo_root / "xcash"
    for path in (str(repo_root), str(source_root)):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    import django

    django.setup()


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def env_optional(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def nile_vault_address(*, owner_address: str) -> str:
    return env_optional("TRON_VAULT_SLOT_TEST_VAULT", owner_address)


def emit(message: object) -> None:
    sys.stdout.write(f"{message}\n")


def nile_chain():
    return SimpleNamespace(
        code=os.environ.get("TRON_NILE_CHAIN_CODE", "tron-nile"),
        chain=os.environ.get("TRON_NILE_CHAIN_CODE", "tron-nile"),
        tron_base_url=env_optional("TRON_NILE_RPC_URL", "https://nile.trongrid.io"),
        tron_api_key=os.environ.get("TRON_API_KEY", ""),
    )


def sign_and_broadcast(
    *,
    client,
    private_key: str,
    transaction: dict,
    broadcast: bool,
) -> str:
    from chains.keys import sign_tron_transaction

    signed = sign_tron_transaction(
        private_key=private_key,
        unsigned_transaction=transaction,
    )
    emit(f"tx_id={signed.tx_hash}")
    if not broadcast:
        emit("broadcast=false")
        return signed.tx_hash
    response = client.broadcast_transaction(transaction=signed.raw_transaction)
    emit(f"broadcast_response={response}")
    return signed.tx_hash


def wait_tx_info(*, client, tx_id: str, timeout_seconds: int = 90) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = client.get_transaction_info_by_id(tx_id)
        if payload and payload.get("id") == tx_id:
            return payload
        time.sleep(3)
    raise SystemExit(f"tx info timeout: {tx_id}")
