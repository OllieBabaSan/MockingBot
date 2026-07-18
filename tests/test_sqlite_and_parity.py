from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

import MockingBot as core
import MockingBot_Compare as compare

from tests.helpers import settings


class SQLiteTests(unittest.TestCase):
    def test_scoring_journal_column_name_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite3"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE marshal_signal_journal "
                    "(id INTEGER PRIMARY KEY, scoring_score REAL)"
                )
                conn.execute(
                    "CREATE TABLE marshal_shadow_positions "
                    "(id INTEGER PRIMARY KEY, scoring_score REAL)"
                )
                conn.execute(
                    "INSERT INTO marshal_signal_journal(id, scoring_score) VALUES(1, 71.5)"
                )
                conn.execute(
                    "INSERT INTO marshal_shadow_positions(id, scoring_score) VALUES(1, 68.25)"
                )
                conn.commit()
            finally:
                conn.close()

            store = core.Store(path)
            try:
                for table, expected in (
                    ("marshal_signal_journal", 71.5),
                    ("marshal_shadow_positions", 68.25),
                ):
                    columns = {
                        row["name"]
                        for row in store.conn.execute(f"PRAGMA table_info({table})")
                    }
                    self.assertIn("marshal_score", columns)
                    actual = store.conn.execute(
                        f"SELECT marshal_score FROM {table} WHERE id = 1"
                    ).fetchone()[0]
                    self.assertEqual(actual, expected)
            finally:
                store.conn.close()

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
            "policy_fingerprint": "policy",
            "environment_fingerprint": "environment",
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
        live["policy_fingerprint"] = "different"
        self.assertEqual(
            compare.compare([paper], [live], 180)[0]["classification"],
            "CONFIG_DIVERGENCE",
        )

    def test_intentional_environment_difference_is_not_config_divergence(self) -> None:
        paper = self.row(1, "paper-main", "2026-07-15 01:00:00")
        live = self.row(2, "live-main", "2026-07-15 01:00:10")
        live["environment_fingerprint"] = "live-four-slots"

        result = compare.compare([paper], [live], 180)[0]

        self.assertEqual(result["classification"], "EXPECTED_ENVIRONMENT_VARIANCE")
        self.assertIn(result["classification"], compare.NON_ISSUE_CLASSIFICATIONS)

    def test_policy_fingerprint_excludes_slots_but_includes_leverage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = settings(Path(td), max_positions=10)
            four_slots = replace(base, max_positions=4, live_margin_reserve_pct=0.10)
            changed_leverage = replace(base, scoring_engine_core_leverage=4)

            self.assertEqual(
                core.parity_policy_fingerprint(base),
                core.parity_policy_fingerprint(four_slots),
            )
            self.assertNotEqual(
                core.parity_environment_fingerprint(base),
                core.parity_environment_fingerprint(four_slots),
            )
            self.assertNotEqual(
                core.parity_policy_fingerprint(base),
                core.parity_policy_fingerprint(changed_leverage),
            )

    def test_missing_signal(self) -> None:
        paper = self.row(1, "paper-main", "2026-07-15 01:00:00")
        self.assertEqual(compare.compare([paper], [], 180)[0]["classification"], "MISSING_SIGNAL")


if __name__ == "__main__":
    unittest.main()
