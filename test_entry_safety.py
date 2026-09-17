"""Offline entry safety checks: no exchange credentials or orders."""
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import trade_engine as te


class EntrySafetyTests(unittest.TestCase):
    def setUp(self):
        self.trade = te.TradeManager({
            "symbol": "BTCUSDT", "strategy": "long",
            "targets": [110, 120], "stop_loss": 90,
        }, equity_usdt=100)
        self.session = MagicMock()
        self.session.get_positions.return_value = {"result": {"list": []}}
        for name, value in (("get_session", self.session), ("calc_qty_from_margin", Decimal("0.1")),
                            ("round_price", "90")):
            p = patch.object(te, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.trade.get_last_price = MagicMock(return_value=100)
        self.trade.set_leverage = MagicMock()
        self.trade._fetch_entry_price = MagicMock(return_value=100)
        p = patch.object(te._time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_initial_stop_is_in_entry_request(self):
        self.trade.open_position()
        params = self.session.place_order.call_args.kwargs
        self.assertEqual(params["stopLoss"], "90")
        self.assertEqual(params["tpslMode"], "Full")
        self.assertTrue(self.trade.entry_submitted)

    def test_existing_position_is_not_modified(self):
        self.session.get_positions.return_value = {"result": {"list": [{"size": "1"}]}}
        with self.assertRaises(ValueError):
            self.trade.open_position()
        self.trade.set_leverage.assert_not_called()
        self.session.place_order.assert_not_called()
        self.assertFalse(self.trade.entry_submitted)

    def test_risk_limit_has_no_twenty_five_percent_tolerance(self):
        with patch.object(te, "calc_qty_from_margin", return_value=Decimal("0.11")):
            with self.assertRaises(ValueError):
                self.trade.open_position()
        self.session.place_order.assert_not_called()
        self.assertFalse(self.trade.entry_submitted)

    def test_stale_signal_does_not_change_leverage(self):
        self.trade.get_last_price.return_value = 111
        with self.assertRaises(ValueError):
            self.trade.open_position()
        self.trade.set_leverage.assert_not_called()
        self.assertFalse(self.trade.entry_submitted)
