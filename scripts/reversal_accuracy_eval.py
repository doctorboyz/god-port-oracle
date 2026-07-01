#!/usr/bin/env python3
"""Reversal-detection tool accuracy evaluation on premium + Exness M5 data.

For each reversal indicator we have in broky/indicators/:
  - Generate signals (OB = expect reversal DOWN, OS = expect reversal UP)
  - Measure precision: when indicator signals, how often does price actually
    reverse within W bars?
  - Measure recall: of actual pivot reversals, how many had a signal in the
    prior K bars?
  - Avg favorable / adverse move after signal
  - Rank by precision, recall, F1, and net favorable move

Ground truth = pivot points:
  - Pivot HIGH: bar[i].high is the max of [i-N, i+N] AND price drops >= X%
    within W bars after i+N
  - Pivot LOW:  bar[i].low  is the min of [i-N, i+N] AND price rises >= X%
    within W bars after i+N

Usage:
  python3 scripts/reversal_accuracy_eval.py
  python3 scripts/reversal_accuracy_eval.py --csv-premium ... --csv-exness ...
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/doctorboyz/Code/github.com/doctorboyz/god-port-oracle")

from broky.indicators.rsi import calculate_rsi
from broky.indicators.stochastic import calculate_stochastic
from broky.indicators.bollinger import calculate_bollinger
from broky.indicators.cci import calculate_cci
from broky.indicators.mfi import calculate_mfi
from broky.indicators.demarker import calculate_demarker
from broky.indicators.williams_r import calculate_williams_r


# ---------- Ground-truth pivots ----------

def find_pivots(df: pd.DataFrame, n: int = 3, x_pct: float = 0.10,
                w: int = 24) -> tuple[list[int], list[int]]:
    """Find pivot highs and lows.

    Pivot HIGH at i: high[i] == max(high[i-n:i+n+1]) AND
      within w bars after i+n, price drops >= x_pct% from high[i].
    Pivot LOW  at i: low[i]  == min(low[i-n:i+n+1])  AND
      within w bars after i+n, price rises >= x_pct% from low[i].
    """
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    N = len(df)
    piv_highs: list[int] = []
    piv_lows: list[int] = []

    for i in range(n, N - n - w):
        window_h = high[i - n:i + n + 1]
        window_l = low[i - n:i + n + 1]
        if high[i] == window_h.max() and np.sum(window_h == high[i]) == 1:
            # subsequent drop >= x_pct
            ref = high[i]
            future = low[i + 1:i + n + w + 1]
            if (ref - future.min()) / ref * 100 >= x_pct:
                piv_highs.append(i)
        if low[i] == window_l.min() and np.sum(window_l == low[i]) == 1:
            ref = low[i]
            future = high[i + 1:i + n + w + 1]
            if (future.max() - ref) / ref * 100 >= x_pct:
                piv_lows.append(i)
    return piv_highs, piv_lows


# ---------- Signal generators ----------

@dataclass
class Signal:
    bar_idx: int
    expected_dir: str  # "UP" (expect price to rise) or "DOWN"


def rsi_signals(close: pd.Series) -> list[Signal]:
    rsi = calculate_rsi(close, 14)
    sigs: list[Signal] = []
    prev = None
    for i, v in enumerate(rsi):
        if pd.isna(v):
            continue
        if prev is not None:
            if prev >= 30 and v < 30:
                sigs.append(Signal(i, "UP"))
            elif prev <= 70 and v > 70:
                sigs.append(Signal(i, "DOWN"))
        prev = v
    return sigs


def stoch_signals(high: pd.Series, low: pd.Series, close: pd.Series) -> list[Signal]:
    res = calculate_stochastic(high, low, close, 14, 3, 3)
    k = res.k_line
    sigs: list[Signal] = []
    prev = None
    for i, v in enumerate(k):
        if pd.isna(v):
            continue
        if prev is not None:
            if prev >= 20 and v < 20:
                sigs.append(Signal(i, "UP"))
            elif prev <= 80 and v > 80:
                sigs.append(Signal(i, "DOWN"))
        prev = v
    return sigs


def stoch_cross_signals(high: pd.Series, low: pd.Series, close: pd.Series) -> list[Signal]:
    """%K crosses %D — bullish cross (K crosses above D) from below 20 → UP;
    bearish cross (K crosses below D) from above 80 → DOWN."""
    res = calculate_stochastic(high, low, close, 14, 3, 3)
    k, d = res.k_line, res.d_line
    sigs: list[Signal] = []
    for i in range(1, len(k)):
        if pd.isna(k.iloc[i]) or pd.isna(d.iloc[i]) or pd.isna(k.iloc[i - 1]):
            continue
        if k.iloc[i - 1] <= d.iloc[i - 1] and k.iloc[i] > d.iloc[i] and k.iloc[i] < 20:
            sigs.append(Signal(i, "UP"))
        elif k.iloc[i - 1] >= d.iloc[i - 1] and k.iloc[i] < d.iloc[i] and k.iloc[i] > 80:
            sigs.append(Signal(i, "DOWN"))
    return sigs


def boll_signals(close: pd.Series) -> list[Signal]:
    """%B ≤ 0.15 (near lower band) → UP; %B ≥ 0.85 (near upper) → DOWN.
    Crossing triggers (cleaner than static threshold)."""
    res = calculate_bollinger(close, 20, 2.0)
    pb = res.percent_b
    sigs: list[Signal] = []
    prev = None
    for i, v in enumerate(pb):
        if pd.isna(v):
            continue
        if prev is not None:
            if prev > 0.15 and v <= 0.15:
                sigs.append(Signal(i, "UP"))
            elif prev < 0.85 and v >= 0.85:
                sigs.append(Signal(i, "DOWN"))
        prev = v
    return sigs


def cci_signals(high: pd.Series, low: pd.Series, close: pd.Series) -> list[Signal]:
    cci = calculate_cci(high, low, close, 20)
    sigs: list[Signal] = []
    prev = None
    for i, v in enumerate(cci):
        if pd.isna(v):
            continue
        if prev is not None:
            if prev >= -100 and v < -100:
                sigs.append(Signal(i, "UP"))
            elif prev <= 100 and v > 100:
                sigs.append(Signal(i, "DOWN"))
        prev = v
    return sigs


def mfi_signals(high: pd.Series, low: pd.Series, close: pd.Series,
                vol: pd.Series) -> list[Signal]:
    mfi = calculate_mfi(high, low, close, vol, 14)
    sigs: list[Signal] = []
    prev = None
    for i, v in enumerate(mfi):
        if pd.isna(v):
            continue
        if prev is not None:
            if prev >= 20 and v < 20:
                sigs.append(Signal(i, "UP"))
            elif prev <= 80 and v > 80:
                sigs.append(Signal(i, "DOWN"))
        prev = v
    return sigs


def demarker_signals(high: pd.Series, low: pd.Series) -> list[Signal]:
    dem = calculate_demarker(high, low, 14)
    sigs: list[Signal] = []
    prev = None
    for i, v in enumerate(dem):
        if pd.isna(v):
            continue
        if prev is not None:
            if prev >= 0.3 and v < 0.3:
                sigs.append(Signal(i, "UP"))
            elif prev <= 0.7 and v > 0.7:
                sigs.append(Signal(i, "DOWN"))
        prev = v
    return sigs


def williams_signals(high: pd.Series, low: pd.Series, close: pd.Series) -> list[Signal]:
    wr = calculate_williams_r(high, low, close, 14)
    sigs: list[Signal] = []
    prev = None
    for i, v in enumerate(wr):
        if pd.isna(v):
            continue
        if prev is not None:
            if prev >= -80 and v < -80:
                sigs.append(Signal(i, "UP"))
            elif prev <= -20 and v > -20:
                sigs.append(Signal(i, "DOWN"))
        prev = v
    return sigs


# ---------- Evaluation ----------

@dataclass
class ToolResult:
    name: str
    n_signals: int = 0
    n_correct: int = 0        # signal predicted dir, price moved that way >= X%
    n_wrong: int = 0          # signal predicted dir, price moved opposite >= X%
    n_neutral: int = 0
    avg_favorable_pct: float = 0.0  # avg move in predicted direction
    avg_adverse_pct: float = 0.0
    recall_hits: int = 0
    recall_total: int = 0
    favorable_move: list = field(default_factory=list)
    adverse_move: list = field(default_factory=list)

    @property
    def precision(self) -> float:
        decided = self.n_correct + self.n_wrong
        return self.n_correct / decided if decided else 0.0

    @property
    def recall(self) -> float:
        return self.recall_hits / self.recall_total if self.recall_total else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def report(self, pivot_count: int) -> str:
        p = self.precision * 100
        r = self.recall * 100
        f1 = self.f1 * 100
        decided = self.n_correct + self.n_wrong
        neutr = self.n_neutral
        fav = self.avg_favorable_pct
        adv = self.avg_adverse_pct
        net = fav - adv
        verdict = "แม่น" if (p >= 55 and net >= 0.10) else ("พอใช้" if p >= 50 else "ไม่แม่น")
        return (f"  {self.name:<28} sig={self.n_signals:>5} "
                f"P={p:>5.1f}% R={r:>5.1f}% F1={f1:>5.1f}% "
                f"fav={fav:>5.2f}% adv={adv:>5.2f}% net={net:>+5.2f}% "
                f"rec={self.recall_hits}/{self.recall_total} | {verdict}")


def evaluate(signals: list[Signal], df: pd.DataFrame,
             piv_highs: list[int], piv_lows: list[int],
             x_pct: float, w: int, k_pre: int) -> ToolResult:
    """For each signal, check if price moved in expected direction by >= x_pct
    within w bars. For recall, check if pivot had a matching-direction signal
    in the k_pre bars before pivot confirmation."""
    res = ToolResult(name="")
    high = df["high"].values
    low = df["low"].values
    N = len(df)

    # Precision + favorable/adverse
    for s in signals:
        end = min(s.bar_idx + w, N - 1)
        if end <= s.bar_idx:
            continue
        res.n_signals += 1
        if s.expected_dir == "UP":
            ref = low[s.bar_idx]
            future_high = high[s.bar_idx + 1:end + 1].max()
            future_low = low[s.bar_idx + 1:end + 1].min()
            fav = (future_high - df["close"].iloc[s.bar_idx]) / df["close"].iloc[s.bar_idx] * 100
            adv = (df["close"].iloc[s.bar_idx] - future_low) / df["close"].iloc[s.bar_idx] * 100
            moved = (future_high - ref) / ref * 100 >= x_pct
        else:
            ref = high[s.bar_idx]
            future_low = low[s.bar_idx + 1:end + 1].min()
            future_high = high[s.bar_idx + 1:end + 1].max()
            fav = (df["close"].iloc[s.bar_idx] - future_low) / df["close"].iloc[s.bar_idx] * 100
            adv = (future_high - df["close"].iloc[s.bar_idx]) / df["close"].iloc[s.bar_idx] * 100
            moved = (ref - future_low) / ref * 100 >= x_pct
        res.favorable_move.append(fav)
        res.adverse_move.append(adv)
        if moved:
            res.n_correct += 1
        else:
            # did it move opposite by x_pct?
            if s.expected_dir == "UP":
                opp = (df["close"].iloc[s.bar_idx] - future_low) / df["close"].iloc[s.bar_idx] * 100
            else:
                opp = (future_high - df["close"].iloc[s.bar_idx]) / df["close"].iloc[s.bar_idx] * 100
            if opp >= x_pct:
                res.n_wrong += 1
            else:
                res.n_neutral += 1

    if res.favorable_move:
        res.avg_favorable_pct = float(np.mean(res.favorable_move))
    if res.adverse_move:
        res.avg_adverse_pct = float(np.mean(res.adverse_move))

    # Recall: pivot highs need DOWN signal in [pivot-k_pre, pivot]
    # pivot lows need UP signal in [pivot-k_pre, pivot]
    sig_by_idx = [(s.bar_idx, s.expected_dir) for s in signals]
    res.recall_total = len(piv_highs) + len(piv_lows)
    for p in piv_highs:
        for bi, d in sig_by_idx:
            if p - k_pre <= bi <= p and d == "DOWN":
                res.recall_hits += 1
                break
    for p in piv_lows:
        for bi, d in sig_by_idx:
            if p - k_pre <= bi <= p and d == "UP":
                res.recall_hits += 1
                break
    return res


def run_dataset(name: str, df: pd.DataFrame, x_pct: float, w: int,
                n_piv: int, k_pre: int) -> dict[str, ToolResult]:
    print(f"\n{'='*78}")
    print(f"=== {name}  bars={len(df)}  range={df.index.min()} -> {df.index.max()}")
    print(f"=== pivots: N={n_piv} on each side, x_pct={x_pct}%, w={w} bars, k_pre={k_pre}")
    print(f"{'='*78}")

    piv_h, piv_l = find_pivots(df, n=n_piv, x_pct=x_pct, w=w)
    print(f"  ground truth: {len(piv_h)} pivot highs, {len(piv_l)} pivot lows "
          f"(total {len(piv_h)+len(piv_l)})")

    close = df["close"]; high = df["high"]; low = df["low"]
    vol = df["tick_volume"] if "tick_volume" in df else df.get("volume", pd.Series(0, index=df.index))

    tools = [
        ("RSI 30/70 cross", rsi_signals(close)),
        ("Stoch %K 20/80 cross", stoch_signals(high, low, close)),
        ("Stoch %K×%D cross", stoch_cross_signals(high, low, close)),
        ("Bollinger %B 0.15/0.85", boll_signals(close)),
        ("CCI ±100 cross", cci_signals(high, low, close)),
        ("MFI 20/80 cross", mfi_signals(high, low, close, vol)),
        ("DeMarker 0.3/0.7", demarker_signals(high, low)),
        ("Williams %R -80/-20", williams_signals(high, low, close)),
    ]

    results: dict[str, ToolResult] = {}
    for tname, sigs in tools:
        r = evaluate(sigs, df, piv_h, piv_l, x_pct, w, k_pre)
        r.name = tname
        results[tname] = r
        print(r.report(len(piv_h) + len(piv_l)))
    return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv-premium", default="/Users/doctorboyz/Documents/xau-data/XAUUSD_M5-2026-04-15-06_40-Premium Data.csv")
    p.add_argument("--csv-exness", default="/tmp/exness_m5_bars.csv")
    p.add_argument("--x-pct", type=float, default=0.10,
                   help="% move defining a reversal")
    p.add_argument("--w", type=int, default=24, help="bars after signal to confirm reversal (24 M5 = 2h)")
    p.add_argument("--n-piv", type=int, default=3, help="bars on each side of pivot")
    p.add_argument("--k-pre", type=int, default=6, help="bars before pivot where signal counts as recall")
    args = p.parse_args()

    # Premium
    print("Loading premium M5...", flush=True)
    dfp = pd.read_csv(args.csv_premium, header=None,
                      names=["timestamp", "open", "high", "low", "close", "volume"])
    dfp["timestamp"] = pd.to_datetime(dfp["timestamp"])
    dfp = dfp.set_index("timestamp").sort_index()
    print(f"  {len(dfp)} bars")

    # Exness
    print("Loading Exness M5...", flush=True)
    dfe = pd.read_csv(args.csv_exness)
    dfe["time"] = pd.to_datetime(dfe["time"])
    dfe = dfe.set_index("time").sort_index()
    # rename for consistency
    if "tick_volume" in dfe.columns:
        dfe = dfe.rename(columns={"tick_volume": "tick_volume"})
    print(f"  {len(dfe)} bars")

    rp = run_dataset("PREMIUM  (2023-2026)", dfp, args.x_pct, args.w, args.n_piv, args.k_pre)
    re_ = run_dataset("EXNESS   (Mar-Jul 2026)", dfe, args.x_pct, args.w, args.n_piv, args.k_pre)

    # Combined ranking
    print(f"\n{'='*78}")
    print("=== COMBINED RANKING (precision × recall = F1, then net favorable)")
    print(f"{'='*78}")
    combined = []
    for tname in rp:
        a, b = rp[tname], re_[tname]
        # pool precision
        corr = a.n_correct + b.n_correct
        wrong = a.n_wrong + b.n_wrong
        prec = corr / (corr + wrong) if (corr + wrong) else 0
        rec_hits = a.recall_hits + b.recall_hits
        rec_tot = a.recall_total + b.recall_total
        rec = rec_hits / rec_tot if rec_tot else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
        net = (a.avg_favorable_pct - a.avg_adverse_pct + b.avg_favorable_pct - b.avg_adverse_pct) / 2
        sigs = a.n_signals + b.n_signals
        combined.append((tname, prec, rec, f1, net, sigs))
    combined.sort(key=lambda x: (x[3], x[4]), reverse=True)
    print(f"  {'tool':<28} {'sigs':>6} {'P':>6} {'R':>6} {'F1':>6} {'net%':>7}  verdict")
    for tname, prec, rec, f1, net, sigs in combined:
        v = "แม่น" if (prec >= 0.55 and net >= 0.10) else ("พอใช้" if prec >= 0.50 else "ไม่แม่น")
        print(f"  {tname:<28} {sigs:>6} {prec*100:>5.1f}% {rec*100:>5.1f}% "
              f"{f1*100:>5.1f}% {net:>+6.2f}%  {v}")


if __name__ == "__main__":
    main()