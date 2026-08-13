"""
Файл-аудит разобранных сигналов.

Раньше здесь жил и сам парсер (parse_signal с зашитыми regex формата ggshot),
но разбор давно делает parsers.py: формат канала описывается конфигом в
parsers.json и правится через веб-панель, без правки кода. Старая функция
осталась неиспользуемой и удалена - два парсера с разной логикой рядом
означали бы, что однажды поправят не тот.

Здесь остаётся только запись в журнал: он нужен, чтобы потом посмотреть,
что именно бот вычитал из сообщения канала, и починить regex, если поле
разобралось не так.
"""

import json
import os

import config

# Журнал пишется на каждый сигнал целиком, вместе с исходным текстом
# сообщения, поэтому его надо ограничивать: файл читается и переписывается
# при каждой записи, и разросшийся до десятков мегабайт он тормозил бы
# обработку сигнала ровно в тот момент, когда важна скорость.
MAX_ENTRIES = 1000


def log_parsed_signal(signal: dict, json_file: str = None) -> None:
    """Дописывает разобранный сигнал в журнал.

    Это только история для отладки - движку сигнал передаётся напрямую,
    через файл ничего не читается.
    """
    json_file = json_file or config.PARSED_SIGNALS_LOG_FILE

    if os.path.exists(json_file) and os.path.getsize(json_file) > 0:
        with open(json_file, "r", encoding="utf-8") as f:
            try:
                all_data = json.load(f)
            except json.JSONDecodeError:
                all_data = []
    else:
        all_data = []

    if not isinstance(all_data, list):
        all_data = []

    all_data.append(signal)

    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(all_data[-MAX_ENTRIES:], f, ensure_ascii=False, indent=2)
