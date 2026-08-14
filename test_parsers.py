"""Тесты разбора сообщений каналов (parsers.py, signal_parser.py).

Запуск:  python -m unittest test_parsers -v

Проверяются реальные пресеты на сообщениях в формате живых каналов.
"""

import json
import logging
import os
import shutil
import tempfile
import unittest

import config
import parsers
import signal_parser

_log = logging.getLogger("parsers")
_log.addHandler(logging.NullHandler())
_log.propagate = False

GGSHOT_MESSAGE = """📩 **#LTCUSDT**** 1h** | Mid-Term
**📈 Long Entry Zone:** **44.84-45.71**

**⏳ Signal Details:**
Target 1: **46.30**
Target 2: **46.88**
Target 3: **47.47**
Target 4: **49.22**

**🔺 Stop-Loss:** **44.26**

🔎 **Signal ID:** #ID0932001391
"""

FATPIG_MESSAGE = """🟢 LONG
#ACE/USDT

Entry: 1.250 - 1.200
Target 1: 1.300
Target 2: 1.350
Target 3: 1.400
Target 4: 1.450
Target 5: 1.500
Target 6: 1.550
Stop Loss: 1.100
"""

FATPIG_REAL_SHORT_MESSAGE = """📍Coin : #ACE/USDT

🔴 SHORT

👉 Entry: 0.1289 - 0.1324

🌐 Leverage: 20x

🎯 Target 1: 0.1277
🎯 Target 2: 0.1265
🎯 Target 3: 0.1252
🎯 Target 4: 0.1240
🎯 Target 5: 0.1228
🎯 Target 6: 0.1214

❌ StopLoss: 0.1350
"""


class GgshotPresetTestCase(unittest.TestCase):
    def parse(self, text=GGSHOT_MESSAGE):
        return parsers.parse_with(parsers.DEFAULT_PARSER, text)

    def test_parses_real_message(self):
        signal = self.parse()

        self.assertEqual(signal["symbol"], "LTCUSDT")
        self.assertEqual(signal["strategy"], "Long")
        self.assertEqual(signal["targets"], [46.30, 46.88, 47.47, 49.22])
        self.assertEqual(signal["stop_loss"], 44.26)
        self.assertEqual(signal["signal_id"], "ID0932001391")
        self.assertEqual(signal["entry_zone"], [44.84, 45.71])

    def test_message_without_prefilter_is_skipped(self):
        self.assertIsNone(self.parse(GGSHOT_MESSAGE.replace("📩", "")))

    def test_chat_noise_is_not_a_signal(self):
        self.assertIsNone(self.parse("📩 ребята, что там по BTC?"))

    def test_missing_stop_loss_rejects_signal(self):
        text = GGSHOT_MESSAGE.replace("**🔺 Stop-Loss:** **44.26**", "")

        self.assertIsNone(self.parse(text))


class FatpigPresetTestCase(unittest.TestCase):
    def test_parses_actual_short_message_with_six_targets(self):
        signal = parsers.parse_with(parsers.FATPIG_PARSER, FATPIG_REAL_SHORT_MESSAGE)

        self.assertEqual(signal["symbol"], "ACEUSDT")
        self.assertEqual(signal["strategy"], "Short")
        self.assertEqual(signal["entry_zone"], [0.1289, 0.1324])
        self.assertEqual(
            signal["targets"],
            [0.1277, 0.1265, 0.1252, 0.1240, 0.1228, 0.1214],
        )
        self.assertEqual(signal["stop_loss"], 0.1350)

    def test_parses_six_targets_and_slashed_symbol(self):
        signal = parsers.parse_with(parsers.FATPIG_PARSER, FATPIG_MESSAGE)

        self.assertEqual(signal["symbol"], "ACEUSDT", "дробь в #ACE/USDT не убрана")
        self.assertEqual(signal["strategy"], "Long")
        self.assertEqual(len(signal["targets"]), 6)
        self.assertEqual(signal["tp_percents"], [20.0, 20.0, 15.0, 15.0, 15.0, 15.0])

    def test_signal_without_id_gets_fingerprint(self):
        signal = parsers.parse_with(parsers.FATPIG_PARSER, FATPIG_MESSAGE)

        self.assertTrue(signal["signal_id"].startswith("auto-"))

    def test_same_message_gives_same_fingerprint(self):
        """Иначе повтор сообщения открыл бы вторую сделку по тому же сигналу."""
        first = parsers.parse_with(parsers.FATPIG_PARSER, FATPIG_MESSAGE)
        second = parsers.parse_with(parsers.FATPIG_PARSER, FATPIG_MESSAGE + "\n(повтор)")

        self.assertEqual(first["signal_id"], second["signal_id"])


class TargetLadderTestCase(unittest.TestCase):
    """Тейки обязаны идти лестницей - по ней движок ведёт стоп."""

    def parse(self, text):
        return parsers.parse_with(parsers.DEFAULT_PARSER, text)

    def test_unordered_targets_for_long_are_rejected(self):
        """Сбитый порядок значит, что regex зацепил постороннее число.

        Движок переносит стоп на уровень предыдущего тейка, и на перепутанной
        лестнице он поехал бы по неверным уровням.
        """
        text = GGSHOT_MESSAGE.replace("Target 3: **47.47**", "Target 3: **40.10**")

        self.assertIsNone(self.parse(text))

    def test_duplicate_targets_are_rejected(self):
        text = GGSHOT_MESSAGE.replace("Target 2: **46.88**", "Target 2: **46.30**")

        self.assertIsNone(self.parse(text))

    def test_descending_targets_for_short_are_accepted(self):
        text = (GGSHOT_MESSAGE
                .replace("Long Entry Zone", "Short Entry Zone")
                .replace("Target 1: **46.30**", "Target 1: **44.00**")
                .replace("Target 2: **46.88**", "Target 2: **43.00**")
                .replace("Target 3: **47.47**", "Target 3: **42.00**")
                .replace("Target 4: **49.22**", "Target 4: **41.00**"))

        signal = self.parse(text)

        self.assertEqual(signal["strategy"], "Short")
        self.assertEqual(signal["targets"], [44.0, 43.0, 42.0, 41.0])

    def test_ascending_targets_for_short_are_rejected(self):
        text = GGSHOT_MESSAGE.replace("Long Entry Zone", "Short Entry Zone")

        self.assertIsNone(self.parse(text))


class PrefilterTestCase(unittest.TestCase):
    """prefilter - подстрока, а не regex; панель не должна утверждать обратное."""

    def make(self, prefilter):
        return dict(parsers.DEFAULT_PARSER, prefilter=prefilter)

    def test_prefilter_is_case_insensitive(self):
        parser = self.make("signal id")
        text = GGSHOT_MESSAGE.replace("📩", "")

        self.assertIsNotNone(parsers.parse_with(parser, text))

    def test_regex_prefilter_is_rejected_on_save(self):
        """Раньше такой prefilter сохранялся, но не совпадал никогда.

        Панель компилировала его как регулярное выражение, а разбор искал
        подстроку - канал молча переставал распознаваться.
        """
        parser = self.make("Signal|Сигнал")

        cleaned = parsers.validate(parser)

        # сохранить можно, но как обычный текст - и он честно не совпадёт
        self.assertEqual(cleaned["prefilter"], "Signal|Сигнал")
        self.assertIsNone(parsers.parse_with(cleaned, GGSHOT_MESSAGE))


class ValidateTestCase(unittest.TestCase):
    def test_missing_required_field(self):
        bad = dict(parsers.DEFAULT_PARSER)
        bad["fields"] = {"symbol": r"#(\w+)"}

        with self.assertRaises(parsers.ParserError) as ctx:
            parsers.validate(bad)
        self.assertIn("обязательные поля", str(ctx.exception))

    def test_broken_regex_is_rejected(self):
        bad = dict(parsers.DEFAULT_PARSER, targets=r"Target (\d+")

        with self.assertRaises(parsers.ParserError):
            parsers.validate(bad)

    def test_pattern_without_capture_group_is_rejected(self):
        bad = dict(parsers.DEFAULT_PARSER, targets=r"Target \d+")

        with self.assertRaises(parsers.ParserError) as ctx:
            parsers.validate(bad)
        self.assertIn("группы захвата", str(ctx.exception))

    def test_tp_percents_must_sum_to_100(self):
        bad = dict(parsers.DEFAULT_PARSER, tp_percents=[50.0, 30.0])

        with self.assertRaises(parsers.ParserError) as ctx:
            parsers.validate(bad)
        self.assertIn("100", str(ctx.exception))

    def test_valid_preset_passes(self):
        for preset in parsers.PRESETS:
            with self.subTest(preset=preset["name"]):
                parsers.validate(preset)


class AuditLogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "parsed.json")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_log_is_capped(self):
        """Журнал переписывается целиком на каждый сигнал - расти без предела
        ему нельзя, иначе запись начнёт тормозить обработку.

        Потолок на время теста занижаем: гонять настоящую тысячу записей
        значило бы проверять логику через ту самую медленную перезапись.
        """
        saved_cap = signal_parser.MAX_ENTRIES
        signal_parser.MAX_ENTRIES = 20
        self.addCleanup(setattr, signal_parser, "MAX_ENTRIES", saved_cap)

        for i in range(signal_parser.MAX_ENTRIES + 5):
            signal_parser.log_parsed_signal({"n": i}, self.path)

        with open(self.path, encoding="utf-8") as f:
            saved = json.load(f)

        self.assertEqual(len(saved), 20)
        self.assertEqual(saved[-1]["n"], 24, "обрезали не с того конца - потерялись свежие")
        self.assertEqual(saved[0]["n"], 5)

    def test_broken_log_file_does_not_crash(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{не json")

        signal_parser.log_parsed_signal({"n": 1}, self.path)

        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [{"n": 1}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
