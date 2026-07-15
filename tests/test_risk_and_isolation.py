from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import MockingBot as core

from tests.helpers import TEST_AGENT, TEST_USER, settings


class FakePortfolio:
    def value(self, _price_fn):
        return 10_000.0


class FakePlatform:
    def __init__(self, value=500.0, positions=None):
        self.value = value
        self.positions = positions or {}

    def account_value(self):
        return self.value

    def mid_price(self, _coin):
        return None

    def live_positions(self):
        return self.positions


class RiskAndIsolationTests(unittest.TestCase):
    def test_warning_and_persistent_hard_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            configured = settings(
                Path(td), live=True, warning_drawdown_pct=0.15, max_drawdown_pct=0.25
            )
            store = core.Store(configured.db_path)
            try:
                risk = core.RiskManager(configured, store, core.Notifier(""))
                platform = FakePlatform(500.0)
                baseline = risk.session_start_value(FakePortfolio(), platform)
                self.assertEqual(baseline, 500.0)
                risk.check_warning(risk.drawdown(baseline, 425.0))
                self.assertFalse(configured.circuit_breaker_file.exists())
                self.assertTrue(risk.check_circuit_breaker(baseline, 375.0))
                payload = json.loads(configured.circuit_breaker_file.read_text(encoding="utf-8"))
                self.assertEqual(payload["mode"], "live")
                self.assertEqual(payload["drawdown_pct"], 25.0)
                platform.value = 700.0
                self.assertEqual(risk.session_start_value(FakePortfolio(), platform), 500.0)
            finally:
                store.conn.close()

    def test_scoring_bootstrap_preserves_score_but_not_trading_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paper_settings = settings(root / "paper")
            paper_store = core.Store(paper_settings.db_path)
            wallet = "0x" + "3" * 40
            try:
                paper_store.replace_roster(
                    [wallet],
                    {wallet: core.WalletMetrics(False, True, 30, 0.60, 1.8)},
                )
                paper_store.log_signal(wallet, "BTC", "LONG", "ENTRY", 100, "EXECUTED")
                paper_store.log_signal(
                    wallet, "BTC", "LONG", "EXIT", 105, "EXECUTED", paper_gain=25, pnl_pct=5
                )
                expected = core.ScoringEngine(paper_settings, paper_store).score_wallet(wallet)

                live_settings = settings(
                    root / "live", live=True, scoring_seed_db_path=paper_settings.db_path
                )
                live_store = core.Store(live_settings.db_path)
                try:
                    metadata = live_store.bootstrap_scoring_history(paper_settings.db_path)
                    actual = core.ScoringEngine(live_settings, live_store).score_wallet(wallet)
                    self.assertEqual(
                        (actual.tier, actual.total_score, actual.sample_size, actual.realized_pnl),
                        (expected.tier, expected.total_score, expected.sample_size, expected.realized_pnl),
                    )
                    self.assertEqual(live_store.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 0)
                    self.assertEqual(
                        live_store.conn.execute("SELECT COUNT(*) FROM paper_position_slices").fetchone()[0],
                        0,
                    )
                    self.assertIsNone(live_store.get_json("paper_account", None))
                    self.assertEqual(metadata, live_store.bootstrap_scoring_history(paper_settings.db_path))
                finally:
                    live_store.conn.close()
            finally:
                paper_store.conn.close()

    def test_first_live_start_uses_real_equity_and_requires_flat_account(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            configured = settings(Path(td), live=True)
            store = core.Store(configured.db_path)
            try:
                bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
                bot.settings = configured
                bot.store = store
                bot.platform = FakePlatform(501.9)
                bot._validate_live_state()
                self.assertEqual(store.paper_account(0)["cash"], 501.9)
                identity = store.get_json("live_account_identity", {})
                self.assertEqual(identity["wallet"], TEST_USER)
                self.assertEqual(identity["api_wallet"], TEST_AGENT)
            finally:
                store.conn.close()

        with tempfile.TemporaryDirectory() as td:
            configured = settings(Path(td), live=True)
            store = core.Store(configured.db_path)
            try:
                bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
                bot.settings = configured
                bot.store = store
                bot.platform = FakePlatform(501.9, {"BTC": core.Position("BTC", "LONG", 1, 100)})
                with self.assertRaisesRegex(RuntimeError, "requires a flat"):
                    bot._validate_live_state()
            finally:
                store.conn.close()


if __name__ == "__main__":
    unittest.main()
