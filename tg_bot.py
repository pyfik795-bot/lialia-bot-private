"""
Telegram-бот для уведомлений о сделках и просмотра статистики.

Доступ даётся только username'ам из config.ALLOWED_USERNAMES (без общей
секретной фразы - её мог ввести кто угодно, кто её узнал). При /start от
разрешённого пользователя его chat_id сохраняется в authorized_chats.json,
после чего он получает уведомления об открытии сделок и достижении TP.
"""

import asyncio
import json
import logging
import os
import threading
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.backoff import Backoff, BackoffConfig

import channels
import config
import risk
import settings
import stats
import status
import synctime
import trade_engine

logger = logging.getLogger("tg_bot")

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# Telegram иногда на короткое время недоступен или отвечает 429/502. Стандартный
# backoff aiogram повторяет запросы каждые 1-5 секунд и только сильнее нагружает
# соединение. Для круглосуточного бота разумнее спокойно ждать до минуты.
POLLING_BACKOFF = BackoffConfig(min_delay=5.0, max_delay=60.0, factor=1.6, jitter=0.5)
BOT_DISPLAY_NAME = "Ляля Бот"

# Только сообщения от разрешённых пользователей доходят до хендлеров ниже
dp.message.filter(F.from_user.username.in_(config.ALLOWED_USERNAMES))

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏠 Обзор"), KeyboardButton(text="📈 Активные")],
        [KeyboardButton(text="📜 История"), KeyboardButton(text="📊 Аналитика")],
        [KeyboardButton(text="🛡 Риск"), KeyboardButton(text="⚙️ Параметры")],
        [KeyboardButton(text="📡 Каналы"), KeyboardButton(text="🖥 Система")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите раздел панели",
)


def _mode_label() -> str:
    """Режим счёта одной строкой. Раньше здесь было зашито «РЕАЛЬНЫЙ СЧЁТ»
    независимо от config.DEMO - на демо бот уверенно врал."""
    return "🧪 ДЕМО-счёт" if settings.is_demo() else "💵 РЕАЛЬНЫЙ СЧЁТ"


# ============ ХРАНЕНИЕ АВТОРИЗОВАННЫХ ЧАТОВ ============

_chats_lock = threading.Lock()


def _load_authorized_chats() -> list[dict]:
    if not os.path.exists(config.AUTHORIZED_CHATS_FILE) or os.path.getsize(config.AUTHORIZED_CHATS_FILE) == 0:
        return []
    with open(config.AUTHORIZED_CHATS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _save_chat(chat_id: int, username: str) -> None:
    with _chats_lock:
        chats = _load_authorized_chats()
        if any(c["chat_id"] == chat_id for c in chats):
            return
        chats.append({"chat_id": chat_id, "username": username})
        with open(config.AUTHORIZED_CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(chats, f, ensure_ascii=False, indent=2)


async def send_to_all(text: str, reply_markup=None) -> None:
    """Рассылает текст всем авторизованным пользователям. Используется
    торговым движком (через notifier) для уведомлений об открытии/TP/закрытии."""
    # Обычное уведомление о запуске заодно заменяет устаревшую клавиатуру у
    # каждого авторизованного пользователя — отдельный /start не требуется.
    if text.startswith("🚀") and reply_markup is None:
        reply_markup = MAIN_KEYBOARD

    for chat in _load_authorized_chats():
        try:
            await bot.send_message(chat["chat_id"], text, reply_markup=reply_markup)
        except Exception as e:
            # print уходил в никуда: вывод бота не пишется в logs/bot.log
            logger.warning(f"не удалось отправить сообщение chat_id={chat['chat_id']}: {e}")


# ============ ФОРМАТИРОВАНИЕ СПИСКОВ СДЕЛОК ============

def _load_list(path: str) -> list[dict]:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _direction_label(side: str) -> str:
    return "Long 📈" if side == "Buy" else "Short 📉"


def _uptime_label(seconds: int) -> str:
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{days}д {hours}ч {minutes}м" if days else f"{hours}ч {minutes}м"


def _format_overview(balance: dict | None = None, balance_error: str | None = None) -> str:
    """Главный экран: компактная read-only сводка тех же данных, что на сайте."""
    system = status.snapshot()
    trading = settings.get_all()
    risk_state = risk.snapshot()
    stat = stats.compute(stats.load_history())
    total_pnl = stat["total_realized_pnl"] or 0.0
    win_rate = stat["win_rate"] or 0.0
    active = _load_list(config.ACTIVE_TRADES_FILE)
    channel_list = channels.get_all()
    enabled_channels = sum(1 for item in channel_list if item.get("enabled", True))

    connection = (
        f"{'🟢' if system['telethon_connected'] else '🔴'} Telegram  ·  "
        f"{'🟢' if system['bybit_ws_connected'] else '🔴'} Bybit"
    )
    trade_state = "🟢 включена" if trading["trading_enabled"] else "⏸ на паузе"
    if risk_state["emergency_stop"]:
        trade_state = "🛑 Emergency Stop"
    elif risk_state["blocked"]:
        trade_state = "⏸ заблокирована риском"

    lines = [
        "⚡ ЛЯЛЯ БОТ",
        f"{_mode_label()}  ·  торговля {trade_state}",
        "",
        connection,
        f"⏱ Аптайм: {_uptime_label(system['uptime_seconds'])}",
        f"📡 Каналы: {enabled_channels}/{len(channel_list)} активны",
        "",
        "💼 СЧЁТ",
    ]

    if balance is not None:
        unrealised = balance.get("unrealised_pnl", 0.0)
        lines.extend([
            f"Equity: {balance.get('equity', 0.0):.2f} USDT",
            f"Доступно: {balance.get('available_balance', 0.0):.2f} USDT",
            f"Нереализованный PnL: {unrealised:+.2f} USDT",
        ])
    else:
        lines.append(f"Баланс временно недоступен{': ' + balance_error if balance_error else ''}")

    lines.extend([
        "",
        "📊 РЕЗУЛЬТАТ",
        f"Открыто позиций: {len(active)}",
        f"PnL за 24 часа: {risk_state['daily_pnl']:+.2f} USDT",
        f"PnL за всё время: {total_pnl:+.2f} USDT",
        f"Win rate: {win_rate:.1f}% · сделок: {stat['total_trades']}",
        "",
        f"Обновлено: {datetime.now().strftime('%H:%M:%S')}",
        "Все кнопки ниже работают только на просмотр.",
    ])
    return "\n".join(lines)


async def format_overview() -> str:
    try:
        balance = await asyncio.get_running_loop().run_in_executor(None, trade_engine.get_wallet_balance)
        return _format_overview(balance=balance)
    except Exception as exc:
        logger.warning("обзор: баланс Bybit временно недоступен: %s", exc)
        return _format_overview(balance_error="ошибка соединения с Bybit")


def format_all_trades() -> str:
    active = _load_list(config.ACTIVE_TRADES_FILE)
    history = _load_list(config.TRADE_HISTORY_FILE)

    if not active and not history:
        return "Сделок пока не было."

    lines = []
    if active:
        lines.append("🟢 Активные:")
        for t in active:
            lines.append(f"  {t['symbol']} {_direction_label(t['side'])} — вход {t['entry_price']}, "
                          f"TP пройдено: {len(t.get('tp_filled', []))}/{len(t.get('targets', []))}")

    if history:
        lines.append("\n📜 История:")
        for t in reversed(history[-20:]):
            lines.append(f"  {t['symbol']} {_direction_label(t['side'])} — {t.get('close_reason', '?')} "
                          f"(вход {t['entry_price']}, закрыта {t.get('closed_at', '?')})")

    return "\n".join(lines)


def format_current_trades() -> str:
    active = _load_list(config.ACTIVE_TRADES_FILE)
    if not active:
        return "Сейчас нет открытых сделок."

    cards = []
    for t in active:
        tp_lines = []
        for i, target in enumerate(t.get("targets", []), start=1):
            mark = "✅" if i in t.get("tp_filled", []) else "⏳"
            tp_lines.append(f"  {mark} TP{i}: {target}")

        # маржа и плечо берутся из самой сделки: настройки могли поменяться
        # после её открытия, и config показывал бы уже не те цифры
        margin = t.get("margin_usdt", config.MARGIN_USDT)
        leverage = t.get("leverage", config.LEVERAGE)

        cards.append(
            f"{t['symbol']} {_direction_label(t['side'])}\n"
            f"Вход: {t.get('entry_price', '?')}\n"
            f"Объём: {t.get('qty_total', '?')} (маржа {margin} USDT x{leverage})\n"
            f"Текущий SL: {t.get('current_sl', '?')}\n" + "\n".join(tp_lines) +
            f"\nОткрыта: {t.get('opened_at', '?')}"
        )

    return "\n\n".join(cards)


def format_stats() -> str:
    """Сводка по закрытым сделкам - те же цифры, что в веб-панели."""
    st = stats.compute(stats.load_history())
    if not st["trades_with_pnl"]:
        return "📉 Статистика\nЗакрытых сделок с посчитанным PnL пока нет."

    pnl = st["total_realized_pnl"]
    return (
        f"📉 Статистика за всё время\n"
        f"Итоговый PnL: {pnl:+.2f} USDT\n"
        f"Процент побед: {st['win_rate']}% ({st['wins']} из {st['trades_with_pnl']})\n"
        f"Прибыльных: {st['wins']}, убыточных: {st['losses']}\n"
        f"Лучшая сделка: {st['best']:+.2f} USDT\n"
        f"Худшая сделка: {st['worst']:+.2f} USDT\n"
        f"Всего закрыто: {st['total_trades']}"
    )


def format_settings() -> str:
    """Торговые настройки. Только просмотр - меняются в веб-панели."""
    s = settings.get_all()
    tp = s["tp_percents"]
    notional = s["margin_usdt"] * s["leverage"]
    tp_lines = "\n".join(f"  TP{i}: {p}% объёма" for i, p in enumerate(tp, start=1))

    return (
        f"⚙️ Торговые настройки\n"
        f"Режим: {_mode_label()}\n"
        f"Торговля: {'🟢 включена' if s['trading_enabled'] else '⏸ выключена'}\n\n"
        f"Маржа на сделку: {s['margin_usdt']} USDT\n"
        f"Плечо: x{s['leverage']}\n"
        f"Объём позиции: ~{notional} USDT\n\n"
        f"Разбивка тейков:\n{tp_lines}\n\n"
        f"Изменить можно в веб-панели."
    )


def format_channels() -> str:
    """Список каналов-источников. Только просмотр - правятся в веб-панели."""
    chans = channels.get_all()
    if not chans:
        return "📡 Каналы-источники\nНи одного канала не подключено."

    lines = []
    for c in chans:
        mark = "🟢" if c.get("enabled", True) else "⬜"
        title = c.get("title") or c.get("username") or c.get("chat_id")
        parser = c.get("parser") or "автоподбор"
        lines.append(f"{mark} {title}\n   парсер: {parser}")

    enabled = sum(1 for c in chans if c.get("enabled", True))
    return (f"📡 Каналы-источники ({enabled} из {len(chans)} активны)\n\n"
            + "\n".join(lines)
            + "\n\nДобавить или отключить можно в веб-панели.")


def format_status() -> str:
    s = status.snapshot()
    hours, rem = divmod(s["uptime_seconds"], 3600)
    minutes, seconds = divmod(rem, 60)

    def badge(ok: bool) -> str:
        return "🟢 подключено" if ok else "🔴 нет соединения"

    return (
        f"🖥 Система\n"
        f"Telegram-канал с сигналами: {badge(s['telethon_connected'])}\n"
        f"Bybit (ордера/сделки): {badge(s['bybit_ws_connected'])}\n"
        f"Режим торговли: {_mode_label()}\n"
        f"Работает: {hours}ч {minutes}м {seconds}с\n"
        f"Смещение часов Bybit: {synctime.offset_ms() if synctime.offset_ms() is not None else '—'} мс\n"
        f"Синхронизация: "
        f"{round(synctime.seconds_since_sync()) if synctime.seconds_since_sync() is not None else '—'} сек. назад"
    )


async def format_balance() -> str:
    try:
        # блокирующий HTTP-запрос к Bybit - уводим в поток, чтобы не подвесить
        # event loop (в нём же крутятся Telethon и веб-дашборд)
        b = await asyncio.get_running_loop().run_in_executor(None, trade_engine.get_wallet_balance)
    except Exception as e:
        return f"❌ Не удалось получить баланс: {e}"

    return (
        f"💰 Баланс Bybit — {_mode_label()}\n"
        f"Equity: {b['equity']:.2f} USDT\n"
        f"Wallet balance: {b['wallet_balance']:.2f} USDT\n"
        f"Доступно: {b['available_balance']:.2f} USDT\n"
        f"Нереализованный PnL: {b['unrealised_pnl']:.2f} USDT"
    )


def format_risk() -> str:
    """Состояние защиты от слива: что включено, сколько убытка за сутки,
    стоит ли блокировка."""
    s = risk.snapshot()
    cfg = s["settings"]

    def limit(enabled: bool, text: str) -> str:
        return f"  {'✅' if enabled else '⬜'} {text}"

    if s["emergency_stop"]:
        head = "🛑 EMERGENCY STOP — торговля остановлена вручную"
    elif s["blocked"]:
        left_min = s["blocked_seconds_left"] // 60
        head = (f"⏸ Торговля на паузе ещё {left_min // 60}ч {left_min % 60}м\n"
                f"Причина: {s['block_reason']}")
    else:
        head = "🟢 Торговля разрешена"

    return (
        f"🛡 Защита от слива\n{head}\n\n"
        f"За 24 часа: PnL {s['daily_pnl']:+.2f} USDT, закрыто сделок {s['closed_today']}\n"
        f"Убыточных подряд: {s['losing_streak']}\n\n"
        f"Лимиты:\n"
        + limit(cfg["max_open_trades_enabled"], f"одновременных сделок: {cfg['max_open_trades']}") + "\n"
        + limit(cfg["daily_loss_percent_enabled"], f"убыток за сутки: {cfg['daily_loss_percent']}% баланса") + "\n"
        + limit(cfg["daily_loss_usdt_enabled"], f"убыток за сутки: {cfg['daily_loss_usdt']} USDT") + "\n"
        + limit(cfg["max_losing_streak_enabled"], f"убыточных подряд: {cfg['max_losing_streak']}") + "\n\n"
        f"Пауза после срабатывания: {cfg['cooldown_hours']}ч"
    )


# ============ ОБРАБОТЧИКИ ============

@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(message: Message):
    _save_chat(message.chat.id, message.from_user.username)
    await message.answer(
        await format_overview(),
        reply_markup=MAIN_KEYBOARD,
    )


@dp.message(F.text == "🏠 Обзор")
async def overview_handler(message: Message):
    await message.answer(await format_overview(), reply_markup=MAIN_KEYBOARD)


@dp.message(F.text.in_({"📜 История", "📊 Все сделки"}))
async def all_trades(message: Message):
    await message.answer(format_all_trades())


@dp.message(F.text.in_({"📈 Активные", "📈 Текущие сделки"}))
async def current_trades(message: Message):
    await message.answer(format_current_trades())


@dp.message(F.text.in_({"📊 Аналитика", "📉 Статистика"}))
async def stats_handler(message: Message):
    await message.answer(format_stats())


@dp.message(F.text.in_({"⚙️ Параметры", "⚙️ Настройки"}))
async def settings_handler(message: Message):
    await message.answer(format_settings())


@dp.message(F.text == "📡 Каналы")
async def channels_handler(message: Message):
    await message.answer(format_channels())


@dp.message(F.text.in_({"🖥 Система", "🔌 Статус"}))
async def status_handler(message: Message):
    await message.answer(format_status())


@dp.message(F.text.in_({"🛡 Риск", "🛡 Защита"}))
async def risk_handler(message: Message):
    await message.answer(format_risk())


@dp.message(F.text == "💰 Баланс")
async def legacy_balance_handler(message: Message):
    await message.answer(await format_overview(), reply_markup=MAIN_KEYBOARD)


@dp.message(F.text.in_({"🛑 Стоп всё", "▶️ Снять блокировку"}))
async def legacy_control_handler(message: Message):
    await message.answer(
        "Эта старая управляющая кнопка отключена: Telegram-панель теперь "
        "работает только на просмотр. Используйте новое меню ниже.",
        reply_markup=MAIN_KEYBOARD,
    )


# ============ ЗАПУСК (для локального теста только tg_bot.py) ============

async def start_polling():
    """Надёжный polling, включая сбои до первого успешного getMe.

    Внутренний backoff aiogram работает уже после запуска polling. DNS/сеть
    могут упасть раньше, на bot.me(), поэтому весь запуск тоже обёрнут в retry.
    """
    backoff = Backoff(POLLING_BACKOFF)
    profile_synced = False

    while True:
        try:
            await bot.me()
            if not profile_synced:
                try:
                    await bot.set_my_name(name=BOT_DISPLAY_NAME)
                except Exception as exc:
                    logger.warning("Не удалось обновить отображаемое имя Telegram-бота: %s", exc)
                profile_synced = True

            await dp.start_polling(
                bot,
                polling_timeout=30,
                backoff_config=POLLING_BACKOFF,
                handle_signals=False,
                close_bot_session=False,
            )
            return
        except asyncio.CancelledError:
            raise
        except (TelegramNetworkError, TelegramRetryAfter, TelegramServerError) as exc:
            delay = next(backoff)
            logger.warning(
                "Telegram Bot API временно недоступен (%s). Повтор через %.1f сек.",
                type(exc).__name__, delay,
            )
            await asyncio.sleep(delay)
        except Exception:
            logger.exception("Polling Telegram-бота завершился неожиданно")
            delay = next(backoff)
            await asyncio.sleep(delay)


async def stop_polling():
    """Корректно завершает long-poll до закрытия HTTP-сессии."""
    try:
        await dp.stop_polling()
    except RuntimeError:
        # Polling мог ещё не успеть стартовать или уже завершиться сам.
        pass


async def close():
    await bot.session.close()


async def main():
    print("Бот запущен")
    await start_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
