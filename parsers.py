"""
Настраиваемые парсеры сигналов - формат сообщения описывается конфигом
(parsers.json), а не кодом.

Зачем: раньше формат канала ggshot был зашит в signal_parser.py, и канал с
другим оформлением сообщения не распознавался вообще. Теперь под каждый
канал можно задать свой набор регулярных выражений через веб-панель, не
трогая код и не перезапуская бота.

Как это работает
----------------
Каждый парсер - это набор regex-полей. Обязательный минимум, без которого
сделку открыть нельзя: symbol, side, targets, stop_loss.

    {
      "name": "ggshot_v1",
      "title": "GG Shot",
      "prefilter": "📩",              # быстрый отсев не-сигналов (необязательно)
      "fields": {
        "symbol":    "#([A-Z0-9]+USDT)\\b",
        "side":      "(Long|Short)\\s+Entry\\s+Zone",
        "stop_loss": "Stop-?Loss\\s*:\\**\\s*\\**\\s*([\\d.]+)",
        "signal_id": "Signal\\s+ID\\s*:\\s*\\**\\s*#(\\S+)"
      },
      "targets": "Target\\s*\\d+\\s*:\\s*\\*+\\s*([\\d.]+)",   # findall, 2..10 штук
      "entry_zone": "Entry\\s+Zone:\\**\\s*\\**([\\d.]+)\\s*-\\s*([\\d.]+)"
    }

Из каждого regex берётся первая группа захвата. `targets` применяется через
findall; тейков должно найтись от MIN_TARGETS до MAX_TARGETS. Разные каналы
дают разное количество - ggshot четыре, Fat Pig шесть, - поэтому число не
фиксировано, а движок расставляет столько TP, сколько пришло.

Необязательное поле `tp_percents` - разбивка объёма по тейкам именно для
этого формата (список чисел, в сумме 100). Нужно, когда тейков не четыре:
глобальная разбивка в настройках рассчитана на четыре и под шесть не подойдёт.
Если не задано - берётся глобальная, а при несовпадении длины объём делится
поровну.

Привязка к каналу: channels.json у канала хранит "parser": "<name>". Если
парсер не указан, пробуются все по очереди - первый подошедший выигрывает.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation

import config

logger = logging.getLogger("parsers")

_lock = threading.Lock()

# Обязательные поля - без них сделка не открывается
REQUIRED_FIELDS = ("symbol", "side", "stop_loss")

# Сколько тейк-профитов допускаем. Меньше двух - лестница переноса стопа
# теряет смысл; больше десяти - почти наверняка regex цепляет лишнее.
MIN_TARGETS = 2
MAX_TARGETS = 10

# Парсер канала ggshot - тот самый формат, что был зашит в signal_parser.py.
# Живёт здесь как пресет: при первом запуске записывается в parsers.json,
# дальше правится через веб-панель.
DEFAULT_PARSER = {
    "name": "ggshot_v1",
    "title": "GG Shot (формат по умолчанию)",
    "prefilter": "📩",
    "fields": {
        "symbol": r"#([A-Z0-9]+USDT)\b",
        "side": r"(Long|Short)\s+Entry\s+Zone",
        "stop_loss": r"Stop-?Loss\s*:\**\s*\**\s*([\d.,*]+)",
        "signal_id": r"Signal\s+ID\s*:\s*\**\s*#(\S+)",
    },
    # Telethon возвращает обычный текст без Markdown-звёздочек. Поэтому
    # выделение ** допускаем, но не требуем. Запятая встречается как
    # разделитель тысяч в ценах BTC (62,806).
    "targets": r"Target\s*\d+\s*:\s*\**\s*([\d.,*]+)",
    "entry_zone": r"Entry\s+Zone:\**\s*\**([\d.,*]+)\s*[-–—]\s*([\d.,*]+)",
    "tp_percents": [30.0, 30.0, 20.0, 20.0],
}

# Парсер канала Fat Pig Signals. Отличий от ggshot три, и каждое ломало разбор:
# монета пишется через дробь (#ACE/USDT), направление стоит отдельной строкой
# без слов "Entry Zone", а тейки не выделены звёздочками. Плюс их шесть,
# поэтому здесь же лежит своя разбивка объёма.
FATPIG_PARSER = {
    "name": "fatpig_v1",
    "title": "Fat Pig Signals",
    "prefilter": "",
    "fields": {
        # ACE/USDT и ACEUSDT - обе записи; дробь и пробелы уберёт _clean_symbol
        "symbol": r"#\s*([A-Z0-9]+\s*[/\-]?\s*USDT)\b",
        # "🟢 LONG" отдельной строкой: цепляемся за начало строки, чтобы не
        # поймать слово long где-нибудь в тексте описания
        "side": r"(?m)^[^\w\n]*\b(LONG|SHORT)\b",
        "stop_loss": r"Stop\s*-?\s*Loss\s*:\s*\**\s*([\d.,*]+)",
    },
    "targets": r"Target\s*\d+\s*:\s*\**\s*([\d.,*]+)",
    "entry_zone": r"Entry\s*:\s*\**\s*([\d.,*]+)\s*[-–—]\s*([\d.,*]+)",
    "tp_percents": [20.0, 20.0, 15.0, 15.0, 15.0, 15.0],
}

PRESETS = [DEFAULT_PARSER, FATPIG_PARSER]


class ParserError(ValueError):
    """Ошибка в конфигурации парсера - текст показывается пользователю в панели."""


# ==================== ХРАНИЛИЩЕ ====================

def _load() -> list:
    if not os.path.exists(config.PARSERS_FILE) or os.path.getsize(config.PARSERS_FILE) == 0:
        return [dict(p) for p in PRESETS]
    with open(config.PARSERS_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            logger.warning("parsers.json повреждён - откатываюсь на парсеры по умолчанию")
            return [dict(p) for p in PRESETS]
    if not isinstance(data, list) or not data:
        return [dict(p) for p in PRESETS]
    return data


def _save(data: list) -> None:
    with open(config.PARSERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_all() -> list:
    with _lock:
        return _load()


def get(name: str) -> dict | None:
    for p in get_all():
        if p.get("name") == name:
            return p
    return None


# ==================== ВАЛИДАЦИЯ ====================

def validate(parser: dict) -> dict:
    """Проверяет конфиг парсера перед сохранением. Бросает ParserError с
    понятным текстом, чтобы панель показала его пользователю как есть."""
    name = (parser.get("name") or "").strip()
    if not name:
        raise ParserError("Укажите имя парсера")
    if not re.fullmatch(r"[\w-]+", name):
        raise ParserError("Имя парсера: только латиница, цифры, дефис и подчёркивание")

    fields = parser.get("fields") or {}
    if not isinstance(fields, dict):
        raise ParserError("Поле fields должно быть объектом")

    missing = [f for f in REQUIRED_FIELDS if not (fields.get(f) or "").strip()]
    if missing:
        raise ParserError(f"Не заданы обязательные поля: {', '.join(missing)}")

    if not (parser.get("targets") or "").strip():
        raise ParserError("Не задано выражение для тейк-профитов (targets)")

    # Компилируем всё сразу - кривой regex должен падать при сохранении,
    # а не молча ронять парсинг живого сигнала
    # prefilter сюда намеренно не попадает: он ищется как обычная подстрока
    # (см. parse_with), а не как регулярное выражение. Раньше он здесь
    # компилировался, и панель принимала запись вроде "Signal|Сигнал" как
    # верную - а на живых сообщениях она не совпадала никогда, и канал молча
    # переставал разбираться
    to_check = dict(fields)
    to_check["targets"] = parser["targets"]
    if parser.get("entry_zone"):
        to_check["entry_zone"] = parser["entry_zone"]

    for key, pattern in to_check.items():
        if not pattern:
            continue
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise ParserError(f"Ошибка в выражении «{key}»: {e}")
        # prefilter ищется как подстрока, группа ему не нужна
        if key != "prefilter" and compiled.groups < 1:
            raise ParserError(f"В выражении «{key}» нет группы захвата — добавьте скобки ( )")

    cleaned = {
        "name": name,
        "title": (parser.get("title") or name).strip(),
        "prefilter": (parser.get("prefilter") or "").strip(),
        "fields": {k: v.strip() for k, v in fields.items() if (v or "").strip()},
        "targets": parser["targets"].strip(),
    }
    if parser.get("entry_zone"):
        cleaned["entry_zone"] = parser["entry_zone"].strip()

    # Своя разбивка объёма - нужна форматам, где тейков не четыре
    percents = parser.get("tp_percents")
    if percents:
        try:
            percents = [float(p) for p in percents]
        except (TypeError, ValueError):
            raise ParserError("Доли TP должны быть числами")
        if not (MIN_TARGETS <= len(percents) <= MAX_TARGETS):
            raise ParserError(f"Долей TP должно быть от {MIN_TARGETS} до {MAX_TARGETS}")
        if any(p <= 0 for p in percents):
            raise ParserError("Все доли TP должны быть больше 0")
        if abs(sum(percents) - 100) > 0.01:
            raise ParserError(f"Сумма долей TP должна быть 100, сейчас {sum(percents):.2f}")
        cleaned["tp_percents"] = percents
    return cleaned


def save(parser: dict) -> dict:
    """Добавляет новый парсер или обновляет существующий (по name)."""
    cleaned = validate(parser)
    with _lock:
        items = _load()
        for i, p in enumerate(items):
            if p.get("name") == cleaned["name"]:
                items[i] = cleaned
                break
        else:
            items.append(cleaned)
        _save(items)
    logger.info(f"Парсер сохранён: {cleaned['name']}")
    return cleaned


def remove(name: str) -> None:
    with _lock:
        items = _load()
        rest = [p for p in items if p.get("name") != name]
        if not rest:
            raise ParserError("Нельзя удалить последний парсер")
        _save(rest)
    logger.info(f"Парсер удалён: {name}")


# ==================== РАЗБОР СООБЩЕНИЯ ====================

def _to_float(value: str):
    text = (str(value).strip().replace(" ", "").replace("\u00a0", "")
            .replace("*", ""))

    # Каналы смешивают 62,806 (запятая как разделитель тысяч) и 0,5356
    # (запятая как десятичный разделитель). Если присутствуют оба знака,
    # последний считаем десятичным. Одиночную запятую перед тремя цифрами
    # считаем разделителем тысяч, кроме значений вида 0,123.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts) > 2 and all(len(part) == 3 for part in parts[1:]):
            text = "".join(parts)
        elif len(parts) == 2 and len(parts[1]) == 3 and parts[0].lstrip("+-") != "0":
            text = "".join(parts)
        else:
            text = text.replace(",", ".")

    try:
        return float(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def _clean_symbol(raw: str) -> str:
    """Приводит тикер к виду, который понимает Bybit.

    Каналы пишут монету по-разному: ACEUSDT, ACE/USDT, ACE-USDT, иногда с
    пробелами. На бирже символ всегда слитный, поэтому разделители убираем.
    """
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def _fingerprint(signal: dict) -> str:
    """Отпечаток сигнала - подменяет Signal ID там, где канал его не даёт.

    Без ID защита от повторов держится только на проверке «по символу уже
    есть сделка». Этого мало: каналы шлют вдогонку сообщения, где исходный
    сигнал процитирован целиком, и после закрытия по стопу такое сообщение
    открыло бы сделку заново. Отпечаток по содержимому ловит такие повторы.

    Обратная сторона: если канал через неделю пришлёт ровно те же цифры по
    той же монете, бот сочтёт это повтором и пропустит. Для живых денег
    ошибиться в эту сторону безопаснее.
    """
    body = "|".join([
        signal.get("parser") or "",
        signal["symbol"],
        signal["strategy"],
        ",".join(f"{t:g}" for t in signal["targets"]),
        f"{signal['stop_loss']:g}",
    ])
    return "auto-" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def _search(pattern: str, text: str):
    """Ищет первое совпадение и возвращает первую группу захвата."""
    if not pattern:
        return None
    try:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    except re.error:
        return None
    return m.group(1) if m else None


def parse_with(parser: dict, raw_text: str) -> dict | None:
    """Пробует разобрать сообщение конкретным парсером.
    Возвращает None, если сообщение не подходит."""
    if not raw_text:
        return None

    # Быстрый отсев: если задан prefilter и его нет в тексте - это не сигнал.
    # Это подстрока, а не regex: в пресетах здесь стоит эмодзи 📩. Регистр не
    # учитываем - в панели легко набрать "signal" вместо "Signal"
    prefilter = parser.get("prefilter")
    if prefilter and prefilter.lower() not in raw_text.lower():
        return None

    fields = parser.get("fields", {})

    symbol = _search(fields.get("symbol"), raw_text)
    side_raw = _search(fields.get("side"), raw_text)
    stop_loss_raw = _search(fields.get("stop_loss"), raw_text)

    if not symbol or not side_raw or not stop_loss_raw:
        return None

    symbol = _clean_symbol(symbol)
    if not symbol:
        return None

    side = side_raw.strip().capitalize()
    if side not in ("Long", "Short"):
        return None

    try:
        target_matches = re.findall(parser["targets"], raw_text, re.IGNORECASE)
    except (re.error, KeyError):
        return None

    # Тейков должно быть разумное количество: одного мало для лестницы стопа,
    # а десяток - признак того, что regex цепляет лишнее (например, цифры из
    # чужого сообщения, склеенного в одну простыню)
    if not (MIN_TARGETS <= len(target_matches) <= MAX_TARGETS):
        return None

    # findall с несколькими группами вернёт кортежи - берём первую группу
    targets = [_to_float(t[0] if isinstance(t, tuple) else t) for t in target_matches]
    stop_loss = _to_float(stop_loss_raw)

    if stop_loss is None or any(t is None for t in targets):
        return None

    # Тейки обязаны идти лестницей: для лонга вверх, для шорта вниз, без
    # повторов. Движок водит стоп по этой лестнице (on_tp_filled переносит его
    # на уровень предыдущего тейка), и сбитый порядок означает, что regex
    # зацепил постороннее число - разбирать такое сообщение дальше нельзя
    expected = sorted(targets, reverse=(side == "Short"))
    if targets != expected or len(set(targets)) != len(targets):
        logger.warning(
            f"Парсер «{parser.get('name')}»: тейки {targets} не образуют лестницу "
            f"для {side} - сообщение разобрано неверно, пропускаю"
        )
        return None

    signal = {
        "symbol": symbol,
        "strategy": side,
        "targets": targets,
        "stop_loss": stop_loss,
        "signal_id": _search(fields.get("signal_id"), raw_text),
        "parser": parser.get("name"),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "raw_message": raw_text,
    }

    # Разбивка объёма именно этого формата - движок предпочтёт её глобальной
    own_percents = parser.get("tp_percents")
    if own_percents:
        if len(own_percents) == len(targets):
            signal["tp_percents"] = list(own_percents)
        else:
            logger.warning(
                f"Парсер «{parser.get('name')}»: тейков {len(targets)}, "
                f"а формат ожидает {len(own_percents)} - сигнал пропускаю"
            )
            return None

    # Канал может не давать ID (у Fat Pig его нет) - тогда считаем отпечаток
    # по самому сигналу, иначе повтор того же сообщения откроет вторую сделку
    if not signal["signal_id"]:
        signal["signal_id"] = _fingerprint(signal)

    # Зона входа - справочно (в уведомлениях и панели). Сделка по-прежнему
    # открывается по рынку: цена сигнала почти никогда не совпадает с текущей
    if parser.get("entry_zone"):
        try:
            m = re.search(parser["entry_zone"], raw_text, re.IGNORECASE | re.DOTALL)
            if m and m.lastindex and m.lastindex >= 2:
                low, high = _to_float(m.group(1)), _to_float(m.group(2))
                if low is not None and high is not None:
                    signal["entry_zone"] = [low, high]
        except re.error:
            pass

    return signal


def parse(raw_text: str, parser_name: str | None = None) -> dict | None:
    """Разбирает сообщение. Если у канала задан парсер - используется он,
    иначе перебираются все до первого подошедшего."""
    if parser_name:
        parser = get(parser_name)
        if parser is None:
            logger.warning(f"Парсер «{parser_name}» не найден - перебираю все")
        else:
            return parse_with(parser, raw_text)

    for parser in get_all():
        signal = parse_with(parser, raw_text)
        if signal is not None:
            return signal
    return None


def test_parser(parser: dict, sample_text: str) -> dict:
    """Прогоняет парсер по примеру сообщения - для кнопки «Тестировать» в
    панели. Возвращает {ok, signal|error} без исключений наружу."""
    try:
        cleaned = validate(parser)
    except ParserError as e:
        return {"ok": False, "error": str(e)}

    if not (sample_text or "").strip():
        return {"ok": False, "error": "Вставьте пример сообщения из канала"}

    signal = parse_with(cleaned, sample_text)
    if signal is None:
        # Разбираем по полям, чтобы показать, что именно не нашлось
        details = []
        if cleaned.get("prefilter") and cleaned["prefilter"] not in sample_text:
            details.append(f"в тексте нет отсеивающей строки «{cleaned['prefilter']}»")
        for field in REQUIRED_FIELDS:
            if not _search(cleaned["fields"].get(field), sample_text):
                details.append(f"не найдено поле «{field}»")
        try:
            found = len(re.findall(cleaned["targets"], sample_text, re.IGNORECASE))
            if not (MIN_TARGETS <= found <= MAX_TARGETS):
                details.append(f"тейк-профитов найдено {found}, "
                               f"а нужно от {MIN_TARGETS} до {MAX_TARGETS}")
            elif cleaned.get("tp_percents") and len(cleaned["tp_percents"]) != found:
                details.append(f"тейков {found}, а долей разбивки "
                               f"{len(cleaned['tp_percents'])}")
        except re.error:
            details.append("ошибка в выражении для тейк-профитов")

        return {"ok": False, "error": "Сигнал не распознан: " + "; ".join(details or ["неизвестная причина"])}

    preview = {k: v for k, v in signal.items() if k != "raw_message"}
    return {"ok": True, "signal": preview}
