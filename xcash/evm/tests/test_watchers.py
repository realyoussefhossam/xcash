from django.core.cache import cache
from django.test import TestCase
from django.test import override_settings
from web3 import Web3

from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import Wallet
from currencies.models import ChainToken
from currencies.models import Crypto
from evm.models import VaultSlot
from evm.scanner.watchers import clear_evm_watch_set_cache
from evm.scanner.watchers import load_watch_set
from projects.models import DifferRecipientAddress
from projects.models import Project
from users.models import Customer

WATCHER_TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "evm-watch-set-tests",
    }
}


@override_settings(CACHES=WATCHER_TEST_CACHES)
class EvmWatchSetCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.native = Crypto.objects.create(
            name="Watcher Native",
            symbol="WNATIVE",
            coingecko_id="watcher-native",
        )
        self.chain = Chain.objects.create(
            code="watcher-chain",
            name="Watcher Chain",
            type=ChainType.EVM,
            chain_id=88_001,
            rpc="http://watcher.local",
            native_coin=self.native,
            confirm_block_count=6,
            active=True,
        )
        self.token = Crypto.objects.create(
            name="Watcher Token",
            symbol="WTKN",
            coingecko_id="watcher-token",
            decimals=18,
        )
        self.token_deployment = ChainToken.objects.create(
            crypto=self.token,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000aa"
            ),
            decimals=18,
        )
        self.wallet = Wallet.objects.create()
        self.address = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000bb"
            ),
        )
        self.project = self._create_project()
        self.customer = Customer.objects.create(
            project=self.project,
            uid="watcher-customer",
        )
        self.vault_slot = VaultSlot.objects.create(
            customer=self.customer,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000bc"
            ),
            vault_address=self.address.address,
            salt=b"\x01" * 32,
        )

    def tearDown(self):
        clear_evm_watch_set_cache()
        cache.clear()

    def test_load_watch_set_refreshes_when_vault_slot_is_deleted(self):
        initial_watch_set = load_watch_set(chain=self.chain, refresh=True)
        self.assertIn(self.vault_slot.address, initial_watch_set.watched_addresses)

        VaultSlot.objects.filter(pk=self.vault_slot.pk).delete()

        watch_set = load_watch_set(chain=self.chain)
        self.assertNotIn(self.vault_slot.address, watch_set.watched_addresses)

    def test_load_watch_set_excludes_system_hot_wallet_addresses(self):
        watch_set = load_watch_set(chain=self.chain, refresh=True)

        self.assertNotIn(self.address.address, watch_set.watched_addresses)
        self.assertIn(self.vault_slot.address, watch_set.watched_addresses)

    def test_vault_slot_save_refreshes_cached_watch_set_after_commit(self):
        load_watch_set(chain=self.chain, refresh=True)
        new_slot_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000d01"
        )
        new_customer = Customer.objects.create(
            project=self.project,
            uid="watcher-customer-new-slot",
        )

        with self.captureOnCommitCallbacks(execute=True):
            VaultSlot.objects.create(
                customer=new_customer,
                chain=self.chain,
                address=new_slot_address,
                vault_address=self.address.address,
                salt=b"\x02" * 32,
            )

        watch_set = load_watch_set(chain=self.chain)
        self.assertIn(new_slot_address, watch_set.watched_addresses)

    def test_load_watch_set_includes_evm_differ_recipient_addresses(self):
        project = self._create_project()
        recipient_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000d01"
        )
        DifferRecipientAddress.objects.create(
            project=project,
            chain_type=ChainType.EVM,
            address=recipient_address,
        )

        watch_set = load_watch_set(chain=self.chain, refresh=True)

        self.assertIn(recipient_address, watch_set.watched_addresses)

    def test_load_watch_set_excludes_non_evm_differ_recipient_addresses(self):
        project = self._create_project()
        recipient_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000d02"
        )
        DifferRecipientAddress.objects.create(
            project=project,
            chain_type=ChainType.TRON,
            address=recipient_address,
        )

        watch_set = load_watch_set(chain=self.chain, refresh=True)

        self.assertNotIn(recipient_address, watch_set.watched_addresses)

    def test_differ_recipient_address_save_refreshes_cached_watch_set_after_commit(
        self,
    ):
        project = self._create_project()
        load_watch_set(chain=self.chain, refresh=True)
        recipient_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000d03"
        )

        with self.captureOnCommitCallbacks(execute=True):
            DifferRecipientAddress.objects.create(
                project=project,
                chain_type=ChainType.EVM,
                address=recipient_address,
            )

        watch_set = load_watch_set(chain=self.chain)
        self.assertIn(recipient_address, watch_set.watched_addresses)

    def test_differ_recipient_address_delete_refreshes_cached_watch_set_after_commit(
        self,
    ):
        project = self._create_project()
        recipient_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000d04"
        )
        differ_address = DifferRecipientAddress.objects.create(
            project=project,
            chain_type=ChainType.EVM,
            address=recipient_address,
        )
        initial_watch_set = load_watch_set(chain=self.chain, refresh=True)
        self.assertIn(recipient_address, initial_watch_set.watched_addresses)

        with self.captureOnCommitCallbacks(execute=True):
            differ_address.delete()

        watch_set = load_watch_set(chain=self.chain)
        self.assertNotIn(recipient_address, watch_set.watched_addresses)

    def test_chain_token_save_refreshes_cached_token_set_after_commit(self):
        load_watch_set(chain=self.chain, refresh=True)
        new_token = Crypto.objects.create(
            name="Watcher Token Two",
            symbol="WTKN2",
            coingecko_id="watcher-token-two",
            decimals=6,
        )
        token_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000ee"
        )

        with self.captureOnCommitCallbacks(execute=True):
            ChainToken.objects.create(
                crypto=new_token,
                chain=self.chain,
                address=token_address,
                decimals=6,
            )

        watch_set = load_watch_set(chain=self.chain)
        self.assertIn(token_address, watch_set.tokens_by_address)

    def test_chain_token_delete_refreshes_cached_token_set_after_commit(self):
        initial_watch_set = load_watch_set(chain=self.chain, refresh=True)
        self.assertIn(
            self.token_deployment.address, initial_watch_set.tokens_by_address
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.token_deployment.delete()

        watch_set = load_watch_set(chain=self.chain)
        self.assertNotIn(self.token_deployment.address, watch_set.tokens_by_address)

    def test_crypto_active_change_refreshes_cached_token_set_after_commit(self):
        initial_watch_set = load_watch_set(chain=self.chain, refresh=True)
        self.assertIn(
            self.token_deployment.address, initial_watch_set.tokens_by_address
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.token.active = False
            self.token.save(update_fields=["active"])

        watch_set = load_watch_set(chain=self.chain)
        self.assertNotIn(self.token_deployment.address, watch_set.tokens_by_address)

    def _create_project(self) -> Project:
        suffix = Wallet.objects.count()
        return Project.objects.create(
            name=f"watcher-project-{suffix}",
            wallet=Wallet.objects.create(),
            webhook="https://example.com/webhook",
        )
