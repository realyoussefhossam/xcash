from __future__ import annotations

from django.core.cache import cache

from chains.models import Chain
from chains.models import VaultSlot
from currencies.models import CryptoOnChain

EVM_CRYPTO_ON_CHAINS_CACHE_KEY_TEMPLATE = "evm:scanner:crypto_on_chains:{chain_id}"


def load_token_registry(
    *, chain: Chain, refresh: bool = False
) -> dict[str, CryptoOnChain]:
    """加载某条 EVM 链当前受支持的 ERC20 代币表，按合约地址索引。

    代币表是 per-chain 的静态配置（CryptoOnChain 后台手动维护），扫描前一次性加载
    并长驻缓存。它只回答“关注哪些代币”，与“本轮命中了哪些系统自有收款地址”是两件事，
    后者按日志窗口走 load_owned_addresses_for_candidates，两者职责互不相干。
    """

    cache_key = _crypto_on_chains_cache_key(chain=chain)
    tokens_by_address = cache.get(cache_key)
    if refresh or tokens_by_address is None:
        tokens_by_address = refresh_evm_crypto_on_chains(chain=chain)
    return tokens_by_address


def refresh_evm_crypto_on_chains(*, chain: Chain) -> dict[str, CryptoOnChain]:
    """重建指定 EVM 链的 ERC20 代币缓存。"""

    tokens_by_address = _load_evm_crypto_on_chains_from_db(chain=chain)
    # timeout=None 表示永不过期，依赖显式刷新（CryptoOnChain 表为后台手动配置，几乎不变）。
    cache.set(
        _crypto_on_chains_cache_key(chain=chain),
        tokens_by_address,
        timeout=None,
    )
    return tokens_by_address


def clear_evm_token_registry_cache(*, chain: Chain | None = None) -> None:
    """清空 EVM 代币表缓存，主要用于测试和运维脚本。"""

    if chain is not None:
        clear_evm_crypto_on_chains_cache(chain=chain)
        return
    delete_pattern = getattr(cache, "delete_pattern", None)
    if callable(delete_pattern):
        delete_pattern(
            EVM_CRYPTO_ON_CHAINS_CACHE_KEY_TEMPLATE.format(chain_id="*")
        )


def clear_evm_crypto_on_chains_cache(*, chain: Chain) -> None:
    """清空指定 EVM 链的 ERC20 代币缓存。"""

    cache.delete(_crypto_on_chains_cache_key(chain=chain))


def load_owned_addresses_for_candidates(
    *,
    chain: Chain,
    addresses: set[str] | frozenset[str],
) -> frozenset[str]:
    """从本轮日志候选地址中批量筛出系统自有的收款地址。

    自有收款地址来自 VaultSlot 与 DifferRecipientAddress；不在扫描前全量加载，
    而是在每个日志窗口内按候选地址即时匹配，所以与代币表分开维护。
    """

    if not addresses:
        return frozenset()

    vault_slot_addresses = VaultSlot.objects.filter(
        chain=chain,
        address__in=addresses,
    ).values_list("address", flat=True)
    from invoices.models import DifferRecipientAddress

    differ_addresses = DifferRecipientAddress.matched_addresses_for_candidates(
        chain=chain,
        candidates=set(addresses),
    )
    return frozenset(set(vault_slot_addresses) | differ_addresses)


def _crypto_on_chains_cache_key(*, chain: Chain) -> str:
    """构造按链区分的 ERC20 缓存 key。"""
    return EVM_CRYPTO_ON_CHAINS_CACHE_KEY_TEMPLATE.format(chain_id=chain.pk)


def _load_evm_crypto_on_chains_from_db(
    *, chain: Chain
) -> dict[str, CryptoOnChain]:
    """从 DB 拉取本链已激活 ERC20，按合约地址建立索引。"""
    token_rows = (
        CryptoOnChain.objects.select_related("crypto")
        .filter(
            chain=chain,
            crypto__active=True,
            active=True,
        )
        .exclude(address="")
    )
    return {token.address: token for token in token_rows}
