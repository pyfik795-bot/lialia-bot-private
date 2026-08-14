"""Reliable ingestion of Telegram channel messages.

Telethon normally delivers ``NewMessage`` events immediately.  A proxy reconnect can
still leave a small gap, so the bot also polls a short tail of every enabled channel.
Messages are deduplicated by channel, Telegram message id and text digest.  The text
digest deliberately makes an edited message eligible for another parse attempt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import channels
import parsers
import signal_parser


logger = logging.getLogger("telegram_ingest")

DEFAULT_SEEN_FILE = "telegram_seen_messages.json"
DEFAULT_MAX_SIGNAL_AGE_SECONDS = 10 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 20
DEFAULT_POLL_LIMIT = 30
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15
MAX_SEEN_MESSAGES = 2_000


class TelegramSignalIngestor:
    def __init__(
        self,
        client,
        engine,
        *,
        seen_file: str | os.PathLike = DEFAULT_SEEN_FILE,
        max_signal_age_seconds: int = DEFAULT_MAX_SIGNAL_AGE_SECONDS,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
        poll_limit: int = DEFAULT_POLL_LIMIT,
        request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        now=None,
    ):
        self.client = client
        self.engine = engine
        self.seen_file = Path(seen_file)
        self.max_signal_age_seconds = max(0, int(max_signal_age_seconds))
        self.poll_interval_seconds = max(1, int(poll_interval_seconds))
        self.poll_limit = max(1, int(poll_limit))
        self.request_timeout_seconds = max(1, int(request_timeout_seconds))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._seen_order = deque(maxlen=MAX_SEEN_MESSAGES)
        self._seen = set()
        self._inflight = set()
        self._lock = asyncio.Lock()
        self._load_seen()

    def _load_seen(self):
        try:
            with self.seen_file.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, list):
            return
        for item in data[-MAX_SEEN_MESSAGES:]:
            if isinstance(item, str) and item not in self._seen:
                self._seen.add(item)
                self._seen_order.append(item)

    def _save_seen(self):
        self.seen_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.seen_file.with_suffix(self.seen_file.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(list(self._seen_order), file, ensure_ascii=False, indent=2)
        os.replace(temporary, self.seen_file)

    @staticmethod
    def _message_key(chat_id: int, message_id: int, text: str) -> str:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        return f"{chat_id}:{message_id}:{digest}"

    def _remember(self, key: str):
        if key in self._seen:
            return
        if len(self._seen_order) == self._seen_order.maxlen:
            oldest = self._seen_order[0]
            self._seen.discard(oldest)
        self._seen_order.append(key)
        self._seen.add(key)
        self._save_seen()

    def _age_seconds(self, message_date) -> float | None:
        if message_date is None:
            return None
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        return max(0.0, (self._now() - message_date.astimezone(timezone.utc)).total_seconds())

    async def handle_message(self, chat_id: int, message, *, origin: str) -> str:
        """Parse and, when fresh, execute one Telegram message.

        Returns a small outcome label.  This is mainly useful for tests and logs.
        """
        if chat_id not in channels.get_enabled_ids():
            return "disabled"

        message_id = int(getattr(message, "id", 0) or 0)
        text = getattr(message, "message", None) or getattr(message, "text", None) or ""
        key = self._message_key(chat_id, message_id, text)

        async with self._lock:
            if key in self._seen or key in self._inflight:
                return "duplicate"
            self._inflight.add(key)

        completed = False
        outcome = "ignored"
        try:
            if not text.strip():
                completed = True
                outcome = "empty"
                return outcome

            age_seconds = self._age_seconds(getattr(message, "date", None))
            if age_seconds is not None and age_seconds > self.max_signal_age_seconds:
                logger.info(
                    "Telegram: старое сообщение пропущено без сделки "
                    "(chat_id=%s, message_id=%s, возраст=%s сек, источник=%s)",
                    chat_id,
                    message_id,
                    int(age_seconds),
                    origin,
                )
                completed = True
                outcome = "stale"
                return outcome

            parser_name = channels.get_parser_name(chat_id)
            signal = parsers.parse(text, parser_name)
            if signal is None:
                logger.debug(
                    "Telegram: сообщение не является сигналом "
                    "(chat_id=%s, message_id=%s, парсер=%s, источник=%s)",
                    chat_id,
                    message_id,
                    parser_name or "auto",
                    origin,
                )
                completed = True
                outcome = "not_signal"
                return outcome

            message_date = getattr(message, "date", None)
            signal["source_chat_id"] = chat_id
            signal["source_message_id"] = message_id
            if message_date is not None:
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)
                signal["source_message_date"] = message_date.astimezone(timezone.utc).isoformat()

            signal_parser.log_parsed_signal(signal)
            logger.info(
                "Новый сигнал: %s %s (парсер %s, chat_id=%s, message_id=%s, источник=%s)",
                signal["symbol"],
                signal["strategy"],
                signal.get("parser"),
                chat_id,
                message_id,
                origin,
            )

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.engine.process_signal, signal)
            completed = True
            outcome = "processed"
            return outcome
        except Exception:
            logger.exception(
                "Telegram: ошибка обработки сообщения chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            return "error"
        finally:
            async with self._lock:
                self._inflight.discard(key)
                if completed:
                    try:
                        self._remember(key)
                    except OSError:
                        logger.exception("Не удалось сохранить журнал Telegram-сообщений")

    async def poll_once(self):
        """Read a short recent tail to cover messages missed during reconnects."""
        for source in channels.get_all():
            if not source.get("enabled", True):
                continue
            chat_id = source.get("chat_id")
            if chat_id is None:
                continue
            try:
                messages = await asyncio.wait_for(
                    self.client.get_messages(chat_id, limit=self.poll_limit),
                    timeout=self.request_timeout_seconds,
                )
                for message in reversed(messages):
                    await self.handle_message(chat_id, message, origin="recovery_poll")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "Telegram: резервный опрос канала %s (%s) не удался: %s",
                    source.get("title") or chat_id,
                    chat_id,
                    error,
                )

    async def poll_forever(self):
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            await self.poll_once()
