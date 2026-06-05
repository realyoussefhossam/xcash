from __future__ import annotations

from tron.config import tron_vault_slot_runtime_ready

from chains.models import ChainType
from chains.service import ChainService
from currencies.models import ChainCryptoDeployment
from projects.models import Project


class ProjectService:
    """集中封装 Project 相关的常用读取逻辑。"""

    @staticmethod
    def get_by_appid(appid: str) -> Project:
        return Project.retrieve(appid)

    @staticmethod
    def get_by_id(project_id: int) -> Project:
        return Project.objects.get(pk=project_id)

    @staticmethod
    def contract_receivable_chain_codes(project: Project) -> set[str]:
        """VaultSlot 合约模式下项目可收款的链 code 集合。

        合约收款依赖项目不可变 vault 地址；Tron 只有 Nile 验证结论与 factory/template/
        fee_limit 明确配置后才暴露，默认配置下始终只返回 EVM。
        """
        if not project.vault:
            return set()
        chain_codes = ChainService.codes_of_types({ChainType.EVM})
        if not tron_vault_slot_runtime_ready():
            return chain_codes

        tron_codes = set(
            ChainCryptoDeployment.objects.filter(
                chain__type=ChainType.TRON,
                chain__active=True,
                crypto__symbol="USDT",
                crypto__active=True,
                active=True,
            )
            .exclude(address="")
            .values_list("chain__code", flat=True)
        )
        return chain_codes | tron_codes
