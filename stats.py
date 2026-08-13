"""
Чтение сделок и сводка по ним.

Вынесено отдельно, потому что одни и те же цифры показывают и веб-дашборд
(webapp.py), и Telegram-бот (tg_bot.py). Пока функция жила в webapp.py, бот
считал бы своё - и две панели расходились бы в показаниях.
"""

import json
import os

import config


def load_list(path: str) -> list:
    """Список из JSON-файла состояния. Битый или пустой файл - пустой список."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return []
    return data if isinstance(data, list) else []


def load_active() -> list:
    return load_list(config.ACTIVE_TRADES_FILE)


def load_history() -> list:
    return load_list(config.TRADE_HISTORY_FILE)


def compute(history: list) -> dict:
    """Сводка по закрытым сделкам.

    Сделки без посчитанного PnL (биржа не отдала closedPnl) в проценте побед
    не участвуют - иначе они считались бы поражениями и занижали статистику.
    """
    with_pnl = [t for t in history if isinstance(t.get("realized_pnl"), (int, float))]
    total_pnl = sum(t["realized_pnl"] for t in with_pnl)
    wins = sum(1 for t in with_pnl if t["realized_pnl"] > 0)
    total = len(with_pnl)
    return {
        "total_trades": len(history),
        "trades_with_pnl": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": round(wins / total * 100, 1) if total else None,
        "total_realized_pnl": round(total_pnl, 4) if total else None,
        "best": round(max((t["realized_pnl"] for t in with_pnl), default=0), 4) if total else None,
        "worst": round(min((t["realized_pnl"] for t in with_pnl), default=0), 4) if total else None,
    }
