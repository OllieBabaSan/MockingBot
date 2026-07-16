from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import MockingBot as core


class BackupTests(unittest.TestCase):
    def test_online_backup_includes_wal_data_and_passes_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = core.Store(root / "state.sqlite3")
            try:
                store.set_json("wal_only_value", {"amount": 501.25})
                backup = store.create_verified_backup(root / "backups", 3)
            finally:
                store.conn.close()

            self.assertTrue(backup.exists())
            snapshot = sqlite3.connect(backup)
            try:
                self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                raw = snapshot.execute(
                    "SELECT value FROM kv WHERE key='wal_only_value'"
                ).fetchone()[0]
                self.assertIn("501.25", raw)
            finally:
                snapshot.close()

    def test_backup_retention_keeps_newest_verified_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = core.Store(root / "state.sqlite3")
            try:
                created = []
                for index in range(4):
                    store.set_json("generation", index)
                    created.append(store.create_verified_backup(root / "backups", 2))
                    time.sleep(0.002)
            finally:
                store.conn.close()

            retained = sorted((root / "backups").glob("mockingbot-*.sqlite3"))
            self.assertEqual(len(retained), 2)
            self.assertFalse(created[0].exists())
            self.assertFalse(created[1].exists())
            self.assertTrue(created[2].exists())
            self.assertTrue(created[3].exists())


if __name__ == "__main__":
    unittest.main()
