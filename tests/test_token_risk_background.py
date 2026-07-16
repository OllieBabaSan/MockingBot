from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

import MockingBot as core
from tests.helpers import settings


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return [{"symbol": "btc", "market_cap_rank": 1}]


class TokenRiskBackgroundTests(unittest.TestCase):
    def test_observe_never_waits_for_network_and_main_thread_persists_result(self):
        with tempfile.TemporaryDirectory() as temp:
            configured = settings(
                Path(temp), token_risk_top_n=1,
                token_risk_refresh_seconds=3600,
            )
            store = core.Store(configured.db_path)
            monitor = core.TokenRiskMonitor(configured, store)
            started = threading.Event()
            release = threading.Event()
            original_get = core.requests.get
            writer_threads: list[int] = []
            original_save = monitor._save_cache

            def slow_get(*_args, **_kwargs):
                started.set()
                release.wait(2)
                return _Response()

            def tracked_save(symbols, ranks):
                writer_threads.append(threading.get_ident())
                original_save(symbols, ranks)

            core.requests.get = slow_get
            monitor._save_cache = tracked_save
            try:
                began = time.perf_counter()
                monitor.observe(core.CopyEvent("ENTRY", "wallet", "BTC", "LONG"))
                elapsed = time.perf_counter() - began
                self.assertLess(elapsed, 0.2)
                self.assertTrue(started.wait(1))
                self.assertIsNone(store.get_json("token_risk_coingecko_cache", None))

                release.set()
                monitor._refresh_thread.join(2)
                main_thread = threading.get_ident()
                monitor.maintain()

                cache = store.get_json("token_risk_coingecko_cache", {})
                self.assertEqual(cache["symbols"], ["BTC"])
                self.assertEqual(writer_threads, [main_thread])
            finally:
                release.set()
                if monitor._refresh_thread is not None:
                    monitor._refresh_thread.join(2)
                core.requests.get = original_get
                store.conn.close()


if __name__ == "__main__":
    unittest.main()
