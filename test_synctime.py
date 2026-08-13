"""Тесты синхронизации часов с Bybit (synctime.py).

Запуск:  python -m unittest test_synctime -v

Сеть не используется: get_server_time() подменяется управляемой заглушкой,
а системные часы - фальшивыми (FakeClock), чтобы можно было имитировать
уход часов вперёд и перевод времени назад.
"""

import contextlib
import unittest

import synctime

# Bybit отвергает подпись, если клиентская метка обгоняет биржу больше чем
# на секунду (ошибка 10002), поэтому все проверки "не убегаем вперёд"
# считают именно от этой границы.
BYBIT_AHEAD_LIMIT_MS = 1000


class FakeClock:
    """Часы, которыми можно управлять из теста.

    wall  - системное время (то, что врёт и прыгает);
    mono  - монотонное время (только вперёд, переводом часов не сбивается).
    """

    def __init__(self, wall: float = 1_700_000_000.0):
        self.wall = wall
        self.mono = 1000.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        """Прошло `seconds` реального времени: идут обе шкалы."""
        self.wall += seconds
        self.mono += seconds

    def set_wall(self, wall: float) -> None:
        """Часы перевели (NTP/руками): монотонная шкала не двигается."""
        self.wall = wall


class FakeBybit:
    """Заглушка pybit.HTTP: отдаёт время сервера с заданным лагом и RTT."""

    def __init__(self, clock: FakeClock, server_ahead_ms: int = 0,
                 rtt_ms: int = 0, fail: bool = False):
        self.clock = clock
        self.server_ahead_ms = server_ahead_ms
        self.rtt_ms = rtt_ms
        self.fail = fail
        self.calls = 0
        self.on_call = None  # хук: вызывается внутри запроса

    def server_now_ms(self) -> int:
        return int(self.clock.wall * 1000) + self.server_ahead_ms

    def get_server_time(self):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        if self.fail:
            raise RuntimeError("сеть недоступна")
        # запрос летит до биржи (половина RTT), там снимается время,
        # ответ летит назад (вторая половина)
        self.clock.advance(self.rtt_ms / 2000.0)
        server_ms = self.server_now_ms()
        self.clock.advance(self.rtt_ms / 2000.0)
        return {"result": {"timeNano": str(server_ms * 1_000_000)}}


class SyncTimeTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bybit = FakeBybit(self.clock)

        synctime.stop_background_sync()
        synctime._session = self.bybit
        synctime._offset_ms = 0
        synctime._offset_known = False
        synctime._last_refresh_mono = 0.0

        self._real_time = synctime.time
        synctime.time = self.clock
        self.addCleanup(self._restore)

    def _restore(self):
        synctime.stop_background_sync()
        synctime.time = self._real_time
        synctime._session = None

    def drift_from_server_ms(self) -> int:
        """На сколько мс наша метка обгоняет (>0) или отстаёт (<0) от биржи."""
        return synctime.now_ms() - self.bybit.server_now_ms()

    # ---------- базовое поведение ----------

    def test_offset_applied_to_now(self):
        """Часы ПК отстают на 5 секунд - now() обязан их догнать."""
        self.bybit.server_ahead_ms = 5000
        synctime.refresh()

        self.assertTrue(synctime.is_synced())
        self.assertLess(abs(self.drift_from_server_ms()), 200)

    def test_offset_applied_when_local_clock_runs_fast(self):
        """Реальный случай из логов: часы ПК убежали вперёд на секунду."""
        self.bybit.server_ahead_ms = -1001
        synctime.refresh()

        self.assertLess(abs(self.drift_from_server_ms()), 200)

    def test_rtt_is_compensated(self):
        """Задержка сети не должна утекать в смещение как ошибка.

        Часы точны, но ответ идёт 400 мс. Наивный расчёт
        (server_time - время_после_ответа) даст -400 мс на ровном месте.
        """
        self.bybit.server_ahead_ms = 0
        self.bybit.rtt_ms = 400
        synctime.refresh()

        self.assertLess(abs(synctime.offset_ms()), 250,
                        "смещение поймало RTT вместо реального расхождения часов")

    def test_never_ahead_of_server(self):
        """Главная защита от 10002: метка не должна обгонять биржу."""
        for rtt in (0, 50, 400, 1500):
            with self.subTest(rtt=rtt):
                self.setUp()
                self.bybit.rtt_ms = rtt
                synctime.refresh()
                self.assertLess(self.drift_from_server_ms(), BYBIT_AHEAD_LIMIT_MS)

    # ---------- устойчивость ----------

    def test_failed_refresh_keeps_last_offset(self):
        """Биржа недоступна - работаем на последнем известном смещении."""
        self.bybit.server_ahead_ms = 3000
        synctime.refresh()
        known = synctime.offset_ms()

        self.bybit.fail = True
        # заодно ловим предупреждение в лог: молчаливый отказ сверки на живом
        # сервере не заметить, а assertLogs не пускает его в вывод тестов
        with self.assertLogs("synctime", level="WARNING"):
            self.assertIsNone(synctime.refresh())

        self.assertEqual(synctime.offset_ms(), known)
        self.assertTrue(synctime.is_synced())

    def test_refresh_does_not_recurse(self):
        """pybit внутри запроса времени сам спросит время - без рекурсии."""
        self.bybit.on_call = synctime.now_ms
        synctime.refresh()

        self.assertLessEqual(self.bybit.calls, synctime._SAMPLES)

    def test_resync_not_tied_to_wall_clock(self):
        """Часы перевели назад - сверка обязана продолжаться.

        Если срок следующей сверки считать по системным часам, перевод
        стрелок назад делает последнюю сверку «вечно свежей», и бот
        останется с устаревшим смещением ровно на величину перевода.
        """
        with self._fast_refresh():
            synctime.start_background_sync()
            calls_after_start = self.bybit.calls

            self.clock.set_wall(self.clock.wall - 3600)  # часы уехали на час назад

            self.assertTrue(
                self._wait_until(
                    lambda: self.bybit.calls >= calls_after_start + 2 * synctime._SAMPLES),
                "после перевода часов назад сверка больше не запускается")

    def test_now_does_not_hit_network(self):
        """now() зовётся из event loop и из подписи ордера - без сетевых пауз.

        Каждый REST-запрос pybit проходит через now_ms(); если внутри может
        случиться поход в сеть, отправка ордера иногда тормозит на RTT,
        а асинхронный дашборд подвешивает весь event loop.
        """
        synctime.refresh()
        calls_after_first = self.bybit.calls

        self.clock.advance(synctime._REFRESH_INTERVAL * 5)
        for _ in range(100):
            synctime.now_ms()

        self.assertEqual(self.bybit.calls, calls_after_first,
                         "now() сходил в сеть в потоке вызывающего")

    def test_drift_between_refreshes_stays_small(self):
        """Часы ПК спешат ~2 мс/мин (замер из логов) - между сверками не копим."""
        drift_per_sec = 0.002 / 60
        synctime.refresh()

        for _ in range(int(synctime._REFRESH_INTERVAL)):
            self.clock.advance(1)
            self.bybit.server_ahead_ms = -int(
                (self.clock.wall - 1_700_000_000.0) * drift_per_sec * 1000)

        self.assertLess(abs(self.drift_from_server_ms()), BYBIT_AHEAD_LIMIT_MS / 2)

    def test_background_sync_refreshes_periodically(self):
        """Фоновый поток сам поддерживает смещение свежим."""
        with self._fast_refresh():
            synctime.start_background_sync()

            self.assertTrue(
                self._wait_until(lambda: self.bybit.calls >= 3 * synctime._SAMPLES),
                "фоновая сверка не повторяется")

    def test_background_sync_starts_once(self):
        """Авторестарт бота не должен плодить потоки сверки."""
        synctime.start_background_sync()
        first = synctime._worker
        synctime.start_background_sync()

        self.assertIs(synctime._worker, first)
        self.assertTrue(first.is_alive())

    @contextlib.contextmanager
    def _fast_refresh(self, interval: float = 0.02):
        saved = synctime._REFRESH_INTERVAL
        synctime._REFRESH_INTERVAL = interval
        try:
            yield
        finally:
            synctime.stop_background_sync()
            synctime._REFRESH_INTERVAL = saved

    @staticmethod
    def _wait_until(predicate, timeout: float = 5.0) -> bool:
        import time as real_time
        deadline = real_time.monotonic() + timeout
        while real_time.monotonic() < deadline:
            if predicate():
                return True
            real_time.sleep(0.01)
        return predicate()


class PybitPatchTestCase(unittest.TestCase):
    """Подмена часов pybit должна доходить до кода, который подписывает запросы."""

    def test_patch_reaches_http_manager_and_websocket(self):
        import trade_engine
        from pybit import _helpers, _http_manager, _websocket_stream

        original = _helpers.generate_timestamp
        try:
            trade_engine.patch_pybit_clock()

            self.assertIs(_helpers.generate_timestamp, synctime.now_ms)
            # оба модуля обязаны читать функцию через модуль _helpers,
            # иначе патч до них не доходит
            self.assertIs(_http_manager._helpers.generate_timestamp, synctime.now_ms)
            self.assertIs(_websocket_stream._helpers.generate_timestamp, synctime.now_ms)
        finally:
            _helpers.generate_timestamp = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
