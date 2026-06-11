"""saas_api 链列表端点的行为测试。

覆盖：
- 仅返回已启用且非测试网的链
- 每条链含 code / name / icon / type 字段契约（供 SaaS 收款页按链类型展示）
- icon 为引擎权威下发的图标 URL
- 需认证
"""

from unittest.mock import patch

import pytest
from django.test import override_settings

from chains.models import Chain

AUTH_HEADER = "Bearer test-saas-token"
URL = "/saas/v1/chains"


def _make_chain(**kwargs) -> Chain:
    """建链辅助。Chain.save() 的 clean() 会对 active EVM 链真连 RPC 校验 chain_id；
    本测试只关心列表端点的过滤与字段契约，故跳过该网络校验（is_testnet/type 仍由
    save() 从 code 常量正常填充，不受影响）。"""
    with patch("chains.models.Chain.clean", return_value=None):
        return Chain.objects.create(**kwargs)


@pytest.mark.django_db
class TestSaasChainsList:
    def test_lists_active_mainnet_chains_with_contract_fields(self, client):
        _make_chain(code="ethereum", active=True, rpc="http://rpc")
        _make_chain(code="tron", active=True, tron_api_key="key")

        resp = client.get(URL, HTTP_AUTHORIZATION=AUTH_HEADER)

        assert resp.status_code == 200
        by_code = {c["code"]: c for c in resp.json()}
        assert set(by_code) == {"ethereum", "tron"}
        eth = by_code["ethereum"]
        assert eth["name"] == "Ethereum"
        assert eth["type"] == "evm"
        assert eth["icon"].startswith("https://")
        assert by_code["tron"]["type"] == "tron"

    def test_excludes_testnet_and_inactive(self, client):
        _make_chain(code="ethereum", active=True, rpc="http://rpc")
        # Sepolia 由 code 常量自动判定为测试网，应被过滤。
        _make_chain(code="sepolia", active=True, rpc="http://rpc")
        # 未启用链应被过滤。
        _make_chain(code="bsc", active=False)

        resp = client.get(URL, HTTP_AUTHORIZATION=AUTH_HEADER)

        assert resp.status_code == 200
        assert {c["code"] for c in resp.json()} == {"ethereum"}

    @override_settings(DEBUG=True)
    def test_debug_lists_local_anvil_without_public_testnets(self, client):
        _make_chain(code="ethereum", active=True, rpc="http://rpc")
        _make_chain(code="anvil", active=True, rpc="http://rpc")
        _make_chain(code="sepolia", active=True, rpc="http://rpc")

        resp = client.get(URL, HTTP_AUTHORIZATION=AUTH_HEADER)

        assert resp.status_code == 200
        assert {c["code"] for c in resp.json()} == {"ethereum", "anvil"}

    def test_requires_auth(self, client):
        resp = client.get(URL)
        assert resp.status_code in (401, 403)
