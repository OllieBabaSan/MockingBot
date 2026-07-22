from __future__ import annotations

import io
import json
import os
import sys
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


class FakeContributionAdapter(FakePreflightAdapter):
    def __init__(self, equity: float, deposits: list[dict[str, object]]):
        super().__init__(equity=equity)
        self.deposits = deposits

    def deposits_since(self, _start_time_ms: int) -> list[dict[str, object]]:
        return self.deposits


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

    def test_live_launcher_ignores_generic_paper_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MOCKINGBOT_DATA_DIR": "wrong-paper-directory",
                "MAX_POSITIONS": "99",
            },
            clear=False,
        ):
            configured = core.live_command_settings()

        self.assertTrue(configured.live)
        self.assertEqual(configured.max_positions, 4)
        self.assertEqual(configured.data_dir.name, "MockingBot_Main_Live_Test_Data")

    def test_live_launcher_reads_confirmed_persisted_slot_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "live_runtime_config.json").write_text(
                '{"max_positions": 6}', encoding="utf-8"
            )
            with patch.dict(
                os.environ,
                {"MOCKINGBOT_LIVE_DATA_DIR": str(data_dir)},
                clear=False,
            ):
                configured = core.live_command_settings()
        self.assertEqual(configured.max_positions, 6)

    def test_confirmed_contribution_preserves_performance_and_enables_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configured = self.configured(Path(tmp))
            store = core.Store(configured.db_path)
            store.save_paper_account({"cash": 25.0, "realized_pnl": -20.0})
            store.set_json(
                "live_account_identity",
                {"wallet": configured.hl_wallet_address, "initial_account_value": 500.0},
            )
            store.set_json(
                "live_risk_baseline",
                {
                    "wallet": configured.hl_wallet_address,
                    "start_value": 500.0,
                    "high_water_value": 520.0,
                },
            )
            store.set_json(
                "pending_live_capital_contribution",
                {
                    "wallet": configured.hl_wallet_address,
                    "amount": 300.0,
                    "target_slots": 6,
                    "prepared_unix_ms": 123,
                    "pre_account_value": 450.0,
                },
            )
            store.conn.close()
            adapter = FakeContributionAdapter(750.0, [{"time": 456, "amount": 300.0}])
            self.assertTrue(core.confirm_live_capital_contribution(configured, adapter))
            store = core.Store(configured.db_path)
            self.assertEqual(store.get_json("paper_account", {})["cash"], 325.0)
            baseline = store.get_json("live_risk_baseline", {})
            self.assertEqual(baseline["start_value"], 800.0)
            self.assertEqual(baseline["high_water_value"], 820.0)
            self.assertEqual(
                store.get_json("live_account_identity", {})["net_capital_contributions"], 300.0
            )
            self.assertIsNone(store.get_json("pending_live_capital_contribution", None))
            flow = store.conn.execute("SELECT * FROM capital_flows").fetchone()
            self.assertEqual(flow["amount"], 300.0)
            store.conn.close()
            self.assertEqual(
                json.loads((configured.data_dir / "live_runtime_config.json").read_text())["max_positions"],
                6,
            )

    def test_contribution_is_not_applied_without_exchange_ledger_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            configured = self.configured(Path(tmp))
            store = core.Store(configured.db_path)
            store.set_json(
                "pending_live_capital_contribution",
                {
                    "wallet": configured.hl_wallet_address,
                    "amount": 300.0,
                    "target_slots": 6,
                    "prepared_unix_ms": 123,
                    "pre_account_value": 450.0,
                },
            )
            store.conn.close()
            adapter = FakeContributionAdapter(750.0, [])
            self.assertFalse(core.confirm_live_capital_contribution(configured, adapter))
            store = core.Store(configured.db_path)
            self.assertIsNotNone(store.get_json("pending_live_capital_contribution", None))
            store.conn.close()

    def test_ordinary_main_rejects_environment_live_mode(self) -> None:
        live_settings = settings(Path("unused"), live=True)
        with (
            patch.object(core, "Settings", return_value=live_settings),
            patch.object(core, "run_bot") as run_bot,
            redirect_stdout(io.StringIO()) as output,
        ):
            result = core.main([sys.executable])

        self.assertEqual(result, 2)
        run_bot.assert_not_called()
        self.assertIn("preflight cannot be bypassed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
