from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import MockingBot as core
from helpers import TEST_USER, settings


class FakePreflightAdapter:
    def __init__(self, equity: float = 500.0, positions: dict[str, core.Position] | None = None):
        self.equity = equity
        self.positions = positions or {}
        self._sz_decimals = {"BTC": 3, "ETH": 3}

    def validate_live_credentials(self) -> None:
        return None

    def _init_sdk(self) -> None:
        return None

    def capital_snapshot(self) -> core.CapitalSnapshot:
        return core.CapitalSnapshot(self.equity, 0.0, self.equity, self.equity)

    def live_positions(self) -> dict[str, core.Position]:
        return self.positions


class LivePreflightTests(unittest.TestCase):
    def configured(self, root: Path, **changes: object) -> core.Settings:
        seed = root / "paper" / "mockingbot_codex.sqlite3"
        seed_store = core.Store(seed)
        seed_store.conn.close()
        return settings(root / "live", live=True, scoring_seed_db_path=seed, max_positions=4, **changes)

    def run_preflight(self, configured: core.Settings, adapter: FakePreflightAdapter) -> tuple[bool, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            result = core.run_live_preflight(configured, adapter)
        return result, output.getvalue()

    def test_flat_first_run_passes_without_submitting_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configured = self.configured(Path(tmp))
            result, output = self.run_preflight(configured, FakePreflightAdapter())
            self.assertTrue(result)
            self.assertIn("[PASS] 4-slot sizing (configured)", output)
            self.assertIn("[INFO] 5-slot sizing (expansion)", output)
            self.assertIn("PASS - no orders submitted", output)
            self.assertNotIn(configured.hl_api_key, output)

    def test_future_six_slot_shortfall_does_not_block_four_slot_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configured = self.configured(Path(tmp))
            result, output = self.run_preflight(configured, FakePreflightAdapter(equity=50.0))
            self.assertTrue(result)
            self.assertIn("[PASS] 4-slot sizing (configured): viable", output)
            self.assertIn("[INFO] 6-slot sizing (expansion): not viable", output)

    def test_nonflat_first_run_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configured = self.configured(Path(tmp))
            positions = {"BTC": core.Position("BTC", "LONG", 0.3, 100.0, 3.0)}
            result, output = self.run_preflight(configured, FakePreflightAdapter(positions=positions))
            self.assertFalse(result)
            self.assertIn("[FAIL] position state", output)

    def test_existing_state_requires_full_position_synchronization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configured = self.configured(Path(tmp))
            store = core.Store(configured.db_path)
            store.set_json("live_account_identity", {"wallet": TEST_USER})
            store.upsert_paper_position("BTC", "LONG", 100.0, 10.0, "source", 3.0)
            store.conn.close()

            matching = {"BTC": core.Position("BTC", "LONG", 0.3, 100.0, 3.0)}
            result, _ = self.run_preflight(configured, FakePreflightAdapter(positions=matching))
            self.assertTrue(result)

            mismatched = {"BTC": core.Position("BTC", "LONG", 0.31, 100.0, 3.0)}
            result, output = self.run_preflight(configured, FakePreflightAdapter(positions=mismatched))
            self.assertFalse(result)
            self.assertIn("[FAIL] position state", output)

    def test_start_live_never_constructs_bot_when_preflight_fails(self) -> None:
        class Lock:
            def __init__(self, configured):
                self.configured = configured

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with (
            patch.object(core, "InstanceLock", Lock),
            patch.object(core, "run_live_preflight", return_value=False) as preflight,
            patch.object(core, "CopyTradingBot") as bot,
            redirect_stdout(io.StringIO()),
        ):
            result = core.start_live()

        self.assertEqual(result, 2)
        bot.assert_not_called()
        configured = preflight.call_args.args[0]
        self.assertTrue(configured.live)
        self.assertEqual(configured.max_positions, 4)
        self.assertEqual(configured.instance_id, "live-main")
        self.assertEqual(configured.data_dir.name, "MockingBot_Main_Live_Test_Data")
        self.assertFalse(preflight.call_args.kwargs["check_instance_lock"])


if __name__ == "__main__":
    unittest.main()
