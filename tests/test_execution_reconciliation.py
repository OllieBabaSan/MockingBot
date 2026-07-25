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

    def test_explicit_zero_fill_exchange_error_is_failed_not_ambiguous(self) -> None:
        result = core.ExecutionResult(
            False, 0.00044, 0.0, status="error", confirmed=False,
            detail="Insufficient margin to place order.",
        )
        self.assertEqual(
            core.HyperliquidAdapter._execution_intent_state(result), "FAILED"
        )

    def test_failed_open_intent_is_logged_without_resubmission(self) -> None:
        key = "copy-event:99:open"
        self.store.prepare_execution_intent(
            key, "OPEN", "BTC", "SHORT", 0.00044,
            core.Position("BTC", "SHORT", 0.00801, 64864.2, 3), 3,
        )
        failed = core.ExecutionResult(
            False, 0.00044, 0.0, status="error", confirmed=False,
            detail="Insufficient margin to place order.",
        )
        self.store.update_execution_intent(key, "FAILED", failed)
        bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
        bot.settings = self.settings
        bot.store = self.store
        bot.platform = self.adapter
        bot.scoring_engine = core.ScoringEngine(self.settings, self.store)
        bot.token_risk = type("TokenRisk", (), {"observe": lambda *_args: None})()
        event = core.CopyEvent(
            "ADD", "wallet", "BTC", "SHORT", entry_price=64930.0, event_id=99
        )

        bot._handle_entry(event, False, {"BTC"})

        row = self.store.conn.execute(
            "SELECT action, reason FROM signals ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["action"], "SKIPPED")
        self.assertIn("Insufficient margin", row["reason"])

    def test_successful_recovery_close_is_not_observed_as_wallet_exit(self) -> None:
        score = core.ScoringEngineScore(
            wallet="wallet",
            tier="Core",
            total_score=60.0,
            realized_component=0.0,
            win_rate_component=0.0,
            recent_form_component=0.0,
            churn_penalty=0.0,
            loss_penalty=0.0,
            sample_size=5,
            realized_pnl=1.0,
            win_rate=0.6,
            avg_pnl_pct=0.1,
            explanation="test",
        )

        class FakeScoring:
            def __init__(self):
                self.observed = []

            def active_entry_decision(self, _event):
                return core.TradeDecision("EXECUTE"), score

            def allocation_multiplier(self, _score):
                return 1.0

            def leverage_for_score(self, _score):
                return 3

            def allocation_note(self, *_args):
                return "test allocation"

            def observe_signal(self, event, *_args):
                self.observed.append(event.kind)

        class FakePaper:
            def position(self, _coin):
                return None

            def position_side(self, *_args):
                return None

            def available_slot(self, *_args, **_kwargs):
                return 10.0

            def value(self, _price_fn):
                return 500.0

            def open(self, *_args, **_kwargs):
                return None

        class FakePlatform:
            def mid_price(self, _coin):
                return 100.0

            def open_position(self, *_args):
                return core.ExecutionResult(
                    True, 0.3, 0.3, 100.0, status="filled", confirmed=True
                )

            def close_position(self, *_args):
                return core.ExecutionResult(
                    True, 0.3, 0.3, 100.0, status="filled", confirmed=True
                )

        bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
        bot.settings = self.settings
        bot.store = self.store
        bot.platform = FakePlatform()
        bot.paper = FakePaper()
        bot.scoring_engine = FakeScoring()
        bot.risk = type(
            "Risk", (), {"allow_entry": lambda *_args, **_kwargs: core.TradeDecision("EXECUTE")}
        )()
        bot.token_risk = type("TokenRisk", (), {"observe": lambda *_args: None})()
        event = core.CopyEvent(
            "ADD", "wallet", "BTC", "LONG", entry_price=100.0, event_id=77
        )

        bot._handle_entry(event, False, set())

        self.assertEqual(bot.scoring_engine.observed, ["ADD"])
        signals = self.store.conn.execute(
            "SELECT signal, reason FROM signals ORDER BY id"
        ).fetchall()
        self.assertEqual([row["signal"] for row in signals], ["ADD", "EXIT"])
        self.assertEqual(
            signals[-1]["reason"], "recovery close after paper commit failure"
        )

    def test_confirmed_open_intent_replays_without_second_order(self) -> None:
        exchange = FakeExchange([fill_response()])
        self.adapter._exchange = exchange
        states = iter([
            (True, None),
            (True, core.Position("BTC", "LONG", 0.12, 100.0, 3)),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)
        key = "copy-event:42:open"

        first = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3, 3, key)
        second = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3, 3, key)

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(exchange.opens, 1)
        row = self.store.execution_intent(key)
        self.assertEqual(row["state"], "CONFIRMED")
        self.assertEqual(row["cloid"], self.store.execution_cloid(key))
        self.assertEqual(exchange.cloids[0].to_raw(), row["cloid"])

    def test_submitting_open_intent_recovers_from_position_delta(self) -> None:
        key = "copy-event:43:open"
        self.store.prepare_execution_intent(key, "OPEN", "BTC", "LONG", 0.12, None)
        self.store.update_execution_intent(key, "SUBMITTING")
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        self.adapter._confirmed_position = lambda _coin: (
            True, core.Position("BTC", "LONG", 0.12, 101.0, 3)
        )

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3, 3, key)

        self.assertTrue(result)
        self.assertEqual(result.status, "recovered_intent")
        self.assertEqual(exchange.opens, 0)
        self.assertEqual(self.store.execution_intent(key)["state"], "CONFIRMED")

    def test_submitting_marker_without_exchange_order_safely_resubmits_same_cloid(self) -> None:
        class UnknownOrderInfo:
            def query_order_by_cloid(self, _wallet, _cloid):
                return {"status": "unknownOid"}

        key = "copy-event:45:open"
        self.store.prepare_execution_intent(
            key, "OPEN", "BTC", "LONG", 0.12, None, leverage=3
        )
        self.store.update_execution_intent(key, "SUBMITTING")
        exchange = FakeExchange([fill_response()])
        self.adapter._exchange = exchange
        self.adapter._info = UnknownOrderInfo()
        states = iter([
            (True, None),
            (True, core.Position("BTC", "LONG", 0.12, 100.0, 3)),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.open_position("BTC", "LONG", 12.0, 100.0, 3, 3, key)

        self.assertTrue(result)
        self.assertEqual(exchange.opens, 1)
        self.assertEqual(exchange.cloids[0].to_raw(), self.store.execution_cloid(key))

    def test_submitting_close_intent_recovers_when_exchange_is_flat(self) -> None:
        class FilledOrderInfo:
            def user_fills(self, _wallet):
                return [{
                    "cloid": core.Store.execution_cloid("copy-event:44:close"),
                    "sz": "0.12", "px": "99.5",
                }]

        key = "copy-event:44:close"
        before = core.Position("BTC", "LONG", 0.12, 100.0, 3)
        self.store.prepare_execution_intent(key, "CLOSE", "BTC", "LONG", 0.12, before)
        self.store.update_execution_intent(key, "SUBMITTING")
        exchange = FakeExchange([])
        self.adapter._exchange = exchange
        self.adapter._info = FilledOrderInfo()
        self.adapter._confirmed_position = lambda _coin: (True, None)

        result = self.adapter.close_position("BTC", 0.12, 100.0, key)

        self.assertTrue(result)
        self.assertEqual(result.status, "recovered_intent")
        self.assertEqual(result.avg_fill_price, 99.5)
        self.assertEqual(exchange.closes, 0)
        self.assertEqual(self.store.execution_intent(key)["state"], "CONFIRMED")

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

    def test_live_risk_allows_only_synchronized_same_side_adds(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open("wallet-a", "BTC", "LONG", 100.0, 10.0, leverage=3)
        risk = core.RiskManager(self.settings, self.store, core.Notifier(""))

        new_wallet = risk.allow_entry(
            "wallet-b", "BTC", "LONG", paper, False, {"BTC"}
        )
        same_wallet_add = risk.allow_entry(
            "wallet-a", "BTC", "LONG", paper, False, {"BTC"},
            allow_same_wallet_add=True,
        )
        unsynchronized = risk.allow_entry(
            "wallet-b", "BTC", "LONG", paper, False, set()
        )
        unowned = risk.allow_entry(
            "wallet-b", "ETH", "LONG", paper, False, {"ETH"}
        )

        self.assertEqual(new_wallet.action, "EXECUTE")
        self.assertEqual(same_wallet_add.action, "EXECUTE")
        self.assertEqual(unsynchronized.reason, "local position missing live")
        self.assertEqual(unowned.reason, "coin already held")

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

    def test_uncommitted_fill_rollback_restores_new_position(self) -> None:
        class RollbackPlatform:
            def __init__(self):
                self.calls = []

            def close_position(self, coin, size=None, reference_price=None):
                self.calls.append((coin, size, reference_price))
                return core.ExecutionResult(
                    True, size or 0, size or 0, 100.0,
                    status="filled", confirmed=True, detail="verified reduction",
                )

        platform = RollbackPlatform()
        bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
        bot.settings = self.settings
        bot.store = self.store
        bot.platform = platform
        bot.notifier = core.Notifier("")
        self.store.quarantine_coin("BTC", "post-entry leverage mismatch")
        event = core.CopyEvent("ENTRY", "wallet", "BTC", "LONG")
        execution = core.ExecutionResult(
            True, 0.12, 0.12, 100.0,
            status="leverage_mismatch", confirmed=False,
        )

        suffix = bot._rollback_uncommitted_entry(event, execution, 100.0, False)

        self.assertEqual(suffix, "; automatic rollback confirmed")
        self.assertEqual(platform.calls, [("BTC", 0.12, 100.0)])
        self.assertIsNone(self.store.coin_quarantine("BTC"))
        self.assertTrue(
            self.store.get_json("last_entry_rollback", {})["rollback_confirmed"]
        )

    def test_failed_uncommitted_fill_rollback_stays_quarantined(self) -> None:
        class FailedRollbackPlatform:
            def close_position(self, _coin, size=None, reference_price=None):
                return core.ExecutionResult(
                    False, size or 0, status="ambiguous", detail="state unavailable"
                )

        bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
        bot.settings = self.settings
        bot.store = self.store
        bot.platform = FailedRollbackPlatform()
        bot.notifier = core.Notifier("")
        event = core.CopyEvent("ENTRY", "wallet", "BTC", "LONG")
        execution = core.ExecutionResult(
            True, 0.12, 0.12, 100.0,
            status="leverage_mismatch", confirmed=False,
        )

        suffix = bot._rollback_uncommitted_entry(event, execution, 100.0, False)

        self.assertIn("ROLLBACK FAILED", suffix)
        self.assertEqual(
            self.store.coin_quarantine("BTC")["reason"], "ENTRY ROLLBACK FAILED"
        )
        self.assertFalse(
            self.store.get_json("last_entry_rollback", {})["rollback_confirmed"]
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
        self.adapter._live_account_mode = lambda: "standard"
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

    def test_unified_capital_uses_spot_available_after_maintenance(self) -> None:
        class UnifiedInfo:
            def spot_user_state(self, _wallet):
                return {
                    "balances": [
                        {"coin": "USDC", "token": 0, "total": "501.897265", "hold": "0"}
                    ],
                    "tokenToAvailableAfterMaintenance": [[0, "451.25"]],
                }

        self.adapter._live_account_mode = lambda: "unifiedAccount"
        self.adapter._info = UnifiedInfo()
        self.adapter._exchange = object()

        snapshot = core.HyperliquidAdapter.capital_snapshot(self.adapter)

        self.assertIsNotNone(snapshot)
        self.assertAlmostEqual(snapshot.account_value, 501.897265)
        self.assertEqual(snapshot.available_margin, 451.25)
        self.assertAlmostEqual(snapshot.total_margin_used, 50.647265)

    def test_unified_capital_rejects_missing_maintenance_availability(self) -> None:
        class IncompleteInfo:
            def spot_user_state(self, _wallet):
                return {
                    "balances": [{"coin": "USDC", "token": 0, "total": "500"}],
                    "tokenToAvailableAfterMaintenance": [],
                }

        self.adapter._live_account_mode = lambda: "unifiedAccount"
        self.adapter._info = IncompleteInfo()
        self.adapter._exchange = object()

        snapshot = core.HyperliquidAdapter.capital_snapshot(self.adapter)

        self.assertIsNone(snapshot)

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

    def test_rejected_zero_fill_close_is_failed_without_quarantine(self) -> None:
        rejected = {
            "status": "ok",
            "response": {"data": {"statuses": [{"error": "Order must have minimum value"}]}},
        }
        self.adapter._exchange = FakeExchange([rejected])
        unchanged = core.Position("BTC", "LONG", 0.50, 100.0, 3)
        self.adapter._confirmed_position = lambda _coin: (True, unchanged)
        key = "copy-event:tiny-close:close"

        result = self.adapter.close_position("BTC", 0.01, 100.0, key)

        self.assertFalse(result)
        self.assertEqual(result.status, "error")
        self.assertIn("position unchanged", result.detail)
        self.assertEqual(self.store.execution_intent(key)["state"], "FAILED")
        self.assertIsNone(self.store.coin_quarantine("BTC"))

    def test_execution_slippage_is_positive_only_when_adverse(self) -> None:
        cases = [
            ("LONG", "OPEN", 101.0, 100.0),
            ("LONG", "CLOSE", 99.0, 100.0),
            ("SHORT", "OPEN", 99.0, 100.0),
            ("SHORT", "CLOSE", 101.0, 100.0),
        ]
        for side, operation, fill, expected_bps in cases:
            result = core.ExecutionResult(
                True, requested_size=1.0, filled_size=1.0,
                avg_fill_price=fill, confirmed=True,
            )
            self.store.log_execution(
                "BTC", side, operation, result, reference_price=100.0
            )
            row = self.store.conn.execute(
                "SELECT slippage_bps FROM execution_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertAlmostEqual(row["slippage_bps"], expected_bps)

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

    def test_wallet_allocation_uses_exact_exchange_fill_size(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open(
            "wallet-a", "kPEPE", "LONG", 0.002832260029209785,
            30.381633, leverage=3, filled_size=32181.0,
        )

        row = self.store.open_position_slices("kPEPE")[0]
        self.assertEqual(row["cost_basis"], 30.38)
        self.assertEqual(row["filled_size"], 32181.0)
        self.assertEqual(
            paper.allocation_position_size("wallet-a", "kPEPE", "LONG"),
            32181.0,
        )

    def test_confirmed_exchange_fill_below_minimum_is_committed(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)

        rejected = paper.open(
            "wallet-a", "ETH", "SHORT", 1863.36, 0.68,
            leverage=3, filled_size=0.0011,
        )
        committed = paper.open(
            "wallet-a", "ETH", "SHORT", 1863.36, 0.68,
            leverage=3, filled_size=0.0011,
            confirmed_exchange_fill=True,
        )

        self.assertIsNone(rejected)
        self.assertEqual(committed, 0.68)
        row = self.store.open_position_slices("ETH")[0]
        self.assertEqual(row["filled_size"], 0.0011)
        self.assertEqual(row["cost_basis"], 0.68)

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

        reconciler._force_close("BTC", "wallet-a", "LONG", "test exit")

        signal = self.store.conn.execute(
            "SELECT price, paper_gain, reason FROM signals ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(signal["price"], 90.0)
        self.assertEqual(signal["paper_gain"], -3.0)
        self.assertIn("price_source=exchange_fill", signal["reason"])

    def test_reconciler_uses_exact_partial_size_then_full_final_close(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open(
            "wallet-a", "BTC", "LONG", 100, 10,
            leverage=3, filled_size=0.301,
        )
        paper.open(
            "wallet-b", "BTC", "LONG", 100, 20,
            leverage=3, filled_size=0.602,
        )
        requested_sizes: list[float | None] = []

        self.adapter.mid_price = lambda _coin: 100.0

        def close_position(_coin, size=None, _price=None, _intent_key=None):
            requested_sizes.append(size)
            return core.ExecutionResult(
                True, size or 0.602, size or 0.602, 100.0,
                status="filled", confirmed=True,
            )

        self.adapter.close_position = close_position
        reconciler = core.Reconciler(
            self.settings, self.store, self.adapter, paper,
            core.RiskManager(self.settings, self.store, core.Notifier("")),
        )

        reconciler._force_close("BTC", "wallet-a", "LONG", "test partial")
        reconciler._force_close("BTC", "wallet-b", "LONG", "test final")

        self.assertEqual(requested_sizes, [0.301, None])
        self.assertIsNone(paper.position("BTC"))

    def test_reconciler_defers_subminimum_partial_close_without_exchange_call(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open(
            "wallet-a", "BTC", "LONG", 100, 1,
            leverage=3, filled_size=0.03, confirmed_exchange_fill=True,
        )
        paper.open(
            "wallet-b", "BTC", "LONG", 100, 20,
            leverage=3, filled_size=0.60, confirmed_exchange_fill=True,
        )
        self.adapter.mid_price = lambda _coin: 100.0
        self.adapter.close_position = lambda *_args, **_kwargs: self.fail(
            "sub-minimum partial close must not reach the exchange"
        )
        reconciler = core.Reconciler(
            self.settings, self.store, self.adapter, paper,
            core.RiskManager(self.settings, self.store, core.Notifier("")),
        )

        reconciler._force_close("BTC", "wallet-a", "LONG", "source closed")

        self.assertTrue(paper.owns_position("wallet-a", "BTC", "LONG"))
        self.assertIsNone(self.store.coin_quarantine("BTC"))
        signal = self.store.conn.execute(
            "SELECT action, reason FROM signals ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(signal["action"], "SKIPPED")
        self.assertIn("deferred sub-minimum partial close", signal["reason"])

    def test_reconciler_does_not_close_quarantined_live_coin(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open("wallet-a", "BTC", "LONG", 100, 10, leverage=3)

        class ClosePlatform:
            closes = 0

            def close_position(self, *_args, **_kwargs):
                self.closes += 1
                return core.ExecutionResult(True, confirmed=True)

        platform = ClosePlatform()
        self.store.quarantine_coin("BTC", "live size mismatch", "test")
        reconciler = core.Reconciler(
            self.settings,
            self.store,
            platform,
            paper,
            core.RiskManager(self.settings, self.store, core.Notifier("")),
        )

        reconciler._force_close("BTC", "wallet-a", "LONG", "source closed")

        self.assertEqual(platform.closes, 0)
        self.assertTrue(paper.owns_position("wallet-a", "BTC", "LONG"))
        signal = self.store.conn.execute(
            "SELECT action, reason FROM signals ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(signal["action"], "SKIPPED")
        self.assertIn("coin quarantined", signal["reason"])

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

    def test_full_close_retries_whole_unit_dust_before_success(self) -> None:
        self.adapter._sz_decimals["kPEPE"] = 0
        exchange = FakeExchange([
            fill_response("184716", "0.002824", 8),
            fill_response("2", "0.002824", 9),
        ])
        self.adapter._exchange = exchange
        states = iter([
            (True, core.Position("kPEPE", "LONG", 184718.0, 0.002837, 3)),
            (True, core.Position("kPEPE", "LONG", 2.0, 0.002837, 3)),
            (True, None),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)

        result = self.adapter.close_position("kPEPE")

        self.assertTrue(result)
        self.assertTrue(result.confirmed)
        self.assertEqual(exchange.close_sizes, [184718.0, 2.0])
        self.assertIsNone(self.store.coin_quarantine("kPEPE"))

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

    def test_newly_ambiguous_copy_exit_is_not_treated_as_handled(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open("wallet", "BTC", "LONG", 100.0, 10.0, leverage=3)
        self.adapter._exchange = FakeExchange([fill_response("0.30", "99")])
        states = iter([
            (True, core.Position("BTC", "LONG", 0.30, 100.0, 3)),
            (False, None),
        ])
        self.adapter._confirmed_position = lambda _coin: next(states)
        self.adapter.mid_price = lambda _coin: 100.0
        bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
        bot.settings = self.settings
        bot.store = self.store
        bot.platform = self.adapter
        bot.paper = paper
        bot.scoring_engine = core.ScoringEngine(self.settings, self.store)
        event = core.CopyEvent("EXIT", "wallet", "BTC", "LONG", event_id=99)

        with self.assertRaisesRegex(RuntimeError, "remains unresolved"):
            bot._handle_exit(event)

        intent = self.store.execution_intent("copy-event:99:close")
        self.assertIn(intent["state"], {"SUBMITTING", "AMBIGUOUS"})
        self.assertTrue(paper.owns_position("wallet", "BTC", "LONG"))

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

    def test_one_percent_size_tolerance_and_rounding_floor(self) -> None:
        paper = core.PaperPortfolio(self.settings, self.store)
        paper.open("wallet", "BTC", "LONG", 100.0, 10.0, leverage=3)
        bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
        bot.settings = self.settings
        bot.store = self.store
        bot.paper = paper
        bot.platform = self.adapter

        bot._reconcile_live_book(
            {"BTC": core.Position("BTC", "LONG", 0.302, 100.0, 3)}
        )
        self.assertIsNone(self.store.coin_quarantine("BTC"))
        snapshot = self.store.get_json("live_position_snapshot", {})["BTC"]
        self.assertAlmostEqual(snapshot["size_difference"], 0.002)
        self.assertAlmostEqual(snapshot["size_tolerance"], 0.003)

        bot._reconcile_live_book(
            {"BTC": core.Position("BTC", "LONG", 0.304, 100.0, 3)}
        )
        quarantine = self.store.coin_quarantine("BTC")
        self.assertEqual(quarantine["reason"], "live size mismatch")
        self.assertIn("tolerance=0.003", quarantine["details"])


if __name__ == "__main__":
    unittest.main()
