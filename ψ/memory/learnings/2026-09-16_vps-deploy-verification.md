# VPS deploy verification — bridge protocol, first boot, และ path ที่ต้อง verify จริง

**Date**: 2026-09-16
**Source**: portfolio-cent P1-P3 deploy phase 1 (mt5p1-3)

## Bridge ไม่ใช่ HTTP

MT5 bridge (port 8001) เป็น raw socket protocol — `curl http://localhost:8001/health`
**ไม่ตอบแม้บน mt5a production ที่ทำงานอยู่จริง**. อย่าใช้ HTTP probe เป็นเกณฑ์สุขภาพ
bridge. Healthcheck ที่ถูกคือ socket connect (แบบที่ compose ตั้งไว้) หรือเรียก
ผ่าน MT5Bridge client เอง

## MT5 container first boot ช้า ~10 นาที

Container ใหม่ที่ volume เปล่า (mt5-config-p*) ต้อง wine init + ติดตั้ง Python
ใน wine — ช่วงนี้ status เป็น `(health: starting)` → `unhealthy` ได้เป็นเวลานาน
ก่อน bridge จะ listen. **อย่า restart container ตอนนี้** — รอจน socket :8001
ต่อได้ (มัก ~10 นาทีจาก container ใหม่เอี่ยม)

## Monitor ที่เพิ่งเขียนต้องถูก audit เหมือน code

Background poll ที่ใช้ string matching บน docker ps output สามารถ false positive
ได้ (เคยเกิด: grep "healthy" จับผิด pattern → รายงานสำเร็จทั้งที่ bridge ยัง
ไม่ขึ้น). กฎ: **verify ผล monitor ด้วย state จริงของระบบอีกชั้น** (docker ps
โดยตรง / จับ error) ก่อนเชื่อและรายงาน

## Path ใน runbook ต้องมาจาก output จริง

อย่าเขียน path บน VPS จากความจำหรือการเดา — SSH ไป `ls` ก่อนเสมอ
(ตัวอย่างจริง: repo คือ `/opt/god-port-oracle` ไม่ใช่ `/opt/god-port`;
data dirs แยกตั้งใจไว้ที่ `/opt/god-port/data` — สองอย่างนี้ต่างกันโดย design)

## Baseline→deploy→verify คือหลักฐานเดียวของ "ไม่โดนแตะ"

จด pre-deploy state (`docker ps` → file) ก่อน operation ใดๆ แล้ว diff หลังจบ —
วิธีเดียวที่พิสูจน์ได้ว่า service เดิม (Real-A) ไม่ถูก recreate. ใช้กับทุก deploy
ที่มี production ร่วมอยู่