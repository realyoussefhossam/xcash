"""项目收款归集地址（Vault）的多签合约校验。

Vault 是 EVM VaultSlot 合约写死的不可变转发目标，商户资金最终汇入此地址，
故必须是已部署的多签合约（threshold>=2 且 owners>=threshold），不能是 EOA 或未部署地址。

校验逻辑集中在此模块，供 admin 表单与内部 API 序列化器共用：二者都在写入边界调用，
避免把涉及 RPC 的链上校验下沉到 Project.save（那会让每次保存都打 RPC）。
不可变性（纯 DB 比对）仍由 Project.save 兜底，见 projects.models.Project.save。
"""

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from web3 import Web3

from chains.constants import ChainType
from chains.models import Chain

# Gnosis Safe 风格多签合约的最小只读接口：用于核验 threshold 与 owners。
MULTISIG_WALLET_ABI = [
    {
        "inputs": [],
        "name": "getThreshold",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getOwners",
        "outputs": [{"internalType": "address[]", "name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def validate_vault_is_multisig(address: str) -> str:
    """校验 address 是合法 EVM 地址，且在某条启用 EVM 链上为有效多签合约。

    成功返回 checksum 地址；任意一项不满足抛 django.core.exceptions.ValidationError
    （forms 与 DRF 序列化器均可适配该异常）。

    判定规则：只要在任意一条可校验 EVM 链上检测到该地址有合约代码、且
    threshold>=2 与 owners>=threshold，即视为有效多签（多签合约跨链同址部署，命中一条即可）。
    """
    if not address:
        raise ValidationError(_("收款归集地址不能为空。"))

    if not Web3.is_address(address):
        raise ValidationError(_("VaultSlot 多签归集地址必须是 EVM 地址。"))

    address = Web3.to_checksum_address(address)

    evm_chains = Chain.objects.filter(type=ChainType.EVM, active=True).exclude(rpc="")
    if not evm_chains.exists():
        raise ValidationError(_("没有可用于校验合约地址的已启用 EVM 链。"))

    checked_chain_names = []
    for chain in evm_chains:
        checked_chain_names.append(chain.name)
        try:
            code = chain.w3.eth.get_code(address)
        except Exception:
            code = None
        if not code:
            continue

        try:
            contract = chain.w3.eth.contract(address=address, abi=MULTISIG_WALLET_ABI)
            threshold = contract.functions.getThreshold().call()
            owners = contract.functions.getOwners().call()
        except Exception:
            threshold = 0
            owners = []

        if threshold >= 2 and len(owners) >= threshold:
            return address

    raise ValidationError(
        _("VaultSlot 多签归集地址未在任何可校验 EVM 链上检测到有效多签合约：%(chains)s")
        % {"chains": ", ".join(checked_chain_names)}
    )
