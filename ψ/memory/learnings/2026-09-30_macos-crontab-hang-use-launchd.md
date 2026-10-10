# macOS crontab ติดตั้งค้างจาก non-interactive shell — launchd คือทางที่เสถียร

**วันที่**: 2026-09-30
**บริบท**: ติดตั้ง cronjob สำรอง paper farm DB บน macOS (OrbStack/Mac mini)

## อาการ

`crontab -l` อ่านได้ปกติ แต่การ **เขียน** (`crontab <file>`, `crontab -`, pipe เข้า crontab) จาก shell ที่ Claude/hook เรียก จะ hang ไม่มีที่สิ้นสุด — ครั้งแรกอาจ error ว่า `crontab: tmp/tmp.XXXX: Interrupted system call`, ครั้งถัดไปเงียบไปเลย กระบวนการจริงคือ `/usr/bin/crontab` รัน **as root** (setuid/XPC) และค้างอยู่ใน state S — เมื่อเรา kill งานที่เรียกมัน (TaskStop/timeout) root process ตัวนั้น **รอดและ kill ไม่ได้ด้วยสิทธิ์ user** ต้องรอตายเองหรือ `sudo pkill`

เกิดขึ้นบน macOS 25.6 (Darwin) ปี 2026-09 — อาจเกี่ยวกับ security policy ของระบบ ไม่ใช่ bug ของเรา

## ทางแก้ที่เสถียร

ใช้ **launchd LaunchAgent** (user-level, ไม่ผ่าน root helper):

1. เขียน plist ที่ `~/Library/LaunchAgents/<label>.plist` — `ProgramArguments`, `WorkingDirectory`, `StartCalendarInterval` (Hour/Minute), `StandardOutPath/StandardErrorPath`
2. `launchctl bootstrap gui/$(id -u) <plist>` (ถ้า load ซ้ำ: `launchctl bootout ...` ก่อน)
3. **ทดสอบยิงจริง**: `launchctl kickstart gui/$(id -u)/<label>` แล้วดู log/ผลลัพธ์ — ห้ามเชื่อว่าติดตั้ง = ทำงาน

## บั๊กที่จับได้เพราะยิงทดสอบจริง

`farm_db_backup.py` glob `data/farm/*/*.db` จับ `data/farm/backup/*.db` **ตัวเอง** → วันต่อมา backup ของ backup ชื่อยาวขึ้นเรื่อยๆ จน disk ตายเงียบๆ — แก้โดย exclude ไดเรกทอรี output ใน loop ทุก backup job ต้องทดสอบอย่างน้อย 2 วันติดกันถึงจะเห็น snowball

## กฎที่ใช้ต่อ

- บน macOS: งาน scheduled ใหม่ = launchd ก่อนเสมอ ไม่ใช่ cron
- ถ้า crontab hang 1 ครั้ง — อย่า retry ด้วยวิธีเดิม (มันจะสร้าง root process ค้างเพิ่มทุกครั้ง) ให้เปลี่ยนวิธีเลย
- automation ทุกตัวต้องผ่าน "ยิงจริงแล้วเห็น output" ก่อนถือว่าเสร็จ