from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import MockingBot as core

from tests.helpers import FakeExchange, fill_response, settings


class ExecutionReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings(Path(self.temp.name), live=True)
        self.store = core.Store(self.settings.db_path)
        self.adapter = core.HyperliquidAdapter(self.settings, self.store)
        self.adapter._sz_decimals["BTC"] = 3

    def tearDown(self) -> None:
        self.store.conn.close()
        self.temp.cleanup()

    def test_authoritative_position_supplies_actual_fill(self) -> None:
        unclear = {"status": "ok", "response": {"data": {"statuses": [{}]}}}
        self.adapter._exchange = FakeExchange([unclear])
        self.adapter._confirmed_position = lambda _coin: (
            True,
            core.Position("BTC", "LONG", 0.12, 101.0),
        )
        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0)
        self.assertTrue(result)
        self.assertEqual(result.filled_size, 0.12)
        self.assertEqual(result.avg_fill_price, 101.0)

    def test_unconfirmed_entry_quarantines_only_that_coin(self) -> None:
        self.adapter._exchange = FakeExchange([fill_response()])
        self.adapter._confirmed_position = lambda _coin: (False, None)
        result = self.adapter.open_position("ETH", "LONG", 12.0, 100.0)
        self.assertFalse(result)
        self.assertEqual(self.store.coin_quarantine("ETH")["reason"], "entry confirmation mismatch")

        paper = core.PaperPortfolio(self.settings, self.store)
        risk = core.RiskManager(self.settings, self.store, core.Notifier(""))
        self.assertEqual(risk.allow_entry("w", "ETH", "LONG", paper, False, set()).action, "SKIP")
        self.assertEqual(risk.allow_entry("w", "SOL", "LONG", paper, False, set()).action, "EXECUTE")

    def test_residual_close_retries_once_and_confirms_flat(self) -> None:
        exchange = FakeExchange(
            [fill_response("0.10", "99", 8), fill_response("0.01", "98", 9)]
        )
        self.adapter._exchange = exchange
        states = iter(
            [
                (True, core.Position("BTC", "LONG", 0.10, 100)),
                (True, core.Position("BTC", "LONG", 0.01, 100)),
                (True, None),
            ]
        )
        self.adapter._confirmed_position = lambda _coin: next(states)
        result = self.adapter.close_position("BTC")
        self.assertTrue(result)
        self.assertEqual(exchange.closes, 2)
        self.assertTrue(result.confirmed)

    def test_already_flat_close_self_heals(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        self.adapter._confirmed_position = lambda _coin: (True, None)
        result = self.adapter.close_position("XLM")
        self.assertTrue(result)
        self.assertEqual(result.status, "already_flat")
        self.assertEqual(exchange.closes, 0)

    def test_book_reconciliation_clears_safe_hold_and_keeps_unowned_hold(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open("wallet", "BTC", "LONG", 100.0, 10.0)
        bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
        bot.settings = self.settings
        bot.store = self.store
        bot.paper = paper
        bot.platform = self.adapter

        self.store.quarantine_coin("BTC", "temporary")
        bot._reconcile_live_book(
            {
                "BTC": core.Position("BTC", "LONG", 0.30, 100.0),
                "SOL": core.Position("SOL", "SHORT", 0.20, 50.0),
            }
        )
        self.assertIsNone(self.store.coin_quarantine("BTC"))
        self.assertEqual(self.store.coin_quarantine("SOL")["reason"], "unowned live position")

        bot._reconcile_live_book({"BTC": core.Position("BTC", "LONG", 0.30, 100.0)})
        self.assertIsNone(self.store.coin_quarantine("SOL"))


if __name__ == "__main__":
    unittest.main()
