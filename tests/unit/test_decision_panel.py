"""Unit tests for DecisionPanel (order + diagnosis; prediction lives on FutureTrendPanel)."""
from __future__ import annotations

import sys
import time

import pytest
from PyQt6.QtWidgets import QApplication

from pa_agent.gui.decision_panel import DecisionPanel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture
def panel(qapp):
    p = DecisionPanel()
    p.show()
    qapp.processEvents()
    return p


def _valid_no_order() -> dict:
    return {
        "decision": {
            "order_type": "不下单",
            "order_direction": None,
            "entry_price": None,
            "take_profit_price": None,
            "stop_loss_price": None,
            "reasoning": "test",
            "diagnosis_confidence": 40,
            "diagnosis_confidence_reasoning": "t",
            "trade_confidence": 30,
            "trade_confidence_reasoning": "t",
            "estimated_win_rate": None,
            "estimated_win_rate_reasoning": "t",
            "key_factors": [],
            "watch_points": [],
            "risk_assessment": "t",
            "invalidation_condition": "t",
        },
        "diagnosis_summary": {
            "cycle_position": "normal_channel",
            "direction": "bullish",
            "key_signals": [],
        },
        "decision_trace": [
            {"node_id": "10.3", "question": "q", "answer": "否", "reason": "r", "bar_range": "K1"},
        ],
        "terminal": {"node_id": "10.3", "outcome": "wait", "label": "test"},
    }


def test_panel_bearish_range_trend_shows_biased_sideways(panel: DecisionPanel):
    """Bearish trading range shows 震荡偏空, aligned with 下跌交易区间 cycle label."""
    data = _valid_no_order()
    data["diagnosis_summary"] = {
        "cycle_position": "trading_range",
        "direction": "bearish",
        "alternative_cycle_position": "trending_tr",
        "key_signals": [],
    }
    panel.set_decision(data["decision"], diagnosis_summary=data["diagnosis_summary"])
    assert "震荡偏空" in panel._trend_label.text()
    assert "下跌交易区间" in panel._cycle_label.text()
    assert "#f85149" in panel._trend_label.styleSheet()


def test_panel_no_order_renders(panel: DecisionPanel):
    data = _valid_no_order()
    panel.set_decision(data["decision"], diagnosis_summary=data.get("diagnosis_summary"))
    assert "不下单" in panel._conclusion_label.text()


def test_panel_render_performance(panel: DecisionPanel):
    """set_decision must complete in ≤ 50ms (NFR1.3)."""
    data = _valid_no_order()
    start = time.perf_counter()
    for _ in range(10):
        panel.set_decision(data["decision"], diagnosis_summary=data.get("diagnosis_summary"))
    elapsed = (time.perf_counter() - start) / 10
    assert elapsed < 0.05, f"set_decision took {elapsed*1000:.1f}ms per call"
