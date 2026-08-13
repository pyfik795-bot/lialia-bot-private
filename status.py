"""
Общее для всего процесса состояние "жив ли компонент" - читается Telegram-ботом
и веб-дашбордом, пишется main.py (Telethon) и trade_engine.py (Bybit WebSocket).
"""

import threading
import time

_lock = threading.Lock()
_state = {
    "telethon_connected": False,
    "bybit_ws_connected": False,
    "started_at": time.time(),
}


def set_flag(key: str, value) -> None:
    with _lock:
        _state[key] = value


def snapshot() -> dict:
    with _lock:
        data = dict(_state)
    data["uptime_seconds"] = int(time.time() - data["started_at"])
    return data
