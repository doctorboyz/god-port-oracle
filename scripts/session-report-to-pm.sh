#!/bin/bash
# Oracle Session End Report to PM
# Automatically called by Stop hook
DATE=$(date +%Y-%m-%d)
TIME=$(date +%H%M)
ORACLE_ROOT="/Users/doctorboyz/Code/github.com/doctorboyz/god-port-oracle"
ORACLE_NAME="god-port"
PM_INBOX="/Users/doctorboyz/Code/github.com/doctorboyz/pm-oracle/ψ/inbox"
OUTBOX="$ORACLE_ROOT/ψ/outbox"

mkdir -p "$PM_INBOX"

# Collect latest outbox activity
latest_outbox=$(ls -t "$OUTBOX"/*.md 2>/dev/null | head -3 | xargs -I{} basename {} | tr '\n' ',' | sed 's/,$//')

# Write session end report
cat > "$PM_INBOX/${DATE}_${TIME}_${ORACLE_NAME}_MSG-${ORACLE_NAME:0:2}-AUTO-${TIME}.md" << EOF
---
msg_id: MSG-${ORACLE_NAME:0:2}-AUTO-${TIME}
from: $ORACLE_NAME
to: pm
type: info
status: pending
sent: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
ack_by: -
result: -
reply_file: -
---

# $ORACLE_NAME — Session End Report

**Session ended**: $(date "+%Y-%m-%d %H:%M:%S %Z")

## Outbox Activity (latest)
$latest_outbox

## Auto-generated
This message was automatically created by $ORACLE_NAME's Stop hook.
PM: please review and update goals if needed.
EOF

echo "$ORACLE_NAME session report sent to PM inbox"
