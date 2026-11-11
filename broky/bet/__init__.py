"""True-bet farm — isolated measurement of ENTRY edge against a fixed contract.

คุณหมอ (via Hermes, approved 2026-11-07): "แก่นแท้ของ bet farm คือจุดเข้า +
สัญญาออกตายตัว (TP/SL ตั้งแล้วเดินหนี) · หากลยุทธ์เข้าหลายแบบ ดูว่าแบบไหน
พอใช้ได้". This package splits the two halves of a trade so each can be
measured alone:

- ``entries``   — 3 deterministic entry families (closed-bar signals only)
- ``contract``  — the bet contract: fixed TP/SL set at entry, held to the end,
                  no trailing / time-stop / hour gate / circuit breaker
- ``engine``    — one-bet-at-a-time cell state machine shared by backtest and
                  the live container (same code path both places)
- ``store``     — SQLite persistence + idempotent cell seeding ($1,000/cell)

Iron constraints (ada-task-20261107-true-bet-farm.md):
- farm1/farm2 are NEVER touched — this package is additive only
- bridge access is read-only market data (copy_ticks_from / symbol_info_tick)
- backtest signals come from CLOSED bars only (no lookahead); entries fill at
  the NEXT bar's open (live fills at the current tick, the same instant
  relative to the signal bar)
"""

from broky.bet import contract, entries, engine, store  # noqa: F401

__all__ = ["contract", "entries", "engine", "store"]
