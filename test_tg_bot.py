"""Тесты вывода Telegram-бота (tg_bot.py).

Запуск:  python -m unittest test_tg_bot -v

Сеть и Telegram не задействованы: проверяются чистые функции форматирования
на подложенных файлах состояния.
"""

import json
import os
import shutil
import tempfile
import unittest

import config


class TgBotTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = {
            "active": config.ACTIVE_TRADES_FILE,
            "history": config.TRADE_HISTORY_FILE,
            "channels": config.CHANNELS_FILE,
            "demo": config.DEMO,
        }
        config.ACTIVE_TRADES_FILE = os.path.join(self.tmp, "active.json")
        config.TRADE_HISTORY_FILE = os.path.join(self.tmp, "history.json")
        config.CHANNELS_FILE = os.path.join(self.tmp, "channels.json")
        self.addCleanup(self._restore)

        import tg_bot
        self.tg = tg_bot

    def _restore(self):
        config.ACTIVE_TRADES_FILE = self._saved["active"]
        config.TRADE_HISTORY_FILE = self._saved["history"]
        config.CHANNELS_FILE = self._saved["channels"]
        config.DEMO = self._saved["demo"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def make_trade(self, **over) -> dict:
        trade = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "entry_price": 60000.0,
            "qty_total": "0.006",
            "current_sl": 60000.0,
            "targets": [61000.0, 62000.0, 63000.0, 64000.0],
            "tp_filled": [1],
            "opened_at": "2026-08-13 01:00:00",
            "margin_usdt": 50.0,
            "leverage": 10,
        }
        trade.update(over)
        return trade

    # ---------- режим счёта ----------

    def test_status_reports_demo_mode(self):
        """Раньше здесь было зашито «РЕАЛЬНЫЙ СЧЁТ» - на демо бот врал."""
        config.DEMO = True

        self.assertIn("ДЕМО", self.tg.format_status())

    def test_status_reports_real_mode(self):
        config.DEMO = False

        self.assertIn("РЕАЛЬНЫЙ", self.tg.format_status())

    def test_settings_report_mode(self):
        config.DEMO = True

        self.assertIn("ДЕМО", self.tg.format_settings())

    # ---------- сделки ----------

    def test_current_trades_use_trade_own_margin(self):
        """Маржа и плечо - из самой сделки, а не из текущих настроек.

        Настройки могли поменяться после открытия, и config показывал бы
        цифры, с которыми сделка не открывалась.
        """
        self.write(config.ACTIVE_TRADES_FILE, [self.make_trade()])

        text = self.tg.format_current_trades()

        self.assertIn("маржа 50.0 USDT x10", text)

    def test_current_trades_marks_filled_targets(self):
        self.write(config.ACTIVE_TRADES_FILE, [self.make_trade()])

        text = self.tg.format_current_trades()

        self.assertIn("✅ TP1", text)
        self.assertIn("⏳ TP2", text)

    def test_current_trades_survive_missing_fields(self):
        """Битую или старую запись бот показывает, а не падает молча."""
        self.write(config.ACTIVE_TRADES_FILE, [{"symbol": "ETHUSDT", "side": "Sell"}])

        text = self.tg.format_current_trades()

        self.assertIn("ETHUSDT", text)

    def test_no_trades_message(self):
        self.assertIn("нет открытых", self.tg.format_current_trades())

    def test_overview_contains_dashboard_summary(self):
        self.write(config.ACTIVE_TRADES_FILE, [self.make_trade()])
        text = self.tg._format_overview({
            "equity": 1000.0,
            "available_balance": 800.0,
            "unrealised_pnl": 12.5,
        })

        self.assertIn("ЛЯЛЯ БОТ", text)
        self.assertIn("Equity: 1000.00", text)
        self.assertIn("Открыто позиций: 1", text)
        self.assertIn("только на просмотр", text)

    # ---------- статистика ----------

    def test_stats_match_dashboard(self):
        """Бот и веб-панель обязаны считать по одному коду."""
        import stats
        history = [
            {"symbol": "BTCUSDT", "realized_pnl": 10.0, "closed_at": "2026-08-13 01:00:00"},
            {"symbol": "BTCUSDT", "realized_pnl": -4.0, "closed_at": "2026-08-13 02:00:00"},
            {"symbol": "BTCUSDT", "realized_pnl": 2.0, "closed_at": "2026-08-13 03:00:00"},
        ]
        self.write(config.TRADE_HISTORY_FILE, history)

        text = self.tg.format_stats()
        computed = stats.compute(history)

        self.assertEqual(computed["win_rate"], 66.7)
        self.assertEqual(computed["total_realized_pnl"], 8.0)
        self.assertIn("66.7%", text)
        self.assertIn("+8.00", text)

    def test_stats_without_pnl(self):
        self.assertIn("пока нет", self.tg.format_stats())

    def test_stats_ignore_trades_without_pnl(self):
        """Сделка без closedPnl не должна считаться поражением."""
        import stats
        history = [
            {"symbol": "BTCUSDT", "realized_pnl": 10.0},
            {"symbol": "BTCUSDT", "realized_pnl": None},
        ]

        computed = stats.compute(history)

        self.assertEqual(computed["win_rate"], 100.0)
        self.assertEqual(computed["trades_with_pnl"], 1)
        self.assertEqual(computed["total_trades"], 2)

    # ---------- каналы ----------

    def test_channels_list_is_read_only_view(self):
        self.write(config.CHANNELS_FILE, [
            {"chat_id": 1, "title": "GG Shot", "enabled": True, "parser": "ggshot_v1"},
            {"chat_id": 2, "title": "Fat Pig", "enabled": False},
        ])

        text = self.tg.format_channels()

        self.assertIn("GG Shot", text)
        self.assertIn("Fat Pig", text)
        self.assertIn("1 из 2", text)
        self.assertIn("автоподбор", text)      # у второго парсер не задан

    def test_channels_empty(self):
        self.assertIn("Ни одного канала", self.tg.format_channels())

    # ---------- клавиатура ----------

    def test_every_button_has_handler(self):
        """Кнопка без обработчика молча ничего не делает - ловим это тестом.

        Фильтры не разбираем по тексту, а прогоняем: подсовываем каждой
        подпись кнопки и смотрим, откликнулся ли хоть один обработчик.
        """
        buttons = [b.text for row in self.tg.MAIN_KEYBOARD.keyboard for b in row]

        unhandled = [text for text in buttons if not self._is_handled(text)]

        self.assertEqual(unhandled, [], "у этих кнопок нет обработчика")

    def test_keyboard_is_read_only(self):
        buttons = [b.text.lower() for row in self.tg.MAIN_KEYBOARD.keyboard for b in row]
        dangerous = ("стоп", "закрыть", "снять блокировку", "включить", "выключить")

        self.assertFalse(
            any(word in text for text in buttons for word in dangerous),
            "Telegram-панель не должна менять торговое состояние",
        )

    def test_legacy_buttons_still_have_handlers(self):
        old_buttons = [
            "📈 Текущие сделки", "📊 Все сделки", "📉 Статистика", "💰 Баланс",
            "🛡 Защита", "⚙️ Настройки", "📡 Каналы", "🔌 Статус",
            "🛑 Стоп всё", "▶️ Снять блокировку",
        ]

        self.assertEqual([text for text in old_buttons if not self._is_handled(text)], [])

    def test_unknown_text_matches_nothing(self):
        """Обратная проверка: иначе тест выше прошёл бы на любом фильтре."""
        self.assertFalse(self._is_handled("случайный текст"))

    def _is_handled(self, text: str) -> bool:
        import magic_filter

        class FakeMessage:
            def __init__(self, value):
                self.text = value

        message = FakeMessage(text)
        for handler in self.tg.dp.message.handlers:
            # aiogram оборачивает F.text == "..." в связанный MagicFilter.resolve;
            # остальные фильтры (CommandStart) требуют живого Bot - их пропускаем
            magic = [f.callback for f in handler.filters or []
                     if isinstance(getattr(f.callback, "__self__", None), magic_filter.MagicFilter)]
            if magic and all(f(message) for f in magic):
                return True
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
