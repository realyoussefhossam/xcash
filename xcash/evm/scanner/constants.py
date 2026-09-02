from __future__ import annotations

from web3 import Web3

# ERC20 Transfer 事件签名主题，所有日志扫描都依赖这一稳定标识。
ERC20_TRANSFER_TOPIC0 = Web3.to_hex(
    Web3.keccak(text="Transfer(address,address,uint256)")
)

# VaultSlot 原生币接收事件签名主题；log.address 即 VaultSlot 地址。
XCASH_NATIVE_RECEIVED_TOPIC0 = Web3.to_hex(
    Web3.keccak(text="XcashNativeReceived(address,uint256)")
)

# 单次 EVM 日志扫描默认净推进块数。
# 早期取 100 过于保守：链一旦落后（RPC 中断数天）就需要数天甚至数周才能追上
# 链头（bsc 落后 41 万块时 ETA 14h，arbitrum 落后 300 万块时 ETA 4 天）。
# 游标按 chunk 分段提交（见 logs.py），中断只损失当前段，大 batch 不会放大回退风险；
# 实时跟随阶段窗口本就很小，该值只在落后追块时生效。
DEFAULT_LOG_SCAN_BATCH_SIZE = 2000
