from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

import MockingBot as core
from helpers import settings


class MutablePositions:
    def __init__(self, states, prices=None):
        self.states = states
        self.prices = prices or {}

    def positions(self, wallet):
        return self.states.get(wallet)

    def mid_price(self, coin):
        return self.prices.get(coin)


class EventDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.configured = settings(Path(self.temp.name), wallet_poll_delay=0)
        self.store = core.Store(self.configured.db_path)

    def tearDown(self) -> None:
        self.store.conn.close()
        self.temp.cleanup()

    def test_unacknowledged_event_survives_snapshot_advance_and_restart_scan(self) -> None:
        platform = MutablePositions({"wallet": {}}, {"SOL": 51.25})
        monitor = core.WalletMonitor(self.configured, self.store, platform)
        monitor.scan(["wallet"])
        platform.states["wallet"] = {
            "BTC": core.Position("BTC", "LONG", 1.25, 100.0)
        }

        first, _ = monitor.scan(["wallet"])
        second, _ = monitor.scan(["wallet"])

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].kind, "ENTRY")
        self.assertEqual(first[0].current_size, 1.25)
        self.assertEqual([event.event_id for event in second], [first[0].event_id])

        self.store.acknowledge_copy_event(first[0].event_id)
        third, _ = monitor.scan(["wallet"])
        self.assertEqual(third, [])

    def test_degraded_batch_retains_successful_wallet_event(self) -> None:
        platform = MutablePositions({"good": {}, "bad-a": {}, "bad-b": {}})
        monitor = core.WalletMonitor(self.configured, self.store, platform)
        monitor.scan(["good", "bad-a", "bad-b"])
        platform.states = {
            "good": {"ETH": core.Position("ETH", "SHORT", 2.0, 200.0)},
            "bad-a": None,
            "bad-b": None,
        }

        events, fail_ratio = monitor.scan(["good", "bad-a", "bad-b"])

        self.assertGreater(fail_ratio, self.configured.api_degraded_max_fail_ratio)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].coin, "ETH")
        self.assertEqual(len(self.store.pending_copy_events()), 1)

    def test_flip_is_persisted_as_ordered_exit_then_entry(self) -> None:
        platform = MutablePositions(
            {"wallet": {"SOL": core.Position("SOL", "LONG", 4.0, 50.0)}}
        )
        monitor = core.WalletMonitor(self.configured, self.store, platform)
        monitor.scan(["wallet"])
        platform.states["wallet"] = {
            "SOL": core.Position("SOL", "SHORT", 3.0, 48.0)
        }

        events, _ = monitor.scan(["wallet"])

        self.assertEqual([(event.kind, event.side) for event in events], [("EXIT", "LONG"), ("ENTRY", "SHORT")])
        self.assertLess(events[0].event_id, events[1].event_id)

    def test_live_consumes_handled_canonical_events_once_in_source_order(self) -> None:
        root = Path(self.temp.name)
        paper_settings = settings(root / "paper", wallet_poll_delay=0)
        live_settings = settings(
            root / "live",
            live=True,
            wallet_poll_delay=0,
            scoring_seed_db_path=paper_settings.db_path,
        )
        paper_store = core.Store(paper_settings.db_path)
        live_store = core.Store(live_settings.db_path)
        platform = MutablePositions({"wallet": {}}, {"SOL": 51.25})
        try:
            with paper_store.conn:
                paper_store.conn.execute(
                    """
                    INSERT INTO marshal_shadow_positions(
                        wallet, coin, side, entry_price, opened_at, marshal_score,
                        marshal_tier, status, exit_price, closed_at, paper_gain,
                        pnl_pct, close_reason
                    ) VALUES('seed-wallet', 'BTC', 'LONG', 100, '2026-01-01',
                             60, 'Core', 'CLOSED', 105, '2026-01-02', 15, 5,
                             'canonical test')
                    """
                )
            paper_monitor = core.WalletMonitor(paper_settings, paper_store, platform)
            live_monitor = core.WalletMonitor(live_settings, live_store, platform)
            paper_monitor.scan(["wallet"])

            # First connection establishes a cursor and never replays history.
            events, failures = live_monitor.scan(["wallet"])
            self.assertEqual(events, [])
            self.assertEqual(failures, 0.0)
            copied_shadow = live_store.conn.execute(
                "SELECT wallet, pnl_pct FROM marshal_shadow_positions"
            ).fetchone()
            self.assertEqual(tuple(copied_shadow), ("seed-wallet", 5.0))
            self.assertEqual(
                live_store.conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0],
                0,
            )

            platform.states["wallet"] = {
                "SOL": core.Position("SOL", "LONG", 2.0, 50.0)
            }
            paper_events, _ = paper_monitor.scan(["wallet"])
            self.assertEqual(len(paper_events), 1)

            # Paper has not completed the decision, so live must wait.
            live_events, _ = live_monitor.scan(["wallet"])
            self.assertEqual(live_events, [])

            paper_store.acknowledge_copy_event(paper_events[0].event_id)
            live_events, _ = live_monitor.scan(["wallet"])
            self.assertEqual(len(live_events), 1)
            self.assertEqual(live_events[0].source_event_id, paper_events[0].event_id)
            self.assertEqual(live_events[0].observed_price, 51.25)
            self.assertEqual(
                (live_events[0].kind, live_events[0].wallet, live_events[0].coin),
                ("ENTRY", "wallet", "SOL"),
            )

            # Repeated scans and a reconstructed monitor cannot duplicate it.
            again, _ = live_monitor.scan(["wallet"])
            self.assertEqual([event.event_id for event in again], [live_events[0].event_id])
            live_store.acknowledge_copy_event(live_events[0].event_id)
            restarted = core.WalletMonitor(live_settings, live_store, platform)
            final, _ = restarted.scan(["wallet"])
            self.assertEqual(final, [])
            self.assertEqual(
                live_store.conn.execute(
                    "SELECT COUNT(*) FROM pending_copy_events WHERE source_event_id IS NOT NULL"
                ).fetchone()[0],
                1,
            )
        finally:
            paper_store.conn.close()
            live_store.conn.close()

    def test_canonical_shadow_import_supports_legacy_required_score_column(self) -> None:
        root = Path(self.temp.name)
        paper_settings = settings(root / "legacy-paper")
        paper_store = core.Store(paper_settings.db_path)
        with paper_store.conn:
            paper_store.conn.execute(
                """
                INSERT INTO marshal_shadow_positions(
                    wallet, coin, side, entry_price, opened_at, marshal_score,
                    marshal_tier, status
                ) VALUES('wallet', 'BTC', 'LONG', 100, '2026-01-01', 61,
                         'Core', 'OPEN')
                """
            )

        live_settings = settings(
            root / "legacy-live", live=True,
            scoring_seed_db_path=paper_settings.db_path,
        )
        live_settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        legacy = sqlite3.connect(live_settings.db_path)
        legacy.execute(
            """
            CREATE TABLE marshal_shadow_positions(
                id INTEGER PRIMARY KEY, wallet TEXT NOT NULL, coin TEXT NOT NULL,
                side TEXT NOT NULL, entry_price REAL NOT NULL, opened_at TEXT NOT NULL,
                source_signal_id INTEGER, scoring_score REAL NOT NULL,
                marshal_tier TEXT NOT NULL, status TEXT NOT NULL, exit_price REAL,
                closed_at TEXT, paper_gain REAL, pnl_pct REAL, close_signal_id INTEGER,
                close_reason TEXT)
            """
        )
        legacy.commit()
        legacy.close()

        live_store = core.Store(live_settings.db_path)
        try:
            live_store.import_canonical_copy_events(paper_settings.db_path)
            row = live_store.conn.execute(
                "SELECT marshal_score, scoring_score FROM marshal_shadow_positions"
            ).fetchone()
            self.assertEqual(tuple(row), (61.0, 61.0))
        finally:
            paper_store.conn.close()
            live_store.conn.close()


if __name__ == "__main__":
    unittest.main()
