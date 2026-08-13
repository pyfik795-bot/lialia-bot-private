"""
Защита от слива депозита.

Четыре независимых предохранителя, каждый настраивается и отключается
отдельно в веб-панели:

1. Лимит одновременных сделок - сколько позиций может быть открыто разом.
   Без него пачка сигналов подряд открывает пачку позиций с плечом 20x.
2. Дневной лимит убытка в процентах от баланса - гибкий, растёт вместе с
   депозитом.
3. Дневной лимит убытка в USDT - жёсткий потолок, не зависит от баланса.
4. Серия убыточных сделок подряд - ловит "чёрную полосу" раньше, чем она
   съест лимит по деньгам.

При срабатывании любого из них торговля встаёт на кулдаун (по умолчанию 12
часов): новые сигналы игнорируются, уже открытые сделки продолжают жить и
закрываются штатно по TP/SL. Отдельно есть Emergency Stop - ручная кнопка,
которая закрывает все позиции по рынку и блокирует торговлю до снятия.

Убыток считается по реализованному PnL из trade_history.json за последние
24 часа - то есть по фактически закрытым сделкам, как их посчитала биржа.
"""

import json
import logging
import os
import threading
import time

import config

logger = logging.getLogger("risk")

_lock = threading.Lock()

DEFAULTS = {
    "max_open_trades": 3,          # одновременно открытых позиций
    "max_open_trades_enabled": True,

    "daily_loss_percent": 10.0,    # % от баланса за сутки
    "daily_loss_percent_enabled": True,

    "daily_loss_usdt": 50.0,       # абсолютный потолок убытка за сутки
    "daily_loss_usdt_enabled": True,

    "max_losing_streak": 3,        # убыточных сделок подряд
    "max_losing_streak_enabled": True,

    "cooldown_hours": 12,          # пауза после срабатывания лимита
}

# Состояние блокировки: до какого времени торговля запрещена и почему.
# Хранится в файле, а не только в памяти: main.py перезапускает бота при любом
# падении, и блокировка, живущая в процессе, обнулялась бы вместе с ним -
# сработавший лимит убытка и нажатый Emergency Stop переставали действовать
# ровно тогда, когда они нужнее всего.
_DEFAULT_STATE = {
    "blocked_until": 0,       # unix-время окончания кулдауна
    "block_reason": None,
    "emergency_stop": False,  # ручная блокировка, снимается только вручную
    "streak_since": 0,        # с какого момента считать серию убытков
}

_state = dict(_DEFAULT_STATE)


def _save_state() -> None:
    """Пишет состояние блокировки на диск. Вызывать под _lock не обязательно."""
    try:
        with open(config.RISK_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error(f"Не удалось сохранить состояние блокировки: {e}")


def _restore_state() -> None:
    """Поднимает блокировку из файла при старте процесса."""
    if not os.path.exists(config.RISK_STATE_FILE) or os.path.getsize(config.RISK_STATE_FILE) == 0:
        return
    try:
        with open(config.RISK_STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Состояние блокировки не прочиталось ({e}), начинаю с чистого")
        return

    _state.update({k: saved.get(k, v) for k, v in _DEFAULT_STATE.items()})

    if _state["emergency_stop"]:
        logger.warning("После перезапуска Emergency Stop всё ещё включён - торговля остановлена")
    elif _state["blocked_until"] > time.time():
        left = int((_state["blocked_until"] - time.time()) / 60)
        logger.warning(f"После перезапуска торговля всё ещё на паузе ещё {left} мин: "
                       f"{_state['block_reason']}")


_restore_state()


class RiskError(ValueError):
    pass


# ==================== НАСТРОЙКИ ====================

def _load_settings() -> dict:
    if not os.path.exists(config.RISK_FILE) or os.path.getsize(config.RISK_FILE) == 0:
        return dict(DEFAULTS)
    with open(config.RISK_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def get_settings() -> dict:
    with _lock:
        return _load_settings()


def update_settings(patch: dict) -> dict:
    """Валидирует и сохраняет лимиты. Тексты ошибок идут прямо в панель."""
    with _lock:
        current = _load_settings()

        if "max_open_trades" in patch:
            try:
                value = int(patch["max_open_trades"])
            except (TypeError, ValueError):
                raise RiskError("Лимит одновременных сделок должен быть целым числом")
            if not (1 <= value <= 50):
                raise RiskError("Лимит одновременных сделок должен быть от 1 до 50")
            current["max_open_trades"] = value

        if "daily_loss_percent" in patch:
            try:
                value = float(patch["daily_loss_percent"])
            except (TypeError, ValueError):
                raise RiskError("Дневной лимит убытка (%) должен быть числом")
            if not (0 < value <= 100):
                raise RiskError("Дневной лимит убытка (%) должен быть от 0 до 100")
            current["daily_loss_percent"] = value

        if "daily_loss_usdt" in patch:
            try:
                value = float(patch["daily_loss_usdt"])
            except (TypeError, ValueError):
                raise RiskError("Дневной лимит убытка (USDT) должен быть числом")
            if value <= 0:
                raise RiskError("Дневной лимит убытка (USDT) должен быть больше 0")
            current["daily_loss_usdt"] = value

        if "max_losing_streak" in patch:
            try:
                value = int(patch["max_losing_streak"])
            except (TypeError, ValueError):
                raise RiskError("Серия убыточных сделок должна быть целым числом")
            if not (1 <= value <= 50):
                raise RiskError("Серия убыточных сделок должна быть от 1 до 50")
            current["max_losing_streak"] = value

        if "cooldown_hours" in patch:
            try:
                value = float(patch["cooldown_hours"])
            except (TypeError, ValueError):
                raise RiskError("Пауза после лимита должна быть числом")
            if not (0 < value <= 168):
                raise RiskError("Пауза после лимита должна быть от 0 до 168 часов")
            current["cooldown_hours"] = value

        for flag in ("max_open_trades_enabled", "daily_loss_percent_enabled",
                     "daily_loss_usdt_enabled", "max_losing_streak_enabled"):
            if flag in patch:
                current[flag] = bool(patch[flag])

        with open(config.RISK_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)

        return current


# ==================== ПОДСЧЁТ УБЫТКА ====================

def _load_history() -> list:
    if not os.path.exists(config.TRADE_HISTORY_FILE) or os.path.getsize(config.TRADE_HISTORY_FILE) == 0:
        return []
    with open(config.TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _closed_since(hours: float = 24) -> list:
    """Сделки, закрытые за последние N часов, в порядке закрытия."""
    cutoff = time.time() - hours * 3600
    result = []
    for trade in _load_history():
        closed_at = trade.get("closed_at")
        if not closed_at:
            continue
        try:
            ts = time.mktime(time.strptime(closed_at, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            result.append(trade)
    return result


def daily_pnl() -> float:
    """Суммарный реализованный PnL за последние 24 часа (отрицательный = убыток)."""
    total = 0.0
    for trade in _closed_since(24):
        pnl = trade.get("realized_pnl")
        if isinstance(pnl, (int, float)):
            total += pnl
    return round(total, 4)


def _closed_at_ts(trade: dict) -> float | None:
    closed_at = trade.get("closed_at")
    if not closed_at:
        return None
    try:
        return time.mktime(time.strptime(closed_at, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


def losing_streak() -> int:
    """Убыточные сделки подряд - считая от последнего сброса серии.

    Точка отсчёта (streak_since) двигается, когда кулдаун за серию убытков
    заканчивается или блокировку снимают вручную. Без неё серия считалась бы
    по всей истории: после трёх убытков бот вставал на паузу, а по её
    окончании видел в истории те же три убытка и блокировался снова - и так
    навсегда, потому что прервать серию может только прибыльная сделка,
    а взять её заблокированный бот не может.
    """
    with _lock:
        since = _state["streak_since"]

    streak = 0
    for trade in reversed(_load_history()):
        ts = _closed_at_ts(trade)
        if ts is not None and ts < since:
            break        # сделки до сброса в серию не входят
        pnl = trade.get("realized_pnl")
        if not isinstance(pnl, (int, float)):
            continue  # сделки без посчитанного PnL серию не прерывают
        if pnl < 0:
            streak += 1
        else:
            break
    return streak


def _expire_block_if_due() -> None:
    """Снимает кулдаун, когда его время вышло, и начинает серию заново.

    Именно здесь серия обнуляется: иначе следующая же проверка увидела бы в
    истории всё те же убытки и заблокировала торговлю повторно.
    """
    with _lock:
        due = _state["blocked_until"] and _state["blocked_until"] <= time.time()
        if due:
            _state["blocked_until"] = 0
            _state["block_reason"] = None
            _state["streak_since"] = time.time()
    if due:
        _save_state()
        logger.info("Кулдаун закончился, торговля снова разрешена")


# ==================== БЛОКИРОВКА ====================

def block(reason: str, hours: float | None = None) -> None:
    """Ставит торговлю на паузу с указанием причины."""
    if hours is None:
        hours = get_settings()["cooldown_hours"]
    with _lock:
        _state["blocked_until"] = time.time() + hours * 3600
        _state["block_reason"] = reason
    _save_state()
    logger.warning(f"Торговля заблокирована на {hours}ч: {reason}")


def unblock() -> None:
    """Снимает блокировку и Emergency Stop - вызывается кнопкой в панели.

    Серию убытков тоже считаем заново: иначе кнопка не действует - проверка
    сразу же увидит прежние убытки и заблокирует торговлю обратно.
    """
    with _lock:
        _state["blocked_until"] = 0
        _state["block_reason"] = None
        _state["emergency_stop"] = False
        _state["streak_since"] = time.time()
    _save_state()
    logger.info("Блокировка торговли снята вручную")


def set_emergency_stop(value: bool) -> None:
    with _lock:
        _state["emergency_stop"] = bool(value)
    _save_state()
    if value:
        logger.warning("EMERGENCY STOP включён - торговля остановлена")
    else:
        logger.info("EMERGENCY STOP снят")


def snapshot() -> dict:
    """Текущее состояние риск-менеджера для панели и Telegram-бота."""
    _expire_block_if_due()
    with _lock:
        blocked_until = _state["blocked_until"]
        reason = _state["block_reason"]
        emergency = _state["emergency_stop"]

    now = time.time()
    is_blocked = emergency or blocked_until > now
    cfg = get_settings()
    pnl = daily_pnl()

    return {
        "blocked": is_blocked,
        "emergency_stop": emergency,
        "block_reason": reason if is_blocked else None,
        "blocked_seconds_left": int(blocked_until - now) if blocked_until > now else 0,
        "daily_pnl": pnl,
        "daily_loss": round(-pnl, 4) if pnl < 0 else 0.0,
        "losing_streak": losing_streak(),
        "closed_today": len(_closed_since(24)),
        "settings": cfg,
    }


# ==================== ГЛАВНАЯ ПРОВЕРКА ====================

def check_can_open(symbol: str, open_trades_count: int, balance_equity: float | None = None) -> tuple[bool, str | None]:
    """Решает, можно ли открывать новую сделку.

    Возвращает (True, None) если можно, иначе (False, причина).
    Причина уходит в лог и в Telegram, чтобы было видно, почему бот молчит.
    """
    _expire_block_if_due()

    with _lock:
        emergency = _state["emergency_stop"]
        blocked_until = _state["blocked_until"]
        reason = _state["block_reason"]

    if emergency:
        return False, "включён Emergency Stop - торговля остановлена вручную"

    if blocked_until > time.time():
        left_min = int((blocked_until - time.time()) / 60)
        return False, f"торговля на паузе ещё {left_min} мин ({reason})"

    cfg = get_settings()

    # 1. Лимит одновременных позиций
    if cfg["max_open_trades_enabled"] and open_trades_count >= cfg["max_open_trades"]:
        return False, (f"достигнут лимит одновременных сделок "
                       f"({open_trades_count}/{cfg['max_open_trades']})")

    pnl = daily_pnl()
    loss = -pnl if pnl < 0 else 0.0

    # 2. Дневной лимит убытка в USDT
    if cfg["daily_loss_usdt_enabled"] and loss >= cfg["daily_loss_usdt"]:
        msg = f"дневной лимит убытка достигнут: -{loss:.2f} из {cfg['daily_loss_usdt']} USDT"
        block(msg)
        return False, msg

    # 3. Дневной лимит убытка в процентах от баланса
    if cfg["daily_loss_percent_enabled"] and balance_equity and balance_equity > 0:
        loss_pct = loss / balance_equity * 100
        if loss_pct >= cfg["daily_loss_percent"]:
            msg = (f"дневной лимит убытка достигнут: -{loss_pct:.1f}% "
                   f"из {cfg['daily_loss_percent']}% баланса")
            block(msg)
            return False, msg

    # 4. Серия убыточных сделок
    if cfg["max_losing_streak_enabled"]:
        streak = losing_streak()
        if streak >= cfg["max_losing_streak"]:
            msg = f"{streak} убыточных сделок подряд (лимит {cfg['max_losing_streak']})"
            block(msg)
            return False, msg

    return True, None
