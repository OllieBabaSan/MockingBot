"""
MockingBot Elite
================

Simple paper baseline:
  - follows the top 10 qualifying Hyperliquid leaderboard wallets
  - uses the existing human-trader / non-HFT filter
  - follows entries and exits directly
  - refreshes the leaderboard roster and exits wallets that fall out
  - keeps all data isolated from the main Scoring Engine bot

Usage:
  python .\\MockingBot_Elite.py
  python .\\MockingBot_Elite.py status
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import signal
import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CORE_PATH = ROOT / "MockingBot.py"
ELITE_CREDENTIALS_PATH = Path(r"C:\Users\user\Documents\Hyperliquid Credentials MockingBot_Elite.txt")


def load_core() -> Any:
    loader = importlib.machinery.SourceFileLoader("mockingbot_core_for_elite", str(CORE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"Unable to load core from {CORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


core = load_core()

ELITE_RECENT_FILL_DAYS = 7


def load_elite_credentials() -> dict[str, str]:
    credentials = {"name": "", "wallet": "", "api_wallet": "", "api_key": ""}
    if not ELITE_CREDENTIALS_PATH.exists():
        return credentials

    for raw_line in ELITE_CREDENTIALS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("name:"):
            credentials["name"] = line.split(":", 1)[1].strip()
        elif lower.startswith("wallet:"):
            credentials["wallet"] = line.split(":", 1)[1].strip()
        elif lower.startswith(("api wallet:", "api wallet address:")):
            credentials["api_wallet"] = line.split(":", 1)[1].strip()
        elif lower.startswith("api key:"):
            credentials["api_key"] = line.split(":", 1)[1].strip()
        elif "=" in line:
            key, value = line.split("=", 1)
            normalized = key.strip().lower()
            if normalized in {"hl_wallet_address", "wallet", "address"}:
                credentials["wallet"] = value.strip()
            elif normalized in {"hl_api_wallet_address", "api_wallet", "api_wallet_address"}:
                credentials["api_wallet"] = value.strip()
            elif normalized in {"hl_api_key", "api_key", "private_key"}:
                credentials["api_key"] = value.strip()
        elif not credentials["api_key"]:
            credentials["api_key"] = line
    return credentials


def elite_settings() -> Any:
    base = core.Settings()
    credentials = load_elite_credentials()
    return replace(
        base,
        data_dir=ROOT / "MockingBot_Elite_Data",
        roster_size=100,
        max_follow=10,
        roster_refresh_seconds=24 * 3600,
        paper_starting_cash=10_000.0,
        live=False,
        wind_down=False,
        max_positions=50,
        max_slices_per_coin=5,
        max_coin_cost_multiplier=5.0,
        max_position_days=3650,
        hl_wallet_address=credentials["wallet"] or base.hl_wallet_address,
        hl_api_key=credentials["api_key"] or base.hl_api_key,
    )


class EliteHyperliquidAdapter(core.HyperliquidAdapter):
    def wallet_metrics(self, wallet: str, lookback_days: int) -> Any:
        start_ms = int((core.unix_now() - lookback_days * 86400) * 1000)
        fills = self._post_info(
            {"type": "userFillsByTime", "user": wallet, "startTime": start_ms},
            "elite_wallet_metrics",
            wallet,
        )
        if fills is None:
            return None
        try:
            recent_cutoff_ms = (core.unix_now() - ELITE_RECENT_FILL_DAYS * 86400) * 1000
            has_recent_fill = any(float(fill.get("time", 0)) >= recent_cutoff_ms for fill in fills)
            if not has_recent_fill:
                return core.WalletMetrics(hft=False, qualifies=False)

            day_cutoff_ms = (core.unix_now() - 86400) * 1000
            fills_24h = sum(1 for fill in fills if float(fill.get("time", 0)) >= day_cutoff_ms)
            if fills_24h > self.settings.hft_fill_limit_24h:
                return core.WalletMetrics(hft=True, qualifies=False)

            pnls = [
                float(fill["closedPnl"])
                for fill in fills
                if fill.get("closedPnl") is not None and float(fill.get("closedPnl") or 0) != 0
            ]
            if len(pnls) < self.settings.min_sample:
                return core.WalletMetrics(hft=False, qualifies=False, sample=len(pnls))

            wins = [pnl for pnl in pnls if pnl > 0]
            losses = [pnl for pnl in pnls if pnl < 0]
            gross_win = sum(wins)
            gross_loss = abs(sum(losses))
            win_rate = len(wins) / len(pnls)
            profit_factor = gross_win / gross_loss if gross_loss > 0 else 999.0
            qualifies = (
                win_rate >= self.settings.min_win_rate
                and profit_factor >= self.settings.min_profit_factor
            )
            return core.WalletMetrics(False, qualifies, len(pnls), win_rate, profit_factor)
        except Exception as exc:
            self.store.log_api_failure(self.name, "elite_parse_wallet_metrics", wallet, str(exc))
            return None


class EliteRosterService(core.RosterService):
    def _config_signature(self) -> dict[str, Any]:
        signature = super()._config_signature()
        signature["elite_recent_fill_days"] = ELITE_RECENT_FILL_DAYS
        return signature

    def refresh(self, pause_hours: int | None = None) -> list[str]:
        follow_limit = self.settings.max_follow if self.settings.max_follow > 0 else self.settings.roster_size
        print(f"[ROSTER] Refreshing up to {follow_limit} active qualifying wallets")
        candidates = self.platform.candidate_wallets(self.settings.roster_size)
        if not candidates:
            print("[ROSTER] No candidates returned; keeping cached roster")
            return self.store.roster()

        qualified: list[str] = []
        failures = 0
        evaluated = 0

        for wallet in candidates:
            if len(qualified) >= follow_limit:
                break
            time.sleep(self.settings.wallet_poll_delay)
            metrics = self.platform.wallet_metrics(wallet, self.settings.fills_lookback_days)
            evaluated += 1
            if metrics is None:
                failures += 1
                time.sleep(self.settings.roster_failure_cooldown)
                continue
            if metrics.hft or not metrics.qualifies:
                continue
            qualified.append(wallet)

        if evaluated > 0 and failures / evaluated > self.settings.api_degraded_max_fail_ratio:
            print(f"[ROSTER] API degraded ({failures}/{evaluated} failed); keeping cached roster")
            return self.store.roster()

        if qualified:
            self.store.replace_roster(qualified)
            self.store.set_json("last_roster_refresh", core.unix_now())
            self.store.set_json("last_roster_config", self._config_signature())
            print(f"[ROSTER] Following {len(qualified)} active wallet(s)")
            return qualified

        print("[ROSTER] No active qualifying wallets; keeping cached roster")
        return self.store.roster()


class EliteBot:
    def __init__(self, settings: Any):
        self.settings = settings
        self.store = core.Store(settings.db_path)
        self.notifier = core.Notifier("")
        self.platform = EliteHyperliquidAdapter(settings, self.store)
        self.paper = core.PaperPortfolio(settings, self.store)
        self.risk = core.RiskManager(settings, self.store, self.notifier)
        self.roster = EliteRosterService(settings, self.store, self.platform)
        self.monitor = core.WalletMonitor(settings, self.store, self.platform)
        self.reconciler = core.Reconciler(settings, self.store, self.platform, self.paper, self.risk)
        self.running = True

    def stop(self, *_args: Any) -> None:
        self.running = False
        print("\n[ELITE] Stop requested; finishing current cycle.")

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        print("[ELITE] MockingBot Elite starting")
        print(f"[ELITE] live={self.settings.live} db={self.settings.db_path}")

        restart_count = 0
        while self.running:
            try:
                self._run_loop()
                break
            except Exception as exc:
                restart_count += 1
                wait = min(60 * restart_count, 300)
                print(f"[ELITE-CRASH] {exc}; restarting in {wait}s")
                traceback.print_exc()
                for _ in range(wait):
                    if not self.running:
                        break
                    time.sleep(1)
        print("[ELITE] Stopped")

    def _run_loop(self) -> None:
        wallets = self.roster.load_or_refresh(force=False)
        last_roster_check = core.unix_now()
        last_reconcile = 0.0

        while self.running:
            cycle_start = core.unix_now()
            paper_value = self.paper.value(self.platform.mid_price)

            if core.unix_now() - last_roster_check >= self.settings.roster_refresh_seconds:
                previous = set(wallets)
                wallets = self.roster.load_or_refresh(force=True)
                removed = sorted(previous - set(wallets))
                self._exit_removed_wallets(removed)
                last_roster_check = core.unix_now()

            scan_wallets = self._effective_wallets(wallets)
            if core.unix_now() - last_reconcile >= self.settings.reconcile_seconds:
                self.reconciler.run(scan_wallets, 999.0)
                last_reconcile = core.unix_now()

            events, fail_ratio = self.monitor.scan(scan_wallets)
            if fail_ratio > self.settings.api_degraded_max_fail_ratio:
                print(f"[ELITE-API] Degraded scan ({fail_ratio:.0%} failed); skipping signal execution")
                self._sleep_remaining(cycle_start)
                continue

            for event in events:
                if event.kind == "ENTRY":
                    self._handle_entry(event)
                elif event.kind == "EXIT":
                    self._handle_exit(event, "source exit")

            print(
                f"[{time.strftime('%H:%M:%S')}] elite_wallets={len(wallets)} "
                f"scan={len(scan_wallets)} events={len(events)} paper=${paper_value:,.2f}"
            )
            self._sleep_remaining(cycle_start)

    def _effective_wallets(self, roster_wallets: list[str]) -> list[str]:
        wallets = list(dict.fromkeys(roster_wallets))
        seen = set(wallets)
        for position in self.store.open_position_slices():
            wallet = str(position["source_wallet"])
            if wallet and wallet not in seen:
                wallets.append(wallet)
                seen.add(wallet)
        return wallets

    def _exit_removed_wallets(self, wallets: list[str]) -> None:
        if not wallets:
            return
        for wallet in wallets:
            for pos in list(self.store.open_position_slices()):
                if pos["source_wallet"] != wallet:
                    continue
                event = core.CopyEvent("EXIT", wallet, str(pos["coin"]), str(pos["side"]))
                self._handle_exit(event, "leaderboard removal")

    def _handle_entry(self, event: Any) -> None:
        if self.paper.owns_position(event.wallet, event.coin, event.side):
            self.store.log_signal(event.wallet, event.coin, event.side, "ENTRY", None, "SKIPPED", "wallet allocation already held")
            print(f"[ELITE-SKIP] ENTRY {event.coin} {event.side}: wallet allocation already held")
            return
        if self.paper.has_opposite_position(event.coin, event.side):
            self.store.log_signal(event.wallet, event.coin, event.side, "ENTRY", None, "SKIPPED", "opposite side already held")
            print(f"[ELITE-SKIP] ENTRY {event.coin} {event.side}: opposite side already held")
            return

        price = self.platform.mid_price(event.coin) or event.entry_price
        if not price:
            self.store.log_signal(event.wallet, event.coin, event.side, "ENTRY", None, "SKIPPED", "no price")
            print(f"[ELITE-SKIP] ENTRY {event.coin}: no price")
            return

        cost = self.paper.available_slot(event.coin, price, 1.0, event.side)
        if cost is None:
            self.store.log_signal(event.wallet, event.coin, event.side, "ENTRY", price, "SKIPPED", "paper rejected")
            print(f"[ELITE-SKIP] ENTRY {event.coin} {event.side}: paper rejected")
            return

        notional = cost * self.settings.leverage
        if not self.platform.open_position(
            event.coin, event.side, notional, price, self.settings.leverage
        ):
            self.store.log_signal(event.wallet, event.coin, event.side, "ENTRY", price, "SKIPPED", "open failed")
            print(f"[ELITE-SKIP] ENTRY {event.coin} {event.side}: open failed")
            return

        opened_cost = self.paper.open(event.wallet, event.coin, event.side, price, cost)
        if opened_cost is None:
            self.store.log_signal(event.wallet, event.coin, event.side, "ENTRY", price, "SKIPPED", "paper commit failed")
            print(f"[ELITE-WARN] ENTRY {event.coin} {event.side}: paper commit failed")
            return

        self.store.log_signal(event.wallet, event.coin, event.side, "ENTRY", price, "EXECUTED", "Elite baseline")
        label = "REINFORCE" if self.paper.slice_count(event.coin, event.side) > 1 else "ENTRY"
        print(f"[ELITE-{label}] {event.coin} {event.side} @ {price:,.4f} source={event.wallet[:16]} slot=${opened_cost:.2f}")

    def _handle_exit(self, event: Any, reason: str) -> None:
        if not self.paper.position(event.coin):
            self.store.log_signal(event.wallet, event.coin, event.side, "EXIT", None, "SKIPPED", "not tracked")
            print(f"[ELITE-EXIT] {event.coin} {event.side}: not tracked")
            return
        if not self.paper.owns_position(event.wallet, event.coin, event.side):
            self.store.log_signal(event.wallet, event.coin, event.side, "EXIT", None, "SKIPPED", "ownership mismatch")
            print(f"[ELITE-EXIT] {event.coin} {event.side}: ownership mismatch")
            return

        price = self.platform.mid_price(event.coin)
        if price is None:
            self.store.log_signal(event.wallet, event.coin, event.side, "EXIT", None, "SKIPPED", "no price")
            print(f"[ELITE-EXIT] {event.coin} {event.side}: no price")
            return

        if not self.platform.close_position(event.coin):
            self.store.log_signal(event.wallet, event.coin, event.side, "EXIT", price, "SKIPPED", "close failed")
            print(f"[ELITE-EXIT] {event.coin} {event.side}: close failed")
            return

        gain, pnl_pct, side = self.paper.close(event.wallet, event.coin, event.side, price)
        if gain is None:
            self.store.log_signal(event.wallet, event.coin, event.side, "EXIT", price, "SKIPPED", "not tracked")
            print(f"[ELITE-EXIT] {event.coin} {event.side}: not tracked")
            return
        self.store.log_signal(event.wallet, event.coin, side, "EXIT", price, "EXECUTED", reason, gain, pnl_pct)
        pnl = "n/a" if pnl_pct is None else f"{pnl_pct:+.2f}%"
        print(f"[ELITE-EXIT] {event.coin} {side} @ {price:,.4f} pnl={pnl} paper=${gain:+.2f} reason={reason}")

    def _sleep_remaining(self, cycle_start: float) -> None:
        elapsed = core.unix_now() - cycle_start
        sleep_for = max(0.0, self.settings.poll_seconds - elapsed)
        end = core.unix_now() + sleep_for
        while self.running and core.unix_now() < end:
            time.sleep(min(1.0, end - core.unix_now()))


def print_status(settings: Any) -> None:
    store = core.Store(settings.db_path)
    paper = core.PaperPortfolio(settings, store)
    print("\n=== MOCKINGBOT ELITE STATUS ===\n")
    print(f"Database: {settings.db_path}")
    print(f"Roster:   {len(store.roster())} wallet(s)")
    acct = paper.account()
    print(f"Cash:     ${float(acct['cash']):,.2f}")
    print(f"Realized: ${float(acct.get('realized_pnl', 0)):,.2f}")
    print(f"Open:     {len(paper.positions())} position(s)")
    row = store.conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()
    print(f"Signals:  {row['n'] if row else 0}")
    print(f"Wallet:   {'configured' if settings.hl_wallet_address else 'missing'}")
    print(f"API Key:  {'configured' if settings.hl_api_key else 'missing'}")
    print()
    store.conn.close()


def main(argv: list[str]) -> int:
    settings = elite_settings()
    if len(argv) > 1 and argv[1] == "status":
        print_status(settings)
        return 0

    log_handle, original_stdout, original_stderr = core.enable_monitor_log(settings)
    bot = EliteBot(settings)
    try:
        bot.run_forever()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        log_handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
