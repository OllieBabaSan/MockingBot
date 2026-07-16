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
        self.adapter._sz_decimals["ETH"] = 3
        self.adapter._max_leverage.update({"BTC": 50, "ETH": 50})
        self.adapter.capital_snapshot = lambda: core.CapitalSnapshot(
            account_value=500.0,
            total_margin_used=0.0,
            withdrawable=500.0,
            available_margin=500.0,
        )

    def tearDown(self) -> None:
        self.store.conn.close()
        self.temp.cleanup()

    def test_authoritative_position_supplies_actual_fill(self) -> None:
        unclear = {"status": "ok", "response": {"data": {"statuses": [{}]}}}
        self.adapter._exchange = FakeExchange([unclear])
        states = iter([
            (True, None),
            (True, core.Position("BTC", "LONG", 0.12, 101.0, 3)),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)
        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3)
        self.assertTrue(result)
        self.assertEqual(result.filled_size, 0.12)
        self.assertEqual(result.avg_fill_price, 101.0)
        self.assertEqual(self.adapter._exchange.leverages, [3])

    def test_unconfirmed_entry_quarantines_only_that_coin(self) -> None:
        self.adapter._exchange = FakeExchange([fill_response()])
        states = iter([(True, None), (False, None)])
        self.adapter._confirmed_position = lambda _coin: next(states)
        result = self.adapter.open_position("ETH", "LONG", 12.0, 100.0, 3)
        self.assertFalse(result)
        self.assertEqual(self.store.coin_quarantine("ETH")["reason"], "entry confirmation mismatch")

        paper = core.PaperPortfolio(self.settings, self.store)
        risk = core.RiskManager(self.settings, self.store, core.Notifier(""))
        self.assertEqual(risk.allow_entry("w", "ETH", "LONG", paper, False, set()).action, "SKIP")
        self.assertEqual(risk.allow_entry("w", "SOL", "LONG", paper, False, set()).action, "EXECUTE")

    def test_same_coin_add_inherits_leverage_without_exchange_reset(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open("wallet-a", "BTC", "LONG", 100.0, 10.0, leverage=3)
        exchange = FakeExchange([fill_response()])
        self.adapter._exchange = exchange
        states = iter([
            (True, core.Position("BTC", "LONG", 0.30, 100.0, 3)),
            (True, core.Position("BTC", "LONG", 0.42, 100.0, 3)),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3, 5)

        self.assertTrue(result)
        self.assertEqual(result.filled_size, 0.12)
        self.assertEqual(exchange.leverages, [])
        audit = self.store.conn.execute(
            "SELECT requested_leverage, leverage FROM execution_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual((audit["requested_leverage"], audit["leverage"]), (5.0, 3.0))

    def test_preflight_rejects_asset_leverage_above_exchange_maximum(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        self.adapter._max_leverage["BTC"] = 2

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3)

        self.assertFalse(result)
        self.assertIn("exceeds BTC maximum 2x", result.detail)
        self.assertEqual(exchange.opens, 0)
        self.assertIsNone(self.store.coin_quarantine("BTC"))

    def test_rejected_leverage_update_blocks_order_submission(self) -> None:
        exchange = FakeExchange([])
        exchange.leverage_response = {"status": "err", "response": "rejected"}
        self.adapter._exchange = exchange
        self.adapter._confirmed_position = lambda _coin: (True, None)

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3)

        self.assertFalse(result)
        self.assertEqual(result.status, "leverage_update_rejected")
        self.assertEqual(exchange.opens, 0)
        self.assertEqual(exchange.leverages, [3])

    def test_existing_exchange_leverage_mismatch_blocks_add(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open("wallet", "BTC", "LONG", 100, 10, leverage=3)
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        self.adapter._confirmed_position = lambda _coin: (
            True, core.Position("BTC", "LONG", 0.30, 100, 2)
        )

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3)

        self.assertFalse(result)
        self.assertEqual(result.status, "leverage_mismatch")
        self.assertEqual(exchange.opens, 0)
        self.assertEqual(
            self.store.coin_quarantine("BTC")["reason"], "exchange leverage mismatch"
        )

    def test_post_fill_exchange_leverage_mismatch_quarantines_coin(self) -> None:
        exchange = FakeExchange([fill_response()])
        self.adapter._exchange = exchange
        states = iter([
            (True, None),
            (True, core.Position("BTC", "LONG", 0.12, 100, 2)),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3)

        self.assertFalse(result)
        self.assertTrue(result.accepted)
        self.assertFalse(result.confirmed)
        self.assertEqual(result.status, "leverage_mismatch")
        self.assertEqual(
            self.store.coin_quarantine("BTC")["reason"],
            "post-entry leverage mismatch",
        )

    def test_preflight_checks_notional_after_size_rounding(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        self.adapter._sz_decimals["BTC"] = 1

        result = self.adapter.open_position("BTC", "LONG", 11.001, 100.0, 3)

        self.assertFalse(result)
        self.assertIn("rounded notional $10.0000", result.detail)
        self.assertEqual(exchange.opens, 0)
        audit = self.store.conn.execute(
            "SELECT exchange_status, detail FROM execution_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(audit["exchange_status"], "rejected")
        self.assertIn("rounded notional", audit["detail"])

    def test_preflight_blocks_entry_when_buying_power_is_unavailable(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        self.adapter.capital_snapshot = lambda: None

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3)

        self.assertFalse(result)
        self.assertEqual(result.status, "buying_power_unavailable")
        self.assertEqual(exchange.opens, 0)
        self.assertIsNone(self.store.coin_quarantine("BTC"))

    def test_capital_snapshot_uses_conservative_available_margin(self) -> None:
        self.adapter._user_state = lambda: {
            "marginSummary": {
                "accountValue": "500",
                "totalMarginUsed": "125",
            },
            "withdrawable": "410",
        }

        snapshot = core.HyperliquidAdapter.capital_snapshot(self.adapter)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.account_value, 500.0)
        self.assertEqual(snapshot.available_margin, 375.0)

    def test_preflight_caps_order_by_verified_usable_margin(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        self.adapter.capital_snapshot = lambda: core.CapitalSnapshot(
            account_value=500.0,
            total_margin_used=100.0,
            withdrawable=450.0,
            available_margin=400.0,
        )

        result = self.adapter.open_position("BTC", "LONG", 1200.0, 100.0, 3)

        self.assertFalse(result)
        self.assertEqual(result.status, "insufficient_buying_power")
        self.assertIn("$375.00", result.detail)
        self.assertEqual(exchange.opens, 0)
        snapshot = self.store.get_json("live_capital_snapshot", {})
        self.assertEqual(snapshot["available_margin"], 400.0)
        self.assertEqual(snapshot["reserve"], 25.0)

    def test_lost_response_recovers_measured_fill_without_resubmission(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        states = iter([
            (True, None),
            (True, core.Position("BTC", "LONG", 0.12, 101.0, 3)),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3)

        self.assertTrue(result)
        self.assertEqual(result.status, "recovered")
        self.assertEqual(result.filled_size, 0.12)
        self.assertEqual(exchange.opens, 1)
        self.assertIsNone(self.store.coin_quarantine("BTC"))

    def test_failed_submission_with_no_position_change_is_clean_failure(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        self.adapter._confirmed_position = lambda _coin: (True, None)

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3)

        self.assertFalse(result)
        self.assertIn("no position change", result.detail)
        self.assertEqual(exchange.opens, 1)
        self.assertIsNone(self.store.coin_quarantine("BTC"))

    def test_lost_response_with_unavailable_state_quarantines_coin(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        states = iter([(True, None), (False, None)])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3)

        self.assertFalse(result)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.store.coin_quarantine("BTC")["reason"], "ambiguous entry state")

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

    def test_partial_close_reduces_only_requested_allocation(self) -> None:
        exchange = FakeExchange([fill_response("0.20", "99", 8)])
        self.adapter._exchange = exchange
        states = iter([
            (True, core.Position("BTC", "LONG", 0.50, 100)),
            (True, core.Position("BTC", "LONG", 0.30, 100)),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.close_position("BTC", 0.20, 100.0)

        self.assertTrue(result)
        self.assertTrue(result.confirmed)
        self.assertAlmostEqual(result.filled_size, 0.20)
        self.assertEqual(exchange.close_sizes, [0.20])
        self.assertIn("remaining=0.3", result.detail)
        audit = self.store.conn.execute(
            """SELECT reference_price, slippage_bps, price_source
               FROM execution_audit ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        self.assertEqual(audit["reference_price"], 100.0)
        self.assertAlmostEqual(audit["slippage_bps"], 100.0)
        self.assertEqual(audit["price_source"], "exchange_fill")

    def test_wallet_allocation_size_excludes_other_wallets(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open("wallet-a", "BTC", "LONG", 100, 10, leverage=3)
        paper.open("wallet-b", "BTC", "LONG", 100, 20, leverage=3)

        self.assertAlmostEqual(
            paper.allocation_position_size("wallet-a", "BTC", "LONG"), 0.30
        )
        self.assertAlmostEqual(
            paper.allocation_position_size("wallet-b", "BTC", "LONG"), 0.60
        )

    def test_reconciler_uses_confirmed_fill_price_for_local_pnl(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open("wallet-a", "BTC", "LONG", 100, 10, leverage=3)

        class FillPlatform:
            def mid_price(self, _coin):
                return 100.0

            def close_position(self, _coin, size=None, reference_price=None):
                return core.ExecutionResult(
                    True, size or 0, size or 0, 90.0,
                    status="filled", confirmed=True,
                )

        risk = core.RiskManager(self.settings, self.store, core.Notifier(""))
        reconciler = core.Reconciler(
            self.settings, self.store, FillPlatform(), paper, risk
        )

        reconciler._force_close("BTC", "wallet-a", "LONG", "test exit", 0.50)

        signal = self.store.conn.execute(
            "SELECT price, paper_gain, reason FROM signals ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(signal["price"], 90.0)
        self.assertEqual(signal["paper_gain"], -3.0)
        self.assertIn("price_source=exchange_fill", signal["reason"])

    def test_already_flat_close_self_heals(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        self.adapter._confirmed_position = lambda _coin: (True, None)
        result = self.adapter.close_position("XLM")
        self.assertTrue(result)
        self.assertEqual(result.status, "already_flat")
        self.assertEqual(exchange.closes, 0)

    def test_lost_close_response_recovers_when_exchange_is_flat(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        states = iter([
            (True, core.Position("BTC", "LONG", 0.10, 100)),
            (True, None),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.close_position("BTC")

        self.assertTrue(result)
        self.assertEqual(result.status, "recovered")
        self.assertTrue(result.confirmed)
        self.assertEqual(exchange.closes, 1)
        self.assertIsNone(self.store.coin_quarantine("BTC"))

    def test_lost_close_response_with_no_change_is_clean_failure(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        position = core.Position("BTC", "LONG", 0.10, 100)
        self.adapter._confirmed_position = lambda _coin: (True, position)

        result = self.adapter.close_position("BTC")

        self.assertFalse(result)
        self.assertIn("no position change", result.detail)
        self.assertEqual(exchange.closes, 1)
        self.assertIsNone(self.store.coin_quarantine("BTC"))

    def test_lost_close_response_retries_measured_residual_once(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        states = iter([
            (True, core.Position("BTC", "LONG", 0.10, 100)),
            (True, core.Position("BTC", "LONG", 0.04, 100)),
            (True, None),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.close_position("BTC")

        self.assertTrue(result)
        self.assertEqual(result.status, "recovered")
        self.assertEqual(exchange.closes, 2)
        self.assertIsNone(self.store.coin_quarantine("BTC"))

    def test_lost_close_response_with_unavailable_state_quarantines_coin(self) -> None:
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        states = iter([
            (True, core.Position("BTC", "LONG", 0.10, 100)),
            (False, None),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.close_position("BTC")

        self.assertFalse(result)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(
            self.store.coin_quarantine("BTC")["reason"], "ambiguous close state"
        )

    def test_unresolved_normal_close_residual_is_not_reported_successful(self) -> None:
        exchange = FakeExchange(
            [fill_response("0.06", "99", 8), fill_response("0.01", "98", 9)]
        )
        self.adapter._exchange = exchange
        states = iter([
            (True, core.Position("BTC", "LONG", 0.10, 100)),
            (True, core.Position("BTC", "LONG", 0.04, 100)),
            (True, core.Position("BTC", "LONG", 0.03, 100)),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.close_position("BTC")

        self.assertFalse(result)
        self.assertFalse(result.confirmed)
        self.assertEqual(
            self.store.coin_quarantine("BTC")["reason"], "residual live position"
        )

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
                "BTC": core.Position("BTC", "LONG", 0.30, 100.0, 3),
                "SOL": core.Position("SOL", "SHORT", 0.20, 50.0, 3),
            }
        )
        self.assertIsNone(self.store.coin_quarantine("BTC"))
        self.assertEqual(self.store.coin_quarantine("SOL")["reason"], "unowned live position")

        bot._reconcile_live_book({"BTC": core.Position("BTC", "LONG", 0.30, 100.0, 3)})
        self.assertIsNone(self.store.coin_quarantine("SOL"))


if __name__ == "__main__":
    unittest.main()
