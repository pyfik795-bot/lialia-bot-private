import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import telegram_ingest


NOW = datetime(2026, 8, 14, 7, 0, tzinfo=timezone.utc)


class FakeEngine:
    def __init__(self):
        self.signals = []

    def process_signal(self, signal):
        self.signals.append(signal)


class FakeClient:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.calls = []

    async def get_messages(self, chat_id, limit):
        self.calls.append((chat_id, limit))
        return self.messages[:limit]


def message(message_id, text, age_seconds=5):
    return SimpleNamespace(
        id=message_id,
        message=text,
        text=text,
        date=NOW - timedelta(seconds=age_seconds),
    )


def parsed_signal(text, parser_name=None):
    return {
        "symbol": "ACEUSDT",
        "strategy": "Short",
        "targets": [0.12, 0.11],
        "stop_loss": 0.14,
        "signal_id": f"signal-{text}",
        "parser": "fatpig_v1",
        "raw_message": text,
    }


class TelegramSignalIngestorTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.seen_file = Path(self.temp.name) / "seen.json"
        self.engine = FakeEngine()
        self.client = FakeClient()
        self.ingestor = telegram_ingest.TelegramSignalIngestor(
            self.client,
            self.engine,
            seen_file=self.seen_file,
            max_signal_age_seconds=600,
            poll_interval_seconds=1,
            now=lambda: NOW,
        )
        self.enabled_patch = patch("telegram_ingest.channels.get_enabled_ids", return_value={-1001})
        self.parser_patch = patch("telegram_ingest.channels.get_parser_name", return_value="fatpig_v1")
        self.log_patch = patch("telegram_ingest.signal_parser.log_parsed_signal")
        self.enabled_patch.start()
        self.parser_patch.start()
        self.log_mock = self.log_patch.start()
        self.addCleanup(self.enabled_patch.stop)
        self.addCleanup(self.parser_patch.stop)
        self.addCleanup(self.log_patch.stop)

    async def test_fresh_message_is_processed_and_audited(self):
        with patch("telegram_ingest.parsers.parse", side_effect=parsed_signal):
            outcome = await self.ingestor.handle_message(
                -1001, message(42, "fresh"), origin="new_message"
            )

        self.assertEqual(outcome, "processed")
        self.assertEqual(len(self.engine.signals), 1)
        signal = self.engine.signals[0]
        self.assertEqual(signal["source_chat_id"], -1001)
        self.assertEqual(signal["source_message_id"], 42)
        self.assertEqual(signal["source_message_date"], "2026-08-14T06:59:55+00:00")
        self.log_mock.assert_called_once()
        self.assertTrue(self.seen_file.exists())

    async def test_old_signal_is_remembered_but_never_executed(self):
        old = message(43, "old", age_seconds=601)
        with patch("telegram_ingest.parsers.parse") as parse_mock:
            first = await self.ingestor.handle_message(-1001, old, origin="recovery_poll")
            second = await self.ingestor.handle_message(-1001, old, origin="recovery_poll")

        self.assertEqual(first, "stale")
        self.assertEqual(second, "duplicate")
        parse_mock.assert_not_called()
        self.assertEqual(self.engine.signals, [])
        with self.seen_file.open(encoding="utf-8") as file:
            self.assertEqual(len(json.load(file)), 1)

    async def test_recovery_poll_closes_reconnect_gap_without_duplicates(self):
        # Telethon returns newest first; the ingestor deliberately replays oldest first.
        self.client.messages = [message(12, "second"), message(11, "first")]
        with patch(
            "telegram_ingest.channels.get_all",
            return_value=[{"chat_id": -1001, "title": "Fat Pig", "enabled": True}],
        ), patch("telegram_ingest.parsers.parse", side_effect=parsed_signal):
            await self.ingestor.poll_once()
            await self.ingestor.poll_once()

        self.assertEqual([s["raw_message"] for s in self.engine.signals], ["first", "second"])
        self.assertEqual(self.client.calls, [(-1001, 30), (-1001, 30)])

    async def test_edited_message_gets_one_new_parse_attempt(self):
        with patch("telegram_ingest.parsers.parse", side_effect=parsed_signal):
            first = await self.ingestor.handle_message(
                -1001, message(50, "draft"), origin="new_message"
            )
            edited = await self.ingestor.handle_message(
                -1001, message(50, "complete signal"), origin="message_edited"
            )
            duplicate_edit = await self.ingestor.handle_message(
                -1001, message(50, "complete signal"), origin="message_edited"
            )

        self.assertEqual((first, edited, duplicate_edit), ("processed", "processed", "duplicate"))
        self.assertEqual(len(self.engine.signals), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
