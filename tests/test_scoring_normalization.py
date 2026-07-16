from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import MockingBot as core
from helpers import settings


class ScoringNormalizationTests(unittest.TestCase):
    def score_for_gains(self, root: Path, gains: list[float], pnls: list[float]):
        configured = settings(root)
        store = core.Store(configured.db_path)
        try:
            for index, (gain, pnl) in enumerate(zip(gains, pnls, strict=True)):
                store.log_signal(
                    "wallet",
                    f"COIN{index}",
                    "LONG",
                    "EXIT",
                    100.0,
                    "EXECUTED",
                    paper_gain=gain,
                    pnl_pct=pnl,
                )
            return core.ScoringEngine(configured, store).score_wallet("wallet")
        finally:
            store.conn.close()

    def test_score_is_independent_of_account_size_and_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            small = self.score_for_gains(root / "small", [3.0, -1.5, 6.0], [1.0, -0.5, 2.0])
            large = self.score_for_gains(root / "large", [60.0, -30.0, 120.0], [1.0, -0.5, 2.0])

        self.assertEqual(small.total_score, large.total_score)
        self.assertEqual(small.realized_pnl, large.realized_pnl)
        self.assertEqual(small.realized_pnl, 75.0)
        self.assertIn("return=+2.500%", small.explanation)
        self.assertIn("actual=$7.50", small.explanation)
        self.assertIn("actual=$150.00", large.explanation)

    def test_normalized_losses_penalize_equally_at_different_allocations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            small = self.score_for_gains(root / "small", [-2.0, -4.0, 1.0], [-1.0, -2.0, 0.5])
            large = self.score_for_gains(root / "large", [-40.0, -80.0, 20.0], [-1.0, -2.0, 0.5])

        self.assertEqual(small.total_score, large.total_score)
        self.assertEqual(small.tier, large.tier)
        self.assertEqual(small.realized_pnl, -75.0)


if __name__ == "__main__":
    unittest.main()
