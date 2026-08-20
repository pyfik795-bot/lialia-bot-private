"""
Торговый движок Bybit (pybit unified_trading).

Перенесено и доработано из test_bit.py:
- открытие маркет-ордером, объём = MARGIN_USDT * LEVERAGE (config.py)
- начальный SL
- реальные лимитные TP-ордера (reduce-only) на бирже - столько, сколько
  тейков пришло в сигнале (каналы дают и 4, и 6)
- приватный WebSocket слушает исполнение ордеров:
    TP1 -> SL на 15% ниже/выше цены входа, дальше каждый TP двигает SL на уровень предыдущего,
    последний TP закрывает позицию
- автопереподключение WebSocket (watchdog по "тишине" в канале)

Отличия от test_bit.py:
- живёт весь процесс, а не выходит после одной сделки - process_signal()
  можно вызывать многократно, для разных символов параллельно
- пропускает сигнал, если по символу уже есть активная сделка, или если
  signal_id уже был обработан ранее (дедупликация повторных сообщений канала)
- состояние сделок сохраняется в JSON (config.ACTIVE_TRADES_FILE /
  config.TRADE_HISTORY_FILE), чтобы Telegram-бот мог их показать, и чтобы
  после перезапуска процесса не открыть вторую сделку по уже открытому символу
- уведомления о событиях (открытие / TP / закрытие) идут через callback
  `notifier(text: str)`, который передаётся снаружи (main.py), - сам движок
  ничего не знает про Telegram
"""

import json
import logging
import os
import threading
import time as _time
from decimal import Decimal, ROUND_DOWN

from pybit.unified_trading import HTTP, WebSocket

import config
import risk
import settings
import status
import synctime

# Логирование настраивается централизованно в logging_setup.configure()
# (main.py вызывает её первой, до импорта этого модуля)
logger = logging.getLogger("trade_engine")


def patch_pybit_clock() -> None:
    """Научить pybit брать время из synctime.

    Все запросы к Bybit (REST-подпись X-BAPI-TIMESTAMP/recv_window и
    авторизация приватного вебсокета) идут через
    pybit._helpers.generate_timestamp, который по умолчанию читает просто
    time.time() — на сервере с отстающими часами это даёт ошибки 10002/10016.
    Подмена на синхронизированное время делается один раз в main.py.
    """
    from pybit import _helpers
    _helpers.generate_timestamp = synctime.now_ms


def _make_http_session(demo=None, testnet=None) -> HTTP:
    if demo is None:
        demo = settings.is_demo()
    if testnet is None:
        testnet = config.TESTNET
    return HTTP(
        testnet=testnet,
        demo=demo,
        api_key=config.BYBIT_API_KEY,
        api_secret=config.BYBIT_API_SECRET,
        recv_window=config.RECV_WINDOW,
    )


# Сессия создаётся лениво — при первом запросе, чтобы успели примениться настройки
_session = None


def get_session() -> HTTP:
    global _session
    if _session is None:
        _session = _make_http_session()
    return _session


# Ссылка на активный движок — устанавливается в BotEngine.__init__
engine = None


def get_realized_pnl(symbol: str, since_ms: int) -> float | None:
    """Суммирует closedPnl всех закрывающих сделок по символу с момента
    открытия нашей позиции (каждый исполненный TP - это отдельная запись
    в get_closed_pnl, поэтому нужно сложить все, что относятся к этой сделке)."""
    try:
        resp = get_session().get_closed_pnl(category=config.CATEGORY, symbol=symbol, limit=50)
        total = 0.0
        for rec in resp["result"]["list"]:
            if int(rec["updatedTime"]) >= since_ms:
                total += float(rec["closedPnl"])
        return total
    except Exception as e:
        logger.warning(f"[{symbol}] Не удалось получить реализованный PnL: {e}")
        return None


def get_wallet_balance() -> dict:
    """Баланс единого (UNIFIED) торгового аккаунта в USDT."""
    try:
        resp = get_session().get_wallet_balance(accountType="UNIFIED", coin="USDT")
        lst = resp["result"]["list"]
        if not lst:
            logger.warning("get_wallet_balance: пустой list — аккаунт не инициализирован")
            return {"equity": 0, "wallet_balance": 0, "available_balance": 0, "unrealised_pnl": 0}
        acc = lst[0]

        def _f(key):
            try:
                return float(acc.get(key))
            except (TypeError, ValueError):
                return 0.0

        return {
            "equity": _f("totalEquity"),
            "wallet_balance": _f("totalWalletBalance"),
            "available_balance": _f("totalAvailableBalance"),
            "unrealised_pnl": _f("totalPerpUPL"),
        }
    except Exception as e:
        logger.warning(f"get_wallet_balance ошибка: {e}")
        return {"equity": 0, "wallet_balance": 0, "available_balance": 0, "unrealised_pnl": 0}


# ==================== ФАЙЛЫ СОСТОЯНИЯ ====================
_state_lock = threading.Lock()


def _load_json_list(path: str) -> list:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _save_json_list(path: str, data: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _append_json_list(path: str, item: dict) -> None:
    with _state_lock:
        data = _load_json_list(path)
        data.append(item)
        _save_json_list(path, data)


# ==================== УТИЛИТЫ ИНСТРУМЕНТА ====================
class SymbolInfo:
    _cache = {}

    @classmethod
    def get(cls, symbol: str) -> dict:
        if symbol not in cls._cache:
            resp = get_session().get_instruments_info(category=config.CATEGORY, symbol=symbol)
            item = resp["result"]["list"][0]
            cls._cache[symbol] = {
                "qty_step": Decimal(item["lotSizeFilter"]["qtyStep"]),
                "min_qty": Decimal(item["lotSizeFilter"]["minOrderQty"]),
                "tick_size": Decimal(item["priceFilter"]["tickSize"]),
            }
        return cls._cache[symbol]


def round_step(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_price(symbol: str, price: float) -> str:
    info = SymbolInfo.get(symbol)
    rounded = round_step(Decimal(str(price)), info["tick_size"])
    return format(rounded, "f")


def format_qty(qty: Decimal) -> str:
    """Количество для биржи обычной записью, без «4.000E+7».

    round_step умножает на шаг объёма, и у монет с крупным шагом (10, 100 -
    дешёвые монеты вроде PEPE) Decimal переходит на экспоненту. str() тогда
    даёт "4.000E+7", а Bybit такой qty отвергает.
    """
    return format(qty, "f")


def calc_qty_from_margin(symbol: str, margin_usdt: float, leverage: float, price: float) -> Decimal:
    info = SymbolInfo.get(symbol)
    notional = Decimal(str(margin_usdt)) * Decimal(str(leverage))
    raw_qty = notional / Decimal(str(price))
    qty = round_step(raw_qty, info["qty_step"])
    if qty < info["min_qty"]:
        qty = info["min_qty"]
    return qty


# ==================== УПРАВЛЕНИЕ ОДНОЙ СДЕЛКОЙ ====================
class TradeManager:
    def __init__(self, signal: dict, notifier=None):
        self.symbol = signal["symbol"]
        self.strategy = signal["strategy"].lower()  # "long" / "short"
        self.targets = signal["targets"]             # [TP1, TP2, ...] - число зависит от канала
        self.initial_sl = signal["stop_loss"]
        self.signal_id = signal.get("signal_id")

        self.side = "Buy" if self.strategy == "long" else "Sell"
        self.close_side = "Sell" if self.side == "Buy" else "Buy"

        self.entry_price = None
        self.qty_total = Decimal("0")
        self.tp_qtys = []
        self.tp_order_ids = {}   # order_id -> tp_index (1..4)
        self.tp_filled = set()
        self.current_sl = self.initial_sl
        self.opened_at = signal.get("timestamp") or _time.strftime("%Y-%m-%d %H:%M:%S")
        self.opened_at_ms = signal.get("opened_at_ms") or int(_time.time() * 1000)
        self.notifier = notifier or (lambda text: None)

        # Захватываются один раз при открытии, чтобы правки в веб-панели не
        # меняли параметры уже открытой сделки "на ходу"
        self.margin_usdt = settings.get_margin_usdt()
        self.leverage = settings.get_leverage()
        # Разбивка объёма: своя у формата сигнала (у каналов разное число
        # тейков), иначе глобальная из настроек
        self.tp_percents = self._resolve_tp_percents(signal.get("tp_percents"))

    def _resolve_tp_percents(self, from_signal) -> list:
        """Доли объёма по тейкам - ровно по одной на каждый тейк.

        Глобальная настройка рассчитана на четыре тейка, а каналы присылают
        и шесть. Если длина не совпала - делим поровну: это честнее, чем
        обрезать сигнал или оставить часть позиции без закрывающего ордера.
        """
        count = len(self.targets)
        for candidate in (from_signal, settings.get_tp_percents()):
            if candidate and len(candidate) == count:
                return [float(p) for p in candidate]
        return [100.0 / count] * count

    # ---------- Открытие ----------
    def set_leverage(self):
        try:
            get_session().set_leverage(
                category=config.CATEGORY,
                symbol=self.symbol,
                buyLeverage=str(self.leverage),
                sellLeverage=str(self.leverage),
            )
        except Exception as e:
            logger.warning(f"[{self.symbol}] Не удалось выставить плечо (возможно, уже установлено): {e}")

    def get_last_price(self) -> float:
        resp = get_session().get_tickers(category=config.CATEGORY, symbol=self.symbol)
        return float(resp["result"]["list"][0]["lastPrice"])

    def repair_thousands_separator(self, price: float) -> bool:
        """Исправляет цены вида ``62.806``, когда точка означает 62 806.

        Автокоррекция применяется только при очень явном разрыве масштаба:
        все уровни примерно в тысячу раз меньше рынка, после умножения
        оказываются рядом с ним и образуют корректную геометрию Long/Short.
        Обычные дробные цены вроде 0.5426 и 1.300 не затрагиваются.
        """
        levels = [float(self.initial_sl), *(float(target) for target in self.targets)]
        if not levels or any(level <= 0 for level in levels) or price <= 0:
            return False

        center = sorted(levels)[len(levels) // 2]
        ratio = price / center
        if not 100 <= ratio <= 2_000:
            return False

        scaled_sl = float(self.initial_sl) * 1_000
        scaled_targets = [float(target) * 1_000 for target in self.targets]
        scaled_center = center * 1_000
        if not 0.5 <= scaled_center / price <= 2.0:
            return False

        if self.side == "Buy":
            geometry_ok = scaled_sl < price and any(target > price for target in scaled_targets)
        else:
            geometry_ok = scaled_sl > price and any(target < price for target in scaled_targets)
        if not geometry_ok:
            return False

        before = [self.initial_sl, *self.targets]
        self.initial_sl = scaled_sl
        self.current_sl = scaled_sl
        self.targets = scaled_targets
        logger.warning(
            "[%s] Точка распознана как разделитель тысяч: уровни %s -> SL %s, TP %s",
            self.symbol,
            before,
            self.initial_sl,
            self.targets,
        )
        return True

    def validate_levels(self, price: float):
        """Проверяет геометрию сигнала по текущей цене - до отправки ордера.

        Вход идёт по рынку, поэтому сверяемся с ценой рынка, а не с зоной
        входа из сигнала. Стоп с той же стороны, что и прибыль, означает
        кривой парс: позиция откроется и мгновенно закроется по стопу, а
        движок ещё и попробует аварийно её закрыть - ошибочный сигнал
        обойдётся в две комиссии и проскальзывание. Дешевле не входить.
        """
        if self.side == "Buy":
            sl_wrong = self.initial_sl >= price
            passed = [t for t in self.targets if t <= price]
        else:
            sl_wrong = self.initial_sl <= price
            passed = [t for t in self.targets if t >= price]

        if sl_wrong:
            raise ValueError(
                f"стоп-лосс {self.initial_sl} с неверной стороны от цены {price} "
                f"для {self.strategy} - сигнал разобран неправильно"
            )
        if len(passed) == len(self.targets):
            raise ValueError(
                f"все тейки {self.targets} уже пройдены ценой {price} - "
                f"сигнал устарел или разобран неправильно"
            )
        if passed:
            # рынок ушёл вперёд: часть тейков закроется сразу по входу.
            # Это не повод пропускать сделку, но знать об этом надо
            logger.warning(f"[{self.symbol}] Тейки {passed} уже пройдены ценой {price} - "
                           f"эта часть позиции закроется сразу")

    def open_position(self):
        self.set_leverage()
        price = self.get_last_price()
        self.repair_thousands_separator(price)
        self.validate_levels(price)
        self.qty_total = calc_qty_from_margin(self.symbol, self.margin_usdt, self.leverage, price)
        notional = self.margin_usdt * self.leverage

        logger.info(f"[{self.symbol}] Открываю {self.side} маркет-ордером, "
                    f"qty={self.qty_total} (маржа {self.margin_usdt} USDT x плечо {self.leverage} "
                    f"= объём ~{notional} USDT по цене ~{price})")

        resp = get_session().place_order(
            category=config.CATEGORY,
            symbol=self.symbol,
            side=self.side,
            orderType="Market",
            qty=format_qty(self.qty_total),
            timeInForce="IOC",
            positionIdx=0,
        )
        logger.info(f"[{self.symbol}] Ответ на открытие позиции: {resp}")

        _time.sleep(1.5)
        self.entry_price = self._fetch_entry_price()
        logger.info(f"[{self.symbol}] Фактическая цена входа: {self.entry_price}")

    def _fetch_entry_price(self) -> float:
        resp = get_session().get_positions(category=config.CATEGORY, symbol=self.symbol)
        for pos in resp["result"]["list"]:
            if float(pos["size"]) > 0:
                return float(pos["avgPrice"])
        raise RuntimeError(f"[{self.symbol}] Открытая позиция не найдена после входа")

    def set_initial_stop_loss(self):
        sl_price = round_price(self.symbol, self.initial_sl)
        get_session().set_trading_stop(
            category=config.CATEGORY,
            symbol=self.symbol,
            stopLoss=sl_price,
            positionIdx=0,
        )
        self.current_sl = float(sl_price)
        logger.info(f"[{self.symbol}] Начальный стоп-лосс выставлен: {sl_price}")

    def _split_qty(self):
        info = SymbolInfo.get(self.symbol)
        step = info["qty_step"]
        remaining = self.qty_total
        qtys = []
        for i, pct in enumerate(self.tp_percents):
            if i == len(self.tp_percents) - 1:
                q = remaining
            else:
                raw = self.qty_total * Decimal(pct) / Decimal(100)
                q = round_step(raw, step)
                remaining -= q
            qtys.append(q)
        self.tp_qtys = qtys

    def place_take_profits(self):
        self._split_qty()
        for i, (target, qty) in enumerate(zip(self.targets, self.tp_qtys), start=1):
            if qty <= 0:
                continue
            price = round_price(self.symbol, target)
            resp = get_session().place_order(
                category=config.CATEGORY,
                symbol=self.symbol,
                side=self.close_side,
                orderType="Limit",
                qty=format_qty(qty),
                price=price,
                timeInForce="GTC",
                reduceOnly=True,
                positionIdx=0,
                orderLinkId=f"{self.symbol}-TP{i}-{int(_time.time())}",
            )
            order_id = resp["result"]["orderId"]
            self.tp_order_ids[order_id] = i
            logger.info(f"[{self.symbol}] TP{i} выставлен: price={price}, qty={qty}, orderId={order_id}")

    # ---------- Управление в процессе ----------
    def improves_stop(self, new_price: float) -> bool:
        """Двигает ли новый стоп в сторону прибыли.

        Для лонга стоп можно только поднимать, для шорта - только опускать.
        """
        if self.current_sl is None:
            return True
        return new_price > self.current_sl if self.side == "Buy" else new_price < self.current_sl

    def move_stop_loss(self, new_price: float, reason: str, allow_worse: bool = False) -> bool:
        """Переносит стоп, если это движение в сторону прибыли.

        Откат назад запрещён: тейки могут исполниться в один тик, когда цена
        прошила сразу несколько уровней, и события об этом приходят в
        произвольном порядке - Bybit не обещает их сортировку. Обработай бот
        TP2 раньше TP1, и TP1 стянул бы стоп с уровня TP1 обратно в точку
        входа, отдав уже зафиксированную прибыль.
        """
        if not allow_worse and not self.improves_stop(new_price):
            logger.warning(
                f"[{self.symbol}] SL {new_price} не лучше текущего {self.current_sl} "
                f"({reason}) - оставляю стоп на месте"
            )
            return False

        price_str = round_price(self.symbol, new_price)
        get_session().set_trading_stop(
            category=config.CATEGORY,
            symbol=self.symbol,
            stopLoss=price_str,
            positionIdx=0,
        )
        self.current_sl = float(price_str)
        logger.info(f"[{self.symbol}] SL перенесён на {price_str} ({reason})")
        return True

    def on_tp_filled(self, tp_index: int, engine_save_cb=None):
        if tp_index in self.tp_filled:
            return
        self.tp_filled.add(tp_index)
        logger.info(f"[{self.symbol}] TP{tp_index} исполнен")

        last = len(self.targets)
        if tp_index == 1:
            # После TP1 допускаем ровно один перенос стопа на -15% от входа.
            # Если TP2 уже пришёл раньше, защиту от отката сохраняем.
            multiplier = 0.85 if self.side == "Buy" else 1.15
            tp1_stop = self.entry_price * multiplier
            tp2_or_later_filled = any(index > 1 for index in self.tp_filled)
            self.move_stop_loss(
                tp1_stop,
                "TP1 достигнут -> перенос на -15% от цены входа",
                allow_worse=not tp2_or_later_filled,
            )
        elif tp_index < last:
            # дальше стоп идёт по пятам: на уровень предыдущего тейка
            self.move_stop_loss(self.targets[tp_index - 2],
                                f"TP{tp_index} достигнут -> перенос на уровень TP{tp_index - 1}")
        # последний TP: позиция закрыта целиком, двигать SL уже некуда

        price = self.targets[tp_index - 1]
        if tp_index < last:
            # сообщаем фактический стоп, а не запрошенный: перенос могли
            # отклонить как откат назад
            self.notifier(
                f"🎯 {self.symbol}: TP{tp_index} исполнен по {price}\n"
                f"🔄 SL перенесён на {self.current_sl}"
            )
        else:
            self.notifier(f"🎯 {self.symbol}: TP{tp_index} исполнен по {price}\n"
                          f"🏁 Сделка полностью закрыта")

        if engine_save_cb:
            engine_save_cb()

    def format_open_message(self) -> str:
        direction = "Long 📈" if self.side == "Buy" else "Short 📉"
        tp_lines = "\n".join(f"TP{i}: {t}" for i, t in enumerate(self.targets, start=1))
        notional = self.margin_usdt * self.leverage
        return (
            f"🟢 Открыта сделка\n"
            f"{self.symbol} {direction}\n"
            f"Вход: {self.entry_price}\n"
            f"Объём: {self.qty_total} (~{self.margin_usdt} USDT x{self.leverage} = ~{notional} USDT)\n"
            f"SL: {self.current_sl}\n"
            f"{tp_lines}"
        )

    # ---------- Сериализация состояния ----------
    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "side": self.side,
            "close_side": self.close_side,
            "targets": self.targets,
            "initial_sl": self.initial_sl,
            "current_sl": self.current_sl,
            "signal_id": self.signal_id,
            "entry_price": self.entry_price,
            "qty_total": format_qty(self.qty_total),
            "tp_qtys": [format_qty(q) for q in self.tp_qtys],
            "tp_order_ids": self.tp_order_ids,
            "tp_filled": sorted(self.tp_filled),
            "opened_at": self.opened_at,
            "opened_at_ms": self.opened_at_ms,
            "margin_usdt": self.margin_usdt,
            "leverage": self.leverage,
            "tp_percents": self.tp_percents,
        }

    @classmethod
    def from_dict(cls, data: dict, notifier=None) -> "TradeManager":
        signal = {
            "symbol": data["symbol"],
            "strategy": data["strategy"],
            "targets": data["targets"],
            "stop_loss": data["initial_sl"],
            "signal_id": data.get("signal_id"),
            "timestamp": data.get("opened_at"),
            "opened_at_ms": data.get("opened_at_ms"),
            "tp_percents": data.get("tp_percents"),
        }
        trade = cls(signal, notifier=notifier)
        trade.entry_price = data.get("entry_price")
        trade.qty_total = Decimal(data.get("qty_total", "0"))
        trade.tp_qtys = [Decimal(q) for q in data.get("tp_qtys", [])]
        trade.tp_order_ids = {k: v for k, v in data.get("tp_order_ids", {}).items()}
        trade.tp_filled = set(data.get("tp_filled", []))
        trade.current_sl = data.get("current_sl", data.get("initial_sl"))
        # параметры сделки на момент открытия - не подменяем текущими settings
        trade.margin_usdt = data.get("margin_usdt", trade.margin_usdt)
        trade.leverage = data.get("leverage", trade.leverage)
        trade.tp_percents = data.get("tp_percents", trade.tp_percents)
        return trade


# ==================== ДВИЖОК (с реконнектом WebSocket) ====================
WS_WATCHDOG_INTERVAL = 15
# Сколько проверок подряд сокет должен быть закрыт, прежде чем вмешиваться.
# pybit переподключается сам (restart_on_error=True), и рвать его попытку
# на полпути незачем - даём ему один интервал на самовосстановление.
WS_DOWN_CHECKS_BEFORE_RECONNECT = 2
WS_RECONNECT_DELAY = 5


class BotEngine:
    def __init__(self, notifier=None):
        self.trades: dict[str, TradeManager] = {}
        self.processed_signal_ids: set[str] = set()
        self.notifier = notifier or (lambda text: None)
        self.ws = None
        self._lock = threading.Lock()
        self._stop = False
        self._last_message_time = _time.time()

        # регистрируем себя как активный движок (нужен веб-панели и Telegram)
        global engine
        engine = self

    def notify(self, text: str):
        try:
            self.notifier(text)
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление: {e}")

    # ---------- Запуск ----------
    def start(self):
        synctime.start_background_sync()   # сверка часов с биржей до любых запросов, дальше в фоне
        self._load_state()
        self._reconcile_with_exchange()
        self._connect(demo=settings.is_demo())
        watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
        watchdog.start()
        logger.info("WebSocket: подписка на поток ордеров запущена, watchdog активен")

    def stop(self):
        self._stop = True
        status.set_flag("bybit_ws_connected", False)
        with self._lock:
            if self.ws is not None:
                try:
                    self.ws.exit()
                except Exception:
                    pass

    # ---------- Восстановление состояния после рестарта ----------
    def _load_state(self):
        for signal_id in _load_json_list(config.PROCESSED_SIGNALS_FILE):
            self.processed_signal_ids.add(signal_id)

        showcase_found = False
        for trade_data in _load_json_list(config.ACTIVE_TRADES_FILE):
            # Визуальный макет для демонстрации панели не является позицией на
            # бирже. После перезапуска просто убираем его, не сверяем с Bybit и
            # не записываем в историю как якобы закрытую сделку.
            if trade_data.get("showcase_fake"):
                showcase_found = True
                logger.info("Демонстрационная fake-сделка удалена при запуске")
                continue
            try:
                trade = TradeManager.from_dict(trade_data, notifier=self.notify)
                self.trades[trade.symbol] = trade
                logger.info(f"[{trade.symbol}] Сделка восстановлена из {config.ACTIVE_TRADES_FILE} после рестарта")
            except Exception as e:
                logger.warning(f"Не удалось восстановить сделку из состояния: {e}")

        if showcase_found:
            self._save_active_trades()

    def _reconcile_with_exchange(self):
        """Сверяет восстановленные сделки с реальными позициями на бирже.

        Пока бот лежал (падение, рестарт, перезагрузка ПК), позиция могла
        закрыться по стопу или последнему тейку - событие об этом получать
        было некому. Без сверки такая сделка остаётся «активной» навсегда:
        движок ждёт событий, которых уже не будет, а новые сигналы по символу
        отклоняются с «по символу уже есть активная сделка».
        """
        if not self.trades:
            return

        try:
            resp = get_session().get_positions(category=config.CATEGORY, settleCoin="USDT")
            live = {p.get("symbol") for p in resp["result"]["list"]
                    if float(p.get("size") or 0) > 0}
        except Exception as e:
            # не смогли спросить биржу - оставляем как есть: потерять
            # сопровождение живой позиции хуже, чем передержать закрытую
            logger.warning(f"Не удалось сверить восстановленные сделки с биржей: {e}")
            return

        for symbol in list(self.trades):
            if symbol in live:
                logger.info(f"[{symbol}] Позиция на бирже на месте, сопровождение продолжается")
                continue
            logger.warning(f"[{symbol}] Позиции на бирже нет - сделка закрылась, "
                           f"пока бот был выключен")
            self._on_trade_closed(symbol, reason="закрыта, пока бот был выключен")

    def _save_active_trades(self):
        with _state_lock:
            _save_json_list(config.ACTIVE_TRADES_FILE, [t.to_dict() for t in self.trades.values()])

    def _mark_signal_processed(self, signal_id: str):
        if not signal_id:
            return
        self.processed_signal_ids.add(signal_id)
        with _state_lock:
            ids = _load_json_list(config.PROCESSED_SIGNALS_FILE)
            ids.append(signal_id)
            # держим только последние 500, чтобы файл не рос бесконечно
            _save_json_list(config.PROCESSED_SIGNALS_FILE, ids[-500:])

    # ---------- Подключение / переподключение ----------
    def _create_ws(self, demo=None) -> WebSocket:
        if demo is None:
            demo = settings.is_demo()
        ws = WebSocket(
            testnet=config.TESTNET,
            demo=demo,
            channel_type="private",
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
            ping_interval=20,
            ping_timeout=10,
            restart_on_error=True,
            retries=10,
        )
        ws.order_stream(callback=self._handle_order_event)
        ws.position_stream(callback=self._handle_position_event)
        return ws

    def _connect(self, demo=None):
        while not self._stop:
            try:
                logger.info("WebSocket: устанавливаю соединение...")
                new_ws = self._create_ws(demo=demo)
                with self._lock:
                    self.ws = new_ws
                self._last_message_time = _time.time()
                status.set_flag("bybit_ws_connected", True)
                logger.info("WebSocket: соединение установлено, подписка активна")
                return
            except Exception as e:
                logger.error(f"WebSocket: ошибка подключения ({e}), повтор через {WS_RECONNECT_DELAY} сек")
                _time.sleep(WS_RECONNECT_DELAY)

    def _reconnect(self, demo=None):
        logger.warning("WebSocket: пересоздаю соединение...")
        status.set_flag("bybit_ws_connected", False)
        # Пока сокет лежит, исполнение тейков до нас не доходит: стоп не
        # переставляется после тейков и не идёт по лестнице. Молчать об этом нельзя
        self.notify("⚠️ Связь с Bybit потеряна, переподключаюсь.\n"
                    "Позиции на бирже остаются со стопами и тейками, но перенос "
                    "стопа за тейками пока приостановлен.")
        with self._lock:
            old_ws = self.ws
            self.ws = None
        if old_ws is not None:
            try:
                old_ws.exit()
            except Exception as e:
                logger.warning(f"WebSocket: ошибка при закрытии старого соединения: {e}")
        self._connect(demo=demo)
        if not self._stop:
            self.notify("✅ Связь с Bybit восстановлена, сопровождение сделок продолжается.")

    def _watchdog_loop(self):
        """Следит за живостью сокета.

        Тишина в приватном канале - это норма, а не обрыв: события приходят
        только по нашим ордерам и позициям, и на спокойном счёте их может не
        быть часами. Раньше watchdog принимал молчание за обрыв и рвал живое
        соединение каждые 90 секунд; в паузе переподключения терялись
        исполнения тейков, а восстановить их потом некому - сверки с биржей
        нет. Признак обрыва - закрытый сокет: свои ping'и pybit шлёт сам и до
        наших колбэков не доводит, поэтому спрашиваем состояние напрямую.
        """
        down_checks = 0
        while not self._stop:
            _time.sleep(WS_WATCHDOG_INTERVAL)

            with self._lock:
                ws = self.ws

            alive = False
            if ws is not None:
                try:
                    alive = bool(ws.is_connected())
                except Exception as e:
                    logger.warning(f"WebSocket: не удалось проверить состояние сокета: {e}")

            if alive:
                down_checks = 0
                status.set_flag("bybit_ws_connected", True)
                continue

            status.set_flag("bybit_ws_connected", False)
            down_checks += 1
            if down_checks < WS_DOWN_CHECKS_BEFORE_RECONNECT:
                logger.warning("WebSocket: сокет закрыт, жду самовосстановления pybit...")
                continue

            down_checks = 0
            quiet = int(_time.time() - self._last_message_time)
            logger.warning(f"WebSocket: сокет закрыт (событий не было {quiet} сек) - пересоздаю")
            self._reconnect()

    # ---------- Обработка нового сигнала ----------
    def process_signal(self, signal: dict):
        symbol = signal["symbol"]
        signal_id = signal.get("signal_id")

        if not settings.is_trading_enabled():
            logger.info(f"[{symbol}] Торговля приостановлена в настройках сайта - сигнал пропущен")
            return

        if signal_id and signal_id in self.processed_signal_ids:
            logger.info(f"[{symbol}] Сигнал {signal_id} уже обработан ранее, пропускаю")
            return

        # Защита от слива: считаем открытые сделки и спрашиваем риск-менеджер.
        # Баланс тянем с биржи только если включён лимит убытка в процентах -
        # чтобы не делать лишний HTTP-запрос на каждый сигнал.
        with self._lock:
            open_count = len(self.trades)

        equity = None
        if risk.get_settings().get("daily_loss_percent_enabled"):
            equity = get_wallet_balance().get("equity") or None

        allowed, reason = risk.check_can_open(symbol, open_count, equity)
        if not allowed:
            logger.warning(f"[{symbol}] Сигнал отклонён защитой от слива: {reason}")
            self.notify(f"🛡 Сигнал {symbol} отклонён: {reason}")
            return

        with self._lock:
            if symbol in self.trades:
                logger.info(f"[{symbol}] По символу уже есть активная сделка, новый сигнал игнорируется")
                return
            # регистрируем сразу под локом, чтобы параллельные сигналы не открыли дубль
            trade = TradeManager(signal, notifier=self.notify)
            self.trades[symbol] = trade

        try:
            trade.open_position()
            trade.set_initial_stop_loss()
            trade.place_take_profits()

            self._mark_signal_processed(signal_id)
            self._save_active_trades()
            self.notify(trade.format_open_message())
        except Exception as e:
            logger.exception(f"[{symbol}] Ошибка обработки сигнала: {e}")
            # Вход мог уже пройти - тогда на бирже висит позиция с плечом,
            # без стоп-лосса и без тейков, и вести её уже некому.
            # Оставлять такое нельзя: закрываем по рынку.
            closed = self._close_after_failure(trade)
            with self._lock:
                self.trades.pop(symbol, None)
            if closed:
                self.notify(
                    f"❌ Ошибка при открытии сделки {symbol}: {e}\n"
                    f"⚠️ Позиция была открыта и аварийно закрыта по рынку."
                )
            else:
                self.notify(f"❌ Ошибка при открытии сделки {symbol}: {e}")

    def _close_after_failure(self, trade) -> bool:
        """Закрывает позицию, если вход прошёл, а стоп или тейки не встали.

        Возвращает True, если позиция реально была и её закрыли.
        """
        symbol = trade.symbol
        try:
            resp = get_session().get_positions(category=config.CATEGORY, symbol=symbol)
            items = resp["result"]["list"]
        except Exception as e:
            logger.exception(f"[{symbol}] Откат: не удалось проверить позицию: {e}")
            self.notify(f"‼️ {symbol}: проверьте биржу вручную - позиция могла остаться открытой")
            return False

        size = Decimal("0")
        for pos in items:
            try:
                candidate = Decimal(str(pos.get("size") or "0"))
            except Exception:
                candidate = Decimal("0")
            if candidate > 0:
                size = candidate
                break

        if size <= 0:
            return False        # вход не прошёл - закрывать нечего

        # снимаем тейки, если часть успела встать, иначе они останутся висеть
        try:
            get_session().cancel_all_orders(category=config.CATEGORY, symbol=symbol)
        except Exception as e:
            logger.warning(f"[{symbol}] Откат: не удалось снять ордера: {e}")

        try:
            get_session().place_order(
                category=config.CATEGORY,
                symbol=symbol,
                side=trade.close_side,
                orderType="Market",
                qty=format_qty(size),
                timeInForce="IOC",
                reduceOnly=True,
                positionIdx=0,
            )
            logger.warning(f"[{symbol}] Откат: позиция {size} закрыта по рынку "
                           f"(сделку не удалось настроить полностью)")
            return True
        except Exception as e:
            logger.exception(f"[{symbol}] Откат: НЕ УДАЛОСЬ закрыть позицию: {e}")
            self.notify(f"‼️ {symbol}: позиция открыта, но её не удалось "
                        f"ни настроить, ни закрыть - вмешайтесь вручную!")
            return False

    # ---------- Emergency Stop: закрыть всё по рынку ----------
    def close_all_positions(self) -> dict:
        """Снимает все ордера и закрывает все позиции по рынку.

        Вызывается кнопкой Emergency Stop. Работает по фактическим позициям на
        бирже, а не только по self.trades - если состояние разъехалось, всё
        равно закрываем всё, что реально открыто. Сам флаг emergency_stop
        ставит вызывающая сторона (webapp/tg_bot), чтобы блокировка встала
        до закрытия, а не после.
        """
        closed, errors = [], []

        try:
            positions = get_session().get_positions(category=config.CATEGORY, settleCoin="USDT")
            items = positions["result"]["list"]
        except Exception as e:
            logger.exception(f"Emergency Stop: не удалось получить список позиций: {e}")
            return {"closed": [], "errors": [f"не удалось получить позиции: {e}"]}

        for pos in items:
            symbol = pos.get("symbol")
            try:
                size = Decimal(str(pos.get("size") or "0"))
            except Exception:
                size = Decimal("0")
            if size <= 0:
                continue

            try:
                # сначала снимаем лимитные TP - иначе после закрытия они
                # останутся висеть и могут открыть позицию в обратную сторону
                try:
                    get_session().cancel_all_orders(category=config.CATEGORY, symbol=symbol)
                except Exception as e:
                    logger.warning(f"[{symbol}] Emergency Stop: не удалось снять ордера: {e}")

                close_side = "Sell" if pos.get("side") == "Buy" else "Buy"
                get_session().place_order(
                    category=config.CATEGORY,
                    symbol=symbol,
                    side=close_side,
                    orderType="Market",
                    qty=format_qty(size),
                    timeInForce="IOC",
                    reduceOnly=True,
                    positionIdx=0,
                )
                closed.append(symbol)
                logger.warning(f"[{symbol}] Emergency Stop: позиция закрыта по рынку ({size})")
                # Состояние чистим сами, не дожидаясь события WebSocket:
                # Emergency Stop жмут как раз тогда, когда с соединением
                # что-то не так, и молчащий сокет оставил бы сделку навсегда
                # активной - новые сигналы по символу отклонялись бы с
                # "уже есть активная сделка" до ручной правки JSON
                self._on_trade_closed(symbol, reason="Emergency Stop")
            except Exception as e:
                errors.append(f"{symbol}: {e}")
                logger.exception(f"[{symbol}] Emergency Stop: ошибка закрытия: {e}")

        # Сделки без позиции на бирже: она закрылась раньше, а событие о
        # закрытии до нас не дошло. После Emergency Stop счёт плоский, так
        # что вести их больше нечего
        for symbol in list(self.trades):
            if symbol not in closed:
                logger.warning(f"[{symbol}] Emergency Stop: позиции на бирже нет, "
                               f"снимаю сделку с сопровождения")
                self._on_trade_closed(symbol, reason="Emergency Stop (позиции на бирже не было)")

        if closed:
            self.notify("🛑 EMERGENCY STOP\nЗакрыты по рынку: " + ", ".join(closed))
        else:
            self.notify("🛑 EMERGENCY STOP\nОткрытых позиций не было, торговля заблокирована")
        if errors:
            self.notify("⚠️ Emergency Stop, ошибки закрытия:\n" + "\n".join(errors))

        return {"closed": closed, "errors": errors}

    # ---------- Обработка событий биржи ----------
    def _handle_order_event(self, message):
        self._last_message_time = _time.time()
        try:
            for item in message.get("data") or []:
                if item.get("orderStatus") != "Filled":
                    continue

                symbol = item.get("symbol")
                order_id = item.get("orderId")

                trade = self.trades.get(symbol)
                if not trade:
                    continue

                tp_index = trade.tp_order_ids.get(order_id)
                if tp_index:
                    trade.on_tp_filled(tp_index, engine_save_cb=self._save_active_trades)
        except Exception as e:
            logger.exception(f"Ошибка обработки события ордера: {e}")

    def _handle_position_event(self, message):
        self._last_message_time = _time.time()
        try:
            for item in message.get("data") or []:
                symbol = item.get("symbol")
                trade = self.trades.get(symbol)
                if not trade:
                    continue

                # Закрываем сделку только если биржа реально прислала размер.
                # Обрезанное событие без "size" раньше читалось как ноль - бот
                # записывал сделку в историю и переставал её вести, хотя позиция
                # и тейки оставались на бирже.
                raw_size = item.get("size")
                if raw_size in (None, ""):
                    continue

                size = float(raw_size)
                if size != 0:
                    continue

                # Сделка попадает в self.trades ещё до маркет-ордера, а вход
                # подтверждается примерно через полсекунды (set_leverage,
                # get_last_price, сам ордер). Снимок позиций биржа шлёт при
                # каждой подписке и переподключении, и по символу без позиции
                # он приходит с size=0. Раньше такой снимок, попав в это окно,
                # записывал ещё не открытую сделку в историю как закрытую по
                # стоп-лоссу: позиция потом висела на бирже без сопровождения -
                # стоп не переставлялся после тейков и не шёл по лестнице за
                # тейками, а в истории оставался фальшивый убыток, который
                # риск-менеджер считал за серию проигрышей.
                if trade.entry_price is None:
                    logger.info(f"[{symbol}] size=0 до подтверждения входа - "
                                f"это не закрытие позиции, игнорирую")
                    continue

                self._on_trade_closed(symbol)
        except Exception as e:
            logger.exception(f"Ошибка обработки события позиции: {e}")

    def _on_trade_closed(self, symbol: str, reason: str | None = None):
        """Снимает сделку с сопровождения и пишет её в историю.

        Идемпотентна: сделку забирает из self.trades под локом, поэтому
        повторный вызов (событие WebSocket пришло уже после Emergency Stop)
        ничего не делает и второй записи в историю не создаёт.

        reason задаётся, когда причина известна заранее (Emergency Stop).
        Иначе она выводится по тейкам: исполнен последний - полный
        тейк-профит, нет - значит сработал стоп.
        """
        with self._lock:
            trade = self.trades.pop(symbol, None)
        if trade is None:
            return

        logger.info(f"[{symbol}] Позиция полностью закрыта")
        self._save_active_trades()

        # даём Bybit секунду-полторы, чтобы запись closedPnl успела появиться
        _time.sleep(1.5)
        realized_pnl = get_realized_pnl(symbol, trade.opened_at_ms)

        # Полный тейк-профит - это исполненный последний TP, а их не всегда
        # четыре: у Fat Pig шесть
        last_tp = len(trade.targets)
        took_all = last_tp in trade.tp_filled
        announce_stop = reason is None and not took_all
        if reason is None:
            reason = f"TP{last_tp} (полный тейк-профит)" if took_all else "стоп-лосс"
        record = trade.to_dict()
        record["closed_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        record["close_reason"] = reason
        record["realized_pnl"] = realized_pnl
        _append_json_list(config.TRADE_HISTORY_FILE, record)

        pnl_line = f"\nPnL: {realized_pnl:+.2f} USDT" if realized_pnl is not None else ""

        if announce_stop:
            direction = "Long 📈" if trade.side == "Buy" else "Short 📉"
            self.notify(
                f"🔴 {symbol} {direction}: сделка закрыта по стоп-лоссу ({trade.current_sl}){pnl_line}"
            )
        elif pnl_line:
            self.notify(f"💰 {symbol}: итоговый PnL сделки{pnl_line}")
