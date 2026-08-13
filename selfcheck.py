"""
Проверка работоспособности бота без ожидания живого сигнала из канала.

Запуск:
    .venv\\Scripts\\python.exe selfcheck.py            - только чтение, ордеров не ставит
    .venv\\Scripts\\python.exe selfcheck.py --trade    - плюс полный цикл сделки, ТОЛЬКО на демо

Обычный режим безопасен на любом счёте: он ничего не отправляет на биржу,
кроме запросов на чтение. Режим --trade открывает реальную позицию минимального
объёма, ставит стоп и тейки, после чего снимает ордера и закрывает позицию по
рынку. Он отказывается работать, если config.DEMO = False.
"""

import argparse
import sys
import time

import config
import logging_setup
import parsers
import risk
import settings
import synctime
import trade_engine

logging_setup.configure()

PASS, FAIL, WARN = "OK  ", "СБОЙ", "!   "
_failures = 0
_warnings = 0

# Сообщение в формате канала ggshot - гоняем настоящий парсер, а не заглушку
SAMPLE_SIGNAL = """📩 Новый сигнал
#BTCUSDT
Long Entry Zone: 60000 - 59500
Target 1: **61000
Target 2: **62000
Target 3: **63000
Target 4: **64000
Stop-Loss: **58000
Signal ID: #selfcheck-dry-run
"""


def check(name: str, fn):
    """Выполняет проверку и печатает результат. Исключение = провал, не падение."""
    global _failures, _warnings
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    if ok is None:
        mark = WARN
        _warnings += 1
    elif ok:
        mark = PASS
    else:
        mark = FAIL
        _failures += 1
    print(f"  [{mark}] {name}: {detail}")


# ---------- проверки только на чтение ----------

def check_config():
    if not config.BYBIT_API_KEY or not config.BYBIT_API_SECRET:
        return False, "ключи Bybit не заданы в config.py"
    mode = "ДЕМО (api-demo.bybit.com)" if config.DEMO else "БОЕВОЙ СЧЁТ (реальные деньги)"
    return True, f"{mode}, testnet={config.TESTNET}, recv_window={config.RECV_WINDOW}"


def check_tp_percents():
    tp = settings.get_tp_percents()
    total = sum(tp)
    if abs(total - 100.0) > 0.01:
        return False, f"доли тейков дают {total}%, а должны 100% - остаток позиции повиснет без закрытия"
    return True, f"{tp} = {total}%"


def check_clock():
    offset = synctime.refresh()
    if offset is None:
        return False, "не удалось получить время Bybit"
    # refresh() уже вычел запас, поэтому реальная ошибка часов - без него
    clock_error = offset + synctime._SAFETY_LAG_MS
    behind = synctime._SAFETY_LAG_MS
    if abs(clock_error) > 5000:
        return False, (f"часы разошлись с биржей на {clock_error:+d} мс - "
                       f"это выходит за recv_window {config.RECV_WINDOW} мс")
    return True, (f"часы ПК {'отстают' if clock_error > 0 else 'спешат'} на "
                  f"{abs(clock_error)} мс; метка уходит на биржу на {behind} мс "
                  f"позади неё (лимит обгона 1000 мс)")


def check_pybit_patch():
    trade_engine.patch_pybit_clock()
    from pybit import _http_manager, _websocket_stream
    ok = (_http_manager._helpers.generate_timestamp is synctime.now_ms
          and _websocket_stream._helpers.generate_timestamp is synctime.now_ms)
    return ok, "REST и WebSocket берут время из synctime" if ok else "патч не дошёл до pybit"


def check_auth():
    """Прямой запрос без обёртки: обёртка глушит ошибку и возвращает нули."""
    resp = trade_engine.get_session().get_wallet_balance(accountType="UNIFIED", coin="USDT")
    lst = resp["result"]["list"]
    if not lst:
        return False, "список счетов пуст - аккаунт UNIFIED не инициализирован"
    equity = float(lst[0].get("totalEquity") or 0)
    margin = settings.get_margin_usdt()
    if equity < margin:
        return None, (f"equity {equity:.8f} USDT меньше маржи на сделку ({margin} USDT) - "
                      f"открыть позицию не выйдет. Если счёт не пустой, значит ключи не того "
                      f"типа, что config.DEMO={config.DEMO}: биржа отвечает 10003 молча. "
                      f"Почти нулевой equity вдобавок отключает процентный лимит убытка")
    return True, (f"equity {equity:.2f} USDT, "
                  f"доступно {float(lst[0].get('totalAvailableBalance') or 0):.2f}")


def check_instrument():
    info = trade_engine.SymbolInfo.get("BTCUSDT")
    return True, f"BTCUSDT шаг объёма {info['qty_step']}, мин. объём {info.get('min_qty')}"


def check_qty_calc():
    margin, leverage = settings.get_margin_usdt(), settings.get_leverage()
    price = trade_engine.get_session().get_tickers(
        category=config.CATEGORY, symbol="BTCUSDT")["result"]["list"][0]["lastPrice"]
    qty = trade_engine.calc_qty_from_margin("BTCUSDT", margin, leverage, float(price))
    info = trade_engine.SymbolInfo.get("BTCUSDT")
    min_qty = info.get("min_qty")
    if min_qty is not None and qty < min_qty:
        return False, (f"расчётный объём {qty} меньше минимального {min_qty} - "
                       f"биржа отклонит ордер при марже {margin} USDT")
    return True, f"маржа {margin} USDT x {leverage} -> объём {qty} BTC (цена {price})"


def check_parser():
    signal = parsers.parse(SAMPLE_SIGNAL)
    if signal is None:
        return False, "тестовое сообщение не распозналось ни одним парсером"
    return True, (f"{signal['symbol']} {signal['strategy']} парсером «{signal.get('parser')}», "
                  f"тейков {len(signal.get('targets', []))}, стоп {signal.get('stop_loss')}")


def check_risk():
    allowed, reason = risk.check_can_open("BTCUSDT", 0, 1000.0)
    snap = risk.snapshot()
    if not allowed:
        return None, f"риск-менеджер сейчас запрещает вход: {reason}"
    return True, (f"вход разрешён; PnL за сутки {snap.get('daily_pnl')}, "
                  f"серия убытков {snap.get('losing_streak')}")


def check_websocket():
    """Приватный WebSocket - именно там кривые часы дают 10016 при авторизации."""
    ws = trade_engine.BotEngine(notifier=lambda _t: None)._create_ws(demo=settings.is_demo())
    try:
        for _ in range(100):
            if ws.is_connected():
                return True, "приватный поток подключён, авторизация принята"
            time.sleep(0.1)
        return False, "не подключился за 10 секунд"
    finally:
        try:
            ws.exit()
        except Exception:
            pass


def check_open_state():
    session = trade_engine.get_session()
    positions = [p for p in session.get_positions(category=config.CATEGORY, settleCoin="USDT")
                 ["result"]["list"] if float(p.get("size") or 0) > 0]
    orders = session.get_open_orders(category=config.CATEGORY, settleCoin="USDT")["result"]["list"]
    detail = f"открытых позиций {len(positions)}, активных ордеров {len(orders)}"
    if positions or orders:
        return None, detail + " - проверьте, что это ожидаемо перед запуском"
    return True, detail


# ---------- живой цикл сделки (только демо) ----------

def run_trade_cycle():
    if not config.DEMO:
        print("\nОТКАЗ: config.DEMO = False. Живой тест разрешён только на демо-счёте.")
        print("Поставьте DEMO = True и демо-ключи в config.py, затем повторите.")
        return False

    # Зачистка в конце закрывает ВСЕ позиции по USDT, а не только тестовую,
    # поэтому чужие открытые позиции - повод отказаться, а не закрыть их молча
    session = trade_engine.get_session()
    existing = [p for p in session.get_positions(category=config.CATEGORY, settleCoin="USDT")
                ["result"]["list"] if float(p.get("size") or 0) > 0]
    if existing:
        print("\nОТКАЗ: на счёте уже есть открытые позиции: "
              + ", ".join(p["symbol"] for p in existing))
        print("Тест закрывает по рынку всё подряд - сначала разберитесь с ними вручную.")
        return False

    print("\n--- Живой цикл сделки на демо-счёте ---")
    engine = trade_engine.BotEngine(notifier=lambda t: print("  уведомление:", t.replace("\n", " | ")))
    engine.start()
    opened = False
    try:
        signal = parsers.parse(SAMPLE_SIGNAL)
        signal["signal_id"] = f"selfcheck-{int(time.time())}"  # иначе дедупликация отсечёт повтор

        # цена из сигнала синтетическая - вход идёт по рынку, а стоп и тейки
        # надо увести от текущей цены, иначе биржа исполнит их мгновенно
        price = float(session.get_tickers(
            category=config.CATEGORY, symbol="BTCUSDT")["result"]["list"][0]["lastPrice"])
        signal["stop_loss"] = price * 0.90
        signal["targets"] = [price * (1 + 0.02 * i) for i in range(1, 5)]

        print(f"  отправляю сигнал BTCUSDT {signal['strategy']} по рынку (~{price})")
        engine.process_signal(signal)
        opened = True

        trade = engine.trades.get("BTCUSDT")
        if trade is None:
            print("  [СБОЙ] движок перестал вести сделку сразу после открытия "
                  "- смотрите logs/bot.log")
            return False

        print(f"  [OK  ] позиция открыта: объём {trade.qty_total}, вход {trade.entry_price}, "
              f"стоп {trade.current_sl}, тейков {len(trade.tp_order_ids)}")
        time.sleep(3)
        return True
    finally:
        # Закрываем при любом исходе: позиция могла открыться даже там, где
        # проверка выше провалилась, и оставить её висеть - худшее, что может
        # сделать тест. Раньше ранний return уводил управление мимо закрытия.
        if opened:
            try:
                print("  зачистка:", engine.close_all_positions())
            except Exception as e:
                print(f"  !!! НЕ УДАЛОСЬ ЗАКРЫТЬ ПОЗИЦИЮ: {e}")
                print("  !!! ЗАКРОЙТЕ ЕЁ ВРУЧНУЮ НА БИРЖЕ")
        engine.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade", action="store_true",
                        help="открыть и закрыть тестовую сделку (только при DEMO = True)")
    args = parser.parse_args()

    print("--- Проверка бота (только чтение) ---")
    check("Конфигурация", check_config)
    check("Доли тейков", check_tp_percents)
    check("Часы и Bybit", check_clock)
    check("Патч часов pybit", check_pybit_patch)
    check("Авторизация REST", check_auth)
    check("Данные инструмента", check_instrument)
    check("Расчёт объёма", check_qty_calc)
    check("Парсер сигналов", check_parser)
    check("Защита от слива", check_risk)
    check("Приватный WebSocket", check_websocket)
    check("Открытые позиции", check_open_state)

    if _failures:
        print(f"\nПровалов: {_failures}. Бота запускать рано.")
        return 1

    if _warnings:
        print(f"\nСбоев нет, но предупреждений: {_warnings} - разберитесь с ними до запуска.")
    else:
        print("\nВсе проверки пройдены, бот готов к работе.")

    if args.trade and not run_trade_cycle():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
