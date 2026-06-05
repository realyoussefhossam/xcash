from __future__ import annotations

from django.conf import settings


def tron_vault_slot_runtime_ready() -> bool:
    """Tron VaultSlot 对外暴露的全局门禁。

    Nile 未验证、factory/template 未配置、fee_limit 未定时都返回 False；业务入口据此
    不把 Tron 暴露给普通项目。内部脚本和手工测试仍可显式调用底层 builder。
    """
    return all(
        (
            settings.TRON_VAULT_SLOT_NILE_VERIFIED,
            settings.TRON_VAULT_SLOT_FACTORY_ADDRESS,
            settings.TRON_VAULT_SLOT_TEMPLATE_ADDRESS,
            settings.TRON_VAULT_SLOT_FEE_LIMIT > 0,
            settings.TRON_VAULT_SLOT_DEPLOY_FEE_LIMIT > 0,
        )
    )

