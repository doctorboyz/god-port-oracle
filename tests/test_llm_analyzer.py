"""Tests for LLM backtest analyzer."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from broky.backtest.engine import BacktestResult, BacktestTrade
from broky.backtest.compare import ComparisonResult
from broky.backtest.llm_analyzer import (
    AnalysisInsight,
    LLMAnalyzer,
    serialize_backtest_metrics,
    serialize_comparison_metrics,
)


def _make_result(**overrides) -> BacktestResult:
    """Create a sample BacktestResult for testing."""
    defaults = dict(
        total_trades=100,
        winning_trades=55,
        losing_trades=45,
        win_rate=0.55,
        total_pnl=385.0,
        total_pnl_pct=38.5,
        max_drawdown_pct=11.8,
        profit_factor=1.64,
        sharpe_ratio=1.2,
        avg_trade_pnl=3.85,
        max_consecutive_wins=6,
        max_consecutive_losses=3,
    )
    defaults.update(overrides)
    return BacktestResult(**defaults)


class TestSerializeBacktestMetrics:
    """Test metric serialization."""

    def test_basic_metrics(self):
        result = _make_result()
        metrics = serialize_backtest_metrics(result)
        assert metrics["total_trades"] == 100
        assert metrics["win_rate"] == 0.55
        assert metrics["total_pnl"] == 385.0
        assert metrics["profit_factor"] == 1.64
        assert "equity_curve" not in metrics

    def test_infinite_profit_factor(self):
        result = _make_result(profit_factor=float("inf"))
        metrics = serialize_backtest_metrics(result)
        assert metrics["profit_factor"] == "inf"

    def test_exit_reason_distribution(self):
        trades = [
            BacktestTrade(entry_idx=0, entry_price=1900, direction=__import__("shared.models", fromlist=["SignalType"]).SignalType.BUY, lot_size=0.01, stop_loss=1895, take_profit=1910, exit_idx=1, exit_price=1905, pnl=5.0, exit_reason="take_profit"),
            BacktestTrade(entry_idx=0, entry_price=1900, direction=__import__("shared.models", fromlist=["SignalType"]).SignalType.BUY, lot_size=0.01, stop_loss=1895, take_profit=1910, exit_idx=1, exit_price=1895, pnl=-5.0, exit_reason="stop_loss"),
        ]
        result = _make_result(trades=trades)
        metrics = serialize_backtest_metrics(result)
        assert metrics["exit_reason_distribution"] == {"take_profit": 1, "stop_loss": 1}

    def test_liquidated_flag(self):
        result = _make_result(liquidated=True)
        metrics = serialize_backtest_metrics(result)
        assert metrics["liquidated"] is True

    def test_rounding(self):
        result = _make_result(win_rate=0.551234, total_pnl=385.5678)
        metrics = serialize_backtest_metrics(result)
        assert metrics["win_rate"] == 0.5512
        assert metrics["total_pnl"] == 385.57


class TestSerializeComparisonMetrics:
    """Test comparison metric serialization."""

    def test_comparison_serialization(self):
        results = [
            ComparisonResult(
                name="conservative",
                total_trades=50, win_rate=0.60, total_pnl=200.0,
                total_pnl_pct=20.0, max_drawdown_pct=8.0, profit_factor=2.0,
                sharpe_ratio=1.5, avg_trade_pnl=4.0,
                max_consecutive_wins=4, max_consecutive_losses=2,
                liquidated=False,
            ),
        ]
        metrics = serialize_comparison_metrics(results)
        assert len(metrics["strategies"]) == 1
        assert metrics["strategies"][0]["name"] == "conservative"
        assert metrics["strategies"][0]["total_trades"] == 50


class TestParseResponse:
    """Test LLM response parsing."""

    def test_valid_json(self):
        analyzer = LLMAnalyzer()
        response = json.dumps({
            "score": 7,
            "strengths": ["Good win rate", "Low drawdown"],
            "weaknesses": ["Low profit factor"],
            "suggestions": ["Increase ATR multiplier"],
            "regime_notes": "Works in trending markets",
            "risk_assessment": "Acceptable risk",
        })
        insight = analyzer._parse_response(response)
        assert insight.score == 7
        assert len(insight.strengths) == 2
        assert "Good win rate" in insight.strengths

    def test_markdown_wrapped_json(self):
        analyzer = LLMAnalyzer()
        response = '```json\n{"score": 8, "strengths": ["A"], "weaknesses": [], "suggestions": [], "regime_notes": "", "risk_assessment": ""}\n```'
        insight = analyzer._parse_response(response)
        assert insight.score == 8

    def test_invalid_text(self):
        analyzer = LLMAnalyzer()
        insight = analyzer._parse_response("This is not JSON at all")
        assert insight.score == 0
        assert "not valid JSON" in insight.weaknesses[0]


class TestLLMAnalyzerCalls:
    """Test LLMAnalyzer with mocked _call_llm."""

    def test_analyze_backtest(self):
        analyzer = LLMAnalyzer()
        mock_response = json.dumps({
            "score": 6,
            "strengths": ["Decent win rate"],
            "weaknesses": ["Low profit factor"],
            "suggestions": ["Tweak ATR multiplier"],
            "regime_notes": "OK in trending",
            "risk_assessment": "Moderate risk",
        })
        with patch.object(analyzer, "_call_llm", return_value=mock_response):
            result = _make_result()
            insight = analyzer.analyze_backtest(result, strategy_name="swing")
            assert insight.score == 6
            assert "Decent win rate" in insight.strengths

    def test_analyze_comparison(self):
        analyzer = LLMAnalyzer()
        mock_response = json.dumps({
            "score": 7,
            "strengths": ["Best performer: moderate"],
            "weaknesses": ["Aggressive has high drawdown"],
            "suggestions": ["Use moderate config"],
            "regime_notes": "All work in trending",
            "risk_assessment": "Moderate risk",
        })
        with patch.object(analyzer, "_call_llm", return_value=mock_response):
            results = [
                ComparisonResult(
                    name="moderate", total_trades=80, win_rate=0.55,
                    total_pnl=300.0, total_pnl_pct=30.0, max_drawdown_pct=12.0,
                    profit_factor=1.8, sharpe_ratio=1.3, avg_trade_pnl=3.75,
                    max_consecutive_wins=5, max_consecutive_losses=3, liquidated=False,
                ),
            ]
            insight = analyzer.analyze_comparison(results)
            assert insight.score == 7

    def test_api_failure_graceful(self):
        analyzer = LLMAnalyzer()
        error_json = json.dumps({
            "score": 0,
            "strengths": [],
            "weaknesses": ["LLM analysis failed"],
            "suggestions": ["Check Ollama is running"],
        })
        with patch.object(analyzer, "_call_llm", return_value=error_json):
            result = _make_result()
            insight = analyzer.analyze_backtest(result)
            assert insight.score == 0
            assert len(insight.weaknesses) > 0