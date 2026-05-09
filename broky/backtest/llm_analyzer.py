"""LLM-powered backtest analysis — sends metrics to Ollama and returns structured insights.

Usage:
    python -m broky.backtest.llm_analyzer --config moderate --analyze
    python -m broky.backtest.llm_analyzer --config moderate (backtest only, no LLM)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import Optional

import requests
from pydantic import BaseModel, Field

from broky.backtest.engine import BacktestResult
from broky.backtest.compare import ComparisonResult

logger = logging.getLogger(__name__)


class AnalysisInsight(BaseModel):
    """Structured response from LLM analysis."""
    score: int = Field(ge=0, le=10, description="Overall strategy quality (0=analysis failed, 1=terrible, 10=excellent)")
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    regime_notes: str = ""
    risk_assessment: str = ""
    raw_response: str = ""


SYSTEM_PROMPT = """You are an expert quantitative trading analyst specializing in XAUUSD (gold) strategies.
Analyze the backtest metrics provided and give structured, actionable insights.

Key benchmarks for XAUUSD strategies:
- Win rate > 50% is acceptable; > 60% is strong
- Profit factor > 1.5 is good; > 2.0 is excellent
- Max drawdown < 20% is acceptable; < 10% is excellent
- Sharpe ratio > 1.0 is good; > 2.0 is excellent
- Consecutive losses > 5 suggests poor regime handling

Respond in JSON format with exactly these keys:
{
  "score": <integer 1-10, overall strategy quality>,
  "strengths": [<list of strings, what works well>],
  "weaknesses": [<list of strings, what needs improvement>],
  "suggestions": [<list of strings, actionable recommendations>],
  "regime_notes": "<string, notes on market regime handling>",
  "risk_assessment": "<string, assessment of risk management quality>"
}"""

BACKTEST_USER_PROMPT = """Analyze this XAUUSD backtest result:

{metrics}

Provide a structured analysis with score, strengths, weaknesses, and suggestions."""

COMPARISON_USER_PROMPT = """Analyze this comparison of XAUUSD backtest strategies:

{metrics}

Compare the strategies, identify the best one, and provide structured analysis with score, strengths, weaknesses, and suggestions for the top performer."""


def serialize_backtest_metrics(result: BacktestResult) -> dict:
    """Convert BacktestResult to a LLM-friendly dict (excludes equity curve)."""
    data = {
        "total_trades": result.total_trades,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "win_rate": round(result.win_rate, 4),
        "total_pnl": round(result.total_pnl, 2),
        "total_pnl_pct": round(result.total_pnl_pct, 2),
        "max_drawdown_pct": round(result.max_drawdown_pct, 2),
        "profit_factor": round(result.profit_factor, 4) if result.profit_factor != float("inf") else "inf",
        "sharpe_ratio": round(result.sharpe_ratio, 4),
        "avg_trade_pnl": round(result.avg_trade_pnl, 2),
        "max_consecutive_wins": result.max_consecutive_wins,
        "max_consecutive_losses": result.max_consecutive_losses,
        "liquidated": result.liquidated,
    }

    if result.trades:
        exit_reasons: dict[str, int] = {}
        for t in result.trades:
            reason = t.exit_reason or "unknown"
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        data["exit_reason_distribution"] = exit_reasons

        buy_trades = [t for t in result.trades if t.direction.value == "BUY"]
        sell_trades = [t for t in result.trades if t.direction.value == "SELL"]
        if buy_trades:
            data["buy_win_rate"] = sum(1 for t in buy_trades if t.pnl > 0) / len(buy_trades)
            data["buy_avg_pnl"] = sum(t.pnl for t in buy_trades) / len(buy_trades)
        if sell_trades:
            data["sell_win_rate"] = sum(1 for t in sell_trades if t.pnl > 0) / len(sell_trades)
            data["sell_avg_pnl"] = sum(t.pnl for t in sell_trades) / len(sell_trades)

    return data


def serialize_comparison_metrics(results: list[ComparisonResult]) -> dict:
    """Convert ComparisonResult list to LLM-friendly dict."""
    return {
        "strategies": [
            {
                "name": r.name,
                "total_trades": r.total_trades,
                "win_rate": round(r.win_rate, 4),
                "total_pnl": round(r.total_pnl, 2),
                "total_pnl_pct": round(r.total_pnl_pct, 2),
                "max_drawdown_pct": round(r.max_drawdown_pct, 2),
                "profit_factor": round(r.profit_factor, 4) if r.profit_factor != float("inf") else "inf",
                "sharpe_ratio": round(r.sharpe_ratio, 4),
                "avg_trade_pnl": round(r.avg_trade_pnl, 2),
                "max_consecutive_wins": r.max_consecutive_wins,
                "max_consecutive_losses": r.max_consecutive_losses,
                "liquidated": r.liquidated,
            }
            for r in results
        ]
    }


class LLMAnalyzer:
    """Sends backtest metrics to an LLM via Ollama and returns structured analysis.

    Works with Ollama (default) or any OpenAI-compatible API.

    Args:
        base_url: Ollama API URL. Defaults to OLLAMA_BASE_URL env var or localhost.
        model: Model name. Defaults to OLLAMA_MODEL env var or llama3.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
    ):
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3")
        self.timeout = timeout

    def analyze_backtest(
        self,
        result: BacktestResult,
        strategy_name: str = "unknown",
    ) -> AnalysisInsight:
        """Analyze a single BacktestResult and return structured insights."""
        metrics = serialize_backtest_metrics(result)
        metrics["strategy_name"] = strategy_name
        user_prompt = BACKTEST_USER_PROMPT.format(metrics=json.dumps(metrics, indent=2))
        response_text = self._call_llm(user_prompt)
        return self._parse_response(response_text)

    def analyze_comparison(
        self,
        results: list[ComparisonResult],
    ) -> AnalysisInsight:
        """Analyze a comparison of multiple strategy results."""
        metrics = serialize_comparison_metrics(results)
        user_prompt = COMPARISON_USER_PROMPT.format(metrics=json.dumps(metrics, indent=2))
        response_text = self._call_llm(user_prompt)
        return self._parse_response(response_text)

    def _call_llm(self, user_prompt: str) -> str:
        """Make the API call to Ollama. Returns raw text response."""
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.3,
            },
        }

        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")
        except requests.exceptions.RequestException as e:
            logger.error("LLM API call failed: %s", e)
            return json.dumps({
                "score": 0,
                "strengths": [],
                "weaknesses": [f"LLM analysis failed: {e}"],
                "suggestions": ["Check Ollama is running: ollama serve"],
                "regime_notes": "",
                "risk_assessment": "Unable to assess — LLM call failed",
            })

    def _parse_response(self, raw_text: str) -> AnalysisInsight:
        """Parse LLM response into structured AnalysisInsight."""
        text = raw_text.strip()
        # Strip markdown code blocks if present
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            data = json.loads(text)
            return AnalysisInsight(
                score=int(data.get("score", 0)),
                strengths=data.get("strengths", []),
                weaknesses=data.get("weaknesses", []),
                suggestions=data.get("suggestions", []),
                regime_notes=data.get("regime_notes", ""),
                risk_assessment=data.get("risk_assessment", ""),
                raw_response=raw_text,
            )
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("Failed to parse LLM response as JSON: %s", e)
            return AnalysisInsight(
                score=0,
                strengths=[],
                weaknesses=["LLM response was not valid JSON"],
                suggestions=[],
                regime_notes="",
                risk_assessment="",
                raw_response=raw_text,
            )


def main():
    """CLI entry point for LLM backtest analysis."""
    import argparse
    from pathlib import Path

    import pandas as pd

    from broky.backtest.compare import PRESET_CONFIGS, run_comparison, format_table

    parser = argparse.ArgumentParser(description="LLM-powered backtest analysis")
    parser.add_argument(
        "--configs", nargs="+", default=["conservative", "moderate", "aggressive"],
        choices=list(PRESET_CONFIGS.keys()),
        help="Strategy configs to compare",
    )
    parser.add_argument("--timeframe", default="M5", help="Timeframe (M5, H1, D1)")
    parser.add_argument("--initial-equity", type=float, default=1000.0, help="Starting equity")
    parser.add_argument("--warmup", type=int, default=50, help="Warmup candles")
    parser.add_argument("--analyze", action="store_true", help="Send results to LLM for analysis")
    parser.add_argument("--model", default=None, help="Ollama model name (default: llama3)")
    args = parser.parse_args()

    from broky.data.loader import load_timeframe

    data_dir = Path("data/xau-data")
    if not data_dir.exists():
        print(f"Error: Data directory {data_dir} not found.", file=__import__("sys").stderr)
        __import__("sys").exit(1)

    print(f"Loading {args.timeframe} data...")
    try:
        df = load_timeframe(data_dir, args.timeframe)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=__import__("sys").stderr)
        __import__("sys").exit(1)

    configs = {name: PRESET_CONFIGS[name] for name in args.configs}
    print(f"Loaded {len(df)} candles. Comparing {len(configs)} strategies...\n")

    results = run_comparison(
        df, configs,
        initial_equity=args.initial_equity,
        warmup=args.warmup,
    )

    print(format_table(results))

    if args.analyze:
        print("\n--- LLM Analysis ---")
        analyzer = LLMAnalyzer(model=args.model)
        insight = analyzer.analyze_comparison(results)
        print(f"\nScore: {insight.score}/10")
        print(f"\nStrengths:")
        for s in insight.strengths:
            print(f"  + {s}")
        print(f"\nWeaknesses:")
        for w in insight.weaknesses:
            print(f"  - {w}")
        print(f"\nSuggestions:")
        for s in insight.suggestions:
            print(f"  > {s}")
        print(f"\nRegime Notes: {insight.regime_notes}")
        print(f"Risk Assessment: {insight.risk_assessment}")


if __name__ == "__main__":
    main()