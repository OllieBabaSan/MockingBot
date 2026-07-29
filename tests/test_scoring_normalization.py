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
                store.conn.execute(
                    """
                    INSERT INTO scoring_seed_signals(
                        source_signal_id, ts, wallet, signal, paper_gain, pnl_pct
                    ) VALUES(?, ?, 'wallet', 'EXIT', ?, ?)
                    """,
                    (index + 1, core.utc_now(), gain, pnl),
                )
            store.conn.commit()
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

    def test_shadow_score_is_independent_of_local_trade_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scores = []
            for name, action, reason in (
                ("accepted", "EXECUTED", "allocated"),
                ("rejected", "SKIPPED", "position cap"),
            ):
                configured = settings(Path(tmp) / name)
                store = core.Store(configured.db_path)
                try:
                    engine = core.ScoringEngine(configured, store)
                    entry = core.CopyEvent("ENTRY", "wallet", "BTC", "LONG")
                    exit_event = core.CopyEvent("EXIT", "wallet", "BTC", "LONG")
                    engine.observe_signal(entry, None, action, reason, 100.0)
                    engine.observe_signal(exit_event, None, action, reason, 102.0)
                    scores.append(engine.score_wallet("wallet"))
                finally:
                    store.conn.close()

        self.assertEqual(scores[0].sample_size, 1)
        self.assertEqual(scores[0].total_score, scores[1].total_score)
        self.assertEqual(scores[0].realized_pnl, scores[1].realized_pnl)

    def test_active_drawdown_caps_tier_and_recovery_restores_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configured = settings(Path(tmp))
            store = core.Store(configured.db_path)
            try:
                for index in range(8):
                    store.conn.execute(
                        """
                        INSERT INTO scoring_seed_signals(
                            source_signal_id, ts, wallet, signal, paper_gain, pnl_pct
                        ) VALUES(?, ?, 'wallet', 'EXIT', 20, 2)
                        """,
                        (index + 1, core.utc_now()),
                    )
                store.conn.execute(
                    """
                    INSERT INTO wallet_positions(
                        wallet, coin, side, size, entry_price,
                        unrealized_pnl, margin_used, seen_at
                    ) VALUES('wallet', 'BTC', 'LONG', 1, 100, -20, 100, ?)
                    """,
                    (core.utc_now(),),
                )
                store.conn.commit()
                engine = core.ScoringEngine(configured, store)

                underwater = engine.score_wallet("wallet")
                store.conn.execute(
                    """
                    UPDATE wallet_positions
                    SET unrealized_pnl = -1
                    WHERE wallet = 'wallet' AND coin = 'BTC'
                    """
                )
                store.conn.commit()
                recovered = engine.score_wallet("wallet")
            finally:
                store.conn.close()

        self.assertEqual(underwater.tier, "Candidate")
        self.assertIn("active=20.0%/1 penalty=-15.0", underwater.explanation)
        self.assertEqual(recovered.tier, "Elite")
        self.assertNotIn("active=", recovered.explanation)

    def test_realized_component_clips_single_trade_outliers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            normal = self.score_for_gains(root / "normal", [1.0] * 8, [1.0] * 7 + [5.0])
            outlier = self.score_for_gains(root / "outlier", [1.0] * 8, [1.0] * 7 + [50.0])

        self.assertEqual(normal.total_score, outlier.total_score)
        self.assertEqual(normal.tier, outlier.tier)


if __name__ == "__main__":
    unittest.main()
