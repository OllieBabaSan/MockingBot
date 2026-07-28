from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import MockingBot as core

from tests.helpers import settings


class _Monitor:
    def __init__(self, events):
        self.events = events

    def scan(self, _wallets):
        return list(self.events), 1.0


class DegradedExecutionTests(unittest.TestCase):
    def test_degraded_exit_selection_preserves_allocation_order(self) -> None:
        events = [
            core.CopyEvent("ENTRY", "wallet-a", "BTC", "LONG", event_id=1),
            core.CopyEvent("ENTRY", "wallet-b", "ETH", "LONG", event_id=2),
            core.CopyEvent("EXIT", "wallet-a", "BTC", "LONG", event_id=3),
            core.CopyEvent("EXIT", "wallet-c", "SOL", "LONG", event_id=4),
        ]
        selected = core.CopyTradingBot._degraded_exit_events(events)
        self.assertEqual([event.event_id for event in selected], [4])

    def test_degraded_cycle_executes_exits_and_retains_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            configured = settings(Path(td), poll_seconds=0)
            store = core.Store(configured.db_path)
            try:
                bot = core.CopyTradingBot.__new__(core.CopyTradingBot)
                bot.settings = configured
                bot.store = store
                bot.running = True
                bot.roster = type("Roster", (), {
                    "load_or_refresh": lambda _self, force=False: ["wallet"],
                    "refresh_in_progress": lambda _self: False,
                })()
                bot.risk = type("Risk", (), {
                    "session_start_value": lambda *_args: 100.0,
                    "record_paper_high_water": lambda _self, value: value,
                    "current_value": lambda *_args: 100.0,
                    "live_equity_available": lambda *_args: None,
                    "drawdown": lambda *_args: 0.0,
                    "check_warning": lambda *_args: None,
                    "check_circuit_breaker": lambda *_args: False,
                    "is_wind_down": lambda *_args: False,
                })()
                bot.paper = type("Paper", (), {
                    "value": lambda *_args: 100.0,
                    "position": lambda *_args: None,
                })()
                bot.platform = type("Platform", (), {"mid_price": lambda *_args: 1.0})()
                bot.token_risk = type("TokenRisk", (), {"maintain": lambda *_args: None})()
                bot.monitor = _Monitor([
                    core.CopyEvent("ENTRY", "wallet", "ETH", "LONG", event_id=1),
                    core.CopyEvent("EXIT", "wallet", "BTC", "LONG", event_id=2),
                ])
                handled = []
                bot._maintain_live_backup = lambda *_args, **_kwargs: None
                bot._effective_wallets = lambda wallets: wallets
                bot._handle_entry = lambda event, *_args: handled.append(event.kind)
                bot._handle_exit = lambda event: handled.append(event.kind)
                bot._sleep_remaining = lambda *_args: setattr(bot, "running", False)
                bot.reconciler = type("Reconciler", (), {"run": lambda *_args: None})()

                bot._run_loop()

                self.assertEqual(handled, ["EXIT"])
            finally:
                store.conn.close()


if __name__ == "__main__":
    unittest.main()
