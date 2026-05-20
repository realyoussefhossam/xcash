from __future__ import annotations

from django.db import connection


def lock_evm_idempotency_key(*, namespace: str, key: str) -> None:
    """Acquire a PostgreSQL transaction-level advisory lock for one EVM idempotency key."""
    lock_key = f"evm:{namespace}:{key}"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [lock_key],
        )
