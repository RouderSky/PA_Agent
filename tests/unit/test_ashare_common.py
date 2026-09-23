"""Unit tests for shared A-share symbol helpers."""
from __future__ import annotations

from pa_agent.data.ashare_common import is_index_symbol, normalize_ashare_symbol


def test_sh000001_is_shanghai_composite_not_ping_an_bank() -> None:
    assert normalize_ashare_symbol("sh000001") == "sh000001"
    assert is_index_symbol("sh000001") is True


def test_bare_000001_remains_stock() -> None:
    assert normalize_ashare_symbol("000001") == "000001"
    assert is_index_symbol("000001") is False
