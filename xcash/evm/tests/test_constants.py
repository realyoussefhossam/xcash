"""x402 gas 常量表与读取函数：行为契约（未配置必须显式报错）。"""
from types import SimpleNamespace

import pytest

from evm.constants import get_x402_eip3009_facilitate_gas


def test_missing_chain_id_raises_with_helpful_message():
    chain = SimpleNamespace(chain_id=999_999, code="unknown-test")
    with pytest.raises(ValueError, match=r"unknown-test.*999999.*evm/constants\.py") as exc:
        get_x402_eip3009_facilitate_gas(chain)
    msg = str(exc.value)
    assert "unknown-test" in msg
    assert "999999" in msg
    assert "evm/constants.py" in msg  # 错误消息要指引修复位置
