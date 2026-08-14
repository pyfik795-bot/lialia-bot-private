"""Тесты обработки событий приватного WebSocket (trade_engine.py).

Запуск:  python -m unittest test_trade_engine -v

Сеть не используется: события подаются в обработчики напрямую, ровно в том
виде, в каком их присылает Bybit.
"""

import json
import logging
import os
import shutil
import tempfile
import unittest
from decimal import Decimal

import config
import trade_engine

# Движок штатно предупреждает о том, что мы здесь и проверяем (откат стопа,
# сделка без позиции). В выводе тестов эти строки выглядят как сбои, поэтому
# не пускаем их к корневому логгеру - assertLogs слушает логгер напрямую и
# продолжает работать
_engine_log = logging.getLogger("trade_engine")
_engine_log.addHandler(logging.NullHandler())   # иначе logging пишет в stderr сам
_engine_log.propagate = False

SIGNAL = {
    "symbol": "BTCUSDT",
    "strategy": "Long",
    "targets": [61000.0, 62000.0, 63000.0, 64000.0],
    "stop_loss": 58000.0,
    "signal_id": "test-1",
}


def position_event(symbol: str, size: str | None) -> dict:
    """Событие потока position. size=None - поле вообще отсутствует."""
    item = {"symbol": symbol}
    if size is not None:
        item["size"] = size
    return {"topic": "position", "data": [item]}


class PositionEventTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = trade_engine.BotEngine(notifier=lambda _t: None)
        self.trade = trade_engine.TradeManager(SIGNAL, notifier=lambda _t: None)
        self.engine.trades["BTCUSDT"] = self.trade

        # _on_trade_closed пишет файлы, спит и ходит в сеть за PnL -
        # проверяем само решение «закрывать или нет», а не его последствия
        self.closed = []
        self.engine._on_trade_closed = self.closed.append

    def test_snapshot_before_entry_does_not_close_trade(self):
        """Снимок позиций при подписке не должен хоронить открывающуюся сделку.

        Между регистрацией сделки и подтверждением входа биржа присылает по
        символу size=0, потому что позиции ещё нет. Раньше движок считал это
        закрытием, переставал вести сделку и писал в историю ложный стоп-лосс,
        а позиция оставалась на бирже без сопровождения.
        """
        self.assertIsNone(self.trade.entry_price, "предусловие: вход ещё не подтверждён")

        self.engine._handle_position_event(position_event("BTCUSDT", "0"))

        self.assertEqual(self.closed, [], "сделка закрыта до того, как открылась")
        self.assertIn("BTCUSDT", self.engine.trades, "сделка потеряна из отслеживания")

    def test_zero_size_closes_trade_after_entry_confirmed(self):
        """После подтверждённого входа size=0 - это реальное закрытие."""
        self.trade.entry_price = 63390.1

        self.engine._handle_position_event(position_event("BTCUSDT", "0"))

        self.assertEqual(self.closed, ["BTCUSDT"])

    def test_event_without_size_is_ignored(self):
        """Обрезанное событие без size раньше читалось как ноль."""
        self.trade.entry_price = 63390.1

        self.engine._handle_position_event(position_event("BTCUSDT", None))

        self.assertEqual(self.closed, [])

    def test_nonzero_size_keeps_trade(self):
        self.trade.entry_price = 63390.1

        self.engine._handle_position_event(position_event("BTCUSDT", "0.006"))

        self.assertEqual(self.closed, [])

    def test_event_for_unknown_symbol_is_ignored(self):
        self.engine._handle_position_event(position_event("ETHUSDT", "0"))

        self.assertEqual(self.closed, [])

    def test_entry_price_survives_restart(self):
        """Признак «вход подтверждён» обязан переживать перезапуск бота.

        Иначе после рестарта восстановленная сделка снова выглядит как
        неоткрытая, и первый же снимок позиций перестанет её закрывать.
        """
        self.trade.entry_price = 63390.1
        restored = trade_engine.TradeManager.from_dict(self.trade.to_dict())

        self.assertEqual(restored.entry_price, 63390.1)


class FakeSession:
    """Минимальная замена pybit.HTTP для проверки Emergency Stop."""

    def __init__(self, positions):
        self.positions = positions
        self.placed = []
        self.cancelled = []
        self.stops = []

    def set_trading_stop(self, **kwargs):
        self.stops.append(kwargs)
        return {"retCode": 0}

    def get_positions(self, **kwargs):
        return {"result": {"list": self.positions}}

    def cancel_all_orders(self, **kwargs):
        self.cancelled.append(kwargs.get("symbol"))
        return {"retCode": 0}

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"retCode": 0, "result": {"orderId": "x"}}

    def get_closed_pnl(self, **kwargs):
        return {"result": {"list": []}}


class NoSleep:
    """Подменяет модуль time внутри движка: без полуторасекундных пауз."""
    sleep = staticmethod(lambda _s: None)

    def __getattr__(self, name):
        import time
        return getattr(time, name)


class EmergencyStopTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (config.ACTIVE_TRADES_FILE, config.TRADE_HISTORY_FILE, trade_engine._time)
        config.ACTIVE_TRADES_FILE = os.path.join(self.tmp, "active.json")
        config.TRADE_HISTORY_FILE = os.path.join(self.tmp, "history.json")
        trade_engine._time = NoSleep()
        self.addCleanup(self._restore)

        self.engine = trade_engine.BotEngine(notifier=lambda _t: None)
        self.trade = trade_engine.TradeManager(SIGNAL, notifier=lambda _t: None)
        self.trade.entry_price = 63384.7
        self.engine.trades["BTCUSDT"] = self.trade

    def _restore(self):
        (config.ACTIVE_TRADES_FILE, config.TRADE_HISTORY_FILE, trade_engine._time) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def history(self) -> list:
        if not os.path.exists(config.TRADE_HISTORY_FILE):
            return []
        with open(config.TRADE_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)

    def test_clears_trade_without_waiting_for_websocket(self):
        """Сделка обязана сняться с сопровождения самим Emergency Stop.

        Раньше это делало только событие приватного сокета. Но Emergency Stop
        жмут как раз при проблемах со связью: позиция на бирже закрывалась, а
        сделка навсегда оставалась активной и блокировала новые сигналы по
        символу - до ручной правки active_trades.json.
        """
        trade_engine._session = FakeSession([
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0.006"},
        ])

        result = self.engine.close_all_positions()

        self.assertEqual(result["closed"], ["BTCUSDT"])
        self.assertEqual(result["errors"], [])
        self.assertNotIn("BTCUSDT", self.engine.trades, "сделка осталась активной после закрытия")
        self.assertEqual([r["close_reason"] for r in self.history()], ["Emergency Stop"])

    def test_clears_trade_that_has_no_position_on_exchange(self):
        """Позиция закрылась раньше, событие потерялось - сделка всё равно уходит."""
        trade_engine._session = FakeSession([])

        result = self.engine.close_all_positions()

        self.assertEqual(result["closed"], [])
        self.assertNotIn("BTCUSDT", self.engine.trades)

    def test_websocket_event_after_emergency_stop_is_harmless(self):
        """Запоздавшее событие не должно писать вторую запись в историю."""
        trade_engine._session = FakeSession([
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0.006"},
        ])
        self.engine.close_all_positions()

        self.engine._handle_position_event(position_event("BTCUSDT", "0"))

        self.assertEqual(len(self.history()), 1, "сделка попала в историю дважды")

    def test_stop_loss_reason_still_inferred_when_not_given(self):
        """Обычное закрытие по-прежнему само определяет причину."""
        trade_engine._session = FakeSession([])
        self.engine._on_trade_closed("BTCUSDT")

        self.assertEqual([r["close_reason"] for r in self.history()], ["стоп-лосс"])


class StopLossLadderTestCase(unittest.TestCase):
    """Лестница стопа: он идёт за тейками и никогда не откатывается назад."""

    def setUp(self):
        # подменяем только сеть - логика move_stop_loss гоняется настоящая,
        # иначе тест переставал бы замечать снятие защиты от отката
        trade_engine.SymbolInfo._cache["BTCUSDT"] = {
            "qty_step": Decimal("0.001"),
            "min_qty": Decimal("0.001"),
            "tick_size": Decimal("0.1"),
        }
        self.addCleanup(trade_engine.SymbolInfo._cache.pop, "BTCUSDT", None)

        self.session = FakeSession([])
        self._saved_session = trade_engine._session
        trade_engine._session = self.session
        self.addCleanup(lambda: setattr(trade_engine, "_session", self._saved_session))

    @property
    def moves(self) -> list:
        """Стопы, реально отправленные на биржу."""
        return [float(call["stopLoss"]) for call in self.session.stops]

    def make_trade(self, strategy="Long") -> trade_engine.TradeManager:
        signal = dict(SIGNAL, strategy=strategy)
        if strategy == "Short":
            signal["targets"] = [59000.0, 58000.0, 57000.0, 56000.0]
            signal["stop_loss"] = 62000.0
        trade = trade_engine.TradeManager(signal, notifier=lambda _t: None)
        trade.entry_price = 60000.0
        return trade

    def test_ladder_in_order(self):
        trade = self.make_trade()

        trade.on_tp_filled(1)
        trade.on_tp_filled(2)
        trade.on_tp_filled(3)

        # TP1 -> безубыток, TP2 -> уровень TP1, TP3 -> уровень TP2
        self.assertEqual(self.moves, [60000.0, 61000.0, 62000.0])
        self.assertEqual(trade.current_sl, 62000.0)

    def test_out_of_order_fills_never_pull_stop_back(self):
        """TP2 обработан раньше TP1 - стоп обязан остаться на уровне TP1.

        Цена может прошить оба уровня в один тик; порядок событий Bybit не
        гарантирует. Раньше запоздавший TP1 стягивал стоп обратно в точку
        входа и отдавал уже зафиксированную прибыль.
        """
        trade = self.make_trade()

        trade.on_tp_filled(2)          # стоп -> уровень TP1 (61000)
        with self.assertLogs("trade_engine", level="WARNING") as logs:
            trade.on_tp_filled(1)      # запоздавший TP1 просит вернуть в 60000

        self.assertIn("оставляю стоп на месте", "\n".join(logs.output))
        self.assertEqual(trade.current_sl, 61000.0, "стоп откатился назад к точке входа")
        self.assertEqual(self.moves, [61000.0], "на биржу ушёл лишний перенос стопа")

    def test_out_of_order_fills_for_short(self):
        """Для шорта «в сторону прибыли» - это вниз."""
        trade = self.make_trade("Short")

        trade.on_tp_filled(2)          # стоп -> уровень TP1 (59000)
        with self.assertLogs("trade_engine", level="WARNING"):
            trade.on_tp_filled(1)      # запоздавший TP1 просит вернуть в 60000

        self.assertEqual(trade.current_sl, 59000.0)

    def test_duplicate_fill_is_ignored(self):
        trade = self.make_trade()

        trade.on_tp_filled(1)
        trade.on_tp_filled(1)

        self.assertEqual(self.moves, [60000.0])

    def test_last_tp_does_not_move_stop(self):
        trade = self.make_trade()

        trade.on_tp_filled(4)

        self.assertEqual(self.moves, [])


class ValidateLevelsTestCase(unittest.TestCase):
    """Геометрия сигнала проверяется до отправки ордера, а не после."""

    def make_trade(self, strategy="Long", **over) -> trade_engine.TradeManager:
        signal = dict(SIGNAL, strategy=strategy, **over)
        return trade_engine.TradeManager(signal, notifier=lambda _t: None)

    def test_healthy_long_passes(self):
        self.make_trade().validate_levels(60000.0)

    def test_healthy_short_passes(self):
        self.make_trade("Short", targets=[59000.0, 58000.0, 57000.0, 56000.0],
                        stop_loss=62000.0).validate_levels(60000.0)

    def test_long_with_stop_above_price_is_rejected(self):
        """Стоп выше цены для лонга сработал бы сразу после входа."""
        trade = self.make_trade(stop_loss=61000.0)

        with self.assertRaises(ValueError) as ctx:
            trade.validate_levels(60000.0)
        self.assertIn("стоп-лосс", str(ctx.exception))

    def test_short_with_stop_below_price_is_rejected(self):
        trade = self.make_trade("Short", targets=[59000.0, 58000.0, 57000.0, 56000.0],
                                stop_loss=59500.0)

        with self.assertRaises(ValueError):
            trade.validate_levels(60000.0)

    def test_fully_passed_targets_are_rejected(self):
        """Все тейки позади цены - сигнал устарел, входить некуда."""
        trade = self.make_trade()

        with self.assertRaises(ValueError) as ctx:
            trade.validate_levels(65000.0)
        self.assertIn("тейки", str(ctx.exception))

    def test_partially_passed_targets_still_open(self):
        """Рынок ушёл за TP1 - сделку берём, но предупреждаем."""
        trade = self.make_trade()

        with self.assertLogs("trade_engine", level="WARNING") as logs:
            trade.validate_levels(61500.0)
        self.assertIn("уже пройдены", "\n".join(logs.output))

    def test_dotted_thousands_are_repaired_for_btc(self):
        signal = {
            "symbol": "BTCUSDT",
            "strategy": "Long",
            "targets": [70.806, 75.806, 80.806, 85.806],
            "stop_loss": 61.806,
            "signal_id": "dotted-thousands",
        }
        trade = trade_engine.TradeManager(signal, notifier=lambda _t: None)

        with self.assertLogs("trade_engine", level="WARNING"):
            repaired = trade.repair_thousands_separator(62847.1)
        trade.validate_levels(62847.1)

        self.assertTrue(repaired)
        self.assertEqual(trade.initial_sl, 61806.0)
        self.assertEqual(trade.targets, [70806.0, 75806.0, 80806.0, 85806.0])

    def test_normal_fractional_prices_are_not_scaled(self):
        signal = {
            "symbol": "ACEUSDT",
            "strategy": "Long",
            "targets": [1.300, 1.350, 1.400, 1.450],
            "stop_loss": 1.100,
            "signal_id": "normal-decimals",
        }
        trade = trade_engine.TradeManager(signal, notifier=lambda _t: None)

        repaired = trade.repair_thousands_separator(1.25)
        trade.validate_levels(1.25)

        self.assertFalse(repaired)
        self.assertEqual(trade.targets[0], 1.3)


class ReconcileTestCase(unittest.TestCase):
    """Сверка восстановленных сделок с биржей при старте."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (config.ACTIVE_TRADES_FILE, config.TRADE_HISTORY_FILE, trade_engine._time)
        config.ACTIVE_TRADES_FILE = os.path.join(self.tmp, "active.json")
        config.TRADE_HISTORY_FILE = os.path.join(self.tmp, "history.json")
        trade_engine._time = NoSleep()
        self.addCleanup(self._restore)

        self.engine = trade_engine.BotEngine(notifier=lambda _t: None)
        trade = trade_engine.TradeManager(SIGNAL, notifier=lambda _t: None)
        trade.entry_price = 60000.0
        self.engine.trades["BTCUSDT"] = trade

    def _restore(self):
        (config.ACTIVE_TRADES_FILE, config.TRADE_HISTORY_FILE, trade_engine._time) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_trade_closed_while_bot_was_down_is_dropped(self):
        """Иначе сделка вечно «активна» и блокирует новые сигналы по символу."""
        trade_engine._session = FakeSession([])

        self.engine._reconcile_with_exchange()

        self.assertNotIn("BTCUSDT", self.engine.trades)

    def test_live_position_keeps_being_tracked(self):
        trade_engine._session = FakeSession([
            {"symbol": "BTCUSDT", "side": "Buy", "size": "0.006"},
        ])

        self.engine._reconcile_with_exchange()

        self.assertIn("BTCUSDT", self.engine.trades)

    def test_unreachable_exchange_keeps_trade(self):
        """Биржа недоступна - сделку не трогаем: потерять живую хуже."""
        class Broken(FakeSession):
            def get_positions(self, **kwargs):
                raise RuntimeError("сеть недоступна")

        trade_engine._session = Broken([])

        with self.assertLogs("trade_engine", level="WARNING"):
            self.engine._reconcile_with_exchange()

        self.assertIn("BTCUSDT", self.engine.trades)


class ShowcaseStateTestCase(unittest.TestCase):
    """Визуальная fake-сделка не должна превращаться в биржевую позицию."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (config.ACTIVE_TRADES_FILE, config.PROCESSED_SIGNALS_FILE)
        config.ACTIVE_TRADES_FILE = os.path.join(self.tmp, "active.json")
        config.PROCESSED_SIGNALS_FILE = os.path.join(self.tmp, "processed.json")
        self.addCleanup(self._restore)

    def _restore(self):
        config.ACTIVE_TRADES_FILE, config.PROCESSED_SIGNALS_FILE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_showcase_trade_is_removed_without_loading(self):
        with open(config.ACTIVE_TRADES_FILE, "w", encoding="utf-8") as file:
            json.dump([{"symbol": "FAKEUSDT", "showcase_fake": True}], file)

        engine = trade_engine.BotEngine(notifier=lambda _t: None)
        engine._load_state()

        self.assertEqual(engine.trades, {})
        with open(config.ACTIVE_TRADES_FILE, "r", encoding="utf-8") as file:
            self.assertEqual(json.load(file), [])


class OrderEventTestCase(unittest.TestCase):
    """Поток ордеров смотрит только на свои orderId - чужие события не трогают сделку."""

    def setUp(self):
        self.engine = trade_engine.BotEngine(notifier=lambda _t: None)
        self.trade = trade_engine.TradeManager(SIGNAL, notifier=lambda _t: None)
        self.engine.trades["BTCUSDT"] = self.trade

    def test_foreign_filled_order_does_not_touch_trade(self):
        self.engine._handle_order_event({
            "topic": "order",
            "data": [{"symbol": "BTCUSDT", "orderId": "chuzhoy-order", "orderStatus": "Filled"}],
        })

        self.assertEqual(self.trade.tp_filled, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
