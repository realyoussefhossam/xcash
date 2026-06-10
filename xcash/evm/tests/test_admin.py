from unittest.mock import Mock

from django.contrib.admin.sites import AdminSite
from django.test import SimpleTestCase
from django.test import TestCase
from web3 import Web3

from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Address
from chains.models import AddressUsage
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import Wallet
from evm.admin import EvmScanCursorAdmin
from evm.admin import EvmTxTaskAdmin
from evm.models import EvmScanCursor
from evm.models import EvmTxTask
from evm.tests._fixtures import make_evm_chain


class EvmTxTaskAdminTests(SimpleTestCase):
    def test_tx_task_admin_excludes_signed_payload(self):
        model_admin = EvmTxTaskAdmin(EvmTxTask, AdminSite())

        self.assertIn("signed_payload", model_admin.get_exclude(Mock(), obj=None))


class EvmTxTaskAdminActionTests(TestCase):
    def setUp(self):
        self.admin = EvmTxTaskAdmin(EvmTxTask, AdminSite())
        self.admin.message_user = Mock()
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.wallet = Wallet.objects.create()
        self.sender = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a01"
            ),
        )

    def create_task(self, *, status: str, nonce: int) -> EvmTxTask:
        base_task = TxTask.objects.create(
            chain=self.chain,
            sender=self.sender,
            tx_type=TxTaskType.VaultSlotCollect,
            status=status,
            tx_hash="0x" + f"{nonce + 1:064x}",
        )
        return EvmTxTask.objects.create(
            base_task=base_task,
            sender=self.sender,
            chain=self.chain,
            nonce=nonce,
            to=Web3.to_checksum_address("0x0000000000000000000000000000000000000b01"),
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
        )

    def test_mark_queued_failed_action_only_updates_queued_tasks(self):
        queued = self.create_task(status=TxTaskStatus.QUEUED, nonce=0)
        submitted = self.create_task(status=TxTaskStatus.SUBMITTED, nonce=1)

        self.admin.mark_queued_failed_after_nonce_handled(
            request=Mock(),
            queryset=EvmTxTask.objects.filter(pk__in=[queued.pk, submitted.pk]),
        )

        queued.base_task.refresh_from_db()
        submitted.base_task.refresh_from_db()
        self.assertEqual(queued.base_task.status, TxTaskStatus.FAILED)
        self.assertEqual(submitted.base_task.status, TxTaskStatus.SUBMITTED)
        self.admin.message_user.assert_called_once()


class EvmScanCursorAdminTests(SimpleTestCase):
    def setUp(self):
        self.admin = EvmScanCursorAdmin(EvmScanCursor, AdminSite())

    def test_scan_cursor_admin_disallows_delete(self):
        self.assertIn("has_delete_permission", EvmScanCursorAdmin.__dict__)
        request = Mock()

        self.assertFalse(self.admin.has_delete_permission(request, obj=None))
        self.assertFalse(self.admin.has_delete_permission(request, obj=object()))
