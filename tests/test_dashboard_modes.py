from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import MockingBot as core
import MockingBot_Dashboard as dashboard

from tests.helpers import TEST_USER, settings


class DashboardModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_paper_dashboard_uses_fixed_starting_equity(self) -> None:
        configured = settings(self.root / "paper")
        store = core.Store(configured.db_path)
        try:
            store.save_paper_account({"cash": 10_050.0, "realized_pnl": 50.0})
            store.set_json("session", {"paper_start": 10_025.0})
            store.set_json("paper_equity_high_water", {"value": 10_200.0})
        finally:
            store.conn.close()

        with patch.object(dashboard, "MODE", "paper"), patch.object(
            dashboard, "DB_PATH", configured.db_path
        ), patch.object(dashboard, "live_prices", return_value=({}, "stored")):
            data = dashboard.dashboard_data()
        self.assertTrue(data["ok"])
        self.assertEqual(data["instance_label"], "PAPER")
        self.assertEqual(data["baseline"], 10_000.0)
        self.assertEqual(data["baseline_source"], "paper-starting-equity")
        self.assertEqual(data["risk_reference"], 10_200.0)
        self.assertAlmostEqual(data["drawdown_pct"], 150.0 / 10_200.0 * 100)
        self.assertEqual(data["estimated_value"], 10_050.0)
        self.assertEqual(data["equity_source"], "paper-ledger")

    def test_live_dashboard_uses_live_baseline_and_equity(self) -> None:
        configured = settings(self.root / "live", live=True)
        store = core.Store(configured.db_path)
        try:
            store.save_paper_account({"cash": 500.0, "realized_pnl": 0.0})
            store.set_json(
                "live_account_identity",
                {
                    "wallet": TEST_USER,
                    "initial_account_value": 500.0,
                    "net_capital_contributions": 300.3,
                },
            )
            store.set_json(
                "live_risk_baseline",
                {"start_value": 800.3, "high_water_value": 810.3},
            )
            store.set_json(
                "live_equity_snapshot",
                {
                    "observed_unix": 1_000.0,
                    "account_value": 475.0,
                    "available": True,
                    "source": "risk-manager",
                },
            )
            store.record_live_equity(500.0, 900.0)
            store.record_live_equity(475.0, 1_000.0)
            store.quarantine_coin("BTC", "test mismatch", "size differs")
            result = core.ExecutionResult(
                True, 0.1, 0.1, 100.0, "7", "filled", True, "confirmed"
            )
            store.log_execution("ETH", "LONG", "OPEN", result)
        finally:
            store.conn.close()

        with patch.object(dashboard, "MODE", "live"), patch.object(
            dashboard, "DB_PATH", configured.db_path
        ), patch.object(dashboard, "live_prices", return_value=({}, "stored")), patch.object(
            dashboard.time, "time", return_value=1_010.0
        ):
            data = dashboard.dashboard_data()
        self.assertTrue(data["ok"])
        self.assertEqual(data["instance_label"], "LIVE")
        self.assertEqual(data["baseline"], 800.3)
        self.assertEqual(data["baseline_source"], "live-contributed-equity")
        self.assertEqual(data["risk_reference"], 810.3)
        self.assertEqual(data["estimated_value"], 475.0)
        self.assertEqual(data["equity_source"], "bot-risk-feed")
        self.assertAlmostEqual(data["drawdown_pct"], 335.3 / 810.3 * 100)
        self.assertAlmostEqual(data["daily_drawdown_pct"], 5.0)
        self.assertAlmostEqual(data["weekly_drawdown_pct"], 5.0)
        self.assertEqual(data["account_wallet"], f"{TEST_USER[:8]}...{TEST_USER[-4:]}")
        self.assertEqual(data["counts"]["quarantined"], 1)
        self.assertEqual(data["quarantines"][0]["coin"], "BTC")
        self.assertEqual(data["recent_executions"][0]["order_id"], "7")

    def test_stale_live_equity_is_unavailable_not_local_ledger_fallback(self) -> None:
        configured = settings(self.root / "stale-live", live=True)
        store = core.Store(configured.db_path)
        try:
            store.save_paper_account({"cash": 9999.0, "realized_pnl": 0.0})
            store.set_json(
                "live_account_identity",
                {"wallet": TEST_USER, "initial_account_value": 500.0},
            )
            store.set_json(
                "live_risk_baseline",
                {"start_value": 500.0, "high_water_value": 550.0},
            )
            store.set_json(
                "live_equity_snapshot",
                {"observed_unix": 100.0, "account_value": 480.0, "available": True},
            )
        finally:
            store.conn.close()

        with patch.object(dashboard, "MODE", "live"), patch.object(
            dashboard, "DB_PATH", configured.db_path
        ), patch.object(dashboard, "live_prices", return_value=({}, "stored")), patch.object(
            dashboard.time, "time", return_value=1_000.0
        ):
            data = dashboard.dashboard_data()

        self.assertIsNone(data["estimated_value"])
        self.assertIsNone(data["drawdown_pct"])
        self.assertEqual(data["equity_source"], "stale-bot-snapshot")
        self.assertEqual(data["local_estimate"], 9999.0)
        self.assertTrue(data["equity_error"])

    def test_token_risk_alerts_expire_from_dashboard_after_24_hours(self) -> None:
        configured = settings(self.root / "token-risk")
        store = core.Store(configured.db_path)
        try:
            with store.conn:
                store.conn.execute(
                    """
                    INSERT INTO token_risk_events(
                        ts, coin, wallet, side, signal, reason, market_cap_rank, source
                    ) VALUES(datetime('now', '-25 hours'), 'OLD', 'wallet', 'LONG',
                             'ENTRY', 'old notice', NULL, 'test')
                    """
                )
                store.conn.execute(
                    """
                    INSERT INTO token_risk_events(
                        ts, coin, wallet, side, signal, reason, market_cap_rank, source
                    ) VALUES(datetime('now', '-23 hours'), 'NEW', 'wallet', 'LONG',
                             'ENTRY', 'recent notice', NULL, 'test')
                    """
                )
        finally:
            store.conn.close()

        with patch.object(dashboard, "MODE", "paper"), patch.object(
            dashboard, "DB_PATH", configured.db_path
        ), patch.object(dashboard, "live_prices", return_value=({}, "stored")):
            data = dashboard.dashboard_data()

        self.assertEqual([row["coin"] for row in data["token_risk_alerts"]], ["NEW"])

    def test_recent_closes_are_ordered_by_close_time_not_slice_id(self) -> None:
        configured = settings(self.root / "recent-closes")
        store = core.Store(configured.db_path)
        try:
            with store.conn:
                store.conn.execute(
                    """
                    INSERT INTO paper_position_slices(
                        coin, side, source_wallet, entry_price, cost_basis,
                        leverage, opened_at, status, closed_at, exit_price,
                        paper_gain, pnl_pct
                    ) VALUES('KAITO','LONG',?,1.0,10.0,2.0,
                             '2026-08-01 00:00:00','CLOSED',
                             '2026-08-13 12:53:26',0.7,-6.0,-30.0)
                    """,
                    (TEST_USER,),
                )
                for index in range(11):
                    store.conn.execute(
                        """
                        INSERT INTO paper_position_slices(
                            coin, side, source_wallet, entry_price, cost_basis,
                            leverage, opened_at, status, closed_at, exit_price,
                            paper_gain, pnl_pct
                        ) VALUES(?,?,?,1.0,10.0,2.0,
                                 '2026-08-02 00:00:00','CLOSED',
                                 ?,1.01,0.2,1.0)
                        """,
                        (f"COIN{index}", "LONG", TEST_USER, f"2026-08-12 12:{index:02d}:00"),
                    )
        finally:
            store.conn.close()

        with patch.object(dashboard, "MODE", "paper"), patch.object(
            dashboard, "DB_PATH", configured.db_path
        ), patch.object(dashboard, "live_prices", return_value=({}, "stored")):
            data = dashboard.dashboard_data()

        self.assertEqual(data["recent_closes"][0]["coin"], "KAITO")
        self.assertEqual(len(data["recent_closes"]), 10)

    def test_dashboard_omits_position_risk_shadow_state(self) -> None:
        configured = settings(self.root / "position-risk")
        store = core.Store(configured.db_path)
        try:
            store.commit_paper_open(
                {"cash": 90.0, "realized_pnl": 0.0},
                "RISK", "LONG", 100.0, 10.0, TEST_USER, 3.0,
            )
            opened_at = str(store.open_position_slices("RISK")[0]["opened_at"])
            store.record_position_risk_snapshot(
                {
                    "observed_unix": 1_000.0,
                    "observed_at": "2026-08-13 00:00:00",
                    "coin": "RISK", "side": "LONG", "opened_at": opened_at,
                    "entry_price": 100.0, "mark_price": 88.0,
                    "relative_anchor_price": 100.0,
                    "return_pct": -12.0, "benchmark_symbols": '["BTC"]',
                    "benchmark_entry_prices": '{"BTC":100}',
                    "benchmark_return_pct": 0.0, "relative_return_pct": -12.0,
                    "funding": 0.0001, "open_interest": 1000.0,
                    "open_interest_change_pct": 3.0, "day_volume": 1_000_000.0,
                    "minutes_below_5": 60.0, "minutes_below_10": 30.0,
                    "minutes_below_15": 0.0, "state": "ADD_FROZEN",
                    "reasons": '["addition_freeze_threshold"]',
                    "shadow_action": "WOULD_FREEZE_ADDS",
                }
            )
        finally:
            store.conn.close()

        with patch.object(dashboard, "MODE", "paper"), patch.object(
            dashboard, "DB_PATH", configured.db_path
        ), patch.object(dashboard, "live_prices", return_value=({"RISK": 88.0}, "live")):
            data = dashboard.dashboard_data()

        self.assertNotIn("risk_state", data["positions"][0])
        self.assertNotIn("risk_action", data["positions"][0])
        self.assertNotIn("position_risk", data)

    def test_html_has_unambiguous_mode_and_safety_panels(self) -> None:
        self.assertIn('id="mode-badge"', dashboard.HTML)
        self.assertIn('id="quarantine-section"', dashboard.HTML)
        self.assertIn('id="executions"', dashboard.HTML)
        self.assertNotIn('id="position-risk"', dashboard.HTML)
        self.assertNotIn('Position Risk Shadow', dashboard.HTML)
        self.assertIn('Token Risk Alerts (last 24 hours)', dashboard.HTML)
        self.assertIn('id="timezone"', dashboard.HTML)
        self.assertIn('hour12: true', dashboard.HTML)
        self.assertIn('localStorage.getItem("mockingbot-timezone")', dashboard.HTML)
        self.assertNotIn('| read-only |', dashboard.HTML)
        self.assertNotIn('live mids cached', dashboard.HTML)
        self.assertNotIn('| equity ${data.equity_source}', dashboard.HTML)


if __name__ == "__main__":
    unittest.main()
