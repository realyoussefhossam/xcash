from __future__ import annotations

from typing import Any

import structlog
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils import timezone
from web3 import Web3

from chains.models import Chain
from chains.models import ChainType
from currencies.models import CryptoOnChain
from evm.models import EvmScanCursor
from evm.scanner.constants import DEFAULT_LOG_SCAN_BATCH_SIZE
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0
from evm.scanner.constants import XCASH_NATIVE_RECEIVED_TOPIC0
from evm.scanner.observed_transfers import EvmObservedTransferProcessor
from evm.scanner.rpc import EvmScannerRpcClient
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.watchers import SCAN_TOPIC2_GROUP_SIZE
from evm.scanner.watchers import load_owned_addresses_for_candidates
from evm.scanner.watchers import load_scan_filter_addresses
from evm.scanner.watchers import load_token_registry

logger = structlog.get_logger()

LOG_SCAN_REPLAY_BLOCKS = 2


class EvmLogScanner:
    """按链扫描外部入账日志。"""

    @classmethod
    def scan_chain(
        cls,
        *,
        chain: Chain,
        batch_size: int = DEFAULT_LOG_SCAN_BATCH_SIZE,
        rpc_client: EvmScannerRpcClient | None = None,
    ):
        """根据游标推进一次正向日志扫描，成功后更新游标。"""
        if chain.type != ChainType.EVM:
            raise ValueError(f"仅支持 EVM 链扫描，当前链为 {chain.code}")

        cursor = cls._get_or_create_cursor(chain=chain)
        if not cursor.enabled:
            return
        rpc_client = rpc_client or EvmScannerRpcClient(chain=chain)

        try:
            latest_block = rpc_client.get_latest_block_number()
            Chain.objects.filter(pk=chain.pk).update(
                latest_block_number=Greatest(F("latest_block_number"), latest_block)
            )

            token_registry = load_token_registry(chain=chain)
            # 收款地址过滤集一次加载、整窗口复用：槽位变更频率远低于扫描频率，
            # 逐 chunk 重复加载只会放大 DB 查询开销。
            topic2_addresses = load_scan_filter_addresses(chain=chain)
            scan_window = cls._compute_scan_window(
                cursor=cursor,
                latest_block=latest_block,
                batch_size=batch_size,
            )
            if scan_window is None:
                cls._mark_cursor_idle(cursor=cursor)
                return
            from_block, to_block = scan_window

            # 按 chunk 推进，每扫完一段立即提交游标。整窗口最多 batch_size 块、会被
            # 拆成多次 eth_getLogs，单 tick 总耗时可能超过 Celery 的软超时（默认 30s）
            # 或撞上进程回收；若等整窗口扫完才提交游标，中断就会把已经扫完的区块一并
            # 回退，下一轮从同一起点重来——节点稍慢即原地踏步、永远追不上链头。
            # 分段提交后中断只损失当前这一段，已完成的进度不丢。
            chunk_size = max(1, chain.evm_log_max_block_range)
            chunk_from = from_block
            while chunk_from <= to_block:
                chunk_to = min(to_block, chunk_from + chunk_size - 1)
                cls.scan_range(
                    chain=chain,
                    rpc_client=rpc_client,
                    token_registry=token_registry,
                    topic2_addresses=topic2_addresses,
                    from_block=chunk_from,
                    to_block=chunk_to,
                )
                cls._advance_cursor(cursor=cursor, scanned_to_block=chunk_to)
                chunk_from = chunk_to + 1
        except EvmScannerRpcError as exc:
            cls._mark_cursor_error(cursor=cursor, exc=exc)
            raise

    @classmethod
    def scan_range(
        cls,
        *,
        chain: Chain,
        rpc_client: EvmScannerRpcClient,
        token_registry: dict[str, CryptoOnChain],
        topic2_addresses: frozenset[str],
        from_block: int,
        to_block: int,
    ) -> None:
        """对 [from_block, to_block] 区间拉取一次日志并按类型落库。"""
        logs = cls._fetch_logs(
            rpc_client=rpc_client,
            token_registry=token_registry,
            from_block=from_block,
            to_block=to_block,
            topic2_addresses=topic2_addresses,
        )
        cls._process_logs(
            chain=chain,
            logs=logs,
            rpc_client=rpc_client,
            token_registry=token_registry,
        )

    @classmethod
    def _process_logs(
        cls,
        *,
        chain: Chain,
        logs: list[dict[str, Any]],
        rpc_client: EvmScannerRpcClient,
        token_registry: dict[str, CryptoOnChain],
    ) -> None:
        """把外部入账日志交给 Transfer 落库。

        代币表（token_registry）决定关注哪些代币与如何解码；本轮命中的系统自有
        收款地址（owned_addresses）由本窗口日志候选地址即时匹配得出，二者各自传入。
        """
        owned_addresses = load_owned_addresses_for_candidates(
            chain=chain,
            addresses=cls._watched_address_candidates_from_logs(logs=logs),
        )
        EvmObservedTransferProcessor.process(
            chain=chain,
            rpc_client=rpc_client,
            raw_logs=logs,
            token_registry=token_registry,
            owned_addresses=owned_addresses,
        )

    @classmethod
    def _fetch_logs(
        cls,
        *,
        rpc_client: EvmScannerRpcClient,
        token_registry: dict[str, CryptoOnChain],
        from_block: int,
        to_block: int,
        topic2_addresses: frozenset[str],
    ) -> list[dict[str, Any]]:
        """拉取本轮关注的外部入账日志。

        ERC20 查询把收款地址过滤下推到节点侧（topic2 OR 列表），避免高频稳定币
        全网日志撑爆结果上限；过滤集超过单请求承载量时按固定分组分批查询，
        分组互不相交，每笔日志只会在唯一分组命中，直接拼接即为完整结果。
        """
        logs: list[dict[str, Any]] = []
        logs.extend(
            rpc_client.get_logs(
                from_block=from_block,
                to_block=to_block,
                addresses=None,
                topic0=XCASH_NATIVE_RECEIVED_TOPIC0,
                summary="获取 EVM Xcash 原生币入账日志失败",
            )
        )
        erc20_addresses = cls._erc20_log_filter_addresses(token_registry=token_registry)
        if erc20_addresses and topic2_addresses:
            ordered = sorted(topic2_addresses)
            erc20_logs: list[dict[str, Any]] = []
            for start in range(0, len(ordered), SCAN_TOPIC2_GROUP_SIZE):
                group = ordered[start : start + SCAN_TOPIC2_GROUP_SIZE]
                erc20_logs.extend(
                    rpc_client.get_logs(
                        from_block=from_block,
                        to_block=to_block,
                        addresses=erc20_addresses,
                        topic0=ERC20_TRANSFER_TOPIC0,
                        topic2=group,
                        summary="获取 EVM ERC20 Transfer 日志失败",
                    )
                )
            # 分批查询后按链上顺序归位，保持与单次查询一致的处理顺序。
            erc20_logs.sort(
                key=lambda log: (
                    cls._log_position_key(log.get("blockNumber", 0)),
                    cls._log_position_key(log.get("logIndex", 0)),
                )
            )
            logs.extend(erc20_logs)
        return logs

    @staticmethod
    def _log_position_key(value: Any) -> int:
        """把日志位置字段统一为 int（兼容 hex 字符串与十进制 int 两种形态）。

        无法解析的字段（后续会在解析阶段被跳过的畸形日志）归为 0，只影响排序、
        不影响最终是否落库。
        """
        if isinstance(value, int):
            return value
        try:
            return int(value, 16)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _erc20_log_filter_addresses(
        *, token_registry: dict[str, CryptoOnChain]
    ) -> list[str]:
        """返回需要在 eth_getLogs 中作为合约地址过滤的 ERC20 列表。"""
        return sorted(token_registry.keys())

    @classmethod
    def _watched_address_candidates_from_logs(
        cls,
        *,
        logs: list[dict[str, Any]],
    ) -> set[str]:
        """从本轮日志里抽出可能命中观察集的地址，供后续批量精确匹配。"""
        candidates: set[str] = set()
        for log in logs:
            if log.get("removed"):
                continue
            topics = list(log.get("topics") or [])
            if not topics:
                continue
            topic0 = cls._normalize_topic(topics[0])
            if topic0 == XCASH_NATIVE_RECEIVED_TOPIC0.lower():
                if address := cls._normalize_address(log.get("address")):
                    candidates.add(address)
                if len(topics) >= 2 and (payer := cls._topic_to_address(topics[1])):
                    candidates.add(payer)
                continue
            if topic0 == ERC20_TRANSFER_TOPIC0.lower() and len(topics) >= 3:
                for topic in topics[1:3]:
                    if address := cls._topic_to_address(topic):
                        candidates.add(address)
        return candidates

    @staticmethod
    def _normalize_topic(value: Any) -> str:
        """把 topic 统一成小写十六进制串，方便比较。"""
        if isinstance(value, bytes):
            return Web3.to_hex(value).lower()
        return str(value or "").lower()

    @staticmethod
    def _normalize_address(value: Any) -> str | None:
        """转 checksum 地址，非法地址返回 None 而不是抛错。"""
        try:
            return Web3.to_checksum_address(str(value or ""))
        except ValueError:
            return None

    @classmethod
    def _topic_to_address(cls, topic: Any) -> str | None:
        """从 32 字节 topic 取最后 20 字节作为地址。"""
        try:
            topic_hex = cls._normalize_topic(topic)
            if len(topic_hex) < 42:
                return None
            return Web3.to_checksum_address("0x" + topic_hex[-40:])
        except ValueError:
            return None

    @classmethod
    def _get_or_create_cursor(cls, *, chain: Chain) -> EvmScanCursor:
        """加锁取出或新建本链的扫描游标，避免并发扫描争抢。"""
        with transaction.atomic():
            cursor, _ = EvmScanCursor.objects.select_for_update().get_or_create(
                chain=chain,
                defaults={"last_scanned_block": 0, "enabled": True},
            )
        return cursor

    @staticmethod
    def _compute_scan_window(
        *,
        cursor: EvmScanCursor,
        latest_block: int,
        batch_size: int,
    ) -> tuple[int, int] | None:
        """根据游标和批次大小算出本轮扫描区间；永远只扫到最新块前一块。

        新建游标（last_scanned_block<=0）只把扫描起点锚定到当前链头，绝不从
        创世块全量回扫历史日志：首轮仅观测最新确认块，之后再按批次正向推进。
        """
        target_block = latest_block - 1
        if target_block <= 0:
            return None

        # 首次扫描：游标尚未锚定，直接对齐链头，避免从区块 1 全量回扫历史。
        if cursor.last_scanned_block <= 0:
            return target_block, target_block

        forward_batch_size = max(1, batch_size)
        to_block = min(target_block, cursor.last_scanned_block + forward_batch_size)
        from_block = max(1, cursor.last_scanned_block + 1 - LOG_SCAN_REPLAY_BLOCKS)
        if from_block > to_block:
            return None
        return from_block, to_block

    @staticmethod
    def _mark_cursor_idle(*, cursor: EvmScanCursor) -> None:
        """无新块可扫时只清空错误状态，不推进游标。"""
        EvmScanCursor.objects.filter(pk=cursor.pk).update(
            last_error="",
            last_error_at=None,
            updated_at=timezone.now(),
        )

    @staticmethod
    def _advance_cursor(*, cursor: EvmScanCursor, scanned_to_block: int) -> None:
        """把游标推进到本轮扫描末端，并清空错误状态。"""
        EvmScanCursor.objects.filter(pk=cursor.pk).update(
            last_scanned_block=Greatest(F("last_scanned_block"), scanned_to_block),
            last_error="",
            last_error_at=None,
            updated_at=timezone.now(),
        )

    @staticmethod
    def _mark_cursor_error(*, cursor: EvmScanCursor, exc: Exception) -> None:
        """记录本轮 RPC 错误到游标，便于运维观察。"""
        logger.warning(
            "EVM 日志扫描失败",
            chain=cursor.chain.code,
            error=str(exc),
        )
        EvmScanCursor.objects.filter(pk=cursor.pk).update(
            last_error=str(exc),
            last_error_at=timezone.now(),
            updated_at=timezone.now(),
        )
