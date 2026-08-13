"""
Настройка логирования для непрерывной (24/7) работы: вывод в консоль
остаётся, плюс пишем в файл с ротацией, чтобы после падения/перезапуска
можно было посмотреть, что произошло, даже если консоль уже не видна.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import config


class _AiogramTransientFilter(logging.Filter):
    """Сетевые сбои polling не являются падением приложения.

    Aiogram сам восстанавливает соединение, но по умолчанию пишет на каждую
    попытку две строки уровня ERROR/WARNING. Оставляем одно предупреждение —
    настоящее исключение при этом не скрывается и остаётся целиком в сообщении.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "aiogram.dispatcher":
            return True

        message = record.getMessage()
        if message.startswith("Sleep for "):
            return False
        if message.startswith("Failed to fetch updates - Telegram") and record.levelno >= logging.ERROR:
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True


def configure() -> None:
    # Иначе кириллица/эмодзи в консоли Windows превращаются в кракозябры
    # при нестандартной кодовой странице.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    os.makedirs(config.LOG_DIR, exist_ok=True)
    log_path = os.path.join(config.LOG_DIR, config.LOG_FILE)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    if getattr(root, "_trading_bot_configured", False):
        return
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(_AiogramTransientFilter())
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(_AiogramTransientFilter())
    root.addHandler(file_handler)
    root._trading_bot_configured = True

    # Гасим шум, из-за которого при работе 24/7 лог прокручивался за несколько
    # часов и события сделок терялись: панель опрашивает сервер каждые 5 секунд
    # (aiohttp.access), а pybit пишет строку на каждый HTTP-запрос к бирже.
    # Вместе это давало ~80% файла. Предупреждения и ошибки от них остаются.
    for noisy in ("aiohttp.access", "pybit._http_manager"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
