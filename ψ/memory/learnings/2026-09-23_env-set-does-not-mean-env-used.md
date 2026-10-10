# Lesson: "Env ตั้งครบ" ≠ "config ถูกใช้" — ตรวจรับที่วัตถุจริง ไม่ใช่ os.environ

**Date**: 2026-09-23
**Source**: mr-bet post-mortem (retro 2026-09/23/03.01) — RR bug 5e91619 + D rebase b51744d
**Tags**: deploy-verification, config, env, compose, god-port-oracle

## The Lesson

ตรวจรับหลัง deploy ด้วยการ echo `os.environ` พิสูจน์ได้แค่ว่า **env ถูกตั้ง** ไม่ใช่ว่า **โค้ดอ่านมัน** — สองอย่างนี้คนละเรื่อง และช่องว่างระหว่างสองอย่างนี้คือที่ที่ bug ซ่อนตัวได้เงียบที่สุด

กรณีจริง 2026-09-21: deploy mr-bet ตรวจรับ "env ครบ" ผ่าน — แต่ไม้จริงออกด้วย RR 2.5 (default) ไม่ใช่ 0.8 ตาม variant เพราะ `RR_RATIO_B` โดนอ่านเฉพาะในสาขา `if not risk_config:` ของ LiveTrader ซึ่งไม่เคยทำงานเลย (oracle_runner ส่ง risk_config เสมอ) การทดสอบ dimension RR เสียข้อมูลไป 2 วันโดยไม่มีใครรู้ จนคุณหมอถามผลเอง

## How to Apply

1. **ตรวจรับ config = อ่านค่าจากวัตถุที่ใช้จริง**: instantiate object ใน container แล้วอ่าน attribute (เช่น `LiveTrader(...).risk.risk_reward_ratio`) — อย่าหยุดที่ `print(os.environ[...])`
2. **สาขาเงื่อนไขที่ไม่เคยทำงาน = โค้ดตายที่เขียนเองไว้**: ก่อนวาง env knob ใหม่ ตรวจว่า branch ที่อ่านมัน reachable บน production path จริงไหม (runner/caller ส่งอะไรมา)
3. **host .env ชนะ `${VAR:-default}` เสมอ**: ค่าที่ห้าม override ต้อง hard-code ใน compose — pattern มาตรฐานแล้ว (ใช้แล้วกับ ML_FILTER_ENABLED, INITIAL_EQUITY_D)
4. **ค่าใน DB ที่ขัดแผน = สัญญาณเตือน ไม่ใช่ anomaly**: เห็น `rr_ratio=2.5` ไม่ตรง variant ตอน deploy แล้ว ต้องไล่ทันที ไม่ใช่รอ 2 วัน

## Related

- [[2026-07-13 ATR env-wins fix]] — รูปแบบ env-ชนะเสมอ ต้นแบบของการแก้ครั้งนี้
- tests/test_rr_conf_env_override_causal.py — causal test ที่พิสูจน์ bug นี้ (RED 3 → GREEN 6)