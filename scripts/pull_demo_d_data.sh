#!/bin/bash
# Pull Demo-D trade data into ψ/inbox/ for next session analysis.
# Runs every 6h via crontab on vpsdeluna. Uses live_trades ONLY
# (rejected_signals table is empty — user approved 2026-08-23).
#
# Crontab entry (UTC, every 6h, off-peak minute 7):
#   7 */6 * * * /root/god-port-oracle/scripts/pull_demo_d_data.sh >> /var/log/pull_demo_d.log 2>&1
#
# Output: ψ/inbox/demo-d-snapshot-<TS>/
#   - summary.json   — rollup stats (open/closed/PnL/last_trade)
#   - trades.csv     — last 50 D trades (for trend analysis)
#   - MSG.md         — inbox message instructing next session
#   - query.err      — stderr from docker exec Python (for debug)
#
# Note: Python runs INSIDE the container via docker exec. It prints
# JSON / CSV to stdout, which we redirect to host files. Python never
# writes to host paths directly (it can't see them from the container).
# All SQL strings are single-line to avoid bash/Python quoting issues.

set -uo pipefail

REPO_DIR="${REPO_DIR:-$HOME/god-port-oracle}"
CONTAINER="${CONTAINER:-oracle-engine-train}"
DB_PATH="${DB_PATH:-/app/data/oracle_train.db}"
KEEP_DB_SNAP="${KEEP_DB_SNAP:-0}"

TS=$(date -u +%Y%m%dT%H%M%SZ)
OUTDIR="$REPO_DIR/ψ/inbox/demo-d-snapshot-$TS"
mkdir -p "$OUTDIR"

# ── 1. Summary JSON (Python prints to stdout, we redirect) ─────────
docker exec "$CONTAINER" python3 -c '
import sqlite3, json, sys
db = sqlite3.connect("'"$DB_PATH"'")
db.row_factory = sqlite3.Row

row = db.execute("SELECT id FROM accounts WHERE name='\''D'\'' LIMIT 1").fetchone()
d_id = row["id"] if row is not None else None
summary = {"ts": "'"$TS"'", "account": "D", "account_id": d_id,
           "container": "'"$CONTAINER"'", "db_path": "'"$DB_PATH"'"}

if d_id is not None:
    summary["open_trades"] = db.execute(
        "SELECT COUNT(*) n FROM live_trades WHERE account_id=? AND is_open=1", (d_id,)
    ).fetchone()["n"]

    c24 = db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl FROM live_trades WHERE account_id=? AND is_open=0 AND exit_time > datetime('\''now'\'','\''-24 hours'\'')",
        (d_id,)
    ).fetchone()
    summary["closed_24h"] = c24["n"]
    summary["pnl_24h"] = round(c24["pnl"], 2)

    c7 = db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins, SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) losses FROM live_trades WHERE account_id=? AND is_open=0 AND exit_time > datetime('\''now'\'','\''-7 days'\'')",
        (d_id,)
    ).fetchone()
    summary["closed_7d"] = c7["n"]
    summary["pnl_7d"] = round(c7["pnl"], 2)
    summary["wins_7d"] = c7["wins"]
    summary["losses_7d"] = c7["losses"]
    gp = db.execute(
        "SELECT COALESCE(SUM(pnl),0) v FROM live_trades WHERE account_id=? AND is_open=0 AND pnl>0 AND exit_time>datetime('\''now'\'','\''-7 days'\'')",
        (d_id,)
    ).fetchone()["v"]
    gl = db.execute(
        "SELECT COALESCE(SUM(pnl),0) v FROM live_trades WHERE account_id=? AND is_open=0 AND pnl<0 AND exit_time>datetime('\''now'\'','\''-7 days'\'')",
        (d_id,)
    ).fetchone()["v"]
    summary["pf_7d"] = round(abs(gp/gl), 3) if gl < 0 else None
    summary["wr_7d"] = round(c7["wins"]/c7["n"], 3) if c7["n"] > 0 else None

    cum = db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins, SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) losses FROM live_trades WHERE account_id=? AND is_open=0 AND exit_time > '\''2026-08-23'\''",
        (d_id,)
    ).fetchone()
    summary["closed_cumulative"] = cum["n"]
    summary["pnl_cumulative"] = round(cum["pnl"], 2)
    summary["wins_cumulative"] = cum["wins"]
    summary["losses_cumulative"] = cum["losses"]
    summary["wr_cumulative"] = round(cum["wins"]/cum["n"], 3) if cum["n"] > 0 else None

    last = db.execute(
        "SELECT timestamp, exit_time, is_open, pnl, direction, ml_loss_proba FROM live_trades WHERE account_id=? ORDER BY id DESC LIMIT 1",
        (d_id,)
    ).fetchone()
    if last is not None:
        summary["last_trade"] = {
            "timestamp": last["timestamp"], "exit_time": last["exit_time"],
            "is_open": bool(last["is_open"]), "pnl": last["pnl"],
            "direction": last["direction"], "ml_loss_proba": last["ml_loss_proba"],
        }

    rows = db.execute(
        "SELECT pnl FROM live_trades WHERE account_id=? AND is_open=0 AND exit_time>'\''2026-08-23'\'' ORDER BY id ASC",
        (d_id,)
    ).fetchall()
    max_streak = cur = 0
    for r in rows:
        if r["pnl"] is not None and r["pnl"] < 0:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 0
    summary["max_consecutive_losses_cumulative"] = max_streak

print(json.dumps(summary, indent=2, default=str))
' > "$OUTDIR/summary.json" 2> "$OUTDIR/query.err"

# ── 2. Trades CSV (separate docker exec, stdout → host file) ───────
docker exec "$CONTAINER" python3 -c '
import sqlite3, csv, sys
db = sqlite3.connect("'"$DB_PATH"'")
db.row_factory = sqlite3.Row
row = db.execute("SELECT id FROM accounts WHERE name='\''D'\'' LIMIT 1").fetchone()
if row is None:
    sys.exit(0)
d_id = row["id"]
last50 = db.execute(
    "SELECT id, timestamp, exit_time, direction, symbol, entry_price, exit_price, lot_size, confidence, regime, session, d1_trend, pnl, pnl_pct, exit_reason, is_open, ml_loss_proba, ml_model_used, atr_at_entry, atr_multiplier, rr_ratio, min_confidence_threshold FROM live_trades WHERE account_id=? ORDER BY id DESC LIMIT 50",
    (d_id,)
).fetchall()
if last50:
    w = csv.writer(sys.stdout)
    w.writerow(last50[0].keys())
    for r in last50:
        w.writerow(r)
' > "$OUTDIR/trades.csv" 2>> "$OUTDIR/query.err"

# ── 3. Optional DB snapshot (off by default — large file) ──────────
if [ "$KEEP_DB_SNAP" = "1" ]; then
    docker cp "$CONTAINER:$DB_PATH" "$OUTDIR/oracle_train.db.snap"
fi

# ── 4. Inbox message for next session ──────────────────────────────
SUMMARY_HUMAN=$(python3 -c "
import json
s = json.load(open('$OUTDIR/summary.json'))
print(f\"open={s.get('open_trades',0)} closed24h={s.get('closed_24h',0)} pnl24h={s.get('pnl_24h',0)} \"
      f\"closed7d={s.get('closed_7d',0)} pnl7d={s.get('pnl_7d',0)} pf7d={s.get('pf_7d','n/a')} wr7d={s.get('wr_7d','n/a')} \"
      f\"cumN={s.get('closed_cumulative',0)} cumPnL={s.get('pnl_cumulative',0)} \"
      f\"maxLossStreak={s.get('max_consecutive_losses_cumulative',0)}\")
" 2>/dev/null || echo "(parse failed — see summary.json)")

cat > "$OUTDIR/MSG.md" <<EOF
# Demo-D Slow-but-Sure Snapshot $TS

Pull อัตโนมัติทุก 6 ชม — วิเคราะห์เพื่อ iterate กลยุทธ์

## สรุปผล 6h ล่าสุด

$SUMMARY_HUMAN

Baseline OOS (atr=2.5/rr=3.0 @0.50): PF 1.66, MaxDD 17.3%, 91 trades
Kill switch: MaxDD>20% / 7d no trade / cum PnL<-10% after 14d / consec losses ≥3

## สิ่งที่ต้องทำ (next session)

1. อ่าน summary.json — เทียบ PnL + PF + WR กับ baseline OOS
2. อ่าน trades.csv — ดู exit_reason distribution, ML loss_proba vs actual outcome
3. Decision tree:
   - PF≥1.5 + MaxDD≤15% → คง config ต่อ → เขียน outbox "on track"
   - PF<1.5 + MaxDD≤15% → เพิ่ม threshold 0.05
   - MaxDD>15% → ลด risk_per_trade 0.005
   - 0 trades ใน 48h → ดู candle data + ตรวจ ML threshold ว่า block หมด
   - consecutive losses ≥3 → ตรวจ circuit breaker ทำงานรึเปล่า
4. เขียน ψ/outbox/result_demo-d-$TS.md — สรุป + adjustment (ถ้ามี)
5. อัปเดต ψ/memory/learnings/ ถ้ามีบทเรียนใหม่

## ไฟล์ใน snapshot นี้

- summary.json — rollup สถิติ
- trades.csv — 50 trades ล่าสุด (entry/exit/ML/regime)
- MSG.md — ไฟล์นี้
EOF

echo "[$(date -u +%FT%TZ)] Snapshot written to $OUTDIR"
echo "  $SUMMARY_HUMAN"