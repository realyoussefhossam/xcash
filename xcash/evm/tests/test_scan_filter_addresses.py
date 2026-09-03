"""Recipient-filtered (topic2) log fetch behavioral tests.

The ERC20 log fetch pushes the recipient filter down to the node via topics[2].
These tests pin the coverage contract: the filter set must include vault slots
from all EVM chains (cross-chain same-address deposits), must batch large sets
into bounded queries, and must skip the ERC20 fetch entirely when no recipient
can possibly match.
"""

from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.test import override_settings
from web3 import Web3

from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from currencies.models import CryptoOnChain
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0
from evm.scanner.constants import XCASH_NATIVE_RECEIVED_TOPIC0
from evm.scanner.logs import EvmLogScanner
from evm.scanner.watchers import SCAN_TOPIC2_GROUP_SIZE
from evm.scanner.watchers import load_scan_filter_addresses
from evm.tests._fixtures import make_crypto
from evm.tests._fixtures import make_evm_chain
from projects.models import Customer
from projects.models import Project


@override_settings(DEBUG=False)
class ScanFilterAddressesTests(TestCase):
    def setUp(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        self.chain = make_evm_chain(code="anvil", rpc="http://evm-test.invalid")
        self.other_chain = make_evm_chain(
            code="sepolia", rpc="http://evm-test.invalid"
        )
        self.project = Project.objects.create(
            name="Filter Project",
            webhook="https://example.com/webhook",
        )
        self.customer = Customer.objects.create(
            project=self.project,
            uid="filter-customer",
        )

    def make_slot(self, *, chain, suffix: str, uid: str | None = None) -> VaultSlot:
        customer = self.customer
        if uid is not None:
            customer = Customer.objects.create(project=self.project, uid=uid)
        return VaultSlot.objects.create(
            customer=customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=chain,
            address=Web3.to_checksum_address("0x" + suffix.rjust(40, "0")),
            salt=b"\x02" * 32,
        )

    def test_filter_includes_slots_from_other_evm_chains(self):
        local = self.make_slot(chain=self.chain, suffix="01")
        cross_chain = self.make_slot(chain=self.other_chain, suffix="02")

        result = load_scan_filter_addresses(chain=self.chain)

        self.assertIn(local.address, result)
        # Cross-chain slot must be included: same address receives on every
        # EVM chain by design (salt does not include the chain).
        self.assertIn(cross_chain.address, result)

    def test_erc20_fetch_skipped_when_no_recipient_addresses(self):
        rpc_client = Mock()
        rpc_client.get_logs.return_value = []

        # No slots exist anywhere: ERC20 fetch is pointless and must be skipped.
        EvmLogScanner.scan_range(
            chain=self.chain,
            rpc_client=rpc_client,
            token_registry={},
            topic2_addresses=frozenset(),
            from_block=100,
            to_block=100,
        )

        # Only the native-coin fetch fires.
        self.assertEqual(rpc_client.get_logs.call_count, 1)
        self.assertEqual(
            rpc_client.get_logs.call_args.kwargs["topic0"],
            XCASH_NATIVE_RECEIVED_TOPIC0,
        )

    @patch("evm.scanner.logs.SCAN_TOPIC2_GROUP_SIZE", new=2)
    def test_large_filter_set_is_batched_into_grouped_queries(self):
        self.make_slot(chain=self.chain, suffix="01", uid="batch-1")
        self.make_slot(chain=self.chain, suffix="02", uid="batch-2")
        self.make_slot(chain=self.chain, suffix="03", uid="batch-3")

        token = make_crypto(symbol="BAT-TKN")
        token_on_chain = CryptoOnChain.objects.create(
            crypto=token,
            chain=self.chain,
            address=Web3.to_checksum_address("0x" + "aa" * 20),
            decimals=18,
        )
        rpc_client = Mock()
        rpc_client.get_logs.return_value = []

        filter_set = load_scan_filter_addresses(chain=self.chain)
        EvmLogScanner.scan_range(
            chain=self.chain,
            rpc_client=rpc_client,
            token_registry={token_on_chain.address: token_on_chain},
            topic2_addresses=filter_set,
            from_block=100,
            to_block=100,
        )

        erc20_calls = [
            c
            for c in rpc_client.get_logs.call_args_list
            if c.kwargs["topic0"] == ERC20_TRANSFER_TOPIC0
        ]
        # 3 addresses with group size 2 -> 2 grouped queries, each bounded.
        self.assertEqual(len(erc20_calls), 2)
        for c in erc20_calls:
            self.assertLessEqual(len(c.kwargs["topic2"]), 2)
        # Every address covered exactly once across the groups.
        flattened = {a for c in erc20_calls for a in c.kwargs["topic2"]}
        self.assertEqual(flattened, set(filter_set))
