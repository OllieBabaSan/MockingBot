from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

import MockingBot as core
import MockingBot_Compare as compare

from tests.helpers import settings


class SQLiteTests(unittest.TestCase):
    def test_wal_reader_and_locked_writer_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "contention.sqlite3"
            store = core.Store(db)
            try:
                self.assertEqual(store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                store.set_json("baseline", {"ok": True})
                blocker = sqlite3.connect(db, timeout=0.1)
                blocker.execute("PRAGMA journal_mode=WAL")
                blocker.execute("BEGIN IMMEDIATE")
                blocker.execute("INSERT OR REPLACE INTO kv(key,value) VALUES('held','1')")

                reader = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1)
                try:
                    self.assertIsNotNone(reader.execute("SELECT value FROM kv WHERE key='baseline'").fetchone())
                finally:
                    reader.close()

                store.conn.execute("PRAGMA busy_timeout=20")
                errors = []

                def writer() -> None:
                    try:
                        store.set_json("after_lock", {"written": True})
                    except Exception as exc:  # pragma: no cover - failure is asserted below
                        errors.append(exc)

                thread = threading.Thread(target=writer)
                thread.start()
                time.sleep(0.25)
                blocker.commit()
                blocker.close()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors)
                self.assertEqual(store.get_json("after_lock", {}), {"written": True})
                self.assertEqual(store.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                store.conn.close()


class ParityTests(unittest.TestCase):
    def row(self, row_id: int, instance: str, ts: str) -> dict:
        return {
            "id": row_id,
            "ts": ts,
            "instance_id": instance,
            "wallet": "wallet",
            "coin": "BTC",
            "side": "LONG",
            "signal": "ENTRY",
            "action": "SKIPPED",
            "reason": "position cap",
            "wallet_tier": "Core",
            "wallet_score": 60.0,
            "sample_size": 5,
            "observed_price": 100.0,
            "config_fingerprint": "config",
            "code_fingerprint": "code",
        }

    def test_match_and_divergence_classification(self) -> None:
        paper = self.row(1, "paper-main", "2026-07-15 01:00:00")
        live = self.row(2, "live-main", "2026-07-15 01:00:10")
        self.assertEqual(compare.compare([paper], [live], 180)[0]["classification"], "MATCH")

        live["action"] = "EXECUTED"
        live["reason"] = ""
        self.assertEqual(
            compare.compare([paper], [live], 180)[0]["classification"],
            "STATE_DIVERGENCE",
        )
        live["config_fingerprint"] = "different"
        self.assertEqual(
            compare.compare([paper], [live], 180)[0]["classification"],
            "CONFIG_DIVERGENCE",
        )

    def test_missing_signal(self) -> None:
        paper = self.row(1, "paper-main", "2026-07-15 01:00:00")
        self.assertEqual(compare.compare([paper], [], 180)[0]["classification"], "MISSING_SIGNAL")


if __name__ == "__main__":
    unittest.main()
