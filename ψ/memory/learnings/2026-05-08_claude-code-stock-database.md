---
name: Claude Code Stock Database Integration
description: Claude Code can connect to 17,000+ stock database via prompts — potential for expanding trading beyond XAUUSD
type: reference
---

Claude Code ต่อฐานข้อมูลหุ้น 17,000+ ตัวได้แล้ว แค่พิมพ์ prompt ก็เรียก

**Why**: ถ้าต่อฐานข้อมูลหุ้นได้ อาจขยาย god-port-oracle จาก XAUUSD ไปสินทรัพย์อื่น — ต้องใช้ BrokerABC ที่สร้างไว้แล้ว (P1A) เป็นตัวเชื่อม

**How to apply**: เมื่อจะขยายไปเทรดสินทรัพย์ใหม่ ให้ใช้ BrokerABC interface + เพิ่ม broker implementation ใหม่ ไม่ต้องเขียนใหม่ทั้งหมด