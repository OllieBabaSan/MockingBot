"""
MockingBot
==========

A clean, platform-aware copy-trading bot core.

Design goals:
  - Platform agnostic engine: the bot talks to a PlatformAdapter interface.
  - Hyperliquid implementation: the current concrete adapter uses Hyperliquid APIs.
  - Unattended safeguards: paper/live drawdown breakers, wind-down mode, wallet pause,
    position age limits, per-coin/per-wallet exposure limits, and API degradation checks.
  - Dropped API call recovery: bounded retries, stale snapshot handling, and periodic
    reconciliation against source wallets and local portfolio state.
  - Auditability: every signal, skip, fill decision, and risk transition is written to
    SQLite so behavior can be reviewed later.

Usage:
  python .\\MockingBot.py

Required packages:
  requests
  python-dotenv

Optional for live Hyperliquid execution:
  hyperliquid-python-sdk
  eth-account
"""

from __future__ import annotations

import csv
import json
import math
import os
import signal
import sqlite3
import sys
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import requests
except ImportError as exc:
    raise SystemExit("Missing dependency: requests. Install requirements first.") from exc

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


SCRIPT_DIR = Path(__file__).resolve().parent


def resolve_root() -> Path:
    """Prefer the project/work directory when the script is launched from elsewhere."""
    explicit = os.getenv("MOCKINGBOT_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()

    candidates = [Path.cwd(), SCRIPT_DIR]
    project_markers = (".env.txt", ".env.execution.txt")
    fallback_markers = ("followlist.txt", "wallets.txt")
    seen: set[Path] = set()
    for marker_set in (project_markers, fallback_markers):
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if any((candidate / marker).exists() for marker in marker_set):
                return candidate
        seen.clear()

    for candidate in (Path.cwd(), SCRIPT_DIR):
        if candidate in seen:
            continue
        seen.add(candidate)
        if any((candidate / marker).exists() for marker in project_markers + fallback_markers):
            return candidate
    return SCRIPT_DIR


ROOT = resolve_root()
load_dotenv(ROOT / ".env.txt")
load_dotenv(ROOT / ".env.execution.txt")


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    platform: str = env_str("MOCKINGBOT_PLATFORM", "hyperliquid").lower()
    data_dir: Path = Path(env_str("MOCKINGBOT_DATA_DIR", str(ROOT / "MockingBot_Data")))

    poll_seconds: int = env_int("POLL_INTERVAL_SECS", 30)
    wallet_poll_delay: float = env_float("WALLET_POLL_DELAY", 0.50)
    roster_failure_cooldown: float = env_float("ROSTER_FAILURE_COOLDOWN_SECS", 2.0)
    roster_refresh_seconds: int = env_int("ROSTER_REFRESH_SECS", 12 * 3600)
    roster_refresh_batch_seconds: int = env_int("ROSTER_REFRESH_BATCH_SECS", 10 * 60)
    roster_refresh_batch_size: int = env_int("ROSTER_REFRESH_BATCH_SIZE", 25)
    reconcile_seconds: int = env_int("RECONCILE_INTERVAL_SECS", 6 * 3600)
    api_degraded_max_fail_ratio: float = env_float("API_DEGRADED_MAX_FAIL_RATIO", 0.50)

    hyperliquid_info_url: str = env_str("HL_INFO_URL", "https://api.hyperliquid.xyz/info")
    hyperliquid_stats_url: str = env_str("HL_STATS_URL", "https://stats-data.hyperliquid.xyz/Mainnet")
    hypertracker_base: str = env_str("HYPERTRACKER_BASE", "https://ht-api.coinmarketman.com")
    hypertracker_api_key: str = env_str("HYPERTRACKER_API_KEY", "")
    coingecko_markets_url: str = env_str("COINGECKO_MARKETS_URL", "https://api.coingecko.com/api/v3/coins/markets")
    token_risk_logging: bool = env_bool("TOKEN_RISK_LOGGING", True)
    token_risk_top_n: int = env_int("TOKEN_RISK_TOP_N", 2500)
    token_risk_refresh_seconds: int = env_int("TOKEN_RISK_REFRESH_SECS", 24 * 3600)
    token_risk_retry_seconds: int = env_int("TOKEN_RISK_RETRY_SECS", 6 * 3600)

    roster_size: int = env_int("ROSTER_SIZE", 150)
    max_follow: int = env_int("MAX_FOLLOW", 0)
    fills_lookback_days: int = env_int("FILLS_LOOKBACK_DAYS", 45)
    hft_fill_limit_24h: int = env_int("HFT_FILL_LIMIT", 50)
    min_sample: int = env_int("MIN_SAMPLE", 20)
    min_win_rate: float = env_float("MIN_WIN_RATE", 0.55)
    min_profit_factor: float = env_float("MIN_PF", 1.50)
    min_pause_hours: int = env_int("MIN_PAUSE_HOURS", 48)
    scoring_engine_active: bool = env_bool("SCORING_ENGINE_ACTIVE", env_bool("MARSHAL_ACTIVE", True))

    paper_starting_cash: float = env_float("PAPER_STARTING_CASH", 10_000.0)
    leverage: int = env_int("HL_LEVERAGE", 3)
    max_positions: int = env_int("MAX_POSITIONS", 10)
    max_slices_per_coin: int = env_int("MAX_SLICES_PER_COIN", 5)
    max_coin_cost_multiplier: float = env_float("MAX_COIN_COST_MULT", 2.0)
    max_allocations_per_wallet_coin_side: int = env_int("MAX_ALLOCATIONS_PER_WALLET_COIN_SIDE", 2)
    same_wallet_add_threshold_pct: float = env_float("SAME_WALLET_ADD_THRESHOLD_PCT", 25.0)
    min_slot_usd: float = env_float("MIN_SLOT_USD", 5.0)
    min_order_notional: float = env_float("MIN_ORDER_NOTIONAL", 11.0)
    slippage: float = env_float("SLIPPAGE", 0.01)
    scoring_engine_default_candidate_multiplier: float = env_float(
        "SCORING_ENGINE_DEFAULT_CANDIDATE_MULT",
        env_float("MARSHAL_DEFAULT_CANDIDATE_MULT", 0.30),
    )
    scoring_engine_candidate_multiplier: float = env_float(
        "SCORING_ENGINE_CANDIDATE_MULT",
        env_float("MARSHAL_CANDIDATE_MULT", 0.70),
    )
    scoring_engine_proven_candidate_multiplier: float = env_float(
        "SCORING_ENGINE_PROVEN_CANDIDATE_MULT",
        env_float("MARSHAL_PROVEN_CANDIDATE_MULT", 0.90),
    )
    scoring_engine_core_multiplier: float = env_float(
        "SCORING_ENGINE_CORE_MULT",
        env_float("MARSHAL_CORE_MULT", 1.15),
    )
    scoring_engine_elite_multiplier: float = env_float(
        "SCORING_ENGINE_ELITE_MULT",
        env_float("MARSHAL_ELITE_MULT", 1.35),
    )
    scoring_engine_max_slot_multiplier: float = env_float(
        "SCORING_ENGINE_MAX_SLOT_MULT",
        env_float("MARSHAL_MAX_SLOT_MULT", 1.50),
    )
    scoring_engine_candidate_max_allocations: int = env_int(
        "SCORING_ENGINE_CANDIDATE_MAX_ALLOCATIONS",
        env_int("MARSHAL_CANDIDATE_MAX_ALLOCATIONS", 2),
    )
    scoring_engine_proven_candidate_max_allocations: int = env_int(
        "SCORING_ENGINE_PROVEN_CANDIDATE_MAX_ALLOCATIONS",
        env_int("MARSHAL_PROVEN_CANDIDATE_MAX_ALLOCATIONS", 3),
    )

    live: bool = env_bool("HL_LIVE", False)
    wind_down: bool = env_bool("WIND_DOWN", False)
    max_drawdown_pct: float = env_float("MAX_DRAWDOWN_PCT", 0.30)
    warning_drawdown_pct: float = env_float("WARNING_DRAWDOWN_PCT", 0.10)
    min_loss_pct_to_pause: float = env_float("MIN_LOSS_PCT_TO_PAUSE", 1.0)
    pause_recent_exits: int = env_int("PAUSE_RECENT_EXITS", 3)
    pause_loss_count: int = env_int("PAUSE_LOSS_COUNT", 2)
    pause_cumulative_loss_pct: float = env_float("PAUSE_CUMULATIVE_LOSS_PCT", 2.0)
    pause_emergency_loss_pct: float = env_float("PAUSE_EMERGENCY_LOSS_PCT", 5.0)
    max_position_days: int = env_int("MAX_POSITION_DAYS", 7)

    hl_api_key: str = env_str("HL_API_KEY", "")
    hl_wallet_address: str = env_str("HL_WALLET_ADDRESS", "")
    hl_account_fallback: float = env_float("HL_ACCOUNT_USD", 0.0)

    notify_webhook_url: str = env_str("NOTIFY_WEBHOOK_URL", "")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mockingbot_codex.sqlite3"

    @property
    def circuit_breaker_file(self) -> Path:
        return self.data_dir / "circuit_breaker.json"

    @property
    def monitor_log_path(self) -> Path:
        return Path(env_str("MOCKINGBOT_MONITOR_LOG", str(self.data_dir / "mockingbot_live.log")))


class TeeStream:
    """Mirror console output to a monitor log without changing normal PowerShell output."""

    def __init__(self, console: Any, log_handle: Any) -> None:
        self.console = console
        self.log_handle = log_handle
        self.encoding = getattr(console, "encoding", "utf-8")

    def write(self, text: str) -> int:
        self.console.write(text)
        self.log_handle.write(text)
        self.log_handle.flush()
        return len(text)

    def flush(self) -> None:
        self.console.flush()
        self.log_handle.flush()

    def isatty(self) -> bool:
        return bool(getattr(self.console, "isatty", lambda: False)())


def enable_monitor_log(settings: Settings) -> tuple[Any, Any, Any]:
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    settings.monitor_log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = settings.monitor_log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = TeeStream(original_stdout, handle)
    sys.stderr = TeeStream(original_stderr, handle)
    print(f"[BOOT] Monitor log: {settings.monitor_log_path}")
    return handle, original_stdout, original_stderr


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def unix_now() -> float:
    return time.time()


@dataclass(frozen=True)
class Position:
    coin: str
    side: str
    size: float
    entry_price: float


@dataclass(frozen=True)
class CopyEvent:
    kind: str
    wallet: str
    coin: str
    side: str
    entry_price: float | None = None
    previous_size: float | None = None
    current_size: float | None = None


@dataclass(frozen=True)
class WalletMetrics:
    hft: bool
    qualifies: bool
    sample: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0


@dataclass(frozen=True)
class TradeDecision:
    action: str
    reason: str = ""


# ---------------------------------------------------------------------------
# Durable state and audit log
# ---------------------------------------------------------------------------


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS roster (
                wallet TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                score REAL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS roster_wallet_metrics (
                wallet TEXT PRIMARY KEY,
                sample INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                profit_factor REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallet_positions (
                wallet TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                size REAL NOT NULL,
                entry_price REAL NOT NULL,
                seen_at TEXT NOT NULL,
                PRIMARY KEY (wallet, coin)
            );

            CREATE TABLE IF NOT EXISTS paper_positions (
                coin TEXT PRIMARY KEY,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                cost_basis REAL NOT NULL,
                source_wallet TEXT NOT NULL,
                opened_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_position_slices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                source_wallet TEXT NOT NULL,
                entry_price REAL NOT NULL,
                cost_basis REAL NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                closed_at TEXT,
                exit_price REAL,
                paper_gain REAL,
                pnl_pct REAL
            );

            CREATE TABLE IF NOT EXISTS paused_wallets (
                wallet TEXT PRIMARY KEY,
                paused_at TEXT NOT NULL,
                coin TEXT,
                pnl_pct REAL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                wallet TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                signal TEXT NOT NULL,
                price REAL,
                action TEXT NOT NULL,
                reason TEXT,
                paper_gain REAL,
                pnl_pct REAL
            );

            CREATE TABLE IF NOT EXISTS api_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                platform TEXT NOT NULL,
                operation TEXT NOT NULL,
                subject TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS token_risk_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                coin TEXT NOT NULL,
                wallet TEXT NOT NULL,
                side TEXT NOT NULL,
                signal TEXT NOT NULL,
                reason TEXT NOT NULL,
                market_cap_rank INTEGER,
                source TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS marshal_wallet_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                wallet TEXT NOT NULL,
                tier TEXT NOT NULL,
                total_score REAL NOT NULL,
                realized_component REAL NOT NULL,
                win_rate_component REAL NOT NULL,
                recent_form_component REAL NOT NULL,
                churn_penalty REAL NOT NULL,
                loss_penalty REAL NOT NULL,
                sample_size INTEGER NOT NULL,
                realized_pnl REAL NOT NULL,
                win_rate REAL,
                avg_pnl_pct REAL,
                explanation TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS marshal_signal_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                signal_id INTEGER,
                wallet TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                signal TEXT NOT NULL,
                actual_action TEXT NOT NULL,
                actual_reason TEXT,
                marshal_tier TEXT NOT NULL,
                scoring_score REAL NOT NULL,
                recommendation TEXT NOT NULL,
                would_execute INTEGER NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS marshal_shadow_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                opened_at TEXT NOT NULL,
                source_signal_id INTEGER,
                scoring_score REAL NOT NULL,
                marshal_tier TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_price REAL,
                closed_at TEXT,
                paper_gain REAL,
                pnl_pct REAL,
                close_signal_id INTEGER,
                close_reason TEXT
            );
            """
        )
        self.conn.commit()
        self._migrate_legacy_paper_positions()

    def _migrate_legacy_paper_positions(self) -> None:
        slice_count = self.conn.execute("SELECT COUNT(*) AS n FROM paper_position_slices").fetchone()["n"]
        if slice_count:
            return
        rows = self.conn.execute("SELECT * FROM paper_positions").fetchall()
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO paper_position_slices(
                    coin, side, source_wallet, entry_price, cost_basis, opened_at, status
                )
                VALUES(?, ?, ?, ?, ?, ?, 'OPEN')
                """,
                [
                    (
                        row["coin"],
                        row["side"],
                        row["source_wallet"],
                        float(row["entry_price"]),
                        float(row["cost_basis"]),
                        row["opened_at"],
                    )
                    for row in rows
                ],
            )

    def get_json(self, key: str, default: Any) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def roster(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT wallet FROM roster WHERE status = 'follow' ORDER BY updated_at DESC"
        ).fetchall()
        return [r["wallet"] for r in rows]

    def replace_roster(self, wallets: list[str], metrics_by_wallet: dict[str, WalletMetrics] | None = None) -> None:
        metrics_by_wallet = metrics_by_wallet or {}
        with self.conn:
            self.conn.execute("DELETE FROM roster")
            self.conn.executemany(
                "INSERT INTO roster(wallet, status, updated_at) VALUES(?, 'follow', ?)",
                [(w, utc_now()) for w in wallets],
            )
            self.conn.executemany(
                """
                INSERT INTO roster_wallet_metrics(wallet, sample, win_rate, profit_factor, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    sample = excluded.sample,
                    win_rate = excluded.win_rate,
                    profit_factor = excluded.profit_factor,
                    updated_at = excluded.updated_at
                """,
                [
                    (w, int(m.sample), float(m.win_rate), float(m.profit_factor), utc_now())
                    for w, m in metrics_by_wallet.items()
                    if w in wallets
                ],
            )

    def roster_wallet_metrics(self, wallet: str) -> WalletMetrics | None:
        row = self.conn.execute(
            """
            SELECT sample, win_rate, profit_factor
            FROM roster_wallet_metrics
            WHERE wallet = ?
            """,
            (wallet,),
        ).fetchone()
        if row is None:
            return None
        return WalletMetrics(
            hft=False,
            qualifies=True,
            sample=int(row["sample"]),
            win_rate=float(row["win_rate"]),
            profit_factor=float(row["profit_factor"]),
        )

    def upsert_roster_wallet_metrics(self, wallet: str, metrics: WalletMetrics) -> None:
        self.conn.execute(
            """
            INSERT INTO roster_wallet_metrics(wallet, sample, win_rate, profit_factor, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                sample = excluded.sample,
                win_rate = excluded.win_rate,
                profit_factor = excluded.profit_factor,
                updated_at = excluded.updated_at
            """,
            (
                wallet,
                int(metrics.sample),
                float(metrics.win_rate),
                float(metrics.profit_factor),
                utc_now(),
            ),
        )
        self.conn.commit()

    def wallet_snapshot(self, wallet: str) -> dict[str, Position] | None:
        rows = self.conn.execute(
            "SELECT coin, side, size, entry_price FROM wallet_positions WHERE wallet = ?",
            (wallet,),
        ).fetchall()
        if not rows:
            seeded = self.get_json("seeded_wallets", [])
            return {} if wallet in seeded else None
        return {
            r["coin"]: Position(r["coin"], r["side"], float(r["size"]), float(r["entry_price"]))
            for r in rows
        }

    def save_wallet_snapshot(self, wallet: str, positions: dict[str, Position]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM wallet_positions WHERE wallet = ?", (wallet,))
            self.conn.executemany(
                """
                INSERT INTO wallet_positions(wallet, coin, side, size, entry_price, seen_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                [
                    (wallet, p.coin, p.side, p.size, p.entry_price, utc_now())
                    for p in positions.values()
                ],
            )
        seeded = set(self.get_json("seeded_wallets", []))
        seeded.add(wallet)
        self.set_json("seeded_wallets", sorted(seeded))

    def paper_account(self, starting_cash: float) -> dict[str, Any]:
        acct = self.get_json("paper_account", None)
        if acct is None:
            acct = {"cash": starting_cash, "realized_pnl": 0.0}
            self.set_json("paper_account", acct)
        return acct

    def save_paper_account(self, acct: dict[str, Any]) -> None:
        self.set_json("paper_account", acct)

    def paper_positions(self) -> dict[str, sqlite3.Row]:
        slice_count = self.conn.execute("SELECT COUNT(*) AS n FROM paper_position_slices").fetchone()["n"]
        rows = self.conn.execute(
            """
            SELECT
                coin,
                side,
                SUM(cost_basis) AS cost_basis,
                SUM(entry_price * cost_basis) / SUM(cost_basis) AS entry_price,
                MIN(opened_at) AS opened_at,
                MIN(source_wallet) AS source_wallet,
                COUNT(*) AS slice_count
            FROM paper_position_slices
            WHERE status = 'OPEN'
            GROUP BY coin, side
            """
        ).fetchall()
        if rows or slice_count:
            return {r["coin"]: r for r in rows}

        rows = self.conn.execute("SELECT * FROM paper_positions").fetchall()
        return {r["coin"]: r for r in rows}

    def open_position_slices(self, coin: str | None = None) -> list[sqlite3.Row]:
        if coin is None:
            return self.conn.execute(
                "SELECT * FROM paper_position_slices WHERE status = 'OPEN' ORDER BY opened_at, id"
            ).fetchall()
        return self.conn.execute(
            """
            SELECT * FROM paper_position_slices
            WHERE status = 'OPEN' AND coin = ?
            ORDER BY opened_at, id
            """,
            (coin,),
        ).fetchall()

    def paper_position_slice(self, wallet: str, coin: str, side: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM paper_position_slices
            WHERE status = 'OPEN' AND source_wallet = ? AND coin = ? AND side = ?
            ORDER BY opened_at, id
            LIMIT 1
            """,
            (wallet, coin, side),
        ).fetchone()

    def paper_position_slice_count(self, wallet: str, coin: str, side: str) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM paper_position_slices
            WHERE status = 'OPEN' AND source_wallet = ? AND coin = ? AND side = ?
            """,
            (wallet, coin, side),
        ).fetchone()
        return int(row["n"] if row else 0)

    def insert_paper_position_slice(
        self, coin: str, side: str, entry_price: float, cost_basis: float, source_wallet: str
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_position_slices(coin, side, source_wallet, entry_price, cost_basis, opened_at, status)
            VALUES(?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (coin, side, source_wallet, entry_price, cost_basis, utc_now()),
        )
        self.conn.commit()

    def close_paper_position_slice(
        self,
        slice_id: int,
        exit_price: float | None,
        paper_gain: float,
        pnl_pct: float | None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE paper_position_slices
            SET status = 'CLOSED',
                closed_at = ?,
                exit_price = ?,
                paper_gain = ?,
                pnl_pct = ?
            WHERE id = ?
            """,
            (utc_now(), exit_price, paper_gain, pnl_pct, slice_id),
        )
        self.conn.commit()

    def sync_paper_position(self, coin: str) -> None:
        row = self.conn.execute(
            """
            SELECT
                coin,
                side,
                SUM(cost_basis) AS cost_basis,
                SUM(entry_price * cost_basis) / SUM(cost_basis) AS entry_price,
                MIN(opened_at) AS opened_at,
                MIN(source_wallet) AS source_wallet
            FROM paper_position_slices
            WHERE status = 'OPEN' AND coin = ?
            GROUP BY coin, side
            ORDER BY cost_basis DESC
            LIMIT 1
            """,
            (coin,),
        ).fetchone()
        if row is None:
            self.delete_paper_position(coin)
            return
        self.conn.execute(
            """
            INSERT INTO paper_positions(coin, side, entry_price, cost_basis, source_wallet, opened_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(coin) DO UPDATE SET
                side = excluded.side,
                entry_price = excluded.entry_price,
                cost_basis = excluded.cost_basis,
                source_wallet = excluded.source_wallet,
                opened_at = excluded.opened_at
            """,
            (
                row["coin"],
                row["side"],
                float(row["entry_price"]),
                float(row["cost_basis"]),
                row["source_wallet"],
                row["opened_at"],
            ),
        )
        self.conn.commit()

    def upsert_paper_position(
        self, coin: str, side: str, entry_price: float, cost_basis: float, source_wallet: str
    ) -> None:
        self.insert_paper_position_slice(coin, side, entry_price, cost_basis, source_wallet)
        self.sync_paper_position(coin)

    def delete_paper_position(self, coin: str) -> None:
        self.conn.execute("DELETE FROM paper_positions WHERE coin = ?", (coin,))
        self.conn.commit()

    def pause_wallet(self, wallet: str, coin: str, pnl_pct: float, reason: str) -> None:
        self.conn.execute(
            """
            INSERT INTO paused_wallets(wallet, paused_at, coin, pnl_pct, reason)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                paused_at = excluded.paused_at,
                coin = excluded.coin,
                pnl_pct = excluded.pnl_pct,
                reason = excluded.reason
            """,
            (wallet, utc_now(), coin, pnl_pct, reason),
        )
        self.conn.commit()

    def unpause_wallet(self, wallet: str) -> None:
        self.conn.execute("DELETE FROM paused_wallets WHERE wallet = ?", (wallet,))
        self.conn.commit()

    def paused_wallets(self) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM paused_wallets").fetchall()
        return {r["wallet"]: r for r in rows}

    def is_paused(self, wallet: str) -> bool:
        return wallet in self.paused_wallets()

    def log_signal(
        self,
        wallet: str,
        coin: str,
        side: str,
        signal_type: str,
        price: float | None,
        action: str,
        reason: str = "",
        paper_gain: float | None = None,
        pnl_pct: float | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO signals(ts, wallet, coin, side, signal, price, action, reason, paper_gain, pnl_pct)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (utc_now(), wallet, coin, side, signal_type, price, action, reason, paper_gain, pnl_pct),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def log_api_failure(self, platform: str, operation: str, subject: str, error: str) -> None:
        self.conn.execute(
            "INSERT INTO api_failures(ts, platform, operation, subject, error) VALUES(?, ?, ?, ?, ?)",
            (utc_now(), platform, operation, subject, error[:500]),
        )
        self.conn.commit()

    def log_token_risk_event(
        self,
        coin: str,
        wallet: str,
        side: str,
        signal_type: str,
        reason: str,
        market_cap_rank: int | None,
        source: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO token_risk_events(ts, coin, wallet, side, signal, reason, market_cap_rank, source)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (utc_now(), coin, wallet, side, signal_type, reason, market_cap_rank, source),
        )
        self.conn.commit()

    def log_scoring_engine_wallet_score(self, score: "ScoringEngineScore") -> None:
        self.conn.execute(
            """
            INSERT INTO marshal_wallet_scores(
                ts, wallet, tier, total_score, realized_component, win_rate_component,
                recent_form_component, churn_penalty, loss_penalty, sample_size,
                realized_pnl, win_rate, avg_pnl_pct, explanation
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                score.wallet,
                score.tier,
                score.total_score,
                score.realized_component,
                score.win_rate_component,
                score.recent_form_component,
                score.churn_penalty,
                score.loss_penalty,
                score.sample_size,
                score.realized_pnl,
                score.win_rate,
                score.avg_pnl_pct,
                score.explanation,
            ),
        )
        self.conn.commit()

    def log_scoring_engine_signal(
        self,
        signal_id: int | None,
        event: "CopyEvent",
        actual_action: str,
        actual_reason: str,
        score: "ScoringEngineScore",
        recommendation: str,
        would_execute: bool,
        reason: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO marshal_signal_journal(
                ts, signal_id, wallet, coin, side, signal, actual_action, actual_reason,
                marshal_tier, marshal_score, recommendation, would_execute, reason
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                signal_id,
                event.wallet,
                event.coin,
                event.side,
                event.kind,
                actual_action,
                actual_reason,
                score.tier,
                score.total_score,
                recommendation,
                1 if would_execute else 0,
                reason,
            ),
        )
        self.conn.commit()

    def open_scoring_engine_shadow_position(
        self,
        event: "CopyEvent",
        price: float,
        signal_id: int | None,
        score: "ScoringEngineScore",
    ) -> None:
        existing = self.conn.execute(
            """
            SELECT id FROM marshal_shadow_positions
            WHERE wallet = ? AND coin = ? AND side = ? AND status = 'OPEN'
            """,
            (event.wallet, event.coin, event.side),
        ).fetchone()
        if existing:
            return
        self.conn.execute(
            """
            INSERT INTO marshal_shadow_positions(
                wallet, coin, side, entry_price, opened_at, source_signal_id,
                marshal_score, marshal_tier, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (
                event.wallet,
                event.coin,
                event.side,
                price,
                utc_now(),
                signal_id,
                score.total_score,
                score.tier,
            ),
        )
        self.conn.commit()

    def close_scoring_engine_shadow_positions(self, event: "CopyEvent", price: float | None, signal_id: int | None) -> None:
        rows = self.conn.execute(
            """
            SELECT id, entry_price, side
            FROM marshal_shadow_positions
            WHERE wallet = ? AND coin = ? AND side = ? AND status = 'OPEN'
            """,
            (event.wallet, event.coin, event.side),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            pnl_pct = None
            paper_gain = None
            if price and float(row["entry_price"]) > 0:
                pnl_pct = (price - float(row["entry_price"])) / float(row["entry_price"]) * 100
                if row["side"] == "SHORT":
                    pnl_pct = -pnl_pct
                paper_gain = round(pnl_pct, 4)
            self.conn.execute(
                """
                UPDATE marshal_shadow_positions
                SET status = 'CLOSED',
                    exit_price = ?,
                    closed_at = ?,
                    paper_gain = ?,
                    pnl_pct = ?,
                    close_signal_id = ?,
                    close_reason = 'source exit'
                WHERE id = ?
                """,
                (price, utc_now(), paper_gain, pnl_pct, signal_id, row["id"]),
            )
        self.conn.commit()


# ---------------------------------------------------------------------------
# Retry helpers and notifications
# ---------------------------------------------------------------------------


class RetryClient:
    def __init__(self, store: Store, platform: str, retries: int = 3, timeout: int = 12):
        self.store = store
        self.platform = platform
        self.retries = retries
        self.timeout = timeout

    def request_json(
        self,
        operation: str,
        subject: str,
        fn: Callable[[], Any],
        retryable_statuses: set[int] | None = None,
    ) -> Any | None:
        retryable_statuses = retryable_statuses or {408, 425, 429, 500, 502, 503, 504}
        last_error = ""
        for attempt in range(self.retries + 1):
            try:
                result = fn()
                return result
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                last_error = f"HTTP {status}: {exc}"
                if status not in retryable_statuses or attempt >= self.retries:
                    break
            except Exception as exc:
                last_error = str(exc)
                if attempt >= self.retries:
                    break
            time.sleep(min(2 ** attempt, 8))
        self.store.log_api_failure(self.platform, operation, subject, last_error)
        return None


class Notifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, message: str) -> None:
        if not self.webhook_url:
            return
        try:
            requests.post(self.webhook_url, json={"content": message}, timeout=5)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Platform adapter contract
# ---------------------------------------------------------------------------


class TokenRiskMonitor:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._cache_loaded = False
        self._symbols: set[str] = set()
        self._ranks: dict[str, int] = {}

    @staticmethod
    def _coin_variants(coin: str) -> set[str]:
        symbol = coin.upper().strip()
        variants = {symbol}
        if symbol.startswith("K") and len(symbol) > 2:
            variants.add(symbol[1:])
        return variants

    def _load_cache(self) -> tuple[float, set[str], dict[str, int]]:
        cache = self.store.get_json("token_risk_coingecko_cache", {})
        refreshed_at = float(cache.get("refreshed_at", 0) or 0)
        symbols = {str(s).upper() for s in cache.get("symbols", [])}
        ranks = {str(k).upper(): int(v) for k, v in cache.get("ranks", {}).items()}
        return refreshed_at, symbols, ranks

    def _save_cache(self, symbols: set[str], ranks: dict[str, int]) -> None:
        self.store.set_json(
            "token_risk_coingecko_cache",
            {
                "refreshed_at": unix_now(),
                "top_n": self.settings.token_risk_top_n,
                "symbols": sorted(symbols),
                "ranks": ranks,
            },
        )

    def _save_partial_cache(self, symbols: set[str], ranks: dict[str, int]) -> None:
        if not symbols:
            return
        self._symbols = symbols
        self._ranks = ranks
        self._cache_loaded = True
        self._save_cache(symbols, ranks)

    def _refresh_cache(self) -> None:
        per_page = 250
        pages = max(1, math.ceil(self.settings.token_risk_top_n / per_page))
        symbols: set[str] = set()
        ranks: dict[str, int] = {}
        for page in range(1, pages + 1):
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": per_page,
                "page": page,
                "sparkline": "false",
            }
            try:
                response = requests.get(self.settings.coingecko_markets_url, params=params, timeout=12)
                response.raise_for_status()
            except Exception:
                self._save_partial_cache(symbols, ranks)
                raise
            rows = response.json()
            if not isinstance(rows, list):
                break
            for item in rows:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol") or "").upper()
                rank = item.get("market_cap_rank")
                if not symbol:
                    continue
                symbols.add(symbol)
                if rank is not None:
                    old = ranks.get(symbol)
                    ranks[symbol] = int(rank) if old is None else min(old, int(rank))
            if len(rows) < per_page:
                break
        if symbols:
            self._symbols = symbols
            self._ranks = ranks
            self._cache_loaded = True
            self._save_cache(symbols, ranks)

    def ensure_cache(self) -> None:
        if not self.settings.token_risk_logging:
            return
        refreshed_at, symbols, ranks = self._load_cache()
        self._symbols = symbols
        self._ranks = ranks
        self._cache_loaded = bool(symbols)
        if symbols and unix_now() - refreshed_at < self.settings.token_risk_refresh_seconds:
            return
        retry_state = self.store.get_json("token_risk_retry_after", {})
        retry_after = float(retry_state.get("ts", 0) or 0)
        if retry_after and unix_now() < retry_after:
            return
        try:
            self._refresh_cache()
        except Exception as exc:
            self.store.set_json(
                "token_risk_retry_after",
                {"ts": unix_now() + max(300, self.settings.token_risk_retry_seconds), "error": str(exc)},
            )
            self.store.log_api_failure("coingecko", "token_risk_cache", "", str(exc))

    def observe(self, event: CopyEvent) -> None:
        if not self.settings.token_risk_logging or event.kind not in {"ENTRY", "ADD"}:
            return
        self.ensure_cache()
        if not self._symbols:
            return
        variants = self._coin_variants(event.coin)
        matched = variants & self._symbols
        if matched:
            return
        self.store.log_token_risk_event(
            event.coin,
            event.wallet,
            event.side,
            event.kind,
            f"not in CoinGecko top {self.settings.token_risk_top_n} symbol cache",
            None,
            "coingecko",
        )


class PlatformAdapter(ABC):
    name: str

    @abstractmethod
    def candidate_wallets(self, limit: int) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def wallet_metrics(self, wallet: str, lookback_days: int) -> WalletMetrics | None:
        raise NotImplementedError

    @abstractmethod
    def positions(self, wallet: str) -> dict[str, Position] | None:
        raise NotImplementedError

    @abstractmethod
    def mid_price(self, coin: str) -> float | None:
        raise NotImplementedError

    @abstractmethod
    def account_value(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def live_positions(self) -> dict[str, Position] | None:
        raise NotImplementedError

    @abstractmethod
    def open_position(self, coin: str, side: str, notional_usd: float, price: float) -> bool:
        raise NotImplementedError

    @abstractmethod
    def close_position(self, coin: str) -> bool:
        raise NotImplementedError


class HyperliquidAdapter(PlatformAdapter):
    name = "hyperliquid"

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.retry = RetryClient(store, self.name)
        self._mids: dict[str, Any] = {}
        self._mids_ts = 0.0
        self._exchange = None
        self._info = None
        self._sz_decimals: dict[str, int] = {}

    def _post_info(self, payload: dict[str, Any], operation: str, subject: str = "") -> Any | None:
        def call() -> Any:
            r = requests.post(self.settings.hyperliquid_info_url, json=payload, timeout=12)
            r.raise_for_status()
            return r.json()

        return self.retry.request_json(operation, subject, call)

    def candidate_wallets(self, limit: int) -> list[str]:
        wallets: list[str] = []
        if self.settings.hypertracker_api_key:
            wallets = self._candidate_wallets_from_hypertracker(limit)
        if not wallets:
            wallets = self._candidate_wallets_from_hyperliquid_stats(limit)
        if not wallets:
            wallets = self._candidate_wallets_from_file(limit)
        return wallets[:limit]

    def _candidate_wallets_from_hypertracker(self, limit: int) -> list[str]:
        url = f"{self.settings.hypertracker_base}/api/external/leaderboards/perp-pnl"
        headers = {"Authorization": f"Bearer {self.settings.hypertracker_api_key}"}
        params = {"orderBy": "pnlMonth", "order": "desc", "limit": min(limit, 100)}

        def call() -> Any:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            return r.json()

        body = self.retry.request_json("leaderboard", "hypertracker", call)
        if body is None:
            return []
        items = body.get("data", body) if isinstance(body, dict) else body
        wallets = []
        for item in items or []:
            if isinstance(item, dict):
                wallet = item.get("address") or item.get("wallet")
                if wallet:
                    wallets.append(wallet)
        return wallets[:limit]

    def _candidate_wallets_from_hyperliquid_stats(self, limit: int) -> list[str]:
        url = f"{self.settings.hyperliquid_stats_url}/leaderboard"

        def call() -> Any:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return r.json()

        body = self.retry.request_json("leaderboard", "hyperliquid_stats", call)
        if not isinstance(body, dict):
            return []
        wallets: list[str] = []
        for row in body.get("leaderboardRows", [])[:limit]:
            if not isinstance(row, dict):
                continue
            wallet = row.get("ethAddress") or row.get("address") or row.get("wallet")
            if wallet:
                wallets.append(str(wallet))
        return wallets

    def _candidate_wallets_from_file(self, limit: int) -> list[str]:
        for filename in ("followlist.txt", "wallets.txt"):
            path = ROOT / filename
            if not path.exists():
                continue
            wallets = []
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    value = line.strip().split(",")[0].strip()
                    if value and not value.startswith("#"):
                        wallets.append(value)
            if wallets:
                return wallets[:limit]
        return []

    def wallet_metrics(self, wallet: str, lookback_days: int) -> WalletMetrics | None:
        start_ms = int((unix_now() - lookback_days * 86400) * 1000)
        fills = self._post_info(
            {"type": "userFillsByTime", "user": wallet, "startTime": start_ms},
            "wallet_metrics",
            wallet,
        )
        if fills is None:
            return None
        try:
            day_cutoff_ms = (unix_now() - 86400) * 1000
            fills_24h = sum(1 for f in fills if float(f.get("time", 0)) >= day_cutoff_ms)
            if fills_24h > self.settings.hft_fill_limit_24h:
                return WalletMetrics(hft=True, qualifies=False)

            pnls = [
                float(f["closedPnl"])
                for f in fills
                if f.get("closedPnl") is not None and float(f.get("closedPnl") or 0) != 0
            ]
            if len(pnls) < self.settings.min_sample:
                return WalletMetrics(hft=False, qualifies=False, sample=len(pnls))

            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            gross_win = sum(wins)
            gross_loss = abs(sum(losses))
            win_rate = len(wins) / len(pnls)
            profit_factor = gross_win / gross_loss if gross_loss > 0 else 999.0
            qualifies = (
                win_rate >= self.settings.min_win_rate
                and profit_factor >= self.settings.min_profit_factor
            )
            return WalletMetrics(False, qualifies, len(pnls), win_rate, profit_factor)
        except Exception as exc:
            self.store.log_api_failure(self.name, "parse_wallet_metrics", wallet, str(exc))
            return None

    def positions(self, wallet: str) -> dict[str, Position] | None:
        state = self._post_info(
            {"type": "clearinghouseState", "user": wallet},
            "wallet_positions",
            wallet,
        )
        if state is None:
            return None
        result: dict[str, Position] = {}
        try:
            for entry in state.get("assetPositions", []):
                pos = entry.get("position", {})
                size = float(pos.get("szi") or 0)
                if size == 0:
                    continue
                coin = str(pos.get("coin") or "")
                result[coin] = Position(
                    coin=coin,
                    side="LONG" if size > 0 else "SHORT",
                    size=abs(size),
                    entry_price=float(pos.get("entryPx") or 0),
                )
            return result
        except Exception as exc:
            self.store.log_api_failure(self.name, "parse_wallet_positions", wallet, str(exc))
            return None

    def all_mids(self) -> dict[str, Any]:
        if self._mids and unix_now() - self._mids_ts < 5:
            return self._mids
        mids = self._post_info({"type": "allMids"}, "all_mids")
        if isinstance(mids, dict):
            self._mids = mids
            self._mids_ts = unix_now()
        return self._mids

    def mid_price(self, coin: str) -> float | None:
        try:
            value = self.all_mids().get(coin)
            return float(value) if value is not None else None
        except Exception:
            return None

    def _init_sdk(self) -> None:
        if self._exchange is not None:
            return
        if not self.settings.hl_api_key or not self.settings.hl_wallet_address:
            raise RuntimeError("HL_API_KEY and HL_WALLET_ADDRESS are required for live execution")

        import eth_account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info

        account = eth_account.Account.from_key(self.settings.hl_api_key)
        self._info = Info("https://api.hyperliquid.xyz", skip_ws=True)
        meta = self._info.meta()
        for asset in meta.get("universe", []):
            self._sz_decimals[asset["name"]] = int(asset.get("szDecimals", 4))
        self._exchange = Exchange(
            account,
            "https://api.hyperliquid.xyz",
            meta=meta,
            account_address=self.settings.hl_wallet_address,
        )

    def _user_state(self) -> dict[str, Any]:
        if self.settings.live:
            try:
                self._init_sdk()
                return self._info.user_state(self.settings.hl_wallet_address)  # type: ignore[union-attr]
            except Exception as exc:
                self.store.log_api_failure(self.name, "live_user_state", "", str(exc))
                return {}
        state = self._post_info(
            {"type": "clearinghouseState", "user": self.settings.hl_wallet_address},
            "account_state",
            self.settings.hl_wallet_address,
        )
        return state or {}

    def account_value(self) -> float:
        state = self._user_state()
        try:
            value = float(state.get("marginSummary", {}).get("accountValue") or 0)
            return value if value > 0 else self.settings.hl_account_fallback
        except Exception:
            return self.settings.hl_account_fallback

    def live_positions(self) -> dict[str, Position] | None:
        state = self._user_state()
        if not state:
            return None
        result: dict[str, Position] = {}
        try:
            for entry in state.get("assetPositions", []):
                pos = entry.get("position", {})
                size = float(pos.get("szi") or 0)
                if size == 0:
                    continue
                coin = str(pos.get("coin") or "")
                result[coin] = Position(
                    coin=coin,
                    side="LONG" if size > 0 else "SHORT",
                    size=abs(size),
                    entry_price=float(pos.get("entryPx") or 0),
                )
        except Exception as exc:
            self.store.log_api_failure(self.name, "parse_live_positions", "", str(exc))
            return None
        return result

    def open_position(self, coin: str, side: str, notional_usd: float, price: float) -> bool:
        if notional_usd < self.settings.min_order_notional or price <= 0:
            return False

        if self.settings.live:
            try:
                self._init_sdk()
            except Exception as exc:
                self.store.log_api_failure(self.name, "init_sdk", coin, str(exc))
                print(f"[LIVE] ENTRY failed {coin} {side}: {exc}")
                return False

        decimals = self._sz_decimals.get(coin, 4)
        size = round(notional_usd / price, decimals)
        if size <= 0:
            return False

        if not self.settings.live:
            print(f"[DRY] ENTRY {coin} {side} size={size} notional~${notional_usd:.2f}")
            return True

        try:
            self._exchange.update_leverage(self.settings.leverage, coin, is_cross=True)  # type: ignore[union-attr]
            result = self._exchange.market_open(  # type: ignore[union-attr]
                coin, side == "LONG", size, slippage=self.settings.slippage
            )
            return self._accepted(result)
        except Exception as exc:
            self.store.log_api_failure(self.name, "open_position", coin, str(exc))
            print(f"[LIVE] ENTRY failed {coin} {side}: {exc}")
            return False

    def close_position(self, coin: str) -> bool:
        if not self.settings.live:
            print(f"[DRY] EXIT {coin}")
            return True
        try:
            self._init_sdk()
            result = self._exchange.market_close(coin, slippage=self.settings.slippage)  # type: ignore[union-attr]
            return self._accepted(result)
        except Exception as exc:
            self.store.log_api_failure(self.name, "close_position", coin, str(exc))
            print(f"[LIVE] EXIT failed {coin}: {exc}")
            return False

    @staticmethod
    def _accepted(result: Any) -> bool:
        if not isinstance(result, dict) or result.get("status") != "ok":
            return False
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            return False
        if not isinstance(statuses[0], dict):
            return False
        if "error" in statuses[0]:
            return False
        if not statuses[0]:
            return False
        return any(key in statuses[0] for key in ("filled", "resting", "filledWith"))


# ---------------------------------------------------------------------------
# Portfolio and risk
# ---------------------------------------------------------------------------


class PaperPortfolio:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def account(self) -> dict[str, Any]:
        return self.store.paper_account(self.settings.paper_starting_cash)

    def positions(self) -> dict[str, sqlite3.Row]:
        return self.store.paper_positions()

    def position(self, coin: str) -> sqlite3.Row | None:
        return self.positions().get(coin)

    def position_side(self, coin: str, side: str) -> sqlite3.Row | None:
        pos = self.position(coin)
        if pos is None or pos["side"] != side:
            return None
        return pos

    def has_opposite_position(self, coin: str, side: str) -> bool:
        pos = self.position(coin)
        return pos is not None and pos["side"] != side

    def owns_position(self, wallet: str, coin: str, side: str | None = None) -> bool:
        if side is None:
            rows = self.store.open_position_slices(coin)
            return any(row["source_wallet"] == wallet for row in rows)
        return self.store.paper_position_slice(wallet, coin, side) is not None

    def allocation_count_for_wallet_coin_side(self, wallet: str, coin: str, side: str) -> int:
        return self.store.paper_position_slice_count(wallet, coin, side)

    def held_coins(self) -> set[str]:
        return set(self.positions().keys())

    def exposure_count(self) -> int:
        return len(self.positions())

    def slice_count(self, coin: str, side: str) -> int:
        return sum(1 for row in self.store.open_position_slices(coin) if row["side"] == side)

    def value(self, price_fn: Callable[[str], float | None]) -> float:
        acct = self.account()
        total = float(acct.get("cash", 0))
        rows = self.store.open_position_slices()
        if not rows:
            rows = list(self.positions().values())
        price_cache: dict[str, float | None] = {}
        for pos in rows:
            coin = str(pos["coin"])
            cost = float(pos["cost_basis"])
            entry = float(pos["entry_price"])
            if coin not in price_cache:
                price_cache[coin] = price_fn(coin)
            price = price_cache[coin]
            if price and entry > 0:
                pct = (price - entry) / entry
                if pos["side"] == "SHORT":
                    pct = -pct
                total += cost + cost * self.settings.leverage * pct
            else:
                total += cost
        return round(total, 2)

    def available_slot(self, coin: str, price: float, multiplier: float = 1.0, side: str | None = None) -> float | None:
        if not price or price <= 0:
            return None
        positions = self.positions()
        if side and self.has_opposite_position(coin, side):
            return None
        if side and self.slice_count(coin, side) >= self.settings.max_slices_per_coin:
            return None
        is_new_coin = coin not in positions
        if is_new_coin and len(positions) >= self.settings.max_positions:
            return None

        acct = self.account()
        total_value = float(acct["cash"]) + sum(float(p["cost_basis"]) for p in self.store.open_position_slices())
        if total_value <= 0:
            return None
        base_slot = total_value / self.settings.max_positions
        slot = base_slot * multiplier
        current_coin_cost = sum(float(row["cost_basis"]) for row in self.store.open_position_slices(coin))
        max_coin_cost = base_slot * self.settings.max_coin_cost_multiplier
        remaining_coin_capacity = max_coin_cost - current_coin_cost
        slot = min(slot, remaining_coin_capacity, float(acct["cash"]))
        if slot < self.settings.min_slot_usd:
            return None
        return round(slot, 2)

    def open(
        self,
        wallet: str,
        coin: str,
        side: str,
        price: float,
        cost_basis: float | None = None,
        allow_same_wallet_add: bool = False,
    ) -> float | None:
        slot = cost_basis if cost_basis is not None else self.available_slot(coin, price, side=side)
        if slot is None:
            return None

        positions = self.positions()
        wallet_allocations = self.allocation_count_for_wallet_coin_side(wallet, coin, side)
        if wallet_allocations and not allow_same_wallet_add:
            return None
        if (
            allow_same_wallet_add
            and wallet_allocations >= self.settings.max_allocations_per_wallet_coin_side
        ):
            return None
        if self.has_opposite_position(coin, side):
            return None
        if self.slice_count(coin, side) >= self.settings.max_slices_per_coin:
            return None
        if coin not in positions and len(positions) >= self.settings.max_positions:
            return None

        acct = self.account()
        if slot < self.settings.min_slot_usd or float(acct["cash"]) < slot:
            return None

        acct["cash"] = round(float(acct["cash"]) - slot, 2)
        self.store.save_paper_account(acct)
        self.store.upsert_paper_position(coin, side, price, round(slot, 2), wallet)
        return round(slot, 2)

    def close(self, wallet: str, coin: str, side: str, price: float | None) -> tuple[float | None, float | None, str]:
        pos = self.store.paper_position_slice(wallet, coin, side)
        if not pos:
            return None, None, ""
        cost = float(pos["cost_basis"])
        entry = float(pos["entry_price"])
        side = str(pos["side"])
        gain = 0.0
        pnl_pct = None
        if price and entry > 0:
            pnl_pct = (price - entry) / entry * 100
            if side == "SHORT":
                pnl_pct = -pnl_pct
            gain = round(cost * self.settings.leverage * (pnl_pct / 100), 2)

        acct = self.account()
        acct["cash"] = round(float(acct["cash"]) + cost + gain, 2)
        acct["realized_pnl"] = round(float(acct.get("realized_pnl", 0)) + gain, 2)
        self.store.save_paper_account(acct)
        self.store.close_paper_position_slice(int(pos["id"]), price, gain, pnl_pct)
        self.store.sync_paper_position(coin)
        return gain, pnl_pct, side


class RiskManager:
    def __init__(self, settings: Settings, store: Store, notifier: Notifier):
        self.settings = settings
        self.store = store
        self.notifier = notifier

    def session_start_value(self, portfolio: PaperPortfolio, platform: PlatformAdapter) -> float:
        start = portfolio.value(platform.mid_price)
        self.store.set_json("session", {"started": utc_now(), "paper_start": start})
        return start

    def is_wind_down(self) -> bool:
        return self.settings.wind_down or self.settings.circuit_breaker_file.exists()

    def drawdown(self, start_value: float, current_value: float) -> float:
        if start_value <= 0:
            return 0.0
        return max(0.0, (start_value - current_value) / start_value)

    def effective_loss_pct(self, drawdown: float) -> float:
        if drawdown >= self.settings.warning_drawdown_pct:
            return self.settings.min_loss_pct_to_pause / 2
        return self.settings.min_loss_pct_to_pause

    def effective_pause_hours(self, drawdown: float) -> int:
        if drawdown >= self.settings.warning_drawdown_pct:
            return self.settings.min_pause_hours * 2
        return self.settings.min_pause_hours

    def check_circuit_breaker(self, start_value: float, current_value: float) -> bool:
        dd = self.drawdown(start_value, current_value)
        if dd < self.settings.max_drawdown_pct:
            return False
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "tripped_at": utc_now(),
            "reason": "paper drawdown",
            "drawdown_pct": round(dd * 100, 2),
            "clear": f"Delete {self.settings.circuit_breaker_file} to allow new entries.",
        }
        self.settings.circuit_breaker_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        msg = f"[CIRCUIT] Drawdown {dd:.1%} reached limit {self.settings.max_drawdown_pct:.0%}; wind-down active."
        print(msg)
        self.notifier.send(msg)
        return True

    def allow_entry(
        self,
        wallet: str,
        coin: str,
        side: str,
        paper: PaperPortfolio,
        wind_down: bool,
        live_held: set[str],
        allow_same_wallet_add: bool = False,
    ) -> TradeDecision:
        if wind_down:
            return TradeDecision("SKIP", "wind-down")
        existing = paper.position(coin)
        if existing is not None and existing["side"] != side:
            return TradeDecision("SKIP", "opposite side already held")
        if self.settings.live and existing is not None:
            return TradeDecision("SKIP", "live slice close unsupported")
        if coin in live_held:
            return TradeDecision("SKIP", "coin already held")
        if paper.owns_position(wallet, coin, side):
            allocation_count = paper.allocation_count_for_wallet_coin_side(wallet, coin, side)
            if not allow_same_wallet_add:
                return TradeDecision("SKIP", "wallet allocation already held")
            if allocation_count >= self.settings.max_allocations_per_wallet_coin_side:
                return TradeDecision("SKIP", "same-wallet add cap")
        if existing is None and len(paper.positions()) >= self.settings.max_positions:
            return TradeDecision("SKIP", "position cap")
        return TradeDecision("EXECUTE")

    def maybe_pause_wallet(self, wallet: str, coin: str, pnl_pct: float | None, threshold: float) -> None:
        return
        if pnl_pct is None:
            return
        emergency_threshold = max(threshold, self.settings.pause_emergency_loss_pct)
        if pnl_pct <= -emergency_threshold:
            reason = f"emergency loss {pnl_pct:+.2f}%"
            self.store.pause_wallet(wallet, coin, pnl_pct, reason)
            print(f"[PAUSE] {wallet[:16]} paused after {coin} {reason}")
            return

        rows = self.store.conn.execute(
            """
            SELECT coin, pnl_pct
            FROM signals
            WHERE wallet = ?
              AND signal = 'EXIT'
              AND action = 'EXECUTED'
              AND pnl_pct IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (wallet, self.settings.pause_recent_exits),
        ).fetchall()
        if len(rows) < self.settings.pause_recent_exits:
            return

        pnls = [float(row["pnl_pct"]) for row in rows]
        loss_count = sum(1 for value in pnls if value < 0)
        cumulative = sum(pnls)
        if (
            loss_count >= self.settings.pause_loss_count
            and cumulative <= -self.settings.pause_cumulative_loss_pct
        ):
            reason = (
                f"{loss_count}/{len(pnls)} recent exits lost; "
                f"cumulative {cumulative:+.2f}%"
            )
            self.store.pause_wallet(wallet, coin, cumulative, reason)
            print(f"[PAUSE] {wallet[:16]} paused: {reason}")


# ---------------------------------------------------------------------------
# Scoring Engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringEngineScore:
    wallet: str
    tier: str
    total_score: float
    realized_component: float
    win_rate_component: float
    recent_form_component: float
    churn_penalty: float
    loss_penalty: float
    sample_size: int
    realized_pnl: float
    win_rate: float | None
    avg_pnl_pct: float | None
    explanation: str


class ScoringEngine:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def score_wallet(self, wallet: str) -> ScoringEngineScore:
        rows = self.store.conn.execute(
            """
            SELECT paper_gain, pnl_pct
            FROM signals
            WHERE wallet = ?
              AND signal = 'EXIT'
              AND action = 'EXECUTED'
              AND pnl_pct IS NOT NULL
            ORDER BY id
            """,
            (wallet,),
        ).fetchall()
        entry_count = int(
            self.store.conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM signals
                WHERE wallet = ?
                  AND signal = 'ENTRY'
                  AND action = 'EXECUTED'
                """,
                (wallet,),
            ).fetchone()["n"]
        )
        add_count = int(
            self.store.conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM signals
                WHERE wallet = ?
                  AND signal = 'ADD'
                  AND action = 'EXECUTED'
                """,
                (wallet,),
            ).fetchone()["n"]
        )
        entry_count += add_count

        sample_size = len(rows)
        historical_metrics = self.store.roster_wallet_metrics(wallet) if sample_size == 0 else None
        if sample_size == 0:
            if historical_metrics is None:
                explanation = "no copied exits; no stored roster history"
                return ScoringEngineScore(
                    wallet=wallet,
                    tier="Bench",
                    total_score=0.0,
                    realized_component=0.0,
                    win_rate_component=0.0,
                    recent_form_component=0.0,
                    churn_penalty=0.0,
                    loss_penalty=0.0,
                    sample_size=0,
                    realized_pnl=0.0,
                    win_rate=None,
                    avg_pnl_pct=None,
                    explanation=explanation,
                )

            win_rate = historical_metrics.win_rate
            profit_factor = historical_metrics.profit_factor
            sample_size = historical_metrics.sample
            sample_weight = self._clamp(sample_size / 40.0, 0.35, 1.0)
            realized_component = 0.0
            win_rate_component = self._clamp((win_rate - self.settings.min_win_rate) * 80.0, 0.0, 12.0) * sample_weight
            recent_form_component = self._clamp(
                (profit_factor - self.settings.min_profit_factor) * 8.0,
                0.0,
                12.0,
            ) * sample_weight
            loss_penalty = 0.0
            churn_penalty = 0.0
            total = 50.0 + realized_component + win_rate_component + recent_form_component
            total = round(self._clamp(total, 0.0, 100.0), 2)

            if total >= 57.0 and sample_size >= 3:
                tier = "Core"
            elif total >= 50.0:
                tier = "Candidate"
            else:
                tier = "Bench"

            explanation = (
                f"historical sample={sample_size}; score={total:.1f}; "
                f"win={win_rate:.0%}; pf={profit_factor:.2f}"
            )
            return ScoringEngineScore(
                wallet=wallet,
                tier=tier,
                total_score=total,
                realized_component=round(realized_component, 4),
                win_rate_component=round(win_rate_component, 4),
                recent_form_component=round(recent_form_component, 4),
                churn_penalty=round(churn_penalty, 4),
                loss_penalty=round(loss_penalty, 4),
                sample_size=sample_size,
                realized_pnl=0.0,
                win_rate=round(win_rate, 4),
                avg_pnl_pct=None,
                explanation=explanation,
            )

        realized = sum(float(row["paper_gain"] or 0.0) for row in rows)
        pnls = [float(row["pnl_pct"]) for row in rows]
        sample_weight = self._clamp(sample_size / 12.0, 0.0, 1.0)

        if sample_size:
            win_rate = sum(1 for value in pnls if value > 0) / sample_size
            avg_pct = sum(pnls) / sample_size
            worst_pct = min(pnls)
        else:
            win_rate = None
            avg_pct = None
            worst_pct = 0.0

        realized_component = self._clamp(realized / 250.0 * 25.0, -25.0, 25.0) * sample_weight
        win_rate_component = 0.0
        if win_rate is not None:
            win_rate_component = self._clamp((win_rate - 0.50) * 45.0, -18.0, 18.0) * sample_weight

        recent_form_component = 0.0
        if pnls:
            recent = pnls[-5:]
            recent_avg = sum(recent) / len(recent)
            recent_losses = sum(1 for value in recent if value < 0)
            recent_form_component = self._clamp(recent_avg / 0.50 * 10.0, -10.0, 10.0) * sample_weight
            if len(recent) >= 3 and recent_losses >= 3:
                recent_form_component -= 5.0 * sample_weight

        loss_penalty = 0.0
        if pnls:
            loss_values = [value for value in pnls if value < 0]
            if worst_pct < 0:
                loss_penalty -= self._clamp(abs(worst_pct) / 2.0 * 10.0, 0.0, 12.0) * sample_weight
            if sample_size and len(loss_values) / sample_size > 0.55:
                loss_penalty -= 6.0 * sample_weight

        churn_penalty = 0.0
        if sample_size >= 10:
            weak_expectancy = realized / sample_size < 3.0 or (win_rate is not None and win_rate < 0.50)
            if weak_expectancy and entry_count >= 15:
                churn_penalty = -self._clamp((entry_count - 12) / 25.0 * 10.0, 0.0, 10.0)

        total = 50.0 + realized_component + win_rate_component + recent_form_component + loss_penalty + churn_penalty
        total = round(self._clamp(total, 0.0, 100.0), 2)

        if sample_size < 3:
            tier = "Candidate" if total >= 50.0 else "Bench"
        elif total >= 65.0 and sample_size >= 8:
            tier = "Elite"
        elif total >= 57.0 and sample_size >= 3:
            tier = "Core"
        elif total >= 50.0:
            tier = "Candidate"
        else:
            tier = "Bench"

        parts = [f"sample={sample_size}", f"realized=${realized:.2f}", f"score={total:.1f}"]
        if win_rate is not None:
            parts.append(f"win={win_rate:.0%}")
        if avg_pct is not None:
            parts.append(f"avg={avg_pct:+.3f}%")
        if loss_penalty < 0:
            parts.append("loss-control penalty")
        if churn_penalty < 0:
            parts.append("weak-churn penalty")
        explanation = "; ".join(parts)

        return ScoringEngineScore(
            wallet=wallet,
            tier=tier,
            total_score=total,
            realized_component=round(realized_component, 4),
            win_rate_component=round(win_rate_component, 4),
            recent_form_component=round(recent_form_component, 4),
            churn_penalty=round(churn_penalty, 4),
            loss_penalty=round(loss_penalty, 4),
            sample_size=sample_size,
            realized_pnl=round(realized, 2),
            win_rate=None if win_rate is None else round(win_rate, 4),
            avg_pnl_pct=None if avg_pct is None else round(avg_pct, 4),
            explanation=explanation,
        )

    def recommendation(self, score: ScoringEngineScore, event: CopyEvent, actual_action: str, actual_reason: str) -> tuple[str, bool, str]:
        if score.tier in {"Elite", "Core"}:
            return "PREFER", True, f"{score.tier} wallet; {score.explanation}"
        if score.tier == "Candidate":
            would_execute = actual_action == "EXECUTED" or actual_reason in {
                "position cap",
                "coin already held",
                "paper rejected",
                "wallet allocation already held",
                "same-wallet add cap",
            }
            if actual_reason.startswith("Scoring Engine unproven add blocked"):
                would_execute = True
            return "NEUTRAL", would_execute, f"Candidate wallet; {score.explanation}"
        return "AVOID", False, f"Bench wallet; {score.explanation}"

    def open_allocations_for_wallet(self, wallet: str) -> int:
        row = self.store.conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM paper_position_slices
            WHERE source_wallet = ?
              AND status = 'OPEN'
            """,
            (wallet,),
        ).fetchone()
        return int(row["n"] if row else 0)

    def active_entry_decision(self, event: CopyEvent) -> tuple[TradeDecision, ScoringEngineScore]:
        score = self.score_wallet(event.wallet)
        if not self.settings.scoring_engine_active:
            return TradeDecision("EXECUTE", ""), score
        if score.tier == "Bench":
            return TradeDecision("SKIP", f"Scoring Engine Bench: {score.explanation}"), score
        if score.total_score < 50.0:
            return TradeDecision("SKIP", f"Scoring Engine below neutral: {score.explanation}"), score
        if score.tier == "Candidate" and score.total_score < 55.0:
            return TradeDecision("SKIP", f"Scoring Engine watch-only Candidate: {score.explanation}"), score
        if event.kind == "ADD" and score.sample_size < 3:
            return TradeDecision("SKIP", f"Scoring Engine unproven add blocked: {score.explanation}"), score
        if score.tier == "Candidate" and self.settings.scoring_engine_candidate_max_allocations > 0:
            open_count = self.open_allocations_for_wallet(event.wallet)
            proven_candidate = score.sample_size >= 3 and score.total_score >= 55.0 and score.realized_pnl > 0
            allocation_cap = (
                self.settings.scoring_engine_proven_candidate_max_allocations
                if proven_candidate
                else self.settings.scoring_engine_candidate_max_allocations
            )
            if open_count >= allocation_cap:
                return (
                    TradeDecision(
                        "SKIP",
                        (
                            "Scoring Engine Candidate concentration cap: "
                            f"{open_count}/{allocation_cap} open allocations; "
                            f"{score.explanation}"
                        ),
                    ),
                    score,
                )
        return TradeDecision("EXECUTE", ""), score

    def allocation_multiplier(self, score: ScoringEngineScore) -> float:
        if score.tier == "Elite":
            raw = self.settings.scoring_engine_elite_multiplier
        elif score.tier == "Core":
            raw = self.settings.scoring_engine_core_multiplier
        elif score.tier == "Candidate":
            if score.sample_size < 3:
                raw = self.settings.scoring_engine_default_candidate_multiplier
            elif score.total_score < 50.0:
                raw = 0.0
            elif score.sample_size >= 3 and score.total_score >= 55.0 and score.realized_pnl > 0:
                raw = self.settings.scoring_engine_proven_candidate_multiplier
            else:
                raw = self.settings.scoring_engine_candidate_multiplier
        else:
            raw = 0.0
        return round(self._clamp(raw, 0.0, self.settings.scoring_engine_max_slot_multiplier), 4)

    def allocation_note(self, score: ScoringEngineScore, multiplier: float) -> str:
        return f"Scoring Engine {score.tier} x{multiplier:.2f}: {score.explanation}"

    def observe_signal(
        self,
        event: CopyEvent,
        signal_id: int | None,
        actual_action: str,
        actual_reason: str = "",
        price: float | None = None,
    ) -> None:
        score = self.score_wallet(event.wallet)
        recommendation, would_execute, reason = self.recommendation(score, event, actual_action, actual_reason)
        self.store.log_scoring_engine_wallet_score(score)
        self.store.log_scoring_engine_signal(
            signal_id,
            event,
            actual_action,
            actual_reason,
            score,
            recommendation,
            would_execute,
            reason,
        )
        if event.kind == "ENTRY" and actual_action == "SKIPPED" and would_execute and price and price > 0:
            self.store.open_scoring_engine_shadow_position(event, price, signal_id, score)
        if event.kind == "EXIT":
            self.store.close_scoring_engine_shadow_positions(event, price, signal_id)


# ---------------------------------------------------------------------------
# Roster, monitoring, and reconciliation
# ---------------------------------------------------------------------------


class RosterService:
    def __init__(self, settings: Settings, store: Store, platform: PlatformAdapter):
        self.settings = settings
        self.store = store
        self.platform = platform

    def _config_signature(self) -> dict[str, Any]:
        return {
            "roster_size": self.settings.roster_size,
            "max_follow": self.settings.max_follow,
            "fills_lookback_days": self.settings.fills_lookback_days,
            "hft_fill_limit_24h": self.settings.hft_fill_limit_24h,
            "min_sample": self.settings.min_sample,
            "min_win_rate": self.settings.min_win_rate,
            "min_profit_factor": self.settings.min_profit_factor,
            "historical_metrics_version": 1,
        }

    def refresh_in_progress(self) -> bool:
        cycle = self.store.get_json("roster_refresh_cycle", {})
        candidates = cycle.get("candidates", [])
        cursor = int(cycle.get("cursor", 0) or 0)
        return bool(candidates) and cursor < len(candidates)

    def load_or_refresh(self, force: bool = False, pause_hours: int | None = None) -> list[str]:
        last = float(self.store.get_json("last_roster_refresh", 0))
        cached = self.store.roster()
        config_changed = self.store.get_json("last_roster_config", {}) != self._config_signature()
        if (
            cached
            and not force
            and not config_changed
            and not self.refresh_in_progress()
            and unix_now() - last < self.settings.roster_refresh_seconds
        ):
            return cached
        if cached and config_changed:
            print("[ROSTER] Settings changed; rebuilding roster")
        return self.refresh(pause_hours=pause_hours)

    def refresh(self, pause_hours: int | None = None) -> list[str]:
        follow_limit = self.settings.max_follow if self.settings.max_follow > 0 else self.settings.roster_size
        cached_roster = self.store.roster()
        signature = self._config_signature()
        cycle = self.store.get_json("roster_refresh_cycle", {})
        if cycle.get("config") != signature:
            cycle = {}

        if not cycle.get("candidates"):
            print(f"[ROSTER] Starting staggered refresh for up to {follow_limit} qualifying wallets")
            candidates = self.platform.candidate_wallets(self.settings.roster_size)
            if not candidates:
                print("[ROSTER] No candidates returned; keeping cached roster")
                return cached_roster
            cycle = {
                "config": signature,
                "started_at": unix_now(),
                "cursor": 0,
                "candidates": candidates,
                "qualified": [],
            }

        candidates = [str(w) for w in cycle.get("candidates", [])]
        cursor = int(cycle.get("cursor", 0) or 0)
        qualified = list(dict.fromkeys(str(w) for w in cycle.get("qualified", [])))
        batch_size = max(1, int(self.settings.roster_refresh_batch_size))
        end = min(len(candidates), cursor + batch_size)
        failures = 0
        evaluated = 0

        print(f"[ROSTER] Evaluating candidates {cursor + 1}-{end}/{len(candidates)}; qualified={len(qualified)}")
        for wallet in candidates[cursor:end]:
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
            self.store.upsert_roster_wallet_metrics(wallet, metrics)

        if evaluated > 0 and failures / evaluated > self.settings.api_degraded_max_fail_ratio:
            print(f"[ROSTER] API degraded in batch ({failures}/{evaluated} failed); keeping cached roster")
            return cached_roster

        cycle["cursor"] = end
        cycle["qualified"] = qualified
        complete = end >= len(candidates) or len(qualified) >= follow_limit

        if not complete:
            self.store.set_json("roster_refresh_cycle", cycle)
            if not cached_roster and qualified:
                self.store.replace_roster(qualified)
                print(f"[ROSTER] Following partial roster of {len(qualified)} wallet(s) while refresh continues")
                return qualified
            print(f"[ROSTER] Staggered refresh progress: {end}/{len(candidates)} evaluated; qualified={len(qualified)}")
            return cached_roster

        if cached_roster and len(qualified) < max(1, len(cached_roster) // 2):
            print(
                f"[ROSTER] Rebuild produced only {len(qualified)}/{len(cached_roster)} "
                "cached wallets; keeping cached roster"
            )
            self.store.set_json("roster_refresh_cycle", {})
            return cached_roster

        if qualified:
            self.store.replace_roster(qualified)
            self.store.set_json("last_roster_refresh", unix_now())
            self.store.set_json("last_roster_config", signature)
            self.store.set_json("roster_refresh_cycle", {})
        print(f"[ROSTER] Following {len(qualified)} wallet(s)")
        return qualified or cached_roster


class WalletMonitor:
    def __init__(self, settings: Settings, store: Store, platform: PlatformAdapter):
        self.settings = settings
        self.store = store
        self.platform = platform

    def scan(self, wallets: list[str]) -> tuple[list[CopyEvent], float]:
        events: list[CopyEvent] = []
        failures = 0
        checked = 0

        for wallet in wallets:
            current = self.platform.positions(wallet)
            checked += 1
            if current is None:
                failures += 1
                time.sleep(self.settings.wallet_poll_delay)
                continue

            previous = self.store.wallet_snapshot(wallet)
            if previous is None:
                self.store.save_wallet_snapshot(wallet, current)
                print(f"[MONITOR] Seeded {wallet[:16]} baseline")
                time.sleep(self.settings.wallet_poll_delay)
                continue

            for coin, pos in current.items():
                old = previous.get(coin)
                if old is None:
                    events.append(CopyEvent("ENTRY", wallet, coin, pos.side, pos.entry_price))
                elif old.side != pos.side:
                    events.append(CopyEvent("EXIT", wallet, coin, old.side))
                    events.append(CopyEvent("ENTRY", wallet, coin, pos.side, pos.entry_price))
                elif old.size > 0:
                    size_increase_pct = (pos.size - old.size) / old.size * 100
                    if size_increase_pct >= self.settings.same_wallet_add_threshold_pct:
                        events.append(
                            CopyEvent(
                                "ADD",
                                wallet,
                                coin,
                                pos.side,
                                pos.entry_price,
                                previous_size=old.size,
                                current_size=pos.size,
                            )
                        )

            for coin, old in previous.items():
                if coin not in current:
                    events.append(CopyEvent("EXIT", wallet, coin, old.side))

            self.store.save_wallet_snapshot(wallet, current)
            time.sleep(self.settings.wallet_poll_delay)

        fail_ratio = failures / checked if checked else 0.0
        return events, fail_ratio


class Reconciler:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        platform: PlatformAdapter,
        paper: PaperPortfolio,
        risk: RiskManager,
    ):
        self.settings = settings
        self.store = store
        self.platform = platform
        self.paper = paper
        self.risk = risk

    def run(self, wallets: list[str], loss_threshold: float) -> None:
        slices = self.store.open_position_slices()
        if not slices:
            return

        now = unix_now()
        for pos in list(slices):
            coin = str(pos["coin"])
            try:
                opened = datetime.strptime(pos["opened_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - opened).total_seconds() / 86400
            except Exception:
                age_days = 0
            if age_days > self.settings.max_position_days:
                self._force_close(coin, pos["source_wallet"], pos["side"], "position timeout", loss_threshold)

        for wallet in wallets:
            current = self.platform.positions(wallet)
            if current is None:
                continue
            for pos in list(self.store.open_position_slices()):
                coin = str(pos["coin"])
                if pos["source_wallet"] != wallet:
                    continue
                source = current.get(coin)
                if source and source.side == pos["side"]:
                    continue
                reason = "source closed" if source is None else "source flipped"
                self._force_close(coin, wallet, pos["side"], f"reconcile: {reason}", loss_threshold)

    def _force_close(self, coin: str, wallet: str, side: str, reason: str, loss_threshold: float) -> None:
        paper_pos = self.paper.position(coin)
        if paper_pos is None:
            self.store.log_signal(wallet, coin, side, "EXIT", None, "SKIPPED", f"{reason}; not tracked")
            return
        if not self.paper.owns_position(wallet, coin, side):
            self.store.log_signal(wallet, coin, side, "EXIT", None, "SKIPPED", f"{reason}; ownership mismatch")
            print(f"[RECONCILE] Skip {coin} {side}: ownership mismatch")
            return

        price = self.platform.mid_price(coin)
        if price is None:
            self.store.log_signal(wallet, coin, side, "EXIT", None, "SKIPPED", f"{reason}; no price")
            print(f"[RECONCILE] Close skipped {coin} {side}: no price")
            return

        if not self.platform.close_position(coin):
            self.store.log_signal(wallet, coin, side, "EXIT", price, "SKIPPED", f"{reason}; live close failed")
            print(f"[RECONCILE] Close failed {coin} {side}: {reason}")
            return

        gain, pnl_pct, _ = self.paper.close(wallet, coin, side, price)
        self.store.log_signal(wallet, coin, side, "EXIT", price, "EXECUTED", reason, gain, pnl_pct)
        self.risk.maybe_pause_wallet(wallet, coin, pnl_pct, loss_threshold)
        print(f"[RECONCILE] Closed {coin} {side}: {reason}")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class CopyTradingBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.notifier = Notifier(settings.notify_webhook_url)
        self.platform = self._build_platform()
        self.paper = PaperPortfolio(settings, self.store)
        self.risk = RiskManager(settings, self.store, self.notifier)
        self.scoring_engine = ScoringEngine(settings, self.store)
        self.token_risk = TokenRiskMonitor(settings, self.store)
        self.roster = RosterService(settings, self.store, self.platform)
        self.monitor = WalletMonitor(settings, self.store, self.platform)
        self.reconciler = Reconciler(settings, self.store, self.platform, self.paper, self.risk)
        self.running = True

    def _build_platform(self) -> PlatformAdapter:
        if self.settings.platform == "hyperliquid":
            return HyperliquidAdapter(self.settings, self.store)
        raise SystemExit(f"Unsupported platform: {self.settings.platform}")

    def stop(self, *_args: Any) -> None:
        self.running = False
        print("\n[BOOT] Stop requested; finishing current cycle.")

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        print("[BOOT] MockingBot starting")
        print(f"[BOOT] platform={self.platform.name} live={self.settings.live} db={self.settings.db_path}")
        self.notifier.send("MockingBot started.")

        restart_count = 0
        while self.running:
            try:
                self._run_loop()
                break
            except Exception as exc:
                restart_count += 1
                wait = min(60 * restart_count, 300)
                print(f"[CRASH] {exc}; restarting in {wait}s")
                traceback.print_exc()
                self.notifier.send(f"MockingBot crash: {exc}. Restarting in {wait}s.")
                for _ in range(wait):
                    if not self.running:
                        break
                    time.sleep(1)
        self.notifier.send("MockingBot stopped.")
        print("[BOOT] Stopped")

    def _run_loop(self) -> None:
        wallets = self.roster.load_or_refresh(force=False)
        session_start = self.risk.session_start_value(self.paper, self.platform)
        last_reconcile = 0.0
        last_roster_check = unix_now()

        while self.running:
            cycle_start = unix_now()
            paper_value = self.paper.value(self.platform.mid_price)
            dd = self.risk.drawdown(session_start, paper_value)
            loss_threshold = self.risk.effective_loss_pct(dd)
            pause_hours = self.risk.effective_pause_hours(dd)
            wind_down = self.risk.is_wind_down() or self.risk.check_circuit_breaker(session_start, paper_value)

            roster_check_interval = (
                self.settings.roster_refresh_batch_seconds
                if self.roster.refresh_in_progress()
                else self.settings.roster_refresh_seconds
            )
            if unix_now() - last_roster_check >= roster_check_interval:
                wallets = self.roster.load_or_refresh(force=True, pause_hours=pause_hours)
                last_roster_check = unix_now()

            scan_wallets = self._effective_wallets(wallets)
            if unix_now() - last_reconcile >= self.settings.reconcile_seconds:
                self.reconciler.run(scan_wallets, loss_threshold)
                last_reconcile = unix_now()

            events, fail_ratio = self.monitor.scan(scan_wallets)
            if fail_ratio > self.settings.api_degraded_max_fail_ratio:
                print(f"[API] Degraded scan ({fail_ratio:.0%} failed); skipping signal execution this cycle")
                self._sleep_remaining(cycle_start)
                continue

            live_held: set[str] | None = set()
            if self.settings.live and events:
                live_positions = self.platform.live_positions()
                if live_positions is None:
                    print("[LIVE] Unable to read live positions; blocking ENTRY signals this cycle")
                    live_held = None
                else:
                    live_held = set(live_positions.keys())
            for event in events:
                if event.kind in {"ENTRY", "ADD"}:
                    self._handle_entry(event, wind_down, live_held)
                elif event.kind == "EXIT":
                    self._handle_exit(event, loss_threshold)

            tag = f" dd={dd:.1%}" if dd >= 0.01 else ""
            print(f"[{time.strftime('%H:%M:%S')}] wallets={len(scan_wallets)} roster={len(wallets)} events={len(events)} paper=${paper_value:,.2f}{tag}")
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

    def _handle_entry(self, event: CopyEvent, wind_down: bool, live_held: set[str] | None) -> None:
        self.token_risk.observe(event)
        if live_held is None:
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, None, "SKIPPED", "live positions unknown")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "live positions unknown", event.entry_price)
            print(f"[SKIP] {event.kind} {event.coin} {event.side}: live positions unknown")
            return

        scoring_decision, scoring_score = self.scoring_engine.active_entry_decision(event)
        if scoring_decision.action != "EXECUTE":
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, None, "SKIPPED", scoring_decision.reason)
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", scoring_decision.reason, event.entry_price)
            print(f"[SCORING] {event.kind} {event.coin} {event.side}: {scoring_decision.reason}")
            return

        is_add = event.kind == "ADD"
        decision = self.risk.allow_entry(
            event.wallet,
            event.coin,
            event.side,
            self.paper,
            wind_down,
            live_held,
            allow_same_wallet_add=is_add,
        )
        if decision.action != "EXECUTE":
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, None, "SKIPPED", decision.reason)
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", decision.reason, event.entry_price)
            print(f"[SKIP] {event.kind} {event.coin} {event.side}: {decision.reason}")
            return

        price = self.platform.mid_price(event.coin) or event.entry_price
        if not price:
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, None, "SKIPPED", "no price")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "no price", event.entry_price)
            print(f"[SKIP] {event.kind} {event.coin}: no price")
            return

        multiplier = self.scoring_engine.allocation_multiplier(scoring_score)
        allocation_reason = self.scoring_engine.allocation_note(scoring_score, multiplier)
        confirming = self.paper.position_side(event.coin, event.side) is not None
        cost = self.paper.available_slot(event.coin, price, multiplier, event.side)
        if cost is None:
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, price, "SKIPPED", "paper rejected")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "paper rejected", price)
            print(f"[SKIP] {event.kind} {event.coin}: paper rejected")
            return

        notional = cost * self.settings.leverage
        ok = self.platform.open_position(event.coin, event.side, notional, price)
        if not ok:
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, price, "SKIPPED", "live open failed")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "live open failed", price)
            print(f"[SKIP] {event.kind} {event.coin} {event.side}: live open failed")
            return

        opened_cost = self.paper.open(event.wallet, event.coin, event.side, price, cost, allow_same_wallet_add=is_add)
        if opened_cost is None:
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, price, "SKIPPED", "paper commit failed")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "paper commit failed", price)
            print(f"[WARN] {event.kind} {event.coin} {event.side}: live opened but paper commit failed")
            if not self.platform.close_position(event.coin):
                recovery_id = self.store.log_signal(
                    event.wallet,
                    event.coin,
                    event.side,
                    "EXIT",
                    price,
                    "SKIPPED",
                    "recovery close failed after paper commit failure",
                )
                self.scoring_engine.observe_signal(event, recovery_id, "SKIPPED", "recovery close failed after paper commit failure", price)
                print(f"[ALERT] {event.coin} {event.side}: live position may be unmanaged")
            else:
                recovery_event = CopyEvent("EXIT", event.wallet, event.coin, event.side)
                recovery_id = self.store.log_signal(
                    event.wallet,
                    event.coin,
                    event.side,
                    "EXIT",
                    price,
                    "EXECUTED",
                    "recovery close after paper commit failure",
                )
                self.scoring_engine.observe_signal(recovery_event, recovery_id, "EXECUTED", "recovery close after paper commit failure", price)
            return

        signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, price, "EXECUTED", allocation_reason)
        self.scoring_engine.observe_signal(event, signal_id, "EXECUTED", allocation_reason, price)
        action_label = "ADD" if is_add else ("REINFORCE" if confirming else "ENTRY")
        print(f"[{action_label}] {event.coin} {event.side} @ {price:,.4f} source={event.wallet[:16]} slot=${cost:.2f} {allocation_reason}")

    def _handle_exit(self, event: CopyEvent, loss_threshold: float) -> None:
        paper_pos = self.paper.position(event.coin)
        if paper_pos is None:
            shadow_price = self.platform.mid_price(event.coin)
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, "EXIT", None, "SKIPPED", "not tracked")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "not tracked", shadow_price)
            print(f"[EXIT] {event.coin} {event.side}: not tracked")
            return
        if not self.paper.owns_position(event.wallet, event.coin, event.side):
            shadow_price = self.platform.mid_price(event.coin)
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, "EXIT", None, "SKIPPED", "ownership mismatch")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "ownership mismatch", shadow_price)
            print(f"[EXIT] {event.coin} {event.side}: ownership mismatch")
            return

        price = self.platform.mid_price(event.coin)
        side = event.side
        if price is None:
            signal_id = self.store.log_signal(event.wallet, event.coin, side, "EXIT", None, "SKIPPED", "no price")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "no price", None)
            print(f"[EXIT] {event.coin} {side}: no price")
            return

        if not self.platform.close_position(event.coin):
            signal_id = self.store.log_signal(event.wallet, event.coin, side, "EXIT", price, "SKIPPED", "live close failed")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "live close failed", price)
            print(f"[EXIT] {event.coin} {side}: live close failed")
            return

        closed_any = False
        total_gain = 0.0
        last_pnl_pct: float | None = None
        side = event.side
        while self.paper.owns_position(event.wallet, event.coin, event.side):
            gain, pnl_pct, paper_side = self.paper.close(event.wallet, event.coin, event.side, price)
            side = paper_side or event.side
            if gain is None:
                break
            closed_any = True
            total_gain += gain
            last_pnl_pct = pnl_pct
            signal_id = self.store.log_signal(event.wallet, event.coin, side, "EXIT", price, "EXECUTED", "", gain, pnl_pct)
            self.scoring_engine.observe_signal(event, signal_id, "EXECUTED", "", price)
            self.risk.maybe_pause_wallet(event.wallet, event.coin, pnl_pct, loss_threshold)

        if not closed_any:
            signal_id = self.store.log_signal(event.wallet, event.coin, side, "EXIT", price, "SKIPPED", "not tracked")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "not tracked", price)
            print(f"[EXIT] {event.coin} {side}: not tracked")
            return

        pnl = "n/a" if last_pnl_pct is None else f"{last_pnl_pct:+.2f}%"
        print(f"[EXIT] {event.coin} {side} @ {price or 0:,.4f} pnl={pnl} paper=${total_gain:+.2f}")

    def _sleep_remaining(self, cycle_start: float) -> None:
        elapsed = unix_now() - cycle_start
        sleep_for = max(0.0, self.settings.poll_seconds - elapsed)
        end = unix_now() + sleep_for
        while self.running and unix_now() < end:
            time.sleep(min(1.0, end - unix_now()))


# ---------------------------------------------------------------------------
# Status helper
# ---------------------------------------------------------------------------


def print_status(settings: Settings) -> None:
    store = Store(settings.db_path)
    paper = PaperPortfolio(settings, store)
    print("\n=== MOCKINGBOT CODEX STATUS ===\n")
    print(f"Database: {settings.db_path}")
    print(f"Roster:   {len(store.roster())} wallet(s)")
    print(f"Paused:   {len(store.paused_wallets())} wallet(s)")
    acct = paper.account()
    print(f"Cash:     ${float(acct['cash']):,.2f}")
    print(f"Realized: ${float(acct.get('realized_pnl', 0)):,.2f}")
    print(f"Open:     {len(paper.positions())} position(s)")
    row = store.conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()
    print(f"Signals:  {row['n'] if row else 0}")
    print()


def export_signals_csv(settings: Settings, output: Path) -> None:
    store = Store(settings.db_path)
    rows = store.conn.execute("SELECT * FROM signals ORDER BY id").fetchall()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys() if rows else ["id"])
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    print(f"Exported {len(rows)} signal(s) to {output}")


def main(argv: list[str]) -> int:
    settings = Settings()
    if len(argv) > 1 and argv[1] == "status":
        print_status(settings)
        return 0
    if len(argv) > 1 and argv[1] == "export-signals":
        output = Path(argv[2]) if len(argv) > 2 else settings.data_dir / "signals.csv"
        export_signals_csv(settings, output)
        return 0
    log_handle, original_stdout, original_stderr = enable_monitor_log(settings)
    bot = CopyTradingBot(settings)
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
