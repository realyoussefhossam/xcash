from __future__ import annotations

import argparse

from verification_common import emit
from verification_common import env_int
from verification_common import env_required
from verification_common import nile_chain
from verification_common import setup_django
from verification_common import sign_and_broadcast
from verification_common import wait_tx_info


def main() -> None:
    setup_django()
    from tron.client import TronHttpClient
    from tron.intents import build_vault_slot_collect_intent

    parser = argparse.ArgumentParser()
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()

    chain = nile_chain()
    owner = env_required("TRON_NILE_OWNER_ADDRESS")
    private_key = env_required("TRON_NILE_PRIVATE_KEY")
    slot = env_required("TRON_VAULT_SLOT_ADDRESS")
    token = env_required("TRON_USDT_CONTRACT_ADDRESS")
    fee_limit = env_int("TRON_VAULT_SLOT_FEE_LIMIT", 0)
    if fee_limit <= 0:
        raise SystemExit("TRON_VAULT_SLOT_FEE_LIMIT must be > 0")

    client = TronHttpClient(chain=chain)
    intent = build_vault_slot_collect_intent(
        sender=type("Sender", (), {"address": owner})(),
        chain=chain,
        vault_slot_address=slot,
        token_address=token,
    )
    unsigned = client.trigger_smart_contract(
        owner_address=owner,
        contract_address=intent.to,
        function_selector=intent.function_selector,
        parameter=intent.parameter,
        fee_limit=fee_limit,
    )
    tx_id = sign_and_broadcast(
        client=client,
        private_key=private_key,
        transaction=unsigned["transaction"],
        broadcast=args.broadcast,
    )
    if args.wait and args.broadcast:
        emit(f"receipt={wait_tx_info(client=client, tx_id=tx_id)}")


if __name__ == "__main__":
    main()
