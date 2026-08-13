"""Тесты защиты от слива депозита (risk.py).

Запуск:  python -m unittest test_risk -v

Файлы состояния и истории на время теста уводятся во временную папку,
чтобы не трогать боевые risk_state.json и trade_history.json.
"""

import importlib
import json
import logging
import os
import shutil
import tempfile
import time
import unittest

import config

# Блокировки, которые мы здесь и проверяем, логируются как WARNING и в выводе
# тестов выглядят сбоями. Пустого propagate=False мало: без единого обработчика
# logging включает аварийный вывод в stderr, поэтому вешаем NullHandler
_risk_log = logging.getLogger("risk")
_risk_log.addHandler(logging.NullHandler())
_risk_log.propagate = False


class RiskTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (config.RISK_FILE, config.RISK_STATE_FILE, config.TRADE_HISTORY_FILE)
        config.RISK_FILE = os.path.join(self.tmp, "risk.json")
        config.RISK_STATE_FILE = os.path.join(self.tmp, "risk_state.json")
        config.TRADE_HISTORY_FILE = os.path.join(self.tmp, "history.json")
        self.addCleanup(self._restore)

        # модуль читает состояние при импорте - перезагружаем на чистых путях
        import risk
        self.risk = importlib.reload(risk)

    def _restore(self):
        (config.RISK_FILE, config.RISK_STATE_FILE, config.TRADE_HISTORY_FILE) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)
        import risk
        importlib.reload(risk)

    def write_history(self, pnls, hours_ago=1):
        """История из подряд идущих сделок с заданными PnL."""
        closed = time.time() - hours_ago * 3600
        records = []
        for i, pnl in enumerate(pnls):
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(closed + i))
            records.append({"symbol": "BTCUSDT", "closed_at": stamp, "realized_pnl": pnl})
        with open(config.TRADE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f)

    def reload_module(self):
        """Имитация перезапуска бота: процесс новый, файлы те же."""
        import risk
        self.risk = importlib.reload(risk)
        return self.risk

    # ---------- состояние переживает рестарт ----------

    def test_cooldown_survives_restart(self):
        """main.py перезапускает бота при падении - лимит убытка обязан устоять.

        Иначе защита снималась сама собой ровно тогда, когда сработала:
        сделка в минус, падение, рестарт - и бот снова торгует.
        """
        self.risk.block("дневной лимит убытка", hours=12)

        risk = self.reload_module()

        allowed, reason = risk.check_can_open("BTCUSDT", 0)
        self.assertFalse(allowed, "после рестарта блокировка исчезла")
        self.assertIn("паузе", reason)

    def test_emergency_stop_survives_restart(self):
        """Нажатая кнопка паники не должна отменяться перезапуском."""
        self.risk.set_emergency_stop(True)

        risk = self.reload_module()

        allowed, reason = risk.check_can_open("BTCUSDT", 0)
        self.assertFalse(allowed)
        self.assertIn("Emergency Stop", reason)

    def test_unblock_survives_restart(self):
        self.risk.block("тест", hours=12)
        self.risk.unblock()

        risk = self.reload_module()

        allowed, _ = risk.check_can_open("BTCUSDT", 0)
        self.assertTrue(allowed)

    def test_broken_state_file_does_not_crash(self):
        with open(config.RISK_STATE_FILE, "w", encoding="utf-8") as f:
            f.write("{не json")

        risk = self.reload_module()

        allowed, _ = risk.check_can_open("BTCUSDT", 0)
        self.assertTrue(allowed)

    # ---------- серия убытков не запирает бота навсегда ----------

    def test_losing_streak_blocks_trading(self):
        self.write_history([-5.0, -5.0, -5.0])

        allowed, reason = self.risk.check_can_open("BTCUSDT", 0)

        self.assertFalse(allowed)
        self.assertIn("подряд", reason)

    def test_cooldown_release_resets_streak(self):
        """После кулдауна бот обязан снова торговать.

        Серия считается по истории, а история не меняется. Без сброса точки
        отсчёта проверка сразу видела те же убытки и блокировала снова -
        навсегда, ведь прервать серию может только прибыльная сделка,
        а взять её заблокированный бот не может.
        """
        self.write_history([-5.0, -5.0, -5.0])
        self.assertFalse(self.risk.check_can_open("BTCUSDT", 0)[0])

        # кулдаун истёк
        self.risk._state["blocked_until"] = time.time() - 1

        allowed, reason = self.risk.check_can_open("BTCUSDT", 0)

        self.assertTrue(allowed, f"бот заперт навсегда: {reason}")
        self.assertEqual(self.risk.losing_streak(), 0)

    def test_manual_unblock_actually_releases(self):
        """Кнопка «снять блокировку» обязана работать при накопленной серии."""
        self.write_history([-5.0, -5.0, -5.0])
        self.assertFalse(self.risk.check_can_open("BTCUSDT", 0)[0])

        self.risk.unblock()

        allowed, reason = self.risk.check_can_open("BTCUSDT", 0)
        self.assertTrue(allowed, f"кнопка не сняла блокировку: {reason}")

    def test_new_losses_after_reset_count_again(self):
        """Сброс не отключает защиту навсегда - новая серия снова блокирует."""
        self.write_history([-5.0, -5.0, -5.0])
        self.risk.check_can_open("BTCUSDT", 0)
        self.risk.unblock()

        time.sleep(1.1)  # closed_at пишется с точностью до секунды
        self.write_history([-5.0, -5.0, -5.0], hours_ago=0)

        allowed, reason = self.risk.check_can_open("BTCUSDT", 0)
        self.assertFalse(allowed, "новая серия убытков не заблокировала торговлю")
        self.assertIn("подряд", reason)

    # ---------- остальные предохранители ----------

    def test_daily_loss_usdt_limit(self):
        self.write_history([-30.0, -25.0])

        allowed, reason = self.risk.check_can_open("BTCUSDT", 0)

        self.assertFalse(allowed)
        self.assertIn("USDT", reason)

    def test_max_open_trades_limit(self):
        allowed, reason = self.risk.check_can_open("BTCUSDT", 3)

        self.assertFalse(allowed)
        self.assertIn("лимит одновременных сделок", reason)

    def test_percent_limit_needs_equity(self):
        """Без баланса процентный лимит молча не срабатывает - это по замыслу."""
        self.write_history([-40.0])
        self.risk.update_settings({"daily_loss_usdt_enabled": False,
                                   "max_losing_streak_enabled": False})

        self.assertTrue(self.risk.check_can_open("BTCUSDT", 0, None)[0])
        self.assertFalse(self.risk.check_can_open("BTCUSDT", 0, 100.0)[0])

    def test_clean_state_allows_trading(self):
        allowed, reason = self.risk.check_can_open("BTCUSDT", 0)

        self.assertTrue(allowed)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
