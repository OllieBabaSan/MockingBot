from __future__ import annotations

from dataclasses import replace
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import MockingBot as core

from tests.helpers import settings


def score(tier: str, sample_size: int = 5, total: float = 60, realized: float = 10) -> core.ScoringEngineScore:
    return core.ScoringEngineScore(
        wallet="wallet", tier=tier, total_score=total, realized_component=0,
        win_rate_component=0, recent_form_component=0, churn_penalty=0,
        loss_penalty=0, sample_size=sample_size, realized_pnl=realized,
        win_rate=None, avg_pnl_pct=None, explanation="test",
    )


class LeveragePolicyTests(unittest.TestCase):
    def test_live_environment_defaults_to_four_positions_and_three_x_tiers(self) -> None:
        env = os.environ.copy()
        env["HL_LIVE"] = "true"
        env.pop("MAX_POSITIONS", None)
        result = subprocess.run(
            [sys.executable, "-c", "import MockingBot as m; s=m.Settings(); print(s.max_positions, s.max_leverage_cap, s.scoring_engine_elite_leverage)"],
            cwd=Path(__file__).parents[1], env=env, text=True, capture_output=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), "4 5 3")

    def test_tier_leverage_selection_and_cap_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            configured = settings(
                Path(td), scoring_engine_default_candidate_leverage=1,
                scoring_engine_candidate_leverage=2,
                scoring_engine_proven_candidate_leverage=3,
                scoring_engine_core_leverage=4, scoring_engine_elite_leverage=5,
            )
            store = core.Store(configured.db_path)
            try:
                engine = core.ScoringEngine(configured, store)
                self.assertEqual(engine.leverage_for_score(score("Candidate", sample_size=2)), 1)
                self.assertEqual(engine.leverage_for_score(score("Candidate", total=52)), 2)
                self.assertEqual(engine.leverage_for_score(score("Candidate")), 3)
                self.assertEqual(engine.leverage_for_score(score("Core")), 4)
                self.assertEqual(engine.leverage_for_score(score("Elite")), 5)
            finally:
                store.conn.close()
            with self.assertRaisesRegex(ValueError, "Elite leverage"):
                core.validate_settings(replace(configured, scoring_engine_elite_leverage=6))
            with self.assertRaisesRegex(ValueError, "SLIPPAGE"):
                core.validate_settings(replace(configured, slippage=0.021))

    def test_position_pnl_uses_persisted_leverage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            configured = settings(Path(td))
            store = core.Store(configured.db_path)
            try:
                portfolio = core.PaperPortfolio(configured, store)
                portfolio.open("wallet", "BTC", "LONG", 100, 100, leverage=2)
                self.assertEqual(portfolio.value(lambda _coin: 110), 10020.0)
                gain, pnl_pct, side = portfolio.close("wallet", "BTC", "LONG", 110)
                self.assertEqual((gain, pnl_pct, side), (20.0, 10.0, "LONG"))
            finally:
                store.conn.close()

    def test_same_coin_slices_share_first_position_leverage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            configured = settings(Path(td))
            store = core.Store(configured.db_path)
            try:
                portfolio = core.PaperPortfolio(configured, store)
                portfolio.open("wallet-a", "BTC", "LONG", 100, 100, leverage=3)
                portfolio.open("wallet-b", "BTC", "LONG", 100, 100, leverage=5)
                self.assertEqual(
                    {float(row["leverage"]) for row in store.open_position_slices("BTC")},
                    {3.0},
                )
            finally:
                store.conn.close()

    def test_legacy_position_table_gets_three_x_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE paper_position_slices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT NOT NULL,
                    side TEXT NOT NULL, source_wallet TEXT NOT NULL,
                    entry_price REAL NOT NULL, cost_basis REAL NOT NULL,
                    opened_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
                    closed_at TEXT, exit_price REAL, paper_gain REAL, pnl_pct REAL
                )"""
            )
            conn.commit()
            conn.close()
            store = core.Store(path)
            try:
                columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(paper_position_slices)")}
                self.assertIn("leverage", columns)
            finally:
                store.conn.close()


if __name__ == "__main__":
    unittest.main()
