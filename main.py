"""
Точка входа: слушает все каналы из channels.json через Telethon, парсит
сигналы, передаёт их торговому движку (trade_engine.py) и одновременно
поднимает Telegram-бота (tg_bot.py) и веб-дашборд (webapp.py) - всё в одном
asyncio-процессе, без subprocess и без блокирующего опроса файлов.
"""

import logging_setup

logging_setup.configure()  # до импорта остальных модулей, чтобы их логгеры сразу писали и в файл

import asyncio
import os
import time

from aiohttp import web
from telethon import TelegramClient, events
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

import channels
import config
import parsers
import settings
import signal_parser
import status
import synctime
import tg_bot
import trade_engine
import webapp

import logging

logger = logging.getLogger("main")


def build_akk_client() -> TelegramClient:
    proxy_enabled = os.getenv("TELEGRAM_PROXY_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }
    client_options = {}
    if proxy_enabled:
        proxy_address = os.getenv("TELEGRAM_PROXY_ADDRESS", config.PROXY_ADDRESS)
        proxy_port = int(os.getenv("TELEGRAM_PROXY_PORT", str(config.PROXY_PORT)))
        proxy_secret = os.getenv("TELEGRAM_PROXY_SECRET", config.PROXY_SECRET)
        client_options.update(
            proxy=(proxy_address, proxy_port, proxy_secret),
            connection=ConnectionTcpMTProxyRandomizedIntermediate,
        )

    return TelegramClient(
        'BOT',
        config.API_ID,
        config.API_HASH,
        **client_options,
    )


async def main():
    loop = asyncio.get_running_loop()

    def notifier(text: str):
        asyncio.run_coroutine_threadsafe(tg_bot.send_to_all(text), loop)

    # Подменяем pybit-шный генератор времени (REST + вебсокет) на
    # синхронизированное с биржей время - на сервере с отстающими часами
    # иначе каждый авторизованный запрос падает с 10002/10016
    trade_engine.patch_pybit_clock()

    engine = trade_engine.BotEngine(notifier=notifier)

    polling_task = asyncio.create_task(tg_bot.start_polling())

    # engine.start() делает блокирующие HTTP-запросы к Bybit (синхронизация
    # времени, восстановление активных сделок) - уводим в поток, чтобы не
    # подвесить event loop прямо на старте
    await loop.run_in_executor(None, engine.start)

    akk = build_akk_client()
    await akk.connect()

    if not akk.is_connected():
        raise RuntimeError("Не удалось подключиться к Telegram (Telethon)")

    status.set_flag("telethon_connected", True)
    logger.info("Telethon подключен")

    # Если каналы ещё не настроены через сайт - засеваем список одним
    # каналом по умолчанию из config.CHANNEL, чтобы поведение "из коробки"
    # не поменялось для тех, кто ещё не открывал дашборд
    if not channels.get_all() and config.CHANNEL:
        try:
            entry = await channels.resolve_and_add(akk, config.CHANNEL)
            logger.info(f"Канал по умолчанию добавлен в источники: {entry['title']}")
        except Exception as e:
            logger.warning(f"Не удалось добавить канал по умолчанию {config.CHANNEL}: {e}")

    web_runner = web.AppRunner(webapp.build_app(akk))
    await web_runner.setup()
    web_site = web.TCPSite(web_runner, config.WEB_HOST, config.WEB_PORT)
    await web_site.start()
    # 0.0.0.0 - это адрес привязки, а не адрес для браузера: в лог пишем то,
    # что реально можно открыть
    shown_host = "localhost" if config.WEB_HOST in ("0.0.0.0", "") else config.WEB_HOST
    logger.info(f"Дашборд: http://{shown_host}:{config.WEB_PORT} "
                f"(слушает {config.WEB_HOST}, доступен из локальной сети)")

    # Слушаем ВСЕ чаты аккаунта (без chats=), а не один жёстко заданный канал -
    # список разрешённых источников (channels.json) правится через сайт "на лету"
    @akk.on(events.NewMessage())
    async def handler(event):
        if event.chat_id not in channels.get_enabled_ids():
            return

        text = event.message.text
        if not text:
            return

        # Формат сообщения больше не зашит в код: у канала может быть свой
        # парсер (channels.json -> "parser"), иначе перебираем все из
        # parsers.json. Отсев не-сигналов делает сам парсер (prefilter).
        parser_name = channels.get_parser_name(event.chat_id)
        signal = parsers.parse(text, parser_name)
        if signal is None:
            return

        signal_parser.log_parsed_signal(signal)
        logger.info(f"новый сигнал: {signal['symbol']} {signal['strategy']} "
                    f"(парсер {signal.get('parser')})")

        # блокирующие HTTP/WS-вызовы Bybit уходят в отдельный поток,
        # event loop продолжает принимать новые сообщения
        loop.run_in_executor(None, engine.process_signal, signal)

    logger.info("Жду сигналы из подключённых каналов...")

    # Уведомление о старте: при авторестарте после падения оно приходит
    # заново - по нему видно, что бот перезапустился, даже если лога под рукой нет
    notifier(
        f"🚀 Ляля Бот запущен\n"
        f"Режим: {'🧪 ДЕМО-счёт' if settings.is_demo() else '💵 РЕАЛЬНЫЙ СЧЁТ'}\n"
        f"Каналов активно: {len(channels.get_enabled_ids())}\n"
        f"Дашборд: http://{config.WEB_HOST}:{config.WEB_PORT}"
    )

    try:
        await akk.run_until_disconnected()
    finally:
        status.set_flag("telethon_connected", False)
        engine.stop()
        await tg_bot.stop_polling()
        await asyncio.gather(polling_task, return_exceptions=True)
        await tg_bot.close()
        await web_runner.cleanup()


def run_bot_with_restart():
    """Запускает бота и перезапускает его при падении."""
    logger.info("=" * 60)
    logger.info("старт")

    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.info("бот офф, нажат ctrl+c")
            break
        except Exception as e:
            logger.exception(f"бот упал с ошибкой: {e}")
            logger.info("рестарт через 10 сек")
            time.sleep(10)
            continue


if __name__ == "__main__":
    run_bot_with_restart()
