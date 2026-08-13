"""
Изменяемые "на лету" торговые параметры (маржа, плечо, разбивка TP).

В отличие от config.py (секреты и системные настройки, меняются только
руками в коде) - эти значения можно менять через веб-панель без перезапуска
бота. Хранятся в settings.json поверх значений по умолчанию из config.py.
Новый сигнал всегда открывается с текущими на момент сигнала параметрами.
"""

import json
import os
import threading

import config

_lock = threading.Lock()

DEFAULTS = {
    "margin_usdt": config.MARGIN_USDT,
    "leverage": config.LEVERAGE,
    "tp_percents": list(config.TP_PERCENTS),
    "trading_enabled": True,
}


class SettingsError(ValueError):
    pass


def _load() -> dict:
    if not os.path.exists(config.SETTINGS_FILE) or os.path.getsize(config.SETTINGS_FILE) == 0:
        return dict(DEFAULTS)
    with open(config.SETTINGS_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def get_all() -> dict:
    with _lock:
        return _load()


def get_margin_usdt() -> float:
    return get_all()["margin_usdt"]


def get_leverage() -> int:
    return get_all()["leverage"]


def get_tp_percents() -> list:
    return get_all()["tp_percents"]


def is_trading_enabled() -> bool:
    return bool(get_all().get("trading_enabled", True))


def is_demo() -> bool:
    """Режим счёта: True - демо (api-demo.bybit.com), False - боевой.

    Определяется только флагом config.DEMO, который должен совпадать с типом
    ключей в config.py. В веб-панели переключателя нет намеренно: случайно
    переключить живой счёт в демо (или наоборот) - это получить тихую ошибку
    авторизации 10003 и отключённый процентный лимит убытка.
    """
    return config.DEMO


def update(patch: dict) -> dict:
    """Валидирует и сохраняет изменения. Бросает SettingsError с понятным
    текстом на русском, если значения некорректны."""
    with _lock:
        current = _load()

        if "margin_usdt" in patch:
            try:
                margin = float(patch["margin_usdt"])
            except (TypeError, ValueError):
                raise SettingsError("Маржа должна быть числом")
            if margin <= 0:
                raise SettingsError("Маржа должна быть больше 0")
            current["margin_usdt"] = margin

        if "leverage" in patch:
            try:
                leverage = int(patch["leverage"])
            except (TypeError, ValueError):
                raise SettingsError("Плечо должно быть целым числом")
            if not (1 <= leverage <= 100):
                raise SettingsError("Плечо должно быть от 1 до 100")
            current["leverage"] = leverage

        if "tp_percents" in patch:
            try:
                percents = [float(p) for p in patch["tp_percents"]]
            except (TypeError, ValueError):
                raise SettingsError("Доли TP должны быть числами")
            if len(percents) != 4:
                raise SettingsError("Нужно ровно 4 значения разбивки TP")
            if any(p <= 0 for p in percents):
                raise SettingsError("Все доли TP должны быть больше 0")
            if abs(sum(percents) - 100) > 0.01:
                raise SettingsError(f"Сумма долей TP должна быть 100, сейчас {sum(percents):.2f}")
            current["tp_percents"] = percents

        if "trading_enabled" in patch:
            current["trading_enabled"] = bool(patch["trading_enabled"])

        # demo_mode больше не настройка: бот работает только на реальном
        # счёте. Присланное значение молча игнорируем и вычищаем из файла,
        # чтобы старое True не сбивало с толку при чтении настроек.
        patch.pop("demo_mode", None)
        current.pop("demo_mode", None)

        with open(config.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)

        return current
