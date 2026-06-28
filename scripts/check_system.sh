#!/bin/bash
# God Port System Check
# Usage: ./scripts/check_system.sh [performance|availability]
#   performance  — check trading performance from DB (no market check needed)
#   availability — check signal/connection/trade ability (checks market open first)
#   (default)    — run both checks, skip availability if market is closed
set -euo pipefail

VPS="vpsdeluna"
COMPOSE="docker compose -f docker-compose.vps.yml"

# ─── Colors ─────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ─── Market Status Check ───────────────────────────────────────────────
check_market() {
    local now_info=$(ssh "$VPS" "date -u '+%u %H %M'")
    local dow=$(echo "$now_info" | awk '{print $1}')  # 1=Mon..7=Sun
    local hour=$(echo "$now_info" | awk '{print $2}')
    local min=$(echo "$now_info" | awk '{print $3}')

    # XAUUSD: Sun 23:05 UTC → Fri 22:58 UTC
    local market_open=false
    local reason=""

    if [ "$dow" -eq 6 ]; then
        market_open=false
        reason="Saturday — market closed all day"
    elif [ "$dow" -eq 7 ]; then
        if [ "$hour" -gt 23 ] || ([ "$hour" -eq 23 ] && [ "$min" -ge 5 ]); then
            market_open=true
            reason="Sunday evening — market just opened"
        else
            market_open=false
            reason="Sunday before 23:05 UTC — market not yet open"
        fi
    elif [ "$dow" -eq 5 ]; then
        if [ "$hour" -lt 22 ] || ([ "$hour" -eq 22 ] && [ "$min" -lt 58 ]); then
            market_open=true
            reason="Friday — market still open"
        else
            market_open=false
            reason="Friday after 22:58 UTC — market just closed"
        fi
    else
        market_open=true
        reason="Weekday — market open"
    fi

    if [ "$market_open" = true ]; then
        echo -e "${GREEN}✅ MARKET OPEN${NC} — XAUUSD trading (dow=$dow, $reason)"
        return 0
    else
        local next_open=""
        if [ "$dow" -eq 6 ]; then
            next_open="Sunday 23:05 UTC → Monday 06:05 Bangkok"
        elif [ "$dow" -eq 7 ]; then
            next_open="Today 23:05 UTC → Monday 06:05 Bangkok"
        elif [ "$dow" -eq 5 ]; then
            next_open="Sunday 23:05 UTC → Monday 06:05 Bangkok"
        else
            next_open="soon (within trading hours)"
        fi
        echo -e "${RED}🔴 MARKET CLOSED${NC} — XAUUSD not trading ($reason)"
        echo -e "   Next open: $next_open"
        return 1
    fi
}

# ─── Performance Check (DB-based, no market needed) ─────────────────────
check_performance() {
    echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  PERFORMANCE CHECK${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"
    echo ""

    ssh "$VPS" "cd /opt/god-port-oracle && $COMPOSE exec -T oracle-engine python3" << 'PYEOF'
import sqlite3
from datetime import datetime, timezone, timedelta

conn = sqlite3.connect('/app/data/oracle.db')
c = conn.cursor()
now = datetime.now(timezone.utc)

# ─── Account Summary ───
print('📊 Account Summary')
print('─' * 50)
c.execute('SELECT account_id, COUNT(*) as total, SUM(CASE WHEN is_open=1 THEN 1 ELSE 0 END) as open_trades FROM live_trades GROUP BY account_id ORDER BY account_id')
for row in c.fetchall():
    aid = {1: 'A (Real)', 2: 'B (Demo)', 3: 'C (Demo)', 4: 'D (Demo)'}.get(row[0], f'#{row[0]}')
    print(f'  Account {aid}: {row[1]} total, {row[2]} open')

# ─── This Week (Account A) ───
week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
c.execute('''
    SELECT COUNT(*),
           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END),
           COALESCE(SUM(pnl), 0)
    FROM live_trades
    WHERE account_id=1 AND is_open=0 AND exit_time >= ?
''', (week_start.isoformat(),))
row = c.fetchone()
if row and row[0] > 0:
    total, wins, losses, breakeven, pnl = row
    wr = (wins/total*100) if total > 0 else 0
    print(f'\n📅 This Week (since {week_start.strftime("%m/%d")}):')
    print(f'  All trades: {total} | W: {wins} | L: {losses} | BE: {breakeven} | Net PnL: ${pnl:.2f}')
    print(f'  Win Rate: {wr:.1f}%')
else:
    print(f'\n📅 This Week: No closed trades')

# ─── Last 7 Days by Exit Reason ───
seven_days = (now - timedelta(days=7)).isoformat()
c.execute('''
    SELECT exit_reason, COUNT(*), COALESCE(SUM(pnl), 0),
           COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0),
           COALESCE(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0)
    FROM live_trades
    WHERE account_id=1 AND is_open=0 AND exit_time >= ?
    GROUP BY exit_reason
    ORDER BY COUNT(*) DESC
''', (seven_days,))
rows = c.fetchall()
if rows:
    print(f'\n📈 Last 7 Days by Exit Reason:')
    for r in rows:
        reason = (r[0] or 'unknown')[:30]
        print(f'  {reason:<30} count={r[1]:>3}  pnl=${r[2]:>9.2f}  wins=${r[3]:>9.2f}  losses=${r[4]:>9.2f}')

# ─── Real Trades (excl. ghost/mt5_inferred) ───
c.execute('''
    SELECT COUNT(*),
           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END),
           COALESCE(SUM(pnl), 0)
    FROM live_trades
    WHERE account_id=1 AND is_open=0
      AND exit_reason NOT IN ('closed_by_mt5_inferred', 'ghost_no_mt5_ticket_inferred')
      AND exit_time >= ?
''', (seven_days,))
row = c.fetchone()
if row and row[0] > 0:
    wr = (row[1]/row[0]*100) if row[0] > 0 else 0
    print(f'\n💰 Real Trades (excl. ghost/mt5) — Last 7 Days:')
    print(f'  Trades: {row[0]} | Wins: {row[1]} | Losses: {row[2]} | Net PnL: ${row[3]:.2f}')
    print(f'  Win Rate: {wr:.1f}%')
else:
    print(f'\n💰 Real Trades: None in last 7 days (only ghost/mt5_inferred)')

# ─── Weekly Performance (4 weeks, real trades only) ───
print(f'\n📅 Weekly Performance (Real Trades Only)')
print('─' * 50)
for weeks_ago in range(4):
    week_end = (now - timedelta(weeks=weeks_ago))
    week_start_w = (week_end - timedelta(weeks=1)).isoformat()
    week_end_str = week_end.isoformat()

    c.execute('''
        SELECT COUNT(*),
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END),
               COALESCE(SUM(pnl), 0),
               COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0)
        FROM live_trades
        WHERE account_id=1 AND is_open=0
          AND exit_reason NOT IN ('closed_by_mt5_inferred', 'ghost_no_mt5_ticket_inferred')
          AND exit_time >= ? AND exit_time < ?
    ''', (week_start_w, week_end_str))
    row = c.fetchone()
    label = 'This week' if weeks_ago == 0 else f'{weeks_ago}w ago'
    if row and row[0] > 0:
        total, wins, losses, pnl, gross_win, gross_loss = row
        wr = (wins/total*100) if total > 0 else 0
        pf = abs(gross_win/gross_loss) if gross_loss and gross_loss != 0 else float('inf')
        pf_str = f'{pf:.2f}' if pf != float('inf') else 'inf'
        print(f'  {label:>8}: {total:>3}t | WR={wr:>5.1f}% | PnL=${pnl:>8.2f} | PF={pf_str}')
    else:
        print(f'  {label:>8}: No real trades')

# ─── All-Time Summary ───
c.execute('''
    SELECT COUNT(*),
           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
           SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END),
           COALESCE(SUM(pnl), 0),
           COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0),
           COALESCE(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0),
           COALESCE(MIN(pnl), 0),
           COALESCE(MAX(pnl), 0)
    FROM live_trades
    WHERE account_id=1 AND is_open=0
      AND exit_reason NOT IN ('closed_by_mt5_inferred', 'ghost_no_mt5_ticket_inferred')
''')
row = c.fetchone()
if row and row[0] > 0:
    total, wins, losses, pnl, gross_win, gross_loss, worst, best = row
    wr = (wins/total*100) if total > 0 else 0
    pf = abs(gross_win/gross_loss) if gross_loss and gross_loss != 0 else float('inf')
    pf_str = f'{pf:.2f}' if pf != float('inf') else 'inf'
    print(f'\n🏆 All-Time (Real Trades Only):')
    print(f'  {total} trades | WR={wr:.1f}% | PF={pf_str}')
    print(f'  Net PnL: ${pnl:.2f} | Best: ${best:.2f} | Worst: ${worst:.2f}')
    print(f'  Gross Win: ${gross_win:.2f} | Gross Loss: ${gross_loss:.2f}')

# ─── Open Positions ───
c.execute('SELECT COUNT(*) FROM live_trades WHERE account_id=1 AND is_open=1')
open_count = c.fetchone()[0]
print(f'\n🔓 Open Positions: {open_count}')

# ─── Ghost/MT5 Trades Summary ───
c.execute('''
    SELECT exit_reason, COUNT(*), MIN(timestamp), MAX(timestamp)
    FROM live_trades
    WHERE account_id=1 AND is_open=0
      AND exit_reason IN ('closed_by_mt5_inferred', 'ghost_no_mt5_ticket_inferred')
    GROUP BY exit_reason
''')
rows = c.fetchall()
if rows:
    print(f'\n⚠️  Ghost/MT5-Inferred Trades:')
    for r in rows:
        reason = r[0] or 'unknown'
        print(f'  {reason}: {r[1]} trades ({r[2][:10]} to {r[3][:10]})')

conn.close()
PYEOF
}

# ─── Availability Check (checks market first) ─────────────────────────
check_availability() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  AVAILABILITY CHECK${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════${NC}"
    echo ""

    # ─── Market Check (MUST be open for availability) ───
    echo "🔍 Market Status"
    echo "────────────────────────────────────────────────────"
    if ! check_market; then
        echo ""
        echo -e "${YELLOW}⚠️  Market CLOSED — cannot verify signal/trade availability${NC}"
        echo "   Re-run 'check_system.sh availability' when XAUUSD market is open"
        echo ""
        echo "   Skipping: MT5 connection, signal freshness, drawdown, position capacity"
        return 1
    fi

    echo ""

    # ─── MT5 Connection ───
    echo "🔌 MT5 Connection"
    echo "────────────────────────────────────────────────────"
    mt5_status=$(ssh "$VPS" "cd /opt/god-port-oracle && $COMPOSE logs oracle-engine --since 5m 2>&1 | grep -iE 're-login successful|MT5.*logged in' | tail -3")
    if [ -n "$mt5_status" ]; then
        echo -e "${GREEN}✅ MT5 connected${NC}"
        echo "$mt5_status" | head -2
    else
        echo -e "${RED}🔴 MT5 NOT connected — check mt5a container${NC}"
    fi

    # ─── Signal Freshness ───
    echo ""
    echo "📡 Signal Freshness (last 5 min)"
    echo "────────────────────────────────────────────────────"
    ssh "$VPS" "cd /opt/god-port-oracle && $COMPOSE logs oracle-engine --since 5m 2>&1 | grep -iE 'LIVE:.*action|skip|signal' | tail -5"

    # ─── Drawdown Status ───
    echo ""
    echo "🛡️  Drawdown Status (last 30 min)"
    echo "────────────────────────────────────────────────────"
    dd_status=$(ssh "$VPS" "cd /opt/god-port-oracle && $COMPOSE logs oracle-engine --since 30m 2>&1 | grep -iE 'BLOCKED|drawdown|Unblocked|Initialized.*equity' | tail -5")
    if [ -n "$dd_status" ]; then
        echo "$dd_status"
    else
        echo -e "${GREEN}✅ No drawdown blocks${NC}"
    fi

    # ─── Dynamic Max Positions ───
    echo ""
    echo "📊 Position Capacity (last 30 min)"
    echo "────────────────────────────────────────────────────"
    pos_info=$(ssh "$VPS" "cd /opt/god-port-oracle && $COMPOSE logs oracle-engine --since 30m 2>&1 | grep -iE 'Dynamic max_positions|position limit|equity=' | tail -5")
    if [ -n "$pos_info" ]; then
        echo "$pos_info"
    else
        echo "ℹ️  No position limit messages (market may be idle)"
    fi

    # ─── Bridge Health ───
    echo ""
    echo "🌉 Bridge Health (last 5 min)"
    echo "────────────────────────────────────────────────────"
    bridge_info=$(ssh "$VPS" "cd /opt/god-port-oracle && $COMPOSE logs oracle-engine --since 5m 2>&1 | grep -iE 'bridge|Resolved symbol|Connected to MT5' | tail -3")
    if [ -n "$bridge_info" ]; then
        echo "$bridge_info"
    else
        echo "ℹ️  No bridge activity (may be idle)"
    fi

    # ─── Equity ───
    echo ""
    echo "💰 Current Equity"
    echo "────────────────────────────────────────────────────"
    equity=$(ssh "$VPS" "cd /opt/god-port-oracle && $COMPOSE logs oracle-engine --since 10m 2>&1 | grep -iE 'balance=|equity=' | tail -1")
    if [ -n "$equity" ]; then
        echo "$equity"
    else
        echo "ℹ️  No equity data in recent logs"
    fi

    echo ""
    echo -e "${GREEN}✅ Availability check complete${NC}"
}

# ─── Main ───────────────────────────────────────────────────────────────
MODE="${1:-both}"

echo -e "${CYAN}🔍 God Port System Check${NC}"
echo -e "📅 $(ssh "$VPS" "date -u '+%Y-%m-%d %H:%M UTC (%A)'")"
echo ""

case "$MODE" in
    performance|perf|p)
        check_performance
        ;;
    availability|avail|a|signal|trade|status|s)
        check_availability
        ;;
    both|all|"")
        check_performance
        echo ""
        check_availability
        ;;
    *)
        echo "Usage: $0 [performance|availability|both]"
        echo ""
        echo "  performance  — check trading performance from DB (no market check)"
        echo "  availability — check signal/connection/trade ability (checks market open first)"
        echo "  both         — run both checks (default)"
        exit 1
        ;;
esac