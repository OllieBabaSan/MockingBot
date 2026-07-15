from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import MockingBot as core

from tests.helpers import settings


class InstanceLockTests(unittest.TestCase):
    def test_duplicate_process_for_same_data_directory_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            configured = settings(Path(td), live=True)
            first = core.InstanceLock(configured)
            first.acquire()
            try:
                second = core.InstanceLock(configured)
                with self.assertRaisesRegex(RuntimeError, "another live bot instance"):
                    second.acquire()
            finally:
                first.release()
            self.assertFalse(first.path.exists())

    def test_stale_lock_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            configured = settings(Path(td))
            configured.data_dir.mkdir(parents=True, exist_ok=True)
            lock_path = configured.data_dir / "mockingbot.instance.lock"
            lock_path.write_text(
                json.dumps({"pid": 999_999_999, "mode": "paper", "token": "stale"}),
                encoding="utf-8",
            )
            lock = core.InstanceLock(configured)
            with lock:
                owner = json.loads(lock.path.read_text(encoding="utf-8"))
                self.assertEqual(owner["pid"], os.getpid())
            self.assertFalse(lock.path.exists())

    def test_different_data_directories_do_not_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = core.InstanceLock(settings(root / "paper"))
            second = core.InstanceLock(settings(root / "live", live=True))
            with first, second:
                self.assertNotEqual(first.path, second.path)
                self.assertTrue(first.path.exists())
                self.assertTrue(second.path.exists())


if __name__ == "__main__":
    unittest.main()
