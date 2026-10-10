---
name: docker-exec-heredoc-needs-i-flag
description: ผ่าน heredoc/Python เข้า container ผ่าน ssh ต้องใช้ `docker exec -i` — ไม่มี `-i` คำสั่ง "สำเร็จ" เงียบๆ ไม่มี output ไม่มี error
metadata:
  type: project
---

เวลายิง Python แบบ heredoc เข้าไปใน container ผ่าน ssh (ขั้นตอนตรวจผลเทรด live บน VPS):

```bash
# ❌ ผิด — stdout ว่างเปล่า ไม่มี error ให้เห็น เหมือน ssh/พัง
ssh vpsdeluna "docker exec oracle-engine-train python - <<'EOF'
...
EOF"

# ✅ ถูก — ต้องมี -i ถึงจะ pipe stdin เข้า container
ssh vpsdeluna "docker exec -i oracle-engine-train python - <<'EOF'
...
EOF"
```

**อาการ:** คำสั่งจบแบบ "success" แต่ไม่มี output เลย — stdin ของ heredoc ไม่ถูกส่งเข้า container เพราะ `docker exec` ไม่มี `-i` จึงไม่ bind stdin

**กฎทั่วไป:** ถ้าคำสั่งไหน "ไม่มี output และไม่มี error" ให้ตรวจคำสั่งเราเองก่อนตรวจระบบ (docker ps ยืนยัน container ยังอยู่ก่อน)

**Why:** เสียเวลาไป 1 รอบเพราะสรุปผิดว่า ssh พัง ทั้งที่ container และ ssh ปกติดี
**How to apply:** ทุกครั้งที่ exec script เข้า container จากเครื่องอื่น — ใส่ `-i` เสมอ หรือเขียน script ไฟล์แล้ว copy เข้าไปรัน

Related: [[live-mr-bet-data-lives-in-vps-container-db]]