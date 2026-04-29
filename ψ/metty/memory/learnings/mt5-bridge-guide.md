# MT5 Bridge Knowledge Base

> ข้อมูลการเชื่อมต่อ MT5 บน Mac ผ่าน Wine + mt5linux

## Architecture

```
[Broky Signal] -> [Oracle Vault outbox] -> [Metty] -> [mt5linux :5005] -> [Wine] -> [MT5 Exness]
                                                                     <- [MT5 Exness] <- [Execution Report]
```

## Bridge Configuration

- **Bridge command**: `python3 -m mt5linux --host 0.0.0.0 -p 5005`
- **PM2 process name**: `mt5-bridge`
- **Port**: 5005 (local only)
- **WINEPREFIX**: `$HOME/Library/Application Support/net.metaquotes.wine.metatrader5`
- **Wine shim**: `~/.mt5_bridge_bin/wine` -> `/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64`
- **Startup script**: `/Users/doctorboyz/MT5/run_bridge.sh`

## Broker Details

- **Broker**: Exness
- **Symbol**: XAUUSD
- **Leverage**: 3-5x (recommended for small accounts)
- **Margin mode**: Isolated (เท่านั้น)
- **Lot sizes**: micro=0.01, mini=0.10, standard=1.00

## Connection Health Check

```python
# Test connection
import MetaTrader5 as mt5
if not mt5.initialize():
    print(f"MT5 init failed: {mt5.last_error()}")
    # Try restart bridge
else:
    account = mt5.account_info()
    print(f"Connected: {account.server}")
```

## Order Execution Flow

1. Metty อ่าน signal จาก `κ/broky/extrinsic/communication/outbox/`
2. ตรวจสอบ bridge health (ping mt5linux)
3. แปลง signal -> MT5 order request
4. ส่ง order ผ่าน mt5linux
5. รอผล (timeout 5 วินาที)
6. เขียน execution report ไป `κ/metty/extrinsic/communication/outbox/`
7. ส่ง Telegram notification

## Error Handling

- **Timeout**: ยกเลิกคำสั่ง รายงาน timeout
- **Requote**: ตรวจ slippage ถ้าเกิน 0.5% = ปฏิเสธ
- **No connection**: พยายาม reconnect 3 ครั้ง ถ้าไม่ได้ = หยุดระบบ
- **Invalid volume**: ปฏิเสธคำสั่ง รายงาน error

## Telegram Notifications

- **Trade opened**: Direction, Symbol, Entry, SL, TP, Lot
- **Trade closed**: Direction, P&L, Close reason, Duration
- **Daily summary**: Win rate, Total P&L, Open positions
- **Error**: Bridge down, Order failed, Connection lost