# God Port Trading — Security

## Secret Management

**NEVER commit these files:**
- `.env` — MT5 credentials, API keys, Telegram bot token
- `credentials.json` — any broker/auth config
- `*.pem`, `*.key` — SSH keys

**Pattern**:
```python
import os
login = os.environ["MT5_LOGIN_A"]  # crashes if missing — fail fast
password = os.environ.get("MT5_PASSWORD_A", "")  # optional
```

## VPS Security

- **Firewall**: only ports 5005-5007 (bridge) + 5900-5902 (VNC) exposed internally
- **SSH**: key-based auth only, no password login
- **Docker**: containers run as non-root where possible
- **Bridge ports**: 5005/5006/5007 not exposed to internet — Docker internal network only

## Trade Safety

### Circuit Breaker (hard stops)
- 3 consecutive losses → stop trading
- 5% daily loss → stop trading
- Learning mode bypasses these (data collection only)

### Position Limits
- Max positions per account (env var `MAX_POSITIONS_PER_ACCOUNT`)
- No double-entry (check existing position before opening)

### Lot Size Guards
- Minimum 0.01 lots (Exness requirement)
- ML risk-scaling can reduce but never increase lot size
- Fixed fraction position sizing (1-2% risk per trade)

## API Keys (in .env)
| Key | Purpose | Rotation |
|-----|---------|----------|
| FINNHUB_API_KEY | News + sentiment | Quarterly |
| TELEGRAM_BOT_TOKEN | Trade notifications | As needed |
| MT5_LOGIN_A/B/C | Broker accounts | Never committed |
| MT5_PASSWORD_A/B/C | Broker passwords | Never committed |

## Known Risks

1. **Bridge exposed on VPS LAN**: RPyC ports (8001) are internal Docker network only — but if VPS is compromised, attacker can send trades
2. **No API key rotation**: keys are manually managed — no automated rotation
3. **No audit log for manual interventions**: SSH + Docker exec leaves no trace in oracle.db
4. **Learning mode bypasses all risk checks**: `LEARNING_MODE=1` disables circuit breaker and calendar filter — must not be used on live accounts with real money

## Incident Response

1. **Unauthorized trades**: Stop oracle-engine container immediately (`docker compose stop oracle-engine`)
2. **Credential leak**: Rotate MT5 passwords on Exness, update `.env`, restart
3. **VPS compromise**: SSH in, stop all containers, rotate all keys, investigate logs

## Security Checklist (pre-deploy)
- [ ] `.env` not in git (`git status` shows untracked only)
- [ ] Bridge ports not exposed in docker-compose (no `ports:` mapping to 0.0.0.0)
- [ ] `LEARNING_MODE=0` on production
- [ ] Circuit breaker config is reasonable (not disabled)
- [ ] Telegram bot token is production, not test bot
