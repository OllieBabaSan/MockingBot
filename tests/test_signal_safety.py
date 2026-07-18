from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MockingBot as core

from tests.helpers import settings


def score(wallet: str, tier: str, total: float) -> core.ScoringEngineScore:
    return core.ScoringEngineScore(
        wallet=wallet,
        tier=tier,
        total_score=total,
        realized_component=0,
        win_rate_component=0,
        recent_form_component=0,
        churn_penalty=0,
        loss_penalty=0,
        sample_size=10,
        realized_pnl=0,
        win_rate=0.6,
        avg_pnl_pct=1.0,
        explanation="test",
    )


class SignalSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings(Path(self.temp.name), live=True)
        self.store = core.Store(self.settings.db_path)
        self.paper = core.PaperPortfolio(self.settings, self.store)

    def tearDown(self) -> None:
        self.store.conn.close()
        self.temp.cleanup()

    def bot(self, incumbent: core.ScoringEngineScore) -> core.CopyTradingBot:
        bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
        bot.settings = self.settings
        bot.store = self.store
        bot.paper = self.paper
        bot.scoring_engine = type(
            "Scores", (), {"score_wallet": lambda _self, _wallet: incumbent}
        )()

        class Reconciler:
            def _force_close(_self, coin, wallet, side, _reason, _intent_key=None):
                while self.paper.owns_position(wallet, coin, side):
                    self.paper.close(wallet, coin, side, 100.0)

        bot.reconciler = Reconciler()
        return bot

    def test_stale_entry_age_uses_durable_detection_time(self) -> None:
        detected = datetime.now(timezone.utc) - timedelta(minutes=6)
        event = core.CopyEvent(
            "ENTRY", "wallet", "BTC", "LONG", 100.0,
            detected_at=detected.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.assertGreater(core.CopyTradingBot._event_age_seconds(event), 300)
        exit_event = core.CopyEvent(
            "EXIT", "wallet", "BTC", "LONG",
            detected_at=event.detected_at,
        )
        self.assertGreater(core.CopyTradingBot._event_age_seconds(exit_event), 300)

    def test_strictly_superior_tier_closes_incumbent_before_replacement(self) -> None:
        self.paper.open("incumbent", "BTC", "LONG", 100.0, 100.0, leverage=3)
        bot = self.bot(score("incumbent", "Candidate", 56.0))
        held = {"BTC"}
        allowed = bot._apply_ranked_opposite_override(
            core.CopyEvent("ENTRY", "incoming", "BTC", "SHORT", event_id=7),
            score("incoming", "Core", 60.0),
            held,
        )
        self.assertTrue(allowed)
        self.assertIsNone(self.paper.position("BTC"))
        self.assertNotIn("BTC", held)

    def test_same_or_lower_tier_cannot_replace_incumbent(self) -> None:
        self.paper.open("incumbent", "BTC", "LONG", 100.0, 100.0, leverage=3)
        bot = self.bot(score("incumbent", "Core", 58.0))
        allowed = bot._apply_ranked_opposite_override(
            core.CopyEvent("ENTRY", "incoming", "BTC", "SHORT"),
            score("incoming", "Core", 74.0),
            {"BTC"},
        )
        self.assertFalse(allowed)
        self.assertIsNotNone(self.paper.position("BTC"))


if __name__ == "__main__":
    unittest.main()
