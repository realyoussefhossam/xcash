"""Dispatch guard behavioral tests: at most one outstanding scan task per chain."""

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from common.dispatch_guard import release_scan_dispatch
from common.dispatch_guard import try_claim_scan_dispatch
from evm.tasks import _scan_evm_chain
from evm.tasks import scan_active_evm_chains
from evm.tests._fixtures import make_evm_chain


class ScanDispatchGuardTests(TestCase):
    def setUp(self):
        cache.clear()
        self.chain = make_evm_chain(code="anvil", rpc="http://evm-test.invalid")

    def make_due(self) -> None:
        """Push last_scanned_at into the past so is_due_for_scan is True."""
        from chains.models import Chain

        Chain.objects.filter(pk=self.chain.pk).update(
            last_scanned_at=timezone.now() - timedelta(minutes=5)
        )

    def test_claim_blocks_second_claim_until_released(self):
        self.assertTrue(try_claim_scan_dispatch(self.chain.pk))
        # Outstanding scan still pending: second claim must fail.
        self.assertFalse(try_claim_scan_dispatch(self.chain.pk))

    def test_release_allows_reclaim(self):
        self.assertTrue(try_claim_scan_dispatch(self.chain.pk))
        release_scan_dispatch(self.chain.pk)
        self.assertTrue(try_claim_scan_dispatch(self.chain.pk))

    def test_claim_is_per_chain(self):
        other = make_evm_chain(code="sepolia", rpc="http://evm-test.invalid")
        self.assertTrue(try_claim_scan_dispatch(self.chain.pk))
        # Different chain keeps its own slot.
        self.assertTrue(try_claim_scan_dispatch(other.pk))
        release_scan_dispatch(other.pk)

    @patch("evm.tasks._scan_evm_chain.delay")
    def test_dispatcher_dispatches_due_chain(self, mock_delay):
        self.make_due()
        scan_active_evm_chains()
        mock_delay.assert_called_once_with(self.chain.pk)

    @patch("evm.tasks._scan_evm_chain.delay")
    def test_dispatcher_skips_chain_with_pending_scan(self, mock_delay):
        self.make_due()
        self.assertTrue(try_claim_scan_dispatch(self.chain.pk))
        scan_active_evm_chains()
        mock_delay.assert_not_called()

    @patch("evm.tasks._scan_evm_chain.delay")
    def test_dispatcher_skips_chain_not_due(self, mock_delay):
        from chains.models import Chain

        # Freshly created chain has last_scanned_at=now: not due yet.
        Chain.objects.filter(pk=self.chain.pk).update(
            last_scanned_at=timezone.now()
        )
        scan_active_evm_chains()
        mock_delay.assert_not_called()

    @patch("evm.tasks.dispatch_block_confirmation_checks_if_needed")
    @patch("evm.scanner.service.EvmScannerService.scan_chain")
    def test_scan_task_releases_marker_on_completion(
        self, mock_scan, mock_dispatch
    ):
        self.assertTrue(try_claim_scan_dispatch(self.chain.pk))
        # Run the task body synchronously; celery wiring is out of scope.
        _scan_evm_chain.run(self.chain.pk)
        mock_scan.assert_called_once()
        # Marker released: a new dispatch can be claimed immediately.
        self.assertTrue(try_claim_scan_dispatch(self.chain.pk))

    @patch("evm.tasks.dispatch_block_confirmation_checks_if_needed")
    @patch("evm.scanner.service.EvmScannerService.scan_chain")
    def test_scan_task_releases_marker_even_on_rpc_error(
        self, mock_scan, mock_dispatch
    ):
        from evm.scanner.rpc import EvmScannerRpcError

        mock_scan.side_effect = EvmScannerRpcError("boom")
        self.assertTrue(try_claim_scan_dispatch(self.chain.pk))
        _scan_evm_chain.run(self.chain.pk)
        self.assertTrue(try_claim_scan_dispatch(self.chain.pk))
