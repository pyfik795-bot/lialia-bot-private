"""
Управление списком Telegram-каналов - источников сигналов.

Список хранится в channels.json и правится через веб-панель (/api/channels)
без перезапуска бота: main.py слушает ВСЕ чаты, к которым подключён
Telethon-аккаунт, и на каждое сообщение сверяется с этим списком (через
get_enabled_ids()), так что новый канал начинает работать сразу после
добавления на сайте.
"""

import json
import os
import threading

from telethon import utils as telethon_utils

import config

_lock = threading.Lock()


def _load() -> list:
    if not os.path.exists(config.CHANNELS_FILE) or os.path.getsize(config.CHANNELS_FILE) == 0:
        return []
    with open(config.CHANNELS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _save(data: list) -> None:
    with open(config.CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_all() -> list:
    with _lock:
        return _load()


def get_enabled_ids() -> set:
    return {c["chat_id"] for c in get_all() if c.get("enabled", True)}


def get_parser_name(chat_id: int) -> str | None:
    """Имя парсера, назначенного каналу. None - перебирать все подряд."""
    for c in get_all():
        if c["chat_id"] == chat_id:
            return c.get("parser")
    return None


def set_parser(chat_id: int, parser_name: str | None) -> None:
    """Привязывает формат сообщений к каналу (или снимает привязку)."""
    with _lock:
        chans = _load()
        for c in chans:
            if c["chat_id"] == chat_id:
                if parser_name:
                    c["parser"] = parser_name
                else:
                    c.pop("parser", None)
        _save(chans)


def add(chat_id: int, username, title: str, parser: str | None = None) -> dict:
    with _lock:
        chans = _load()
        if any(c["chat_id"] == chat_id for c in chans):
            raise ValueError("Этот канал уже добавлен")
        entry = {
            "chat_id": chat_id,
            "username": username,
            "title": title,
            "enabled": True,
        }
        if parser:
            entry["parser"] = parser
        chans.append(entry)
        _save(chans)
        return entry


def remove(chat_id: int) -> None:
    with _lock:
        chans = [c for c in _load() if c["chat_id"] != chat_id]
        _save(chans)


def set_enabled(chat_id: int, enabled: bool) -> None:
    with _lock:
        chans = _load()
        for c in chans:
            if c["chat_id"] == chat_id:
                c["enabled"] = enabled
        _save(chans)


async def resolve_and_add(akk, identifier: str) -> dict:
    """Резолвит канал через Telethon (username/ссылка/id) и добавляет его в
    список. chat_id считается через telethon.utils.get_peer_id, чтобы
    гарантированно совпадать с event.chat_id входящих сообщений (у каналов
    это не то же самое, что "сырой" entity.id)."""
    entity = await akk.get_entity(identifier)
    chat_id = telethon_utils.get_peer_id(entity)
    username = getattr(entity, "username", None)
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)
    return add(chat_id, username, title)
