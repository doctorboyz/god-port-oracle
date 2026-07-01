#!/usr/bin/env python3
"""Trend-aligned reversal detection evaluation.

Ground truth = trend-aligned reversals ONLY:
  - Uptrend (D1 EMA50>EMA200 OR H4 EMA10>EMA50): pullback (down move) that
    bottoms at a Higher Low, then price makes a new Higher High within W bars.
    → BUY entry point = the swing low (HL).
  - Downtrend (D1 EMA50<EMA200 OR H4 EMA10<EMA50): rally (up move) that
    tops at a Lower High, then price makes a new Lower Low within W bars.
    → SELL entry point = the swing high (LH).

Counter-trend reversals (LH in uptrend, HL in downtrend) are IGNORED — we
don't trade them per CLAUDE.md trend-following rule.

Methods compared:
  A: Trend-only baseline — every trend-aligned swing HL/LH is a signal
  B: Single best indicator (Williams %R) + trend filter
  C: Confluence ≥2 indicators firing within 3 bars + trend filter
  D: Swing-HL/LH structure only (no indicator) — pure price action
  E: Swing-HL/LH + 1 indicator confirmation
  F: Swing-HL/LH + confluence ≥2

Also measures reversal depth distribution (move from reversal point to
next HH/LL) for trailing TP sizing.

Usage:
  python3 scripts/trend_aligned_reversal_eval.py
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
from broky.indicators.ema import calculate_ema
from broky.data.resampler import resample_timeframe


# ---------- Trend classification ----------

def classify_trend(d1: pd.DataFrame, h4: pd.DataFrame) -> dict[pd.Timestamp, str]:
    """Per-bar trend: bull/bear/ranging. Use D1 EMA50/200; fall back to H4 EMA10/50.
    Returns dict M5-timestamp -> trend label (forward-filled from D1 close)."""
    d1_ema50 = calculate_ema(d1["close"], 50)
    d1_ema200 = calculate_ema(d1["close"], 200)
    d1_trend = []
    for i in range(len(d1)):
        e50 = d1_ema50.iloc[i]; e200 = d1_ema200.iloc[i]
        if pd.isna(e50) or pd.isna(e200):
            d1_trend.append("ranging")
        elif e50 > e200 * 1.0005:
            d1_trend.append("bull")
        elif e50 < e200 * 0.9995:
            d1_trend.append("bear")
        else:
            d1_trend.append("ranging")
    d1_lab = pd.Series(d1_trend, index=d1.index)

    # Map D1 label to each M5 bar (forward fill by index search)
    def label_at(ts):
        # D1 bar whose close <= ts
        valid = d1_lab.index[d1_lab.index <= ts]
        if len(valid) == 0:
            return "ranging"
        return d1_lab.loc[valid[-1]]
    return label_at


# ---------- Swing pivots + structure ----------

def find_swings(df: pd.DataFrame, n: int = 3) -> tuple[list[int], list[int]]:
    """Fractal pivots: swing high at i if high[i] is max of [i-n, i+n]."""
    high = df["high"].values; low = df["low"].values
    N = len(df); sh: list[int] = []; sl: list[int] = []
    for i in range(n, N - n):
        if high[i] == high[i - n:i + n + 1].max() and np.sum(high[i - n:i + n + 1] == high[i]) == 1:
            sh.append(i)
        if low[i] == low[i - n:i + n + 1].min() and np.sum(low[i - n:i + n + 1] == low[i]) == 1:
            sl.append(i)
    return sh, sl


def label_structure(pivots: list[int], prices: np.ndarray, is_high: bool) -> list[str]:
    """Label each pivot as HH/HL/LH/LL relative to previous pivot of same type.
    is_high=True for swing highs (HH = higher high, LH = lower high).
    is_high=False for swing lows (HL = higher low, LL = lower low)."""
    labels = []
    prev_price = None
    for p in pivots:
        px = prices[p]
        if prev_price is None:
            labels.append("init")
        else:
            if is_high:
                labels.append("HH" if px > prev_price else "LH")
            else:
                labels.append("HL" if px > prev_price else "LL")
        prev_price = px
    return labels


# ---------- Trend-aligned reversal ground truth ----------

@dataclass
class Reversal:
    bar_idx: int          # the swing pivot (entry point)
    direction: str        # "BUY" (uptrend pullback) or "SELL" (downtrend rally)
    structure: str        # "HL" / "LH" — confirmation that trend structure intact
    reversal_size_pct: float  # depth of pullback/rally (from prior opposite pivot)
    resume_move_pct: float    # move from reversal point to next HH/LL (the resumption)


def find_trend_aligned_reversals(
    df: pd.DataFrame, sh: list[int], sl: list[int],
    trend_at, w: int = 36, min_pullback_pct: float = 0.10,
) -> list[Reversal]:
    """Find trend-aligned reversal points.

    BUY reversal (uptrend): swing low where:
      - trend_at(bar) == bull
      - structure = HL (higher low than prev swing low)
      - prior swing high exists (price was rising into this pullback)
      - within w bars after the swing low, price makes a new HH
      - pullback depth (prior swing high → this swing low) >= min_pullback_pct
    SELL reversal (downtrend): swing high where:
      - trend_at(bar) == bear
      - structure = LH
      - within w bars after, price makes a new LL
    """
    high = df["high"].values; low = df["low"].values; close = df["close"].values
    N = len(df)

    sh_prices = high[sh] if sh else np.array([])
    sl_prices = low[sl] if sl else np.array([])
    sh_labels = label_structure(sh, high, is_high=True) if sh else []
    sl_labels = label_structure(sl, low, is_high=False) if sl else []

    reversals: list[Reversal] = []

    # BUY reversals at swing lows labeled HL
    last_high_idx = -1
    si = 0  # swing high pointer
    for k, p in enumerate(sl):
        lbl = sl_labels[k]
        if lbl != "HL":
            continue
        trend = trend_at(df.index[p])
        if trend != "bull":
            continue
        # find most recent swing high before p
        prior_highs = [h for h in sh if h < p]
        if not prior_highs:
            continue
        prior_high = prior_highs[-1]
        pullback_pct = (high[prior_high] - low[p]) / high[prior_high] * 100 if high[prior_high] > 0 else 0
        if pullback_pct < min_pullback_pct:
            continue
        # after p, within w bars, must make HH (price > high[prior_high])
        end = min(p + w, N - 1)
        future_high = high[p + 1:end + 1].max()
        if future_high > high[prior_high]:
            resume_pct = (future_high - close[p]) / close[p] * 100
            reversals.append(Reversal(p, "BUY", "HL", pullback_pct, resume_pct))

    # SELL reversals at swing highs labeled LH
    for k, p in enumerate(sh):
        lbl = sh_labels[k]
        if lbl != "LH":
            continue
        trend = trend_at(df.index[p])
        if trend != "bear":
            continue
        prior_lows = [l for l in sl if l < p]
        if not prior_lows:
            continue
        prior_low = prior_lows[-1]
        rally_pct = (high[p] - low[prior_low]) / low[prior_low] * 100 if low[prior_low] > 0 else 0
        if rally_pct < min_pullback_pct:
            continue
        end = min(p + w, N - 1)
        future_low = low[p + 1:end + 1].min()
        if future_low < low[prior_low]:
            resume_pct = (close[p] - future_low) / close[p] * 100
            reversals.append(Reversal(p, "SELL", "LH", rally_pct, resume_pct))

    return reversals


# ---------- Indicator signals (cross-based) ----------

def all_indicator_signals(df: pd.DataFrame) -> dict[str, list[tuple[int, str]]]:
    """Returns dict indicator_name -> list of (bar_idx, dir) where dir = 'UP'/'DOWN'."""
    close = df["close"]; high = df["high"]; low = df["low"]
    vol = df["tick_volume"] if "tick_volume" in df else df.get("volume", pd.Series(0, index=df.index))
    out: dict[str, list[tuple[int, str]]] = {}

    def cross_series(series: pd.Series, low_thr: float, high_thr: float, name: str):
        sigs = []
        prev = None
        for i, v in enumerate(series):
            if pd.isna(v):
                continue
            if prev is not None:
                if prev >= low_thr and v < low_thr:
                    sigs.append((i, "UP"))
                elif prev <= high_thr and v > high_thr:
                    sigs.append((i, "DOWN"))
            prev = v
        out[name] = sigs

    cross_series(calculate_rsi(close, 14), 30, 70, "RSI")
    st = calculate_stochastic(high, low, close, 14, 3, 3)
    cross_series(st.k_line, 20, 80, "StochK")
    cross_series(calculate_bollinger(close, 20, 2.0).percent_b, 0.15, 0.85, "BollPB")
    cross_series(calculate_cci(high, low, close, 20), -100, 100, "CCI")
    cross_series(calculate_mfi(high, low, close, vol, 14), 20, 80, "MFI")
    cross_series(calculate_demarker(high, low, 14), 0.3, 0.7, "DeMarker")
    cross_series(calculate_williams_r(high, low, close, 14), -80, -20, "WilliamsR")

    # Stoch K×D cross
    k, d = st.k_line, st.d_line
    sigs = []
    for i in range(1, len(k)):
        if pd.isna(k.iloc[i]) or pd.isna(d.iloc[i]) or pd.isna(k.iloc[i - 1]):
            continue
        if k.iloc[i - 1] <= d.iloc[i - 1] and k.iloc[i] > d.iloc[i] and k.iloc[i] < 20:
            sigs.append((i, "UP"))
        elif k.iloc[i - 1] >= d.iloc[i - 1] and k.iloc[i] < d.iloc[i] and k.iloc[i] > 80:
            sigs.append((i, "DOWN"))
    out["StochCross"] = sigs

    return out


# ---------- Methods ----------

@dataclass
class MethodResult:
    name: str
    n_signals: int = 0
    n_correct: int = 0
    n_wrong: int = 0
    favorable: list = field(default_factory=list)
    adverse: list = field(default_factory=list)
    recall_hits: int = 0
    recall_total: int = 0

    @property
    def precision(self) -> float:
        d = self.n_correct + self.n_wrong
        return self.n_correct / d if d else 0.0

    @property
    def recall(self) -> float:
        return self.recall_hits / self.recall_total if self.recall_total else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def report(self) -> str:
        p = self.precision * 100; r = self.recall * 100; f1 = self.f1 * 100
        fav = np.mean(self.favorable) if self.favorable else 0.0
        adv = np.mean(self.adverse) if self.adverse else 0.0
        net = fav - adv
        v = "แม่น" if (p >= 65 and net >= 0.20) else ("พอใช้" if p >= 55 else "ไม่แม่น")
        return (f"  {self.name:<38} sig={self.n_signals:>5} "
                f"P={p:>5.1f}% R={r:>5.1f}% F1={f1:>5.1f}% "
                f"fav={fav:>5.2f}% adv={adv:>5.2f}% net={net:>+5.2f}% "
                f"rec={self.recall_hits}/{self.recall_total} | {v}")


def signal_outcome(sig_idx: int, direction: str, df: pd.DataFrame,
                   w: int, x_pct: float) -> tuple[bool, float, float]:
    """For a signal at bar sig_idx predicting direction UP/DOWN, check within w bars:
       - did price move in predicted dir by >= x_pct? (correct)
       - what was favorable% (max move in predicted dir from close[sig])?
       - what was adverse% (max move against)?
    """
    high = df["high"].values; low = df["low"].values; close = df["close"].values
    N = len(df)
    end = min(sig_idx + w, N - 1)
    if end <= sig_idx:
        return False, 0.0, 0.0
    c0 = close[sig_idx]
    fh = high[sig_idx + 1:end + 1].max()
    fl = low[sig_idx + 1:end + 1].min()
    if direction == "UP":
        fav = (fh - c0) / c0 * 100
        adv = (c0 - fl) / c0 * 100
        moved = (fh - c0) / c0 * 100 >= x_pct
        opp = (c0 - fl) / c0 * 100 >= x_pct
    else:
        fav = (c0 - fl) / c0 * 100
        adv = (fh - c0) / c0 * 100
        moved = (c0 - fl) / c0 * 100 >= x_pct
        opp = (fh - c0) / c0 * 100 >= x_pct
    correct = moved
    wrong = (not moved) and opp
    return correct, fav, adv


def evaluate_method(name: str, signals: list[tuple[int, str]],
                    df: pd.DataFrame, reversals: list[Reversal],
                    w: int, x_pct: float, k_pre: int) -> MethodResult:
    res = MethodResult(name=name)
    for idx, d in signals:
        ok, fav, adv = signal_outcome(idx, d, df, w, x_pct)
        res.n_signals += 1
        res.favorable.append(fav)
        res.adverse.append(adv)
        if ok:
            res.n_correct += 1
        elif (fav - adv) < -x_pct:
            res.n_wrong += 1

    # Recall via two-pointer: for each reversal, find same-direction signal in
    # [bar_idx - k_pre, bar_idx]. signals sorted by idx.
    rev_dir = {"BUY": "UP", "SELL": "DOWN"}
    sigs_sorted = sorted(signals, key=lambda x: x[0])
    sig_idx_arr = [s[0] for s in sigs_sorted]
    sig_dir_arr = [s[1] for s in sigs_sorted]
    import bisect
    for rev in reversals:
        lo = rev.bar_idx - k_pre
        hi = rev.bar_idx
        want = rev_dir[rev.direction]
        l = bisect.bisect_left(sig_idx_arr, lo)
        r = bisect.bisect_right(sig_idx_arr, hi)
        for j in range(l, r):
            if sig_dir_arr[j] == want:
                res.recall_hits += 1
                break
    res.recall_total = len(reversals)
    return res


def confluence_signals(all_sigs: dict[str, list[tuple[int, str]]],
                       min_n: int, window: int) -> list[tuple[int, str]]:
    """A confluence signal fires at bar i if ≥min_n distinct indicators fire same
    direction within [i-window, i]. O(n log n) with sliding window per direction."""
    # Group signals by direction; sort by idx
    by_dir: dict[str, list[tuple[int, str]]] = {"UP": [], "DOWN": []}
    for name, sigs in all_sigs.items():
        for idx, d in sigs:
            by_dir[d].append((idx, name))
    for d in by_dir:
        by_dir[d].sort()

    out: list[tuple[int, str]] = []
    for d, items in by_dir.items():
        # Two-pointer sliding window over sorted items
        left = 0
        # We iterate every bar idx where a signal occurs; check window [idx-window, idx]
        # Use deque-like pointer since items sorted
        for right in range(len(items)):
            idx_r = items[right][0]
            while items[left][0] < idx_r - window:
                left += 1
            window_items = items[left:right + 1]
            names = {nm for (_, nm) in window_items}
            if len(names) >= min_n:
                if not out or out[-1][0] != idx_r or out[-1][1] != d:
                    out.append((idx_r, d))
    out.sort()
    return out


def swing_structure_signals(df: pd.DataFrame, sh: list[int], sl: list[int],
                            sh_labels: list[str], sl_labels: list[str],
                            trend_at) -> list[tuple[int, str]]:
    """Pure price-action: BUY at swing low labeled HL in bull trend; SELL at swing
    high labeled LH in bear trend. Signal fires at bar p+n (pivot confirmed n bars later)."""
    n = 3  # confirmation delay (matches swing detection)
    out = []
    for k, p in enumerate(sl):
        if sl_labels[k] != "HL":
            continue
        if trend_at(df.index[p]) != "bull":
            continue
        out.append((p + n, "UP"))
    for k, p in enumerate(sh):
        if sh_labels[k] != "LH":
            continue
        if trend_at(df.index[p]) != "bear":
            continue
        out.append((p + n, "DOWN"))
    return out


def filter_signals_by_trend(signals: list[tuple[int, str]], df, trend_at) -> list[tuple[int, str]]:
    """Keep only trend-aligned: UP signal in bull, DOWN signal in bear."""
    out = []
    for idx, d in signals:
        t = trend_at(df.index[idx])
        if d == "UP" and t == "bull":
            out.append((idx, d))
        elif d == "DOWN" and t == "bear":
            out.append((idx, d))
    return out


# ---------- Main ----------

def run_dataset(name: str, df: pd.DataFrame, w: int, x_pct: float,
                k_pre: int, min_pullback: float) -> None:
    print(f"\n{'='*82}")
    print(f"=== {name}  bars={len(df)}  range={df.index.min()} -> {df.index.max()}")
    print(f"=== w={w} bars  x_pct={x_pct}%  k_pre={k_pre}  min_pullback={min_pullback}%")
    print(f"{'='*82}")

    print("Resampling M5 -> H4 / D1...", flush=True)
    h4 = resample_timeframe(df, "H4")
    d1 = resample_timeframe(df, "D1")
    print(f"  H4 bars={len(h4)}  D1 bars={len(d1)}")

    print("Classifying trend (D1 EMA50/200)...", flush=True)
    trend_at = classify_trend(d1, h4)

    print("Finding swing pivots (N=3)...", flush=True)
    sh, sl = find_swings(df, n=3)
    print(f"  swing highs={len(sh)}  swing lows={len(sl)}")
    sh_labels = label_structure(sh, df["high"].values, is_high=True)
    sl_labels = label_structure(sl, df["low"].values, is_high=False)
    n_hh = sh_labels.count("HH"); n_lh = sh_labels.count("LH")
    n_hl = sl_labels.count("HL"); n_ll = sl_labels.count("LL")
    print(f"  HH={n_hh} LH={n_lh}  HL={n_hl} LL={n_ll}")

    print("Finding trend-aligned reversals...", flush=True)
    revs = find_trend_aligned_reversals(df, sh, sl, trend_at, w=w, min_pullback_pct=min_pullback)
    buys = [r for r in revs if r.direction == "BUY"]
    sells = [r for r in revs if r.direction == "SELL"]
    print(f"  BUY reversals (HL in uptrend → HH): {len(buys)}")
    print(f"  SELL reversals (LH in downtrend → LL): {len(sells)}")
    if not revs:
        print("  ⚠️ No trend-aligned reversals found. Skipping.")
        return

    # Reversal depth distribution
    depths = [r.reversal_size_pct for r in revs]
    resumes = [r.resume_move_pct for r in revs]
    print(f"\n  Reversal depth (pullback/rally size):")
    print(f"    p10={np.percentile(depths,10):.2f}%  p50={np.percentile(depths,50):.2f}%  "
          f"p90={np.percentile(depths,90):.2f}%  mean={np.mean(depths):.2f}%")
    print(f"  Resume move (reversal point → next HH/LL):")
    print(f"    p10={np.percentile(resumes,10):.2f}%  p50={np.percentile(resumes,50):.2f}%  "
          f"p90={np.percentile(resumes,90):.2f}%  mean={np.mean(resumes):.2f}%")

    # Indicator signals
    print("\nComputing indicator signals...", flush=True)
    ind = all_indicator_signals(df)
    for nm, sigs in ind.items():
        print(f"  {nm}: {len(sigs)} signals")

    # Build methods
    methods: list[tuple[str, list[tuple[int, str]]]] = []

    # A: Trend-only baseline — every trend-aligned swing HL/LH (with confirmation delay)
    sigs_a = swing_structure_signals(df, sh, sl, sh_labels, sl_labels, trend_at)
    methods.append(("A: Trend-only (swing HL/LH)", sigs_a))

    # B: Williams %R + trend filter
    sigs_b = filter_signals_by_trend(ind["WilliamsR"], df, trend_at)
    methods.append(("B: Williams %R + trend", sigs_b))

    # C: Confluence ≥2 + trend filter
    conf2 = confluence_signals(ind, min_n=2, window=3)
    sigs_c = filter_signals_by_trend(conf2, df, trend_at)
    methods.append(("C: Confluence ≥2 + trend", sigs_c))

    # C3: Confluence ≥3 + trend filter
    conf3 = confluence_signals(ind, min_n=3, window=3)
    sigs_c3 = filter_signals_by_trend(conf3, df, trend_at)
    methods.append(("C3: Confluence ≥3 + trend", sigs_c3))

    # D: Swing structure only (same as A but separated for clarity)
    methods.append(("D: Swing HL/LH structure", sigs_a))

    # E: Swing HL/LH + 1 indicator within 3 bars
    sigs_e = []
    pa_signals = sigs_a
    for idx, d in pa_signals:
        # any indicator firing same direction in [idx-3, idx]
        match = False
        for nm, sigs in ind.items():
            for j, dd in sigs:
                if dd == d and idx - 3 <= j <= idx:
                    match = True; break
            if match: break
        if match:
            sigs_e.append((idx, d))
    methods.append(("E: Swing + 1 indicator", sigs_e))

    # F: Swing HL/LH + confluence ≥2 within 3 bars
    sigs_f = []
    for idx, d in pa_signals:
        recent_names = set()
        for nm, sigs in ind.items():
            for j, dd in sigs:
                if dd == d and idx - 3 <= j <= idx:
                    recent_names.add(nm); break
        if len(recent_names) >= 2:
            sigs_f.append((idx, d))
    methods.append(("F: Swing + confluence ≥2", sigs_f))

    # G: Swing HL/LH + STRICT D1+H4 alignment (require H4 same as D1)
    def trend_strict_at(ts):
        # both D1 and H4 must agree
        d1_lab = trend_at(ts)
        # H4 trend at ts
        h4_ema10 = calculate_ema(h4["close"], 10)
        h4_ema50 = calculate_ema(h4["close"], 50)
        valid = h4.index[h4.index <= ts]
        if len(valid) == 0:
            return "ranging"
        i = h4.index.get_loc(valid[-1])
        e10 = h4_ema10.iloc[i]; e50 = h4_ema50.iloc[i]
        if pd.isna(e10) or pd.isna(e50):
            h4_lab = "ranging"
        elif e10 > e50 * 1.0005:
            h4_lab = "bull"
        elif e10 < e50 * 0.9995:
            h4_lab = "bear"
        else:
            h4_lab = "ranging"
        if d1_lab == h4_lab:
            return d1_lab
        return "ranging"
    sigs_g = []
    for idx, d in pa_signals:
        t = trend_strict_at(df.index[idx])
        if d == "UP" and t == "bull":
            sigs_g.append((idx, d))
        elif d == "DOWN" and t == "bear":
            sigs_g.append((idx, d))
    methods.append(("G: Swing + D1&H4 strict", sigs_g))

    # H: Swing HL/LH + deeper pullback (≥0.30%)
    sigs_h = []
    pa_set = {(idx, d) for idx, d in pa_signals}
    for rev in revs:
        if rev.reversal_size_pct >= 0.30:
            sig_h_entry = (rev.bar_idx + 3, "UP" if rev.direction == "BUY" else "DOWN")
            if sig_h_entry in pa_set:
                sigs_h.append(sig_h_entry)
    methods.append(("H: Swing + pullback ≥0.30%", sigs_h))

    # H2/H3: deeper pullback thresholds
    for thr, label in [(0.40, "H2: Swing + pullback ≥0.40%"),
                        (0.50, "H3: Swing + pullback ≥0.50%")]:
        sigs_hx = []
        for rev in revs:
            if rev.reversal_size_pct >= thr:
                entry = (rev.bar_idx + 3, "UP" if rev.direction == "BUY" else "DOWN")
                if entry in pa_set:
                    sigs_hx.append(entry)
        methods.append((label, sigs_hx))

    # I: Method H + Williams %R confirmation (confluence of structure + indicator)
    sigs_i = []
    wr_sigs = ind["WilliamsR"]
    for idx, d in sigs_h:
        for j, dd in wr_sigs:
            if dd == d and idx - 6 <= j <= idx:
                sigs_i.append((idx, d))
                break
    methods.append(("I: H + Williams %R confirm", sigs_i))

    # Evaluate
    print(f"\n--- METHOD RESULTS (x_pct={x_pct}%, w={w} bars, k_pre={k_pre}) ---")
    results = []
    for mname, sigs in methods:
        r = evaluate_method(mname, sigs, df, revs, w, x_pct, k_pre)
        results.append(r)
        print(r.report())

    return results, revs, ind, df, trend_at


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv-premium", default="/Users/doctorboyz/Documents/xau-data/XAUUSD_M5-2026-04-15-06_40-Premium Data.csv")
    p.add_argument("--csv-exness", default="/tmp/exness_m5_bars.csv")
    p.add_argument("--w", type=int, default=36, help="bars after signal to confirm (36 M5 = 3h)")
    p.add_argument("--x-pct", type=float, default=0.20, help="% move defining successful reversal resumption")
    p.add_argument("--k-pre", type=int, default=6, help="bars before reversal where signal counts as recall")
    p.add_argument("--min-pullback", type=float, default=0.10, help="min pullback/rally size to count as reversal")
    args = p.parse_args()

    print("Loading premium M5...", flush=True)
    dfp = pd.read_csv(args.csv_premium, header=None,
                      names=["timestamp", "open", "high", "low", "close", "volume"])
    dfp["timestamp"] = pd.to_datetime(dfp["timestamp"])
    dfp = dfp.set_index("timestamp").sort_index()

    print("Loading Exness M5...", flush=True)
    dfe = pd.read_csv(args.csv_exness)
    dfe["time"] = pd.to_datetime(dfe["time"])
    dfe = dfe.set_index("time").sort_index()

    rp = run_dataset("PREMIUM (2023-2026)", dfp, args.w, args.x_pct, args.k_pre, args.min_pullback)
    re_ = run_dataset("EXNESS  (Mar-Jul 2026)", dfe, args.w, args.x_pct, args.k_pre, args.min_pullback)

    if rp and re_:
        rp_res, rp_revs, _, _, _ = rp
        re_res, re_revs, _, _, _ = re_
        print(f"\n{'='*82}")
        print("=== COMBINED RANKING (pooled precision × recall = F1, then net fav) ===")
        print(f"{'='*82}")
        combined = []
        for (a, b) in zip(rp_res, re_res):
            corr = a.n_correct + b.n_correct
            wrong = a.n_wrong + b.n_wrong
            prec = corr / (corr + wrong) if (corr + wrong) else 0
            rec_h = a.recall_hits + b.recall_hits
            rec_t = a.recall_total + b.recall_total
            rec = rec_h / rec_t if rec_t else 0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
            fav = (np.mean(a.favorable) + np.mean(b.favorable)) / 2 if (a.favorable and b.favorable) else 0
            adv = (np.mean(a.adverse) + np.mean(b.adverse)) / 2 if (a.adverse and b.adverse) else 0
            net = fav - adv
            sigs = a.n_signals + b.n_signals
            combined.append((a.name, prec, rec, f1, net, sigs))
        combined.sort(key=lambda x: (x[3], x[4]), reverse=True)
        print(f"  {'method':<38} {'sigs':>6} {'P':>6} {'R':>6} {'F1':>6} {'net%':>7}  verdict")
        for nm, prec, rec, f1, net, sigs in combined:
            v = "แม่น" if (prec >= 0.65 and net >= 0.20) else ("พอใช้" if prec >= 0.55 else "ไม่แม่น")
            print(f"  {nm:<38} {sigs:>6} {prec*100:>5.1f}% {rec*100:>5.1f}% "
                  f"{f1*100:>5.1f}% {net:>+6.2f}%  {v}")


if __name__ == "__main__":
    main()