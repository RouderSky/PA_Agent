"""机械平移上一轮分析 JSON 中的 K 线序号引用到本轮坐标。

增量分析时，新 K 线插到序列最前端，上一轮所有 K 线序号整体 +N（N=新增根数）。
把上一轮结论里的 K 引用（结构化 bar_range/bar_from/bar_to + 自由文本 reason/
key_signals/summary 等）按 +N 平移成本轮坐标后再喂给 AI，AI 直接引用即可，零换算。

设计要点：
- 自由文本统一用正则 `K(\d+)` 匹配序号并 +N；"K线" 等无数字的 K 不受影响。
- `incremental_delta` 子树记录的是"上一轮增量事实"，不参与平移。
- `bar_from`/`bar_to` 为整数序号字段，单独 +N。
"""
from __future__ import annotations

import re
from typing import Any

_K_REF_RE = re.compile(r"K(\d+)")

#: 这些 key 的子树记录的是"上一轮增量事实"，平移会产生错误语义，需跳过
_SKIP_KEYS = frozenset({"incremental_delta"})

#: 这些整数 key 是 K 线序号，需要 +N
_BAR_INT_KEYS = frozenset({"bar_from", "bar_to"})


def shift_kline_refs_in_text(text: str, n: int) -> str:
    """Shift all `K<number>` references in *text* by +n."""
    if n <= 0 or "K" not in text:
        return text
    return _K_REF_RE.sub(lambda m: f"K{int(m.group(1)) + n}", text)


def _shift_value(value: Any, n: int, *, key: str | None = None) -> Any:
    if key in _SKIP_KEYS:
        return value
    if isinstance(value, str):
        return shift_kline_refs_in_text(value, n)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if key in _BAR_INT_KEYS and n > 0:
            return value + n
        return value
    if isinstance(value, dict):
        return {k: _shift_value(v, n, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_shift_value(v, n) for v in value]
    return value


def shift_kline_refs(obj: Any, n: int) -> Any:
    """Return a deep copy of *obj* with all K-line references shifted by +n.

    When *n* <= 0 the original object is returned unchanged.
    """
    if n <= 0:
        return obj
    return _shift_value(obj, n)
