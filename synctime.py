"""
Единая логика времени, синхронизированного с сервером Bybit.

Проблема, которую решает модуль: часы ПК/сервера уходят от биржи (замер по
логам - около 2 мс в минуту, то есть ~3 секунды в сутки). Bybit подписывает
запросы временной меткой клиента (X-BAPI-TIMESTAMP, recv_window и подпись
вебсокета) и принимает её только в окне
    server_time - recv_window  <  timestamp  <  server_time + 1000 мс
Окно несимметрично: отставание прощается на recv_window (10 сек), а обгон -
всего на секунду. Убежавшие вперёд часы ПК дают 10002/10016, и увеличение
recv_window от этого не спасает.

Правила:
- now() / now_ms() - только арифметика: локальное время плюс смещение.
  Никакой сети: эти функции зовутся из подписи каждого ордера и из
  асинхронного дашборда, поход в сеть там тормозил бы отправку ордера и
  подвешивал event loop.
- смещение обновляет фоновый поток (start_background_sync); сетевая ошибка
  никогда не роняет вызов - остаётся последнее известное смещение.
- срок следующей сверки считается по монотонным часам, а не по системным:
  иначе перевод системных часов назад (NTP, ручная правка) замораживает
  сверку ровно на величину перевода.
- смещение меряется по середине round-trip и дополнительно уводится на
  _SAFETY_LAG_MS назад - лучше отставать на десятые доли секунды (окно 10 сек),
  чем обогнать биржу (окно 1 сек).
- pybit._helpers.generate_timestamp подменяется на now_ms() - это чинит
  timestamp и recv_window для всех REST-запросов и авторизацию приватного
  вебсокета. Подмена делается явно в main.py, не при импорте.
"""

import logging
import threading
import time

logger = logging.getLogger("synctime")

_lock = threading.Lock()
_offset_ms: int = 0            # server_time - local_time, мс
_offset_known: bool = False
_last_refresh_mono: float = 0.0   # time.monotonic() последней попытки сверки

_REFRESH_INTERVAL = 60         # секунд между сверками часов с биржей
_SAMPLES = 3                   # замеров за сверку; берём тот, где RTT меньше
_SAFETY_LAG_MS = 100           # намеренно держимся чуть позади биржи

_stop_event = threading.Event()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()

# http-клиент создаётся лениво, чтобы импорт не зависел от config/settings
_session = None


def _get_session():
    """Сессия только для чтения времени сервера.

    Без api-ключей: get_server_time - публичный метод, подпись ему не нужна,
    а значит запрос времени не проходит через подмененные часы pybit.
    """
    global _session
    if _session is None:
        from pybit.unified_trading import HTTP

        import config
        import settings

        _session = HTTP(testnet=config.TESTNET, demo=settings.is_demo())
    return _session


def _measure() -> tuple[int, int]:
    """Один замер: (смещение_мс, rtt_мс).

    Время сервера снимается где-то между отправкой и получением ответа,
    поэтому сравниваем его с локальной серединой round-trip - иначе задержка
    сети целиком уходит в смещение и выглядит как расхождение часов.
    """
    sent = time.time()
    result = _get_session().get_server_time()["result"]
    received = time.time()

    server_ms = int(result["timeNano"]) // 1_000_000 if "timeNano" in result \
        else int(result["timeSecond"]) * 1000
    local_mid_ms = int((sent + received) / 2 * 1000)
    return server_ms - local_mid_ms, int((received - sent) * 1000)


def refresh() -> int | None:
    """Пересчитать смещение с сервером Bybit. Возвращает offset_ms или None."""
    global _offset_ms, _offset_known, _last_refresh_mono

    best: tuple[int, int] | None = None
    last_error: Exception | None = None
    for _ in range(_SAMPLES):
        try:
            sample = _measure()
        except Exception as e:
            last_error = e
            continue
        if best is None or sample[1] < best[1]:
            best = sample

    if best is None:
        # даже неудачную попытку отмечаем по времени, чтобы при лежащей сети
        # фоновый поток не долбил биржу чаще обычного
        with _lock:
            _last_refresh_mono = time.monotonic()
        logger.warning(f"Не удалось синхронизировать время с Bybit: {last_error}")
        return None

    offset, rtt = best
    offset -= _SAFETY_LAG_MS
    with _lock:
        first_sync = not _offset_known
        jumped = abs(offset - _offset_ms) > 250
        _offset_ms = offset
        _offset_known = True
        _last_refresh_mono = time.monotonic()

    message = "Синхронизация времени с Bybit: смещение %+d мс (rtt %d мс)"
    if first_sync or jumped:
        logger.info(message, offset, rtt)
    else:
        logger.debug(message, offset, rtt)
    return offset


def _worker_loop() -> None:
    while not _stop_event.wait(_REFRESH_INTERVAL):
        refresh()


def start_background_sync() -> None:
    """Свериться с биржей сейчас и держать смещение свежим в фоне.

    Идемпотентно: повторный вызов (например после авторестарта в main.py)
    делает новую сверку, но второй поток не поднимает.
    """
    global _worker
    refresh()
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop_event.clear()
        _worker = threading.Thread(target=_worker_loop, name="synctime", daemon=True)
        _worker.start()


def stop_background_sync() -> None:
    global _worker
    _stop_event.set()
    with _worker_lock:
        worker, _worker = _worker, None
    if worker is not None:
        worker.join(timeout=5)


def now() -> float:
    """Текущее время в секундах (Unix), сдвинутое к времени сервера Bybit."""
    with _lock:
        return time.time() + _offset_ms / 1000.0


def now_ms() -> int:
    """Текущее время в миллисекундах, сдвинутое к времени сервера Bybit."""
    return int(now() * 1000)


def is_synced() -> bool:
    with _lock:
        return _offset_known


def offset_ms() -> int | None:
    """Известное смещение в мс, или None если сверка ещё не удавалась."""
    with _lock:
        return _offset_ms if _offset_known else None


def seconds_since_sync() -> float | None:
    """Сколько секунд прошло с последней попытки сверки (None - ни одной)."""
    with _lock:
        if not _offset_known:
            return None
        return time.monotonic() - _last_refresh_mono
