from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import MockingBot as core
from helpers import settings


class MutablePositions:
    def __init__(self, states):
        self.states = states

    def positions(self, wallet):
        return self.states.get(wallet)


class EventDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.configured = settings(Path(self.temp.name), wallet_poll_delay=0)
        self.store = core.Store(self.configured.db_path)

    def tearDown(self) -> None:
        self.store.conn.close()
        self.temp.cleanup()

    def test_unacknowledged_event_survives_snapshot_advance_and_restart_scan(self) -> None:
        platform = MutablePositions({"wallet": {}})
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


if __name__ == "__main__":
    unittest.main()
