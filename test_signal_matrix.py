"""Расширенная матрица форматов Telegram-сигналов.

Тесты не подключаются к Telegram или Bybit и не создают ордера. Они проверяют
только распознавание текста и защитный отказ на неоднозначных сообщениях.
"""

import unittest

import parsers
import trade_engine


def ggshot_message(
    *,
    symbol="BTCUSDT",
    side="Long",
    entry=("62,806", "64,806"),
    targets=("70,806", "75,806", "80,806", "85,806"),
    stop="61,806",
    signal_id="MATRIX-1",
    markdown=False,
    dash="-",
):
    value = (lambda item: f"**{item}**") if markdown else str
    lines = [
        f"📩 {'**' if markdown else ''}#{symbol}{'**' if markdown else ''} 1h | Mid-Term",
        f"{'**' if markdown else ''}📈 {side} Entry Zone:{'**' if markdown else ''} "
        f"{value(entry[0])} {dash} {value(entry[1])}",
        "",
        "⏳ Signal Details:",
    ]
    lines.extend(f"Target {index}: {value(target)}" for index, target in enumerate(targets, 1))
    lines.extend([
        "",
        f"🔺 Stop-Loss: {value(stop)}",
        f"🔎 Signal ID: #{signal_id}",
    ])
    return "\n".join(lines)


def fatpig_message(
    *,
    symbol="ACE/USDT",
    side="LONG",
    entry=("1.250", "1.200"),
    targets=("1.300", "1.350", "1.400", "1.450", "1.500", "1.550"),
    stop="1.100",
    stop_label="Stop Loss",
    dash="-",
):
    lines = [
        f"📍 Coin: #{symbol}",
        "",
        f"🟢 {side}",
        "",
        f"👉 Entry: {entry[0]} {dash} {entry[1]}",
        "🌐 Leverage: 20x",
        "",
    ]
    lines.extend(f"🎯 Target {index}: {target}" for index, target in enumerate(targets, 1))
    lines.append(f"❌ {stop_label}: {stop}")
    return "\n".join(lines)


class GgshotFormatMatrixTestCase(unittest.TestCase):
    def test_markdown_case_whitespace_and_line_endings(self):
        cases = [
            ggshot_message(),
            ggshot_message(markdown=True),
            ggshot_message(side="LONG").replace("Entry Zone", "Entry\tZone"),
            ggshot_message().replace("\n", "\r\n"),
            ggshot_message().replace("Target 1:", "Target   1 :"),
        ]

        for index, text in enumerate(cases):
            with self.subTest(case=index):
                signal = parsers.parse_with(parsers.DEFAULT_PARSER, text)
                self.assertIsNotNone(signal)
                self.assertEqual(signal["symbol"], "BTCUSDT")
                self.assertEqual(signal["strategy"], "Long")
                self.assertEqual(len(signal["targets"]), 4)

    def test_ascii_en_and_em_dash_entry_zones(self):
        for dash in ("-", "–", "—"):
            with self.subTest(dash=dash):
                signal = parsers.parse_with(
                    parsers.DEFAULT_PARSER,
                    ggshot_message(dash=dash),
                )
                self.assertIsNotNone(signal)
                self.assertEqual(signal.get("entry_zone"), [62806.0, 64806.0])

    def test_two_to_ten_targets_are_supported(self):
        parser = dict(parsers.DEFAULT_PARSER, tp_percents=None)
        for count in range(2, 11):
            targets = tuple(str(100 + index) for index in range(1, count + 1))
            text = ggshot_message(
                entry=("99", "100"),
                targets=targets,
                stop="98",
                signal_id=f"COUNT-{count}",
            )
            with self.subTest(count=count):
                signal = parsers.parse_with(parser, text)
                self.assertIsNotNone(signal)
                self.assertEqual(len(signal["targets"]), count)

    def test_one_and_eleven_targets_are_rejected(self):
        parser = dict(parsers.DEFAULT_PARSER, tp_percents=None)
        for count in (1, 11):
            targets = tuple(str(100 + index) for index in range(1, count + 1))
            with self.subTest(count=count):
                self.assertIsNone(
                    parsers.parse_with(
                        parser,
                        ggshot_message(entry=("99", "100"), targets=targets, stop="98"),
                    )
                )

    def test_literal_split_markdown_inside_prices(self):
        text = ggshot_message(
            entry=("62**.806**", "64**.806**"),
            targets=("70.806", "75**.806**", "80**.806**", "85**.806**"),
            stop="61**.806**",
        )

        signal = parsers.parse_with(parsers.DEFAULT_PARSER, text)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["targets"], [70.806, 75.806, 80.806, 85.806])

    def test_long_and_short_ladders(self):
        long_signal = parsers.parse_with(parsers.DEFAULT_PARSER, ggshot_message())
        short_signal = parsers.parse_with(
            parsers.DEFAULT_PARSER,
            ggshot_message(
                side="Short",
                entry=("64,806", "62,806"),
                targets=("61,806", "60,806", "59,806", "58,806"),
                stop="65,806",
            ),
        )

        self.assertEqual(long_signal["strategy"], "Long")
        self.assertEqual(short_signal["strategy"], "Short")

    def test_invalid_signal_corpus_is_rejected(self):
        valid = ggshot_message()
        cases = {
            "no prefilter": valid.replace("📩", ""),
            "no symbol": valid.replace("#BTCUSDT", "BTC"),
            "no side": valid.replace("Long Entry Zone", "Entry Zone"),
            "no stop": valid.replace("🔺 Stop-Loss: 61,806", ""),
            "duplicate targets": valid.replace("75,806", "70,806"),
            "unordered targets": valid.replace("80,806", "69,806"),
            "broken number": valid.replace("80,806", "not-a-price"),
        }

        for name, text in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(parsers.parse_with(parsers.DEFAULT_PARSER, text))


class FatpigFormatMatrixTestCase(unittest.TestCase):
    def test_symbol_and_stop_label_variants(self):
        for symbol in ("ACEUSDT", "ACE/USDT", "ACE-USDT", "ACE / USDT"):
            for stop_label in ("Stop Loss", "Stop-Loss", "StopLoss"):
                with self.subTest(symbol=symbol, stop_label=stop_label):
                    signal = parsers.parse_with(
                        parsers.FATPIG_PARSER,
                        fatpig_message(symbol=symbol, stop_label=stop_label),
                    )
                    self.assertIsNotNone(signal)
                    self.assertEqual(signal["symbol"], "ACEUSDT")
                    self.assertEqual(len(signal["targets"]), 6)

    def test_long_and_short_with_six_targets(self):
        long_signal = parsers.parse_with(parsers.FATPIG_PARSER, fatpig_message())
        short_signal = parsers.parse_with(
            parsers.FATPIG_PARSER,
            fatpig_message(
                side="SHORT",
                entry=("1.250", "1.300"),
                targets=("1.200", "1.150", "1.100", "1.050", "1.000", "0.950"),
                stop="1.350",
            ),
        )

        self.assertEqual(long_signal["strategy"], "Long")
        self.assertEqual(short_signal["strategy"], "Short")
        self.assertEqual(short_signal["tp_percents"], [20.0, 20.0, 15.0, 15.0, 15.0, 15.0])

    def test_decimal_comma_and_unicode_dash(self):
        signal = parsers.parse_with(
            parsers.FATPIG_PARSER,
            fatpig_message(
                entry=("0,1200", "0,1250"),
                targets=("0,1300", "0,1350", "0,1400", "0,1450", "0,1500", "0,1550"),
                stop="0,1100",
                dash="–",
            ),
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal["targets"][0], 0.13)
        self.assertEqual(signal.get("entry_zone"), [0.12, 0.125])

    def test_fingerprint_is_stable_but_changed_levels_are_new(self):
        first = parsers.parse_with(parsers.FATPIG_PARSER, fatpig_message())
        repeated = parsers.parse_with(parsers.FATPIG_PARSER, fatpig_message() + "\nКомментарий")
        changed = parsers.parse_with(
            parsers.FATPIG_PARSER,
            fatpig_message(targets=("1.310", "1.360", "1.410", "1.460", "1.510", "1.560")),
        )

        self.assertEqual(first["signal_id"], repeated["signal_id"])
        self.assertNotEqual(first["signal_id"], changed["signal_id"])


class EndToEndSignalPipelineTestCase(unittest.TestCase):
    """Текст → парсер → нормализация масштаба → проверка уровней."""

    def assert_pipeline(self, text, parser, market_price, expected_targets, expected_stop):
        signal = parsers.parse_with(parser, text)
        self.assertIsNotNone(signal)

        trade = trade_engine.TradeManager(signal, notifier=lambda _text: None)
        trade.repair_thousands_separator(market_price)
        trade.validate_levels(market_price)

        self.assertEqual(trade.targets, expected_targets)
        self.assertEqual(trade.initial_sl, expected_stop)

    def test_ggshot_comma_thousands_pipeline(self):
        self.assert_pipeline(
            ggshot_message(),
            parsers.DEFAULT_PARSER,
            62_850.0,
            [70_806.0, 75_806.0, 80_806.0, 85_806.0],
            61_806.0,
        )

    def test_ggshot_dotted_thousands_and_split_markdown_pipeline(self):
        self.assert_pipeline(
            ggshot_message(
                entry=("62**.806**", "64**.806**"),
                targets=("70.806", "75**.806**", "80**.806**", "85**.806**"),
                stop="61**.806**",
            ),
            parsers.DEFAULT_PARSER,
            62_850.0,
            [70_806.0, 75_806.0, 80_806.0, 85_806.0],
            61_806.0,
        )

    def test_low_price_decimal_comma_pipeline_is_not_scaled(self):
        self.assert_pipeline(
            ggshot_message(
                symbol="VIRTUALUSDT",
                entry=("0,5426", "0,5699"),
                targets=("0,5821", "0,5943", "0,6066", "0,6432"),
                stop="0,5310",
            ),
            parsers.DEFAULT_PARSER,
            0.56,
            [0.5821, 0.5943, 0.6066, 0.6432],
            0.531,
        )

    def test_fatpig_short_pipeline(self):
        self.assert_pipeline(
            fatpig_message(
                side="SHORT",
                entry=("0.1324", "0.1289"),
                targets=("0.1277", "0.1265", "0.1252", "0.1240", "0.1228", "0.1214"),
                stop="0.1350",
            ),
            parsers.FATPIG_PARSER,
            0.13,
            [0.1277, 0.1265, 0.1252, 0.124, 0.1228, 0.1214],
            0.135,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
