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
import hashlib
import json
import math
import os
import signal
import sqlite3
import sys
import time
import traceback
import uuid
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


def load_main_credentials(path: Path) -> dict[str, str]:
    credentials = {"wallet": "", "api_wallet": "", "api_key": ""}
    if not path.exists():
        return credentials

    aliases = {
        "hl_wallet_address": "wallet",
        "wallet": "wallet",
        "address": "wallet",
        "hl_api_wallet_address": "api_wallet",
        "api_wallet": "api_wallet",
        "api_wallet_address": "api_wallet",
        "api wallet": "api_wallet",
        "api wallet address": "api_wallet",
        "hl_api_key": "api_key",
        "api_key": "api_key",
        "api key": "api_key",
        "private_key": "api_key",
        "private key": "api_key",
    }
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        separator = "=" if "=" in line else (":" if ":" in line else "")
        if not separator:
            continue
        key, value = line.split(separator, 1)
        target = aliases.get(key.strip().lower())
        if target:
            credentials[target] = value.strip().strip('"').strip("'")
    return credentials


MAIN_CREDENTIALS_PATH = Path(
    os.getenv(
        "MOCKINGBOT_MAIN_CREDENTIALS_PATH",
        str(ROOT / "MockingBot_Main_Live_Test.Hyper.txt"),
    )
).expanduser()
MAIN_CREDENTIALS = load_main_credentials(MAIN_CREDENTIALS_PATH)


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"Invalid integer setting {name}") from exc


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"Invalid numeric setting {name}") from exc


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean setting {name}")


@dataclass(frozen=True)
class Settings:
    platform: str = env_str("MOCKINGBOT_PLATFORM", "hyperliquid").lower()
    data_dir: Path = Path(
        env_str(
            "MOCKINGBOT_DATA_DIR",
            str(
                ROOT
                / (
                    "MockingBot_Main_Live_Test_Data"
                    if env_bool("HL_LIVE", False)
                    else "MockingBot_Data"
                )
            ),
        )
    )
    instance_id: str = env_str(
        "MOCKINGBOT_INSTANCE_ID",
        "live-main" if env_bool("HL_LIVE", False) else "paper-main",
    )
    scoring_seed_db_path: Path = Path(
        env_str("MOCKINGBOT_SCORING_SEED_DB", str(ROOT / "MockingBot_Data" / "mockingbot_codex.sqlite3"))
    )

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
    max_leverage_cap: int = env_int("MAX_LEVERAGE_CAP", 5)
    scoring_engine_default_candidate_leverage: int = env_int("SCORING_ENGINE_DEFAULT_CANDIDATE_LEVERAGE", 3)
    scoring_engine_candidate_leverage: int = env_int("SCORING_ENGINE_CANDIDATE_LEVERAGE", 3)
    scoring_engine_proven_candidate_leverage: int = env_int("SCORING_ENGINE_PROVEN_CANDIDATE_LEVERAGE", 3)
    scoring_engine_core_leverage: int = env_int("SCORING_ENGINE_CORE_LEVERAGE", 3)
    scoring_engine_elite_leverage: int = env_int("SCORING_ENGINE_ELITE_LEVERAGE", 3)
    max_positions: int = env_int("MAX_POSITIONS", 4 if env_bool("HL_LIVE", False) else 10)
    max_slices_per_coin: int = env_int("MAX_SLICES_PER_COIN", 5)
    max_coin_cost_multiplier: float = env_float("MAX_COIN_COST_MULT", 2.0)
    max_allocations_per_wallet_coin_side: int = env_int("MAX_ALLOCATIONS_PER_WALLET_COIN_SIDE", 2)
    same_wallet_add_threshold_pct: float = env_float("SAME_WALLET_ADD_THRESHOLD_PCT", 25.0)
    min_slot_usd: float = env_float("MIN_SLOT_USD", 5.0)
    min_order_notional: float = env_float("MIN_ORDER_NOTIONAL", 11.0)
    live_margin_reserve_pct: float = env_float("LIVE_MARGIN_RESERVE_PCT", 0.05)
    slippage: float = env_float("SLIPPAGE", 0.01)
    scoring_engine_default_candidate_multiplier: float = env_float(
        "SCORING_ENGINE_DEFAULT_CANDIDATE_MULT",
        env_float("MARSHAL_DEFAULT_CANDIDATE_MULT", 0.30),
    )
    scoring_engine_candidate_multiplier: float = env_float(
        "SCORING_ENGINE_CANDIDATE_MULT",
        env_float("MARSHAL_CANDIDATE_MULT", 0.50),
    )
    scoring_engine_proven_candidate_multiplier: float = env_float(
        "SCORING_ENGINE_PROVEN_CANDIDATE_MULT",
        env_float("MARSHAL_PROVEN_CANDIDATE_MULT", 0.70),
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
        env_int("MARSHAL_CANDIDATE_MAX_ALLOCATIONS", 1),
    )
    scoring_engine_proven_candidate_max_allocations: int = env_int(
        "SCORING_ENGINE_PROVEN_CANDIDATE_MAX_ALLOCATIONS",
        env_int("MARSHAL_PROVEN_CANDIDATE_MAX_ALLOCATIONS", 2),
    )

    live: bool = env_bool("HL_LIVE", False)
    wind_down: bool = env_bool("WIND_DOWN", False)
    max_drawdown_pct: float = env_float("MAX_DRAWDOWN_PCT", 0.25)
    warning_drawdown_pct: float = env_float("WARNING_DRAWDOWN_PCT", 0.15)
    min_loss_pct_to_pause: float = env_float("MIN_LOSS_PCT_TO_PAUSE", 1.0)
    pause_recent_exits: int = env_int("PAUSE_RECENT_EXITS", 3)
    pause_loss_count: int = env_int("PAUSE_LOSS_COUNT", 2)
    pause_cumulative_loss_pct: float = env_float("PAUSE_CUMULATIVE_LOSS_PCT", 2.0)
    pause_emergency_loss_pct: float = env_float("PAUSE_EMERGENCY_LOSS_PCT", 5.0)
    max_position_days: int = env_int("MAX_POSITION_DAYS", 7)

    hl_api_key: str = MAIN_CREDENTIALS["api_key"] or env_str("HL_API_KEY", "")
    hl_wallet_address: str = MAIN_CREDENTIALS["wallet"] or env_str("HL_WALLET_ADDRESS", "")
    hl_api_wallet_address: str = MAIN_CREDENTIALS["api_wallet"] or env_str("HL_API_WALLET_ADDRESS", "")
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


CODE_FINGERPRINT = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def settings_fingerprint(settings: Settings) -> str:
    values = {
        "platform": settings.platform,
        "poll_seconds": settings.poll_seconds,
        "roster_size": settings.roster_size,
        "max_follow": settings.max_follow,
        "min_sample": settings.min_sample,
        "min_win_rate": settings.min_win_rate,
        "min_profit_factor": settings.min_profit_factor,
        "leverage": settings.leverage,
        "max_leverage_cap": settings.max_leverage_cap,
        "default_candidate_leverage": settings.scoring_engine_default_candidate_leverage,
        "candidate_leverage": settings.scoring_engine_candidate_leverage,
        "proven_candidate_leverage": settings.scoring_engine_proven_candidate_leverage,
        "core_leverage": settings.scoring_engine_core_leverage,
        "elite_leverage": settings.scoring_engine_elite_leverage,
        "max_positions": settings.max_positions,
        "live_margin_reserve_pct": settings.live_margin_reserve_pct,
        "max_slices_per_coin": settings.max_slices_per_coin,
        "max_coin_cost_multiplier": settings.max_coin_cost_multiplier,
        "max_allocations_per_wallet_coin_side": settings.max_allocations_per_wallet_coin_side,
        "same_wallet_add_threshold_pct": settings.same_wallet_add_threshold_pct,
        "scoring_engine_active": settings.scoring_engine_active,
        "candidate_multiplier": settings.scoring_engine_candidate_multiplier,
        "proven_candidate_multiplier": settings.scoring_engine_proven_candidate_multiplier,
        "core_multiplier": settings.scoring_engine_core_multiplier,
        "elite_multiplier": settings.scoring_engine_elite_multiplier,
        "candidate_max_allocations": settings.scoring_engine_candidate_max_allocations,
        "proven_candidate_max_allocations": settings.scoring_engine_proven_candidate_max_allocations,
        "warning_drawdown_pct": settings.warning_drawdown_pct,
        "max_drawdown_pct": settings.max_drawdown_pct,
    }
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def validate_settings(settings: Settings) -> None:
    errors: list[str] = []
    if settings.max_positions <= 0:
        errors.append("MAX_POSITIONS must be positive")
    if settings.max_slices_per_coin <= 0:
        errors.append("MAX_SLICES_PER_COIN must be positive")
    if not 0.001 <= settings.slippage <= 0.02:
        errors.append("SLIPPAGE must be between 0.001 (0.1%) and 0.02 (2%)")
    if not 1 <= settings.max_leverage_cap <= 5:
        errors.append("MAX_LEVERAGE_CAP must be between 1 and 5")
    leverages = {
        "default Candidate": settings.scoring_engine_default_candidate_leverage,
        "Candidate": settings.scoring_engine_candidate_leverage,
        "proven Candidate": settings.scoring_engine_proven_candidate_leverage,
        "Core": settings.scoring_engine_core_leverage,
        "Elite": settings.scoring_engine_elite_leverage,
    }
    for tier, leverage in leverages.items():
        if not 1 <= leverage <= settings.max_leverage_cap:
            errors.append(f"{tier} leverage must be between 1 and MAX_LEVERAGE_CAP")
    if not 0 < settings.warning_drawdown_pct < settings.max_drawdown_pct < 1:
        errors.append("drawdown settings must satisfy 0 < warning < maximum < 1")
    if settings.poll_seconds <= 0 or settings.reconcile_seconds <= 0:
        errors.append("poll and reconciliation intervals must be positive")
    if settings.roster_refresh_seconds <= 0 or settings.roster_refresh_batch_seconds <= 0:
        errors.append("roster refresh intervals must be positive")
    if settings.min_order_notional <= 0 or settings.min_slot_usd <= 0:
        errors.append("minimum order and slot values must be positive")
    if not 0 <= settings.live_margin_reserve_pct < 1:
        errors.append("LIVE_MARGIN_RESERVE_PCT must be between 0 and 1")
    multipliers = (
        settings.scoring_engine_default_candidate_multiplier,
        settings.scoring_engine_candidate_multiplier,
        settings.scoring_engine_proven_candidate_multiplier,
        settings.scoring_engine_core_multiplier,
        settings.scoring_engine_elite_multiplier,
    )
    if settings.scoring_engine_max_slot_multiplier <= 0:
        errors.append("maximum slot multiplier must be positive")
    if any(value < 0 or value > settings.scoring_engine_max_slot_multiplier for value in multipliers):
        errors.append("tier allocation multipliers must be between 0 and the maximum slot multiplier")
    if settings.live and settings.db_path.resolve() == settings.scoring_seed_db_path.resolve():
        errors.append("live database must be distinct from the paper scoring database")
    if errors:
        raise ValueError("Invalid MockingBot configuration: " + "; ".join(errors))


class InstanceLock:
    """Atomic per-data-directory guard against duplicate trading processes."""

    def __init__(self, settings: Settings):
        self.path = settings.data_dir / "mockingbot.instance.lock"
        self.mode = "live" if settings.live else "paper"
        self.wallet = settings.hl_wallet_address
        self.token = uuid.uuid4().hex
        self.acquired = False

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _existing_owner(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "mode": self.mode,
            "wallet": self.wallet,
            "started_at": utc_now(),
            "token": self.token,
        }
        for _attempt in range(3):
            try:
                with self.path.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True)
                self.acquired = True
                return
            except FileExistsError:
                owner = self._existing_owner()
                owner_pid = int(owner.get("pid", 0) or 0)
                if self._process_alive(owner_pid):
                    owner_mode = str(owner.get("mode", "unknown"))
                    started = str(owner.get("started_at", "unknown"))
                    raise RuntimeError(
                        "MockingBot startup blocked: another "
                        f"{owner_mode} bot instance owns {self.path} "
                        f"(PID {owner_pid}, started {started})"
                    )
                if not owner:
                    try:
                        age_seconds = time.time() - self.path.stat().st_mtime
                    except FileNotFoundError:
                        continue
                    if age_seconds < 5:
                        time.sleep(0.05)
                        continue
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        raise RuntimeError(f"MockingBot startup blocked: unable to acquire {self.path}")

    def release(self) -> None:
        if not self.acquired:
            return
        owner = self._existing_owner()
        if owner.get("token") == self.token:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


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
class CapitalSnapshot:
    account_value: float
    total_margin_used: float
    withdrawable: float
    available_margin: float


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


@dataclass(frozen=True)
class ExecutionResult:
    accepted: bool
    requested_size: float = 0.0
    filled_size: float = 0.0
    avg_fill_price: float | None = None
    order_id: str | None = None
    status: str = ""
    confirmed: bool = False
    detail: str = ""

    def __bool__(self) -> bool:
        return self.accepted and self.confirmed


# ---------------------------------------------------------------------------
# Durable state and audit log
# ---------------------------------------------------------------------------


class ResilientConnection(sqlite3.Connection):
    """SQLite connection with bounded retry for transient lock contention."""

    lock_retry_attempts = 7
    lock_retry_initial_seconds = 0.05

    @staticmethod
    def _is_busy(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "database is locked" in message or "database is busy" in message

    def _retry(self, operation: Callable[[], Any]) -> Any:
        delay = self.lock_retry_initial_seconds
        for attempt in range(self.lock_retry_attempts):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not self._is_busy(exc) or attempt + 1 >= self.lock_retry_attempts:
                    raise
                if attempt == 0:
                    print("[DB] SQLite busy; retrying transaction")
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
        raise RuntimeError("unreachable SQLite retry state")

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return self._retry(lambda: super(ResilientConnection, self).execute(sql, parameters))

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> sqlite3.Cursor:
        parameters = list(seq_of_parameters)
        return self._retry(
            lambda: super(ResilientConnection, self).executemany(sql, parameters)
        )

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return self._retry(lambda: super(ResilientConnection, self).executescript(sql_script))

    def commit(self) -> None:
        self._retry(lambda: super(ResilientConnection, self).commit())

    def __exit__(self, exc_type: Any, exc_value: Any, traceback_value: Any) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(db_path),
            timeout=30.0,
            factory=ResilientConnection,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
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
                leverage REAL NOT NULL DEFAULT 3,
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

            CREATE TABLE IF NOT EXISTS scoring_seed_signals (
                source_signal_id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                wallet TEXT NOT NULL,
                signal TEXT NOT NULL,
                paper_gain REAL,
                pnl_pct REAL
            );

            CREATE TABLE IF NOT EXISTS decision_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                signal_id INTEGER,
                wallet TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                signal TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT,
                wallet_tier TEXT NOT NULL,
                wallet_score REAL NOT NULL,
                sample_size INTEGER NOT NULL,
                observed_price REAL,
                previous_size REAL,
                current_size REAL,
                config_fingerprint TEXT NOT NULL,
                code_fingerprint TEXT NOT NULL,
                scoring_seed_fingerprint TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_decision_audit_match
            ON decision_audit(wallet, coin, side, signal, ts);

            CREATE TABLE IF NOT EXISTS execution_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT,
                operation TEXT NOT NULL,
                requested_leverage REAL,
                leverage REAL,
                requested_size REAL,
                filled_size REAL,
                avg_fill_price REAL,
                reference_price REAL,
                slippage_bps REAL,
                price_source TEXT,
                order_id TEXT,
                exchange_status TEXT,
                confirmed INTEGER NOT NULL,
                detail TEXT
            );

            CREATE TABLE IF NOT EXISTS reconciliation_quarantine (
                coin TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                details TEXT,
                quarantined_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()
        self._ensure_column("paper_position_slices", "leverage", "REAL NOT NULL DEFAULT 3")
        self._ensure_column("execution_audit", "leverage", "REAL")
        self._ensure_column("execution_audit", "requested_leverage", "REAL")
        self._ensure_column("execution_audit", "reference_price", "REAL")
        self._ensure_column("execution_audit", "slippage_bps", "REAL")
        self._ensure_column("execution_audit", "price_source", "TEXT")
        self._migrate_legacy_paper_positions()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {str(row["name"]) for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            self.conn.commit()

    def bootstrap_scoring_history(self, source_path: Path) -> dict[str, Any]:
        existing = self.get_json("scoring_bootstrap", {})
        if existing:
            return existing
        if not source_path.exists() or source_path.resolve() == self.db_path.resolve():
            raise RuntimeError(f"Scoring bootstrap database unavailable: {source_path}")

        source = sqlite3.connect(f"file:{source_path.resolve().as_posix()}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            roster = source.execute(
                "SELECT wallet, status, score, updated_at FROM roster"
            ).fetchall()
            metrics = source.execute(
                "SELECT wallet, sample, win_rate, profit_factor, updated_at FROM roster_wallet_metrics"
            ).fetchall()
            signals = source.execute(
                """
                SELECT id, ts, wallet, signal, paper_gain, pnl_pct
                FROM signals
                WHERE action = 'EXECUTED'
                  AND (
                      signal IN ('ENTRY', 'ADD')
                      OR (signal = 'EXIT' AND pnl_pct IS NOT NULL)
                  )
                ORDER BY id
                """
            ).fetchall()
        finally:
            source.close()

        digest_rows = [tuple(row) for row in signals]
        digest = hashlib.sha256(
            json.dumps(digest_rows, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        metadata = {
            "imported_at": utc_now(),
            "source_path": str(source_path.resolve()),
            "source_signal_count": len(signals),
            "source_roster_count": len(roster),
            "fingerprint": digest,
        }
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO roster(wallet, status, score, updated_at) VALUES(?, ?, ?, ?)",
                [tuple(row) for row in roster],
            )
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO roster_wallet_metrics(
                    wallet, sample, win_rate, profit_factor, updated_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                [tuple(row) for row in metrics],
            )
            self.conn.executemany(
                """
                INSERT INTO scoring_seed_signals(
                    source_signal_id, ts, wallet, signal, paper_gain, pnl_pct
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                [tuple(row) for row in signals],
            )
            self.conn.execute(
                "INSERT INTO kv(key, value) VALUES('scoring_bootstrap', ?)",
                (json.dumps(metadata),),
            )
        return metadata

    def scoring_seed_fingerprint(self) -> str:
        return str(self.get_json("scoring_bootstrap", {}).get("fingerprint", ""))

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

    def position_leverage(self, coin: str) -> float | None:
        leverages = {
            round(float(row["leverage"]), 8)
            for row in self.open_position_slices(coin)
        }
        if not leverages:
            return None
        if len(leverages) != 1:
            raise RuntimeError(f"mixed local leverage recorded for {coin}: {sorted(leverages)}")
        return next(iter(leverages))

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
        self,
        coin: str,
        side: str,
        entry_price: float,
        cost_basis: float,
        source_wallet: str,
        leverage: float,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_position_slices(
                coin, side, source_wallet, entry_price, cost_basis, leverage, opened_at, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """,
            (coin, side, source_wallet, entry_price, cost_basis, leverage, utc_now()),
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
        self,
        coin: str,
        side: str,
        entry_price: float,
        cost_basis: float,
        source_wallet: str,
        leverage: float,
    ) -> None:
        self.insert_paper_position_slice(
            coin, side, entry_price, cost_basis, source_wallet, leverage
        )
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

    def log_execution(
        self,
        coin: str,
        side: str | None,
        operation: str,
        result: ExecutionResult,
        leverage: float | None = None,
        requested_leverage: float | None = None,
        reference_price: float | None = None,
        price_source: str | None = None,
    ) -> None:
        slippage_bps: float | None = None
        if reference_price and result.avg_fill_price and side in {"LONG", "SHORT"}:
            direction = -1.0 if side == "LONG" else 1.0
            slippage_bps = (
                (result.avg_fill_price - reference_price)
                / reference_price
                * direction
                * 10_000
            )
        self.conn.execute(
            """
            INSERT INTO execution_audit(
                ts, coin, side, operation, requested_leverage, leverage, requested_size, filled_size,
                avg_fill_price, reference_price, slippage_bps, price_source,
                order_id, exchange_status, confirmed, detail
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(), coin, side, operation, requested_leverage, leverage, result.requested_size,
                result.filled_size, result.avg_fill_price, reference_price,
                slippage_bps, price_source, result.order_id,
                result.status, 1 if result.confirmed else 0, result.detail,
            ),
        )
        self.conn.commit()

    def quarantine_coin(self, coin: str, reason: str, details: str = "") -> None:
        existing = self.coin_quarantine(coin)
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO reconciliation_quarantine(coin, reason, details, quarantined_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(coin) DO UPDATE SET
                reason = excluded.reason,
                details = excluded.details,
                updated_at = excluded.updated_at
            """,
            (coin, reason, details[:500], now, now),
        )
        self.conn.commit()
        if existing is None or existing["reason"] != reason or existing["details"] != details[:500]:
            print(f"[QUARANTINE] {coin}: {reason}; remaining book continues")

    def clear_coin_quarantine(self, coin: str) -> None:
        self.conn.execute("DELETE FROM reconciliation_quarantine WHERE coin = ?", (coin,))
        self.conn.commit()

    def coin_quarantine(self, coin: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM reconciliation_quarantine WHERE coin = ?", (coin,)
        ).fetchone()

    def quarantined_coins(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reconciliation_quarantine ORDER BY updated_at DESC"
        ).fetchall()

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

    def log_decision_audit(
        self,
        settings: Settings,
        event: "CopyEvent",
        signal_id: int | None,
        actual_action: str,
        actual_reason: str,
        score: "ScoringEngineScore",
        price: float | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO decision_audit(
                ts, instance_id, signal_id, wallet, coin, side, signal, action, reason,
                wallet_tier, wallet_score, sample_size, observed_price,
                previous_size, current_size, config_fingerprint, code_fingerprint,
                scoring_seed_fingerprint
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                settings.instance_id,
                signal_id,
                event.wallet,
                event.coin,
                event.side,
                event.kind,
                actual_action,
                actual_reason,
                score.tier,
                score.total_score,
                score.sample_size,
                price,
                event.previous_size,
                event.current_size,
                settings_fingerprint(settings),
                CODE_FINGERPRINT,
                self.scoring_seed_fingerprint(),
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
    def account_value(self) -> float | None:
        raise NotImplementedError

    @abstractmethod
    def live_positions(self) -> dict[str, Position] | None:
        raise NotImplementedError

    @abstractmethod
    def open_position(
        self, coin: str, side: str, notional_usd: float, price: float,
        leverage: int, requested_leverage: int | None = None,
    ) -> ExecutionResult:
        raise NotImplementedError

    @abstractmethod
    def close_position(
        self, coin: str, size: float | None = None,
        reference_price: float | None = None,
    ) -> ExecutionResult:
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
        self._max_leverage: dict[str, int] = {}

    def _post_info(self, payload: dict[str, Any], operation: str, subject: str = "") -> Any | None:
        def call() -> Any:
            r = requests.post(self.settings.hyperliquid_info_url, json=payload, timeout=12)
            r.raise_for_status()
            return r.json()

        return self.retry.request_json(operation, subject, call)

    def validate_live_credentials(self) -> None:
        if not self.settings.live:
            return
        if not self.settings.hl_wallet_address:
            raise RuntimeError("Live startup blocked: HL_WALLET_ADDRESS is missing")
        if not self.settings.hl_api_wallet_address:
            raise RuntimeError("Live startup blocked: HL_API_WALLET_ADDRESS is missing")
        if not self.settings.hl_api_key:
            raise RuntimeError("Live startup blocked: HL_API_KEY is missing")

        before: Position | None = None
        requested_size = 0.0
        try:
            import eth_account
        except ImportError as exc:
            raise RuntimeError("Live startup blocked: eth-account is not installed") from exc

        try:
            signer = eth_account.Account.from_key(self.settings.hl_api_key).address
        except Exception as exc:
            raise RuntimeError("Live startup blocked: HL_API_KEY is invalid") from exc
        if signer.lower() != self.settings.hl_api_wallet_address.lower():
            raise RuntimeError(
                "Live startup blocked: API key does not derive the configured API wallet"
            )

        role = self._post_info(
            {"type": "userRole", "user": signer},
            "validate_live_credentials",
            signer,
        )
        if not isinstance(role, dict) or role.get("role") != "agent":
            raise RuntimeError("Live startup blocked: configured API wallet is not an active agent")
        linked_user = role.get("data", {}).get("user")
        if not isinstance(linked_user, str) or linked_user.lower() != self.settings.hl_wallet_address.lower():
            raise RuntimeError(
                "Live startup blocked: API wallet is not linked to the configured trading account"
            )

        wallet = self.settings.hl_wallet_address
        print(
            f"[LIVE] Credentials verified account={wallet[:8]}...{wallet[-4:]} "
            f"agent={signer[:8]}...{signer[-4:]}"
        )

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
            name = str(asset["name"])
            self._sz_decimals[name] = int(asset.get("szDecimals", 4))
            self._max_leverage[name] = int(asset.get("maxLeverage", 1))
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

    def account_value(self) -> float | None:
        state = self._user_state()
        if not state:
            return None
        try:
            value = float(state.get("marginSummary", {}).get("accountValue") or 0)
            return value if value > 0 else None
        except Exception:
            return None

    def capital_snapshot(self) -> CapitalSnapshot | None:
        state = self._user_state()
        if not state:
            return None
        try:
            summary = state.get("marginSummary", {})
            account_value = float(summary.get("accountValue") or 0)
            total_margin_used = float(summary.get("totalMarginUsed") or 0)
            withdrawable = float(state.get("withdrawable") or 0)
            if account_value <= 0 or min(total_margin_used, withdrawable) < 0:
                return None
            return CapitalSnapshot(
                account_value=account_value,
                total_margin_used=total_margin_used,
                withdrawable=withdrawable,
                available_margin=max(
                    0.0, min(withdrawable, account_value - total_margin_used)
                ),
            )
        except (TypeError, ValueError):
            return None

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

    @staticmethod
    def _entry_position_delta(
        before: Position | None, after: Position | None, side: str
    ) -> tuple[float, float | None, str]:
        if after is None:
            return 0.0, None, "position unchanged"
        if after.side != side:
            return 0.0, None, f"confirmed side={after.side} expected={side}"
        if before is None:
            return after.size, after.entry_price, "new position measured"
        if before.side != side:
            return 0.0, None, f"pre-order side={before.side} expected={side}"
        delta = after.size - before.size
        if delta <= 0:
            return 0.0, None, f"position did not increase ({before.size:g}->{after.size:g})"
        fill_price = after.entry_price
        weighted_delta = after.entry_price * after.size - before.entry_price * before.size
        if weighted_delta > 0:
            fill_price = weighted_delta / delta
        return delta, fill_price, f"position increase measured ({before.size:g}->{after.size:g})"

    def open_position(
        self, coin: str, side: str, notional_usd: float, price: float,
        leverage: int, requested_leverage: int | None = None,
    ) -> ExecutionResult:
        requested_leverage = requested_leverage or leverage

        def reject(detail: str, requested_size: float = 0.0, status: str = "rejected") -> ExecutionResult:
            result = ExecutionResult(
                False, requested_size=requested_size, status=status, detail=detail
            )
            self.store.log_execution(
                coin, side, "OPEN", result, leverage, requested_leverage
            )
            return result

        try:
            existing_leverage = self.store.position_leverage(coin)
        except RuntimeError as exc:
            result = ExecutionResult(False, status="leverage_mismatch", detail=str(exc))
            self.store.quarantine_coin(coin, "mixed local leverage", str(exc))
            self.store.log_execution(coin, side, "OPEN", result, leverage, requested_leverage)
            return result
        if existing_leverage is not None and abs(existing_leverage - leverage) > 1e-8:
            detail = f"existing={existing_leverage:g}x effective={leverage:g}x"
            result = ExecutionResult(False, status="leverage_mismatch", detail=detail)
            self.store.quarantine_coin(coin, "position leverage mismatch", detail)
            self.store.log_execution(coin, side, "OPEN", result, leverage, requested_leverage)
            return result
        if notional_usd < self.settings.min_order_notional or price <= 0:
            return reject(
                f"requested notional ${notional_usd:.4f} is below "
                f"${self.settings.min_order_notional:.2f} minimum or price is invalid"
            )

        if self.settings.live:
            try:
                self._init_sdk()
            except Exception as exc:
                self.store.log_api_failure(self.name, "init_sdk", coin, str(exc))
                print(f"[LIVE] ENTRY failed {coin} {side}: {exc}")
                return reject(str(exc), status="init_failed")

            if coin not in self._sz_decimals or coin not in self._max_leverage:
                return reject(f"{coin} is absent from Hyperliquid asset metadata")
            asset_max_leverage = self._max_leverage[coin]
            if leverage > asset_max_leverage:
                return reject(
                    f"effective leverage {leverage}x exceeds {coin} maximum "
                    f"{asset_max_leverage}x"
                )

            capital = self.capital_snapshot()
            if capital is None:
                return reject(
                    "live buying power unavailable; no order submitted",
                    status="buying_power_unavailable",
                )
            reserve = capital.account_value * self.settings.live_margin_reserve_pct
            usable_margin = max(0.0, capital.available_margin - reserve)
            required_margin = notional_usd / leverage
            self.store.set_json(
                "live_capital_snapshot",
                {
                    "ts": utc_now(),
                    "account_value": capital.account_value,
                    "total_margin_used": capital.total_margin_used,
                    "withdrawable": capital.withdrawable,
                    "available_margin": capital.available_margin,
                    "reserve": reserve,
                    "usable_margin": usable_margin,
                    "required_margin": required_margin,
                },
            )
            if required_margin > usable_margin + 1e-8:
                return reject(
                    f"required margin ${required_margin:.2f} exceeds verified usable "
                    f"margin ${usable_margin:.2f} after ${reserve:.2f} reserve",
                    status="insufficient_buying_power",
                )

        decimals = self._sz_decimals.get(coin, 4)
        size = round(notional_usd / price, decimals)
        if size <= 0:
            return reject("rounded order size is zero")
        rounded_notional = size * price
        if rounded_notional < self.settings.min_order_notional:
            return reject(
                f"rounded notional ${rounded_notional:.4f} is below "
                f"${self.settings.min_order_notional:.2f} minimum",
                requested_size=size,
            )

        if not self.settings.live:
            print(f"[DRY] ENTRY {coin} {side} size={size} notional~${notional_usd:.2f}")
            result = ExecutionResult(True, size, size, price, status="paper", confirmed=True)
            self.store.log_execution(coin, side, "OPEN", result, leverage, requested_leverage)
            return result

        pre_state_available, before = self._confirmed_position(coin)
        if not pre_state_available:
            detail = "pre-order position state unavailable; no order submitted"
            self.store.quarantine_coin(coin, "pre-order state unavailable", detail)
            return reject(detail, requested_size=size, status="state_unavailable")
        if (existing_leverage is None) != (before is None):
            detail = (
                f"local_open={existing_leverage is not None} "
                f"live_open={before is not None}; no order submitted"
            )
            self.store.quarantine_coin(coin, "pre-order position mismatch", detail)
            return reject(detail, requested_size=size, status="state_mismatch")
        if before is not None and before.side != side:
            detail = f"pre-order live side={before.side} expected={side}; no order submitted"
            self.store.quarantine_coin(coin, "pre-order side mismatch", detail)
            return reject(detail, requested_size=size, status="state_mismatch")

        try:
            if existing_leverage is None:
                self._exchange.update_leverage(leverage, coin, is_cross=True)  # type: ignore[union-attr]
            result = self._exchange.market_open(  # type: ignore[union-attr]
                coin, side == "LONG", size, slippage=self.settings.slippage
            )
            execution = self._parse_execution_result(result, size)
            state_available, confirmed = self._confirmed_position(coin)
            if not state_available:
                detail = "post-order position state unavailable"
                execution = ExecutionResult(
                    execution.accepted, size, execution.filled_size,
                    execution.avg_fill_price, execution.order_id, execution.status,
                    False, detail,
                )
                self.store.quarantine_coin(coin, "entry confirmation mismatch", detail)
            else:
                fill_size, fill_price, detail = self._entry_position_delta(before, confirmed, side)
                tolerance = 10 ** (-decimals) / 2
                if fill_size > tolerance:
                    execution = ExecutionResult(
                        True, size, fill_size, fill_price or execution.avg_fill_price or price,
                        execution.order_id,
                        execution.status if execution.accepted else "recovered",
                        True, detail,
                    )
                    self.store.clear_coin_quarantine(coin)
                elif execution.accepted:
                    execution = ExecutionResult(
                        True, size, execution.filled_size, execution.avg_fill_price,
                        execution.order_id, execution.status, False, detail,
                    )
                    self.store.quarantine_coin(coin, "entry confirmation mismatch", detail)
            self.store.log_execution(coin, side, "OPEN", execution, leverage, requested_leverage)
            return execution
        except Exception as exc:
            self.store.log_api_failure(self.name, "open_position", coin, str(exc))
            state_available, confirmed = self._confirmed_position(coin)
            if not state_available:
                detail = f"ambiguous submission after error: {exc}; position state unavailable"
                self.store.quarantine_coin(coin, "ambiguous entry state", detail)
                result = ExecutionResult(False, size, status="ambiguous", detail=detail)
            else:
                fill_size, fill_price, delta_detail = self._entry_position_delta(before, confirmed, side)
                tolerance = 10 ** (-decimals) / 2
                if fill_size > tolerance:
                    detail = f"recovered after submission error: {exc}; {delta_detail}"
                    result = ExecutionResult(
                        True, size, fill_size, fill_price or price,
                        status="recovered", confirmed=True, detail=detail,
                    )
                    self.store.clear_coin_quarantine(coin)
                elif confirmed is not None and confirmed.side != side:
                    detail = f"ambiguous submission after error: {exc}; {delta_detail}"
                    self.store.quarantine_coin(coin, "ambiguous entry mismatch", detail)
                    result = ExecutionResult(False, size, status="ambiguous", detail=detail)
                else:
                    detail = f"submission failed with no position change: {exc}"
                    result = ExecutionResult(False, size, status="exception", detail=detail)
            print(f"[LIVE] ENTRY {coin} {side}: {result.detail}")
            self.store.log_execution(coin, side, "OPEN", result, leverage, requested_leverage)
            return result

    def _log_close_execution(
        self, coin: str, side: str | None, result: ExecutionResult,
        reference_price: float | None,
    ) -> None:
        if result.avg_fill_price is not None:
            price_source = "exchange_fill"
        elif result.accepted and result.confirmed and reference_price is not None:
            price_source = "midpoint_estimate"
        else:
            price_source = None
        self.store.log_execution(
            coin, side, "CLOSE", result,
            reference_price=reference_price, price_source=price_source,
        )

    def close_position(
        self, coin: str, size: float | None = None,
        reference_price: float | None = None,
    ) -> ExecutionResult:
        if not self.settings.live:
            result = ExecutionResult(
                True, requested_size=float(size or 0), status="paper", confirmed=True
            )
            self._log_close_execution(coin, None, result, reference_price)
            return result
        try:
            self._init_sdk()
            state_available, before = self._confirmed_position(coin)
        except Exception as exc:
            self.store.log_api_failure(self.name, "close_position", coin, str(exc))
            result = ExecutionResult(False, status="exception", detail=str(exc))
            self.store.quarantine_coin(coin, "close exception", str(exc))
            self._log_close_execution(coin, None, result, reference_price)
            return result
        if not state_available:
            result = ExecutionResult(
                False, status="state_unavailable", detail="pre-close position state unavailable"
            )
            self.store.quarantine_coin(coin, "close state unavailable", result.detail)
            self._log_close_execution(coin, None, result, reference_price)
            return result
        if before is None:
            result = ExecutionResult(
                True, status="already_flat", confirmed=True,
                detail="exchange already flat; local state may be cleared",
            )
            self.store.clear_coin_quarantine(coin)
            self._log_close_execution(coin, None, result, reference_price)
            return result

        decimals = self._sz_decimals.get(coin, 4)
        tolerance = 10 ** (-decimals) * 1.5
        requested_size = before.size if size is None else round(float(size), decimals)
        requested_size = min(requested_size, before.size)
        if requested_size <= 0:
            result = ExecutionResult(
                False, requested_size, status="rejected", detail="close size rounds to zero"
            )
            self._log_close_execution(coin, before.side, result, reference_price)
            return result

        def measure(remaining: Position | None) -> tuple[float, str]:
            if remaining is None:
                return before.size, "exchange is flat"
            if remaining.side != before.side or remaining.size > before.size + tolerance:
                return -1.0, (
                    f"before={before.side} {before.size:g}, "
                    f"after={remaining.side} {remaining.size:g}"
                )
            return max(0.0, before.size - remaining.size), f"remaining size={remaining.size:g}"

        response_execution = ExecutionResult(False, requested_size, status="unsubmitted")
        submission_error = ""
        try:
            response = self._exchange.market_close(  # type: ignore[union-attr]
                coin, sz=requested_size, slippage=self.settings.slippage
            )
            response_execution = self._parse_execution_result(response, requested_size)
        except Exception as exc:
            submission_error = str(exc)
            self.store.log_api_failure(self.name, "close_position", coin, submission_error)

        state_available, remaining = self._confirmed_position(coin)
        if not state_available:
            detail = "post-close position state unavailable"
            if submission_error:
                detail = f"ambiguous close after error: {submission_error}; {detail}"
            result = ExecutionResult(
                False, requested_size, response_execution.filled_size,
                response_execution.avg_fill_price, response_execution.order_id,
                "ambiguous", False, detail,
            )
            self.store.quarantine_coin(coin, "ambiguous close state", detail)
            self._log_close_execution(coin, before.side, result, reference_price)
            return result

        reduction, state_detail = measure(remaining)
        if reduction < 0 or reduction > requested_size + tolerance:
            detail = f"close reduction mismatch: requested={requested_size:g}; {state_detail}"
            result = ExecutionResult(False, requested_size, status="mismatch", detail=detail)
            self.store.quarantine_coin(coin, "close reduction mismatch", detail)
            self._log_close_execution(coin, before.side, result, reference_price)
            return result

        if tolerance < reduction < requested_size - tolerance:
            retry_size = round(requested_size - reduction, decimals)
            retry_error = ""
            try:
                retry_response = self._exchange.market_close(  # type: ignore[union-attr]
                    coin, sz=retry_size, slippage=self.settings.slippage
                )
                retry_execution = self._parse_execution_result(retry_response, retry_size)
                if retry_execution.avg_fill_price:
                    prior_qty = response_execution.filled_size
                    retry_qty = retry_execution.filled_size
                    combined_qty = prior_qty + retry_qty
                    combined_price = retry_execution.avg_fill_price
                    if response_execution.avg_fill_price and combined_qty > 0:
                        combined_price = (
                            response_execution.avg_fill_price * prior_qty
                            + retry_execution.avg_fill_price * retry_qty
                        ) / combined_qty
                    response_execution = ExecutionResult(
                        response_execution.accepted or retry_execution.accepted,
                        requested_size, combined_qty, combined_price,
                        retry_execution.order_id or response_execution.order_id,
                        retry_execution.status or response_execution.status,
                    )
            except Exception as exc:
                retry_error = str(exc)
                self.store.log_api_failure(
                    self.name, "close_position_residual", coin, retry_error
                )
            final_available, final_remaining = self._confirmed_position(coin)
            if not final_available:
                detail = "residual close state unavailable"
                if retry_error:
                    detail += f" after error: {retry_error}"
                result = ExecutionResult(
                    False, requested_size, reduction, status="partial", detail=detail
                )
                self.store.quarantine_coin(coin, "residual live position", detail)
                self._log_close_execution(coin, before.side, result, reference_price)
                return result
            reduction, state_detail = measure(final_remaining)

        success = abs(reduction - requested_size) <= tolerance
        if success:
            detail = (
                f"verified reduction={reduction:g}; "
                f"remaining={max(0.0, before.size - reduction):g}"
            )
            if submission_error:
                detail = f"recovered after response error: {submission_error}; {detail}"
            result = ExecutionResult(
                True, requested_size, reduction,
                response_execution.avg_fill_price, response_execution.order_id,
                "recovered"
                if submission_error or not response_execution.accepted
                else response_execution.status,
                True, detail,
            )
            if before.size - reduction <= tolerance:
                self.store.clear_coin_quarantine(coin)
        elif reduction <= tolerance and submission_error:
            result = ExecutionResult(
                False, requested_size, status="exception",
                detail=f"close submission failed with no position change: {submission_error}",
            )
        else:
            detail = f"requested reduction={requested_size:g}; measured={reduction:g}; {state_detail}"
            result = ExecutionResult(
                False, requested_size, max(0.0, reduction),
                response_execution.avg_fill_price, response_execution.order_id,
                "partial", False, detail,
            )
            self.store.quarantine_coin(coin, "residual live position", detail)
        self._log_close_execution(coin, before.side, result, reference_price)
        return result

    def _confirmed_position(self, coin: str, attempts: int = 3) -> tuple[bool, Position | None]:
        for attempt in range(attempts):
            positions = self.live_positions()
            if positions is not None:
                position = positions.get(coin)
                if position is not None or attempt + 1 >= attempts:
                    return True, position
            if attempt + 1 < attempts:
                time.sleep(0.5)
        return False, None

    @staticmethod
    def _parse_execution_result(result: Any, requested_size: float) -> ExecutionResult:
        if not isinstance(result, dict) or result.get("status") != "ok":
            return ExecutionResult(False, requested_size, status="rejected", detail=str(result)[:500])
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        status = statuses[0] if statuses else None
        if not isinstance(status, dict) or "error" in status:
            detail = str(status.get("error", status) if isinstance(status, dict) else status)
            return ExecutionResult(False, requested_size, status="error", detail=detail[:500])
        filled = status.get("filled") or status.get("filledWith")
        if not isinstance(filled, dict):
            return ExecutionResult(False, requested_size, status="unfilled", detail=str(status)[:500])
        try:
            filled_size = float(filled.get("totalSz") or filled.get("sz") or 0)
            avg_price = float(filled.get("avgPx") or filled.get("px") or 0) or None
        except (TypeError, ValueError):
            return ExecutionResult(False, requested_size, status="invalid_fill", detail=str(filled)[:500])
        order_id = filled.get("oid")
        return ExecutionResult(
            filled_size > 0,
            requested_size,
            filled_size,
            avg_price,
            None if order_id is None else str(order_id),
            "filled" if filled_size > 0 else "unfilled",
            False,
        )

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

    def allocation_position_size(self, wallet: str, coin: str, side: str) -> float:
        return sum(
            float(row["cost_basis"])
            * float(row["leverage"])
            / float(row["entry_price"])
            for row in self.store.open_position_slices(coin)
            if row["source_wallet"] == wallet
            and row["side"] == side
            and float(row["entry_price"]) > 0
        )

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
                leverage = float(pos["leverage"]) if "leverage" in pos.keys() else float(self.settings.leverage)
                total += cost + cost * leverage * pct
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
        leverage: float | None = None,
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
        requested_position_leverage = float(
            leverage if leverage is not None else self.settings.leverage
        )
        existing_position_leverage = self.store.position_leverage(coin)
        position_leverage = (
            existing_position_leverage
            if existing_position_leverage is not None
            else requested_position_leverage
        )
        self.store.upsert_paper_position(
            coin, side, price, round(slot, 2), wallet, position_leverage
        )
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
            leverage = float(pos["leverage"]) if "leverage" in pos.keys() else float(self.settings.leverage)
            gain = round(cost * leverage * (pnl_pct / 100), 2)

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
        self._warning_active = False
        self._live_equity_unavailable = False

    def session_start_value(self, portfolio: PaperPortfolio, platform: PlatformAdapter) -> float | None:
        if self.settings.live:
            stored = self.store.get_json("live_risk_baseline", {})
            if (
                stored.get("wallet") == self.settings.hl_wallet_address
                and float(stored.get("start_value", 0) or 0) > 0
            ):
                return float(stored["start_value"])

            start = platform.account_value()
            if start is None:
                return None
            self.store.set_json(
                "live_risk_baseline",
                {
                    "started": utc_now(),
                    "wallet": self.settings.hl_wallet_address,
                    "start_value": start,
                },
            )
            return start

        start = portfolio.value(platform.mid_price)
        self.store.set_json("session", {"started": utc_now(), "paper_start": start})
        return start

    def current_value(self, portfolio: PaperPortfolio, platform: PlatformAdapter) -> float | None:
        if self.settings.live:
            return platform.account_value()
        return portfolio.value(platform.mid_price)

    def live_equity_available(self, available: bool) -> None:
        if not self.settings.live:
            return
        if not available and not self._live_equity_unavailable:
            msg = "[LIVE-RISK] Account equity unavailable; blocking new entries until it recovers."
            print(msg)
            self.notifier.send(msg)
        elif available and self._live_equity_unavailable:
            msg = "[LIVE-RISK] Account equity feed recovered."
            print(msg)
            self.notifier.send(msg)
        self._live_equity_unavailable = not available

    def is_wind_down(self) -> bool:
        return self.settings.wind_down or self.settings.circuit_breaker_file.exists()

    def drawdown(self, start_value: float, current_value: float) -> float:
        if start_value <= 0:
            return 0.0
        return max(0.0, (start_value - current_value) / start_value)

    def check_warning(self, drawdown: float) -> None:
        warning = drawdown >= self.settings.warning_drawdown_pct
        if warning and not self._warning_active:
            msg = (
                f"[RISK-WARNING] Drawdown {drawdown:.1%} reached warning level "
                f"{self.settings.warning_drawdown_pct:.0%}; trading continues."
            )
            print(msg)
            self.notifier.send(msg)
        elif not warning and self._warning_active:
            msg = f"[RISK] Drawdown recovered below {self.settings.warning_drawdown_pct:.0%}."
            print(msg)
            self.notifier.send(msg)
        self._warning_active = warning

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
            "reason": "live account drawdown" if self.settings.live else "paper drawdown",
            "mode": "live" if self.settings.live else "paper",
            "wallet": self.settings.hl_wallet_address if self.settings.live else "",
            "start_value": round(start_value, 2),
            "current_value": round(current_value, 2),
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
        quarantine = self.store.coin_quarantine(coin)
        if quarantine is not None:
            return TradeDecision("SKIP", f"coin quarantined: {quarantine['reason']}")
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
            FROM (
                SELECT paper_gain, pnl_pct, 0 AS source_order, source_signal_id AS sort_id
                FROM scoring_seed_signals
                WHERE wallet = ? AND signal = 'EXIT' AND pnl_pct IS NOT NULL
                UNION ALL
                SELECT paper_gain, pnl_pct, 1 AS source_order, id AS sort_id
                FROM signals
                WHERE wallet = ?
                  AND signal = 'EXIT'
                  AND action = 'EXECUTED'
                  AND pnl_pct IS NOT NULL
            )
            ORDER BY source_order, sort_id
            """,
            (wallet, wallet),
        ).fetchall()
        entry_count = int(
            self.store.conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM scoring_seed_signals WHERE wallet = ? AND signal = 'ENTRY')
                    +
                    (SELECT COUNT(*) FROM signals
                     WHERE wallet = ? AND signal = 'ENTRY' AND action = 'EXECUTED') AS n
                """,
                (wallet, wallet),
            ).fetchone()["n"]
        )
        add_count = int(
            self.store.conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM scoring_seed_signals WHERE wallet = ? AND signal = 'ADD')
                    +
                    (SELECT COUNT(*) FROM signals
                     WHERE wallet = ? AND signal = 'ADD' AND action = 'EXECUTED') AS n
                """,
                (wallet, wallet),
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

    def leverage_for_score(self, score: ScoringEngineScore) -> int:
        if score.tier == "Elite":
            raw = self.settings.scoring_engine_elite_leverage
        elif score.tier == "Core":
            raw = self.settings.scoring_engine_core_leverage
        elif score.tier == "Candidate":
            if score.sample_size < 3:
                raw = self.settings.scoring_engine_default_candidate_leverage
            elif score.total_score >= 55.0 and score.realized_pnl > 0:
                raw = self.settings.scoring_engine_proven_candidate_leverage
            else:
                raw = self.settings.scoring_engine_candidate_leverage
        else:
            raw = self.settings.scoring_engine_default_candidate_leverage
        return max(1, min(int(raw), self.settings.max_leverage_cap))

    def allocation_note(
        self, score: ScoringEngineScore, multiplier: float, leverage: int
    ) -> str:
        return (
            f"Scoring Engine {score.tier} x{multiplier:.2f} leverage={leverage}x: "
            f"{score.explanation}"
        )

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
        self.store.log_decision_audit(
            self.settings,
            event,
            signal_id,
            actual_action,
            actual_reason,
            score,
            price,
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

        close_size = self.paper.allocation_position_size(wallet, coin, side)
        execution = self.platform.close_position(coin, close_size, price)
        if not execution:
            self.store.log_signal(wallet, coin, side, "EXIT", price, "SKIPPED", f"{reason}; live close failed")
            print(f"[RECONCILE] Close failed {coin} {side}: {reason}")
            return

        exit_price = execution.avg_fill_price or price
        price_source = "exchange_fill" if execution.avg_fill_price else "midpoint_estimate"
        total_gain = 0.0
        last_pnl_pct: float | None = None
        while self.paper.owns_position(wallet, coin, side):
            gain, pnl_pct, _ = self.paper.close(wallet, coin, side, exit_price)
            if gain is None:
                break
            total_gain += gain
            last_pnl_pct = pnl_pct
            self.store.log_signal(
                wallet, coin, side, "EXIT", exit_price, "EXECUTED",
                f"{reason}; price_source={price_source} quote={price:g}", gain, pnl_pct
            )
        self.risk.maybe_pause_wallet(wallet, coin, last_pnl_pct, loss_threshold)
        print(f"[RECONCILE] Closed {coin} {side}: {reason}")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class CopyTradingBot:
    def __init__(self, settings: Settings):
        validate_settings(settings)
        self.settings = settings
        self.store = Store(settings.db_path)
        if settings.live:
            bootstrap = self.store.bootstrap_scoring_history(settings.scoring_seed_db_path)
            print(
                f"[BOOTSTRAP] Loaded {bootstrap['source_signal_count']} scoring events "
                f"from paper history fingerprint={bootstrap['fingerprint'][:12]}"
            )
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
        if self.settings.live and isinstance(self.platform, HyperliquidAdapter):
            self.platform.validate_live_credentials()
            self._validate_live_state()
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

    def _validate_live_state(self) -> None:
        identity = self.store.get_json("live_account_identity", {})
        wallet = self.settings.hl_wallet_address
        if identity:
            if str(identity.get("wallet", "")).lower() != wallet.lower():
                raise RuntimeError("Live startup blocked: database belongs to a different account")
            return

        if self.store.open_position_slices():
            raise RuntimeError("Live startup blocked: new live database contains local open positions")
        live_positions = self.platform.live_positions()
        if live_positions is None:
            raise RuntimeError("Live startup blocked: unable to verify initial live positions")
        if live_positions:
            raise RuntimeError(
                "Live startup blocked: first run requires a flat Hyperliquid perpetuals account"
            )
        account_value = self.platform.account_value()
        if account_value is None or account_value <= 0:
            raise RuntimeError(
                "Live startup blocked: perpetuals account equity is unavailable or zero"
            )
        self.store.save_paper_account({"cash": round(account_value, 2), "realized_pnl": 0.0})
        self.store.set_json(
            "live_account_identity",
            {
                "wallet": wallet,
                "api_wallet": self.settings.hl_api_wallet_address,
                "initialized_at": utc_now(),
                "initial_account_value": round(account_value, 2),
                "scoring_seed_fingerprint": self.store.scoring_seed_fingerprint(),
            },
        )
        print(
            f"[LIVE] Fresh isolated state verified; new signals only; "
            f"allocation basis=${account_value:,.2f}"
        )

    def _run_loop(self) -> None:
        wallets = self.roster.load_or_refresh(force=False)
        session_start = self.risk.session_start_value(self.paper, self.platform)
        last_reconcile = 0.0
        last_roster_check = unix_now()

        while self.running:
            cycle_start = unix_now()
            paper_value = self.paper.value(self.platform.mid_price)
            risk_value = self.risk.current_value(self.paper, self.platform)
            if session_start is None and risk_value is not None:
                session_start = self.risk.session_start_value(self.paper, self.platform)
            equity_available = session_start is not None and risk_value is not None
            self.risk.live_equity_available(equity_available)
            dd = self.risk.drawdown(session_start, risk_value) if equity_available else 0.0
            self.risk.check_warning(dd)
            loss_threshold = self.risk.effective_loss_pct(dd)
            pause_hours = self.risk.effective_pause_hours(dd)
            breaker_tripped = (
                self.risk.check_circuit_breaker(session_start, risk_value)
                if equity_available
                else False
            )
            wind_down = self.risk.is_wind_down() or breaker_tripped or (
                self.settings.live and not equity_available
            )

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
            if self.settings.live:
                live_positions = self.platform.live_positions()
                if live_positions is None:
                    print("[LIVE] Unable to read live positions; blocking ENTRY signals this cycle")
                    live_held = None
                else:
                    self._reconcile_live_book(live_positions)
                    live_held = set(live_positions.keys())
            for event in events:
                if event.kind in {"ENTRY", "ADD"}:
                    self._handle_entry(event, wind_down, live_held)
                elif event.kind == "EXIT":
                    self._handle_exit(event, loss_threshold)

            tag = f" dd={dd:.1%}" if dd >= 0.01 else ""
            live_tag = f" live=${risk_value:,.2f}" if self.settings.live and risk_value is not None else ""
            print(f"[{time.strftime('%H:%M:%S')}] wallets={len(scan_wallets)} roster={len(wallets)} events={len(events)} paper=${paper_value:,.2f}{live_tag}{tag}")
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

    def _reconcile_live_book(self, live_positions: dict[str, Position]) -> None:
        local_positions = self.paper.positions()
        quarantined = {str(row["coin"]) for row in self.store.quarantined_coins()}
        for coin in sorted(set(local_positions) | set(live_positions) | quarantined):
            local = local_positions.get(coin)
            live = live_positions.get(coin)
            if local is None and live is None:
                self.store.clear_coin_quarantine(coin)
                continue
            if local is None and live is not None:
                self.store.quarantine_coin(
                    coin, "unowned live position", f"side={live.side} size={live.size}"
                )
                continue
            if local is not None and live is None:
                self.store.quarantine_coin(coin, "local position missing live", "exchange is flat")
                continue
            if local is None or live is None:
                continue
            if str(local["side"]) != live.side:
                self.store.quarantine_coin(
                    coin,
                    "live side mismatch",
                    f"local={local['side']} live={live.side}",
                )
                continue
            expected_size = sum(
                float(row["cost_basis"])
                * float(row["leverage"])
                / float(row["entry_price"])
                for row in self.store.open_position_slices(coin)
                if float(row["entry_price"]) > 0
            )
            if isinstance(self.platform, HyperliquidAdapter):
                rounding_tolerance = 10 ** (-self.platform._sz_decimals.get(coin, 4)) * 2
            else:
                rounding_tolerance = 0.0
            tolerance = max(expected_size * 0.05, rounding_tolerance)
            if expected_size > 0 and abs(live.size - expected_size) > tolerance:
                self.store.quarantine_coin(
                    coin,
                    "live size mismatch",
                    f"local={expected_size:.10g} live={live.size:.10g}",
                )
                continue
            self.store.clear_coin_quarantine(coin)

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
        tier_leverage = self.scoring_engine.leverage_for_score(scoring_score)
        try:
            existing_leverage = self.store.position_leverage(event.coin)
        except RuntimeError as exc:
            self.store.quarantine_coin(event.coin, "mixed local leverage", str(exc))
            signal_id = self.store.log_signal(
                event.wallet, event.coin, event.side, event.kind, price, "SKIPPED", str(exc)
            )
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", str(exc), price)
            print(f"[SKIP] {event.kind} {event.coin}: {exc}")
            return
        effective_leverage = int(existing_leverage) if existing_leverage is not None else tier_leverage
        allocation_reason = self.scoring_engine.allocation_note(
            scoring_score, multiplier, effective_leverage
        )
        if effective_leverage != tier_leverage:
            allocation_reason += (
                f"; requested={tier_leverage}x inherited={effective_leverage}x"
            )
        confirming = self.paper.position_side(event.coin, event.side) is not None
        cost = self.paper.available_slot(event.coin, price, multiplier, event.side)
        if cost is None:
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, price, "SKIPPED", "paper rejected")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "paper rejected", price)
            print(f"[SKIP] {event.kind} {event.coin}: paper rejected")
            return

        notional = cost * effective_leverage
        execution = self.platform.open_position(
            event.coin, event.side, notional, price, effective_leverage, tier_leverage
        )
        if self.settings.live:
            capital_snapshot = self.store.get_json("live_capital_snapshot", {})
            if capital_snapshot:
                local_value = self.paper.value(self.platform.mid_price)
                capital_snapshot["local_estimated_value"] = local_value
                capital_snapshot["equity_variance"] = round(
                    float(capital_snapshot.get("account_value", 0)) - local_value, 2
                )
                self.store.set_json("live_capital_snapshot", capital_snapshot)
        if not execution:
            failure_reason = execution.detail or execution.status or "live open failed"
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, price, "SKIPPED", failure_reason)
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", failure_reason, price)
            print(f"[SKIP] {event.kind} {event.coin} {event.side}: {failure_reason}")
            return

        actual_price = execution.avg_fill_price or price
        actual_cost = cost
        if self.settings.live and execution.filled_size > 0 and actual_price > 0:
            actual_cost = execution.filled_size * actual_price / effective_leverage
        opened_cost = self.paper.open(
            event.wallet,
            event.coin,
            event.side,
            actual_price,
            actual_cost,
            allow_same_wallet_add=is_add,
            leverage=effective_leverage,
        )
        if opened_cost is None:
            signal_id = self.store.log_signal(event.wallet, event.coin, event.side, event.kind, price, "SKIPPED", "paper commit failed")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "paper commit failed", price)
            print(f"[WARN] {event.kind} {event.coin} {event.side}: live opened but paper commit failed")
            if not self.platform.close_position(
                event.coin, execution.filled_size, price
            ):
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

        close_size = self.paper.allocation_position_size(
            event.wallet, event.coin, event.side
        )
        execution = self.platform.close_position(event.coin, close_size, price)
        if not execution:
            signal_id = self.store.log_signal(event.wallet, event.coin, side, "EXIT", price, "SKIPPED", "live close failed")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "live close failed", price)
            print(f"[EXIT] {event.coin} {side}: live close failed")
            return

        exit_price = execution.avg_fill_price or price
        price_source = "exchange_fill" if execution.avg_fill_price else "midpoint_estimate"
        closed_any = False
        total_gain = 0.0
        last_pnl_pct: float | None = None
        side = event.side
        while self.paper.owns_position(event.wallet, event.coin, event.side):
            gain, pnl_pct, paper_side = self.paper.close(
                event.wallet, event.coin, event.side, exit_price
            )
            side = paper_side or event.side
            if gain is None:
                break
            closed_any = True
            total_gain += gain
            last_pnl_pct = pnl_pct
            close_reason = f"price_source={price_source} quote={price:g}"
            signal_id = self.store.log_signal(event.wallet, event.coin, side, "EXIT", exit_price, "EXECUTED", close_reason, gain, pnl_pct)
            self.scoring_engine.observe_signal(event, signal_id, "EXECUTED", close_reason, exit_price)
            self.risk.maybe_pause_wallet(event.wallet, event.coin, pnl_pct, loss_threshold)

        if not closed_any:
            signal_id = self.store.log_signal(event.wallet, event.coin, side, "EXIT", price, "SKIPPED", "not tracked")
            self.scoring_engine.observe_signal(event, signal_id, "SKIPPED", "not tracked", price)
            print(f"[EXIT] {event.coin} {side}: not tracked")
            return

        pnl = "n/a" if last_pnl_pct is None else f"{last_pnl_pct:+.2f}%"
        print(f"[EXIT] {event.coin} {side} @ {exit_price:,.4f} pnl={pnl} paper=${total_gain:+.2f} source={price_source}")

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
    print(f"Quarantine: {len(store.quarantined_coins())} coin(s)")
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
    with InstanceLock(settings):
        log_handle, original_stdout, original_stderr = enable_monitor_log(settings)
        try:
            bot = CopyTradingBot(settings)
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
