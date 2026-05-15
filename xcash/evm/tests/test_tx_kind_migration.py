"""tx_kind 回填迁移：按 data 是否为空/0x 区分历史 NATIVE / CONTRACT_CALL 行。"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from web3 import Web3


@pytest.mark.django_db(transaction=True)
def test_backfill_tx_kind_classifies_existing_rows_by_data():
    executor = MigrationExecutor(connection)
    target_before = _targets_with_evm(
        executor,
        "0002_alter_evmscancursor_last_error",
    )
    executor.migrate(target_before)
    old_apps = executor.loader.project_state(target_before).apps
    old_evm_broadcast_task = old_apps.get_model(
        "evm",
        "EvmBroadcastTask",
    )

    chain = _create_minimal_chain(old_apps, suffix="backfill")
    address = _create_minimal_address(old_apps, suffix="backfill")

    old_evm_broadcast_task.objects.create(
        address=address,
        chain=chain,
        nonce=0,
        to=Web3.to_checksum_address("0x" + "a" * 40),
        value=0,
        data="",
        gas=21000,
    )
    old_evm_broadcast_task.objects.create(
        address=address,
        chain=chain,
        nonce=1,
        to=Web3.to_checksum_address("0x" + "a" * 40),
        value=0,
        data="0x",
        gas=21000,
    )
    old_evm_broadcast_task.objects.create(
        address=address,
        chain=chain,
        nonce=2,
        to=Web3.to_checksum_address("0x" + "b" * 40),
        value=0,
        data="0xa9059cbb0000",
        gas=60000,
    )

    executor = MigrationExecutor(connection)
    target_after = _targets_with_evm(
        executor,
        "0003_add_tx_kind_to_evm_broadcast_task",
    )
    executor.migrate(target_after)
    new_apps = executor.loader.project_state(target_after).apps
    new_evm_broadcast_task = new_apps.get_model(
        "evm",
        "EvmBroadcastTask",
    )

    rows = list(
        new_evm_broadcast_task.objects.filter(
            address_id=address.pk,
            chain_id=chain.pk,
        )
        .order_by("nonce")
        .values("nonce", "tx_kind")
    )
    assert rows == [
        {"nonce": 0, "tx_kind": "native_transfer"},
        {"nonce": 1, "tx_kind": "native_transfer"},
        {"nonce": 2, "tx_kind": "contract_call"},
    ]


@pytest.mark.django_db(transaction=True)
def test_normalize_tx_kind_preserves_valid_rows_and_repairs_legacy_values():
    executor = MigrationExecutor(connection)
    target_before = _targets_with_evm(
        executor,
        "0003_add_tx_kind_to_evm_broadcast_task",
    )
    executor.migrate(target_before)
    old_apps = executor.loader.project_state(target_before).apps
    old_evm_broadcast_task = old_apps.get_model(
        "evm",
        "EvmBroadcastTask",
    )

    chain = _create_minimal_chain(old_apps, suffix="normalize")
    address = _create_minimal_address(old_apps, suffix="normalize")

    _create_old_task(
        old_evm_broadcast_task,
        address=address,
        chain=chain,
        nonce=0,
        data="",
        tx_kind="native_transfer",
    )
    _create_old_task(
        old_evm_broadcast_task,
        address=address,
        chain=chain,
        nonce=1,
        data="0xa9059cbb0000",
        tx_kind="contract_call",
    )
    _create_old_task(
        old_evm_broadcast_task,
        address=address,
        chain=chain,
        nonce=2,
        data="",
        tx_kind="",
    )
    _create_old_task(
        old_evm_broadcast_task,
        address=address,
        chain=chain,
        nonce=3,
        data="0x",
        tx_kind="legacy",
    )
    _create_old_task(
        old_evm_broadcast_task,
        address=address,
        chain=chain,
        nonce=4,
        data="0xa9059cbb0000",
        tx_kind="legacy",
    )

    executor = MigrationExecutor(connection)
    target_after = _targets_with_evm(
        executor,
        "0004_add_tx_kind_check_constraint",
    )
    executor.migrate(target_after)
    new_apps = executor.loader.project_state(target_after).apps
    new_evm_broadcast_task = new_apps.get_model(
        "evm",
        "EvmBroadcastTask",
    )

    rows = list(
        new_evm_broadcast_task.objects.filter(
            address_id=address.pk,
            chain_id=chain.pk,
        )
        .order_by("nonce")
        .values("nonce", "tx_kind")
    )
    assert rows == [
        {"nonce": 0, "tx_kind": "native_transfer"},
        {"nonce": 1, "tx_kind": "contract_call"},
        {"nonce": 2, "tx_kind": "native_transfer"},
        {"nonce": 3, "tx_kind": "native_transfer"},
        {"nonce": 4, "tx_kind": "contract_call"},
    ]


def _create_old_task(evm_broadcast_task, *, address, chain, nonce, data, tx_kind):
    return evm_broadcast_task.objects.create(
        address=address,
        chain=chain,
        nonce=nonce,
        to=Web3.to_checksum_address("0x" + "a" * 40),
        value=0,
        data=data,
        gas=21000,
        tx_kind=tx_kind,
    )


def _create_minimal_chain(apps, *, suffix):
    Crypto = apps.get_model("currencies", "Crypto")
    Chain = apps.get_model("chains", "Chain")
    native = Crypto.objects.create(
        name=f"Migration Test Native {suffix}",
        symbol=f"MTN{suffix[:3].upper()}",
        coingecko_id=f"migration-test-native-{suffix}",
    )
    return Chain.objects.create(
        code=f"migration-test-{suffix}",
        chain_id=999_000 + len(suffix),
        name=f"MT {suffix}",
        type="evm",
        native_coin=native,
    )


def _create_minimal_address(apps, *, suffix):
    Wallet = apps.get_model("chains", "Wallet")
    Address = apps.get_model("chains", "Address")
    wallet = Wallet.objects.create()
    return Address.objects.create(
        wallet=wallet,
        chain_type="evm",
        usage="vault",
        bip44_account=0,
        address_index=0,
        address=Web3.to_checksum_address(
            "0x" + f"{len(suffix):040x}",
        ),
    )


def _targets_with_evm(executor, evm_migration):
    """仅回退 evm 自身；其它 app 保持 leaf，避免污染复用测试库 schema。"""
    return [
        target
        for target in executor.loader.graph.leaf_nodes()
        if target[0] != "evm"
    ] + [("evm", evm_migration)]
