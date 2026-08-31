"""Dispatch guard: caps per-chain scan tasks at one outstanding dispatch.

Beat scans for due chains every few seconds and enqueues a scan task per due
chain.  If scans run slower than the chain's scan interval (slow or degraded
RPC), the chain looks "due" for the whole duration of the scan, so the beat
keeps enqueueing duplicate scan tasks until the broker queue grows without
bound.  A 6-day backlog of duplicates once filled the entire Redis maxmemory
and took down the whole stack (see the OutOfMemoryError incident).

The guard is claimed atomically at dispatch time (cache.add) and released
when the scan task finishes.  At most one outstanding scan task per chain can
exist.  Slow RPC degrades to "this chain scans less often" instead of "the
queue grows forever".

The TTL is not a cooldown: it only covers the case where a worker is hard
killed (SIGKILL skips finally blocks) or the broker drop the message, so the
marker cannot block a chain permanently.  It must exceed the task's hard
time limit plus worst-case queue wait.
"""

from django.core.cache import cache

SCAN_DISPATCH_GUARD_TTL_SECONDS = 180

SCAN_DISPATCH_KEY_PREFIX = "scan_dispatch"


def try_claim_scan_dispatch(chain_pk: int) -> bool:
    """Atomically claim the dispatch slot for a chain.

    Returns True if this caller now owns the slot (no outstanding scan task
    for the chain), False if a scan is already dispatched and pending.
    """
    return cache.add(
        f"{SCAN_DISPATCH_KEY_PREFIX}:{chain_pk}",
        "1",
        timeout=SCAN_DISPATCH_GUARD_TTL_SECONDS,
    )


def release_scan_dispatch(chain_pk: int) -> None:
    """Release the dispatch slot after a scan task completes."""
    cache.delete(f"{SCAN_DISPATCH_KEY_PREFIX}:{chain_pk}")
