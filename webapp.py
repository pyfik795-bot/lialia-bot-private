"""
Локальный веб-дашборд (aiohttp), работает в том же event loop, что Telethon
и aiogram (main.py). Показывает статус компонентов, баланс Bybit, активные и
закрытые сделки, статистику по PnL, позволяет менять маржу/плечо/разбивку TP
и список каналов-источников сигналов на лету.

Доступ закрыт паролем (config.WEB_PASSWORD) - сайт слушает 0.0.0.0, то есть
виден всей локальной сети выделенного ПК, поэтому без пароля любой в сети
смог бы менять плечо/маржу или добавлять каналы.
"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

from aiohttp import web
from telethon import utils as telethon_utils

import channels
import config
import parsers
import risk
import settings
import stats
import status
import synctime
import trade_engine

STATIC_DIR = Path(__file__).parent / "web"
COOKIE_NAME = "trading_bot_session"
AKK_KEY = web.AppKey("akk", object)

# Токен сессии живёт в памяти процесса - после перезапуска бота нужно
# залогиниться заново. Так проще и безопаснее, чем хранить пароль в cookie.
SESSION_TOKEN = secrets.token_urlsafe(32)

PUBLIC_PATHS = {"/api/login"}


def _load_list(path: str) -> list:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _same_secret(supplied, expected: str) -> bool:
    """Сравнение секретов за постоянное время.

    Сравниваем именно байты: secrets.compare_digest на строках работает только
    с чистым ASCII и на кириллице бросает TypeError. Пароль дашборда у нас
    русский, и на строковом сравнении каждый вход падал в 500 - браузер
    показывал невнятную ошибку разбора JSON вместо «неверный пароль».
    """
    if not isinstance(supplied, str):
        return False
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _is_authenticated(request: web.Request) -> bool:
    # compare_digest вместо == : обычное сравнение выходит на первом
    # несовпавшем байте, и по времени ответа токен можно подобрать побайтно
    return _same_secret(request.cookies.get(COOKIE_NAME) or "", SESSION_TOKEN)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in PUBLIC_PATHS or _is_authenticated(request):
        return await handler(request)

    if request.path.startswith("/api/"):
        return web.json_response({"error": "Не авторизован"}, status=401)

    html = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


# ============ СТРАНИЦЫ ============

async def index(request: web.Request) -> web.Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


# ============ АВТОРИЗАЦИЯ ============

async def api_login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Некорректный запрос"}, status=400)

    if not _same_secret(body.get("password"), config.WEB_PASSWORD):
        return web.json_response({"error": "Неверный пароль"}, status=401)

    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE_NAME, SESSION_TOKEN, httponly=True, samesite="Lax",
                     max_age=60 * 60 * 24 * 30)
    return resp


async def api_logout(request: web.Request) -> web.Response:
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE_NAME)
    return resp


# ============ СТАТУС / БАЛАНС ============

async def api_status(request: web.Request) -> web.Response:
    data = status.snapshot()
    data["demo"] = settings.is_demo()
    data["server_time_ms"] = synctime.now_ms()
    data["time_offset_ms"] = synctime.offset_ms()
    data["time_sync_age_sec"] = synctime.seconds_since_sync()
    return web.json_response(data)


async def api_balance(request: web.Request) -> web.Response:
    try:
        balance = await asyncio.get_running_loop().run_in_executor(None, trade_engine.get_wallet_balance)
        return web.json_response(balance)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


async def api_sync_time(request: web.Request) -> web.Response:
    try:
        offset = await asyncio.get_running_loop().run_in_executor(None, synctime.refresh)
        if offset is not None:
            return web.json_response({"offset_ms": offset})
        return web.json_response({"error": "Не удалось получить время сервера"}, status=502)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)


# ============ ТОРГОВЫЕ ПАРАМЕТРЫ ============

async def api_settings_get(request: web.Request) -> web.Response:
    return web.json_response(settings.get_all())


async def api_settings_post(request: web.Request) -> web.Response:
    try:
        patch = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Некорректный JSON в теле запроса"}, status=400)

    try:
        updated = settings.update(patch)
    except settings.SettingsError as e:
        return web.json_response({"error": str(e)}, status=400)

    return web.json_response(updated)


# ============ СДЕЛКИ + СТАТИСТИКА ============

async def api_trades(request: web.Request) -> web.Response:
    active = stats.load_active()
    history_full = stats.load_history()
    history = list(reversed(history_full[-50:]))
    return web.json_response({
        "active": active,
        "history": history,
        "stats": stats.compute(history_full),
    })


# ============ КАНАЛЫ-ИСТОЧНИКИ ============

async def api_channels_get(request: web.Request) -> web.Response:
    return web.json_response(channels.get_all())


async def api_channels_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Некорректный JSON"}, status=400)

    identifier = (body.get("identifier") or "").strip()
    if not identifier:
        return web.json_response({"error": "Укажите username или ссылку на канал"}, status=400)

    akk = request.app.get(AKK_KEY)
    if akk is None:
        return web.json_response({"error": "Telegram-клиент ещё не готов, попробуйте через пару секунд"}, status=503)

    try:
        entity = await akk.get_entity(identifier)
    except Exception as e:
        return web.json_response({"error": f"Не удалось найти канал: {e}"}, status=400)

    chat_id = telethon_utils.get_peer_id(entity)
    username = getattr(entity, "username", None)
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)

    try:
        entry = channels.add(chat_id, username, title)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    return web.json_response(entry)


async def api_channels_delete(request: web.Request) -> web.Response:
    chat_id = int(request.match_info["chat_id"])
    channels.remove(chat_id)
    return web.json_response({"ok": True})


async def api_channels_toggle(request: web.Request) -> web.Response:
    chat_id = int(request.match_info["chat_id"])
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    channels.set_enabled(chat_id, bool(body.get("enabled", True)))
    return web.json_response({"ok": True})


async def api_channels_parser(request: web.Request) -> web.Response:
    """Привязка формата сообщений к каналу. parser=null - перебирать все."""
    chat_id = int(request.match_info["chat_id"])
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}

    parser_name = body.get("parser") or None
    if parser_name and parsers.get(parser_name) is None:
        return web.json_response({"error": f"Парсер «{parser_name}» не найден"}, status=400)

    channels.set_parser(chat_id, parser_name)
    return web.json_response({"ok": True, "parser": parser_name})


# ============ ФОРМАТЫ СООБЩЕНИЙ (ПАРСЕРЫ) ============

async def api_parsers_get(request: web.Request) -> web.Response:
    return web.json_response(parsers.get_all())


async def api_parsers_post(request: web.Request) -> web.Response:
    """Создать или обновить парсер (ключ - name)."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Некорректный JSON"}, status=400)

    try:
        saved = parsers.save(body)
    except parsers.ParserError as e:
        return web.json_response({"error": str(e)}, status=400)

    return web.json_response(saved)


async def api_parsers_delete(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    try:
        parsers.remove(name)
    except parsers.ParserError as e:
        return web.json_response({"error": str(e)}, status=400)

    # снимаем привязку у каналов, которые ссылались на удалённый парсер
    for c in channels.get_all():
        if c.get("parser") == name:
            channels.set_parser(c["chat_id"], None)

    return web.json_response({"ok": True})


async def api_parsers_test(request: web.Request) -> web.Response:
    """Прогон парсера по примеру сообщения - кнопка «Тестировать» в панели."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Некорректный JSON"}, status=400)

    parser = body.get("parser") or {}
    sample = body.get("sample") or ""
    return web.json_response(parsers.test_parser(parser, sample))


# ============ ЗАЩИТА ОТ СЛИВА ============

async def api_risk_get(request: web.Request) -> web.Response:
    return web.json_response(risk.snapshot())


async def api_risk_post(request: web.Request) -> web.Response:
    try:
        patch = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Некорректный JSON"}, status=400)

    try:
        risk.update_settings(patch)
    except risk.RiskError as e:
        return web.json_response({"error": str(e)}, status=400)

    return web.json_response(risk.snapshot())


async def api_risk_unblock(request: web.Request) -> web.Response:
    """Снимает кулдаун и Emergency Stop - торговля снова разрешена."""
    risk.unblock()
    return web.json_response(risk.snapshot())


async def api_risk_emergency_stop(request: web.Request) -> web.Response:
    """Блокирует торговлю и закрывает все позиции по рынку."""
    risk.set_emergency_stop(True)

    engine = trade_engine.engine
    if engine is None:
        return web.json_response({"error": "Торговый движок ещё не запущен",
                                  "risk": risk.snapshot()}, status=503)

    try:
        result = await asyncio.get_running_loop().run_in_executor(None, engine.close_all_positions)
    except Exception as e:
        return web.json_response({"error": str(e), "risk": risk.snapshot()}, status=502)

    return web.json_response({"ok": True, **result, "risk": risk.snapshot()})


def build_app(akk=None) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app[AKK_KEY] = akk

    app.router.add_get("/", index)
    app.router.add_static("/assets/", STATIC_DIR / "assets", name="assets")
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/sync-time", api_sync_time)
    app.router.add_get("/api/balance", api_balance)
    app.router.add_get("/api/settings", api_settings_get)
    app.router.add_post("/api/settings", api_settings_post)
    app.router.add_get("/api/trades", api_trades)
    app.router.add_get("/api/channels", api_channels_get)
    app.router.add_post("/api/channels", api_channels_post)
    app.router.add_delete("/api/channels/{chat_id}", api_channels_delete)
    app.router.add_post("/api/channels/{chat_id}/toggle", api_channels_toggle)
    app.router.add_post("/api/channels/{chat_id}/parser", api_channels_parser)
    app.router.add_get("/api/parsers", api_parsers_get)
    app.router.add_post("/api/parsers", api_parsers_post)
    app.router.add_post("/api/parsers/test", api_parsers_test)
    app.router.add_delete("/api/parsers/{name}", api_parsers_delete)
    app.router.add_get("/api/risk", api_risk_get)
    app.router.add_post("/api/risk", api_risk_post)
    app.router.add_post("/api/risk/unblock", api_risk_unblock)
    app.router.add_post("/api/risk/emergency-stop", api_risk_emergency_stop)
    return app
