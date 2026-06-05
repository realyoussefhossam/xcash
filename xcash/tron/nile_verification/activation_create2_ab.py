from __future__ import annotations

import argparse

from verification_common import emit
from verification_common import env_int
from verification_common import env_required
from verification_common import nile_chain
from verification_common import nile_vault_address
from verification_common import setup_django
from verification_common import sign_and_broadcast
from verification_common import wait_tx_info


def main() -> None:
    setup_django()
    from eth_utils import keccak
    from tron.client import TronHttpClient
    from tron.contracts_codec import predict_tron_vault_slot_address
    from tron.intents import build_contract_call_intent
    from tron.intents import build_vault_slot_deploy_intent
    from tron.intents import trc20_balance_of_parameter

    from chains.models import TxTaskType

    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("a", "b"), required=True)
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()

    chain = nile_chain()
    owner = env_required("TRON_NILE_OWNER_ADDRESS")
    private_key = env_required("TRON_NILE_PRIVATE_KEY")
    factory = env_required("TRON_VAULT_SLOT_FACTORY_ADDRESS")
    template = env_required("TRON_VAULT_SLOT_TEMPLATE_ADDRESS")
    vault = nile_vault_address(owner_address=owner)
    token = env_required("TRON_USDT_CONTRACT_ADDRESS")
    fee_limit = env_int("TRON_VAULT_SLOT_FEE_LIMIT", 300_000_000)
    salt = keccak(f"xcash:tron-activation:{args.case}".encode())
    predicted = predict_tron_vault_slot_address(
        vault=vault,
        salt=salt,
        factory=factory,
        vault_slot_template=template,
    )
    emit(f"case={args.case}")
    emit(f"salt_hex={salt.hex()}")
    emit(f"predicted={predicted}")
    client = TronHttpClient(chain=chain)

    if args.case == "a":
        unsigned = client.create_trx_transfer(
            owner_address=owner,
            to_address=predicted,
            amount_sun=1_000_000,
        )
        tx_id = sign_and_broadcast(
            client=client,
            private_key=private_key,
            transaction=unsigned,
            broadcast=args.broadcast,
        )
        if args.wait and args.broadcast:
            emit(f"activation_receipt={wait_tx_info(client=client, tx_id=tx_id)}")
    else:
        parameter = trc20_balance_of_parameter(predicted)
        intent = build_contract_call_intent(
            sender=type("Sender", (), {"address": owner})(),
            chain=chain,
            contract_address=token,
            function_selector_value="transfer(address,uint256)",
            parameter=parameter + f"{1:064x}",
            fee_limit=fee_limit,
            tx_type=TxTaskType.VaultSlotCollect,
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
            emit(f"trc20_prefund_receipt={wait_tx_info(client=client, tx_id=tx_id)}")

    deploy_intent = build_vault_slot_deploy_intent(
        sender=type("Sender", (), {"address": owner})(),
        chain=chain,
        factory_address=factory,
        vault_address=vault,
        salt=salt,
    )
    unsigned = client.trigger_smart_contract(
        owner_address=owner,
        contract_address=deploy_intent.to,
        function_selector=deploy_intent.function_selector,
        parameter=deploy_intent.parameter,
        fee_limit=fee_limit,
    )
    deploy_tx_id = sign_and_broadcast(
        client=client,
        private_key=private_key,
        transaction=unsigned["transaction"],
        broadcast=args.broadcast,
    )
    if args.wait and args.broadcast:
        emit(f"deploy_receipt={wait_tx_info(client=client, tx_id=deploy_tx_id)}")
    emit("结论块：把 deploy_receipt 的 SUCCESS/失败原因粘回 docs/tron-vaultslot-migration.md Phase 0B。")


if __name__ == "__main__":
    main()
