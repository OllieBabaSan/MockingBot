from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import MockingBot as core

from tests.helpers import settings


class FakeMarket:
    name = "fake"

    def __init__(self) -> None:
        self.prices = {"RISK": 90.0, "BTC": 100.0, "ETH": 100.0, "SOL": 100.0}
        self.open_interest = 1_000.0

    def mid_price(self, coin: str) -> float | None:
        return self.prices.get(coin)

    def market_contexts(self):
        return {
            "RISK": {
                "funding": 0.0001,
                "open_interest": self.open_interest,
                "day_volume": 1_000_000.0,
            }
        }


class PositionRiskShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.configured = settings(
            Path(self.temp.name),
            position_risk_interval_seconds=300,
        )
        self.store = core.Store(self.configured.db_path)
        self.store.commit_paper_open(
            {"cash": 90.0, "realized_pnl": 0.0},
            "RISK", "LONG", 100.0, 10.0, "wallet", 3.0,
        )
        self.market = FakeMarket()
        self.monitor = core.PositionRiskMonitor(
            self.configured, self.store, self.market
        )

    def tearDown(self) -> None:
        self.store.conn.close()
        self.temp.cleanup()

    def snapshots(self):
        return self.store.conn.execute(
            "SELECT * FROM position_risk_snapshots ORDER BY id"
        ).fetchall()

    def test_records_freeze_candidate_without_changing_position(self) -> None:
        self.assertEqual(self.monitor.observe(now=1_000.0), 1)
        row = self.snapshots()[0]
        self.assertEqual(row["state"], "ADD_FROZEN")
        self.assertEqual(row["shadow_action"], "WOULD_FREEZE_ADDS")
        self.assertAlmostEqual(float(row["return_pct"]), -10.0)
        self.assertAlmostEqual(float(row["relative_return_pct"]), 0.0)
        self.assertIn("addition_freeze_threshold", json.loads(row["reasons"]))
        self.assertIsNotNone(self.store.paper_position_slice("wallet", "RISK", "LONG"))

    def test_respects_snapshot_interval(self) -> None:
        self.assertEqual(self.monitor.observe(now=1_000.0), 1)
        self.assertEqual(self.monitor.observe(now=1_299.0), 0)
        self.assertEqual(self.monitor.observe(now=1_300.0), 1)
        self.assertEqual(len(self.snapshots()), 2)

    def test_compound_state_requires_duration_relative_weakness_and_confirmation(self) -> None:
        self.market.prices["RISK"] = 80.0
        self.assertEqual(self.monitor.observe(now=1_000.0), 1)
        for step in range(1, 72):
            self.monitor.observe(now=1_000.0 + step * 300)
        before = self.snapshots()[-1]
        self.assertEqual(before["state"], "THESIS_IMPAIRED")
        self.market.prices.update({"RISK": 75.0, "BTC": 104.0, "ETH": 104.0, "SOL": 104.0})
        self.market.open_interest = 1_030.0
        self.monitor.observe(now=1_000.0 + 72 * 300)
        final = self.snapshots()[-1]
        self.assertEqual(final["state"], "EXIT_CANDIDATE")
        self.assertEqual(final["shadow_action"], "WOULD_EXIT")
        self.assertAlmostEqual(float(final["minutes_below_10"]), 360.0)
        self.assertIn("confirmation=worsening_price", json.loads(final["reasons"]))


if __name__ == "__main__":
    unittest.main()
