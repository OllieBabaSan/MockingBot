"""
MockingBot
==========

A clean, platform-aware copy-trading bot core.

Design goals:
  - Platform agnostic engine: the bot talks to a PlatformAdapter interface.
  - Hyperliquid implementation: the current concrete adapter uses Hyperliquid APIs.
  - Unattended safeguards: paper/live drawdown breakers, wind-down mode,
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
import threading
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
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
    backup_interval_seconds: int = env_int("BACKUP_INTERVAL_SECS", 6 * 3600)
    backup_retention_count: int = env_int("BACKUP_RETENTION_COUNT", 14)
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
    live_size_tolerance_pct: float = env_float("LIVE_SIZE_TOLERANCE_PCT", 0.01)
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
    scoring_reference_margin_usd: float = env_float(
        "SCORING_REFERENCE_MARGIN_USD", 1_000.0
    )
    scoring_reference_leverage: float = env_float(
        "SCORING_REFERENCE_LEVERAGE", 3.0
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

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


CODE_FINGERPRINT = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def settings_signature(settings: Settings) -> dict[str, Any]:
    return {
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
        "live_size_tolerance_pct": settings.live_size_tolerance_pct,
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
        "scoring_reference_margin_usd": settings.scoring_reference_margin_usd,
        "scoring_reference_leverage": settings.scoring_reference_leverage,
        "warning_drawdown_pct": settings.warning_drawdown_pct,
        "max_drawdown_pct": settings.max_drawdown_pct,
    }


PARITY_ENVIRONMENT_FIELDS = {
    "platform",
    "poll_seconds",
    "roster_size",
    "max_follow",
    "max_positions",
    "live_margin_reserve_pct",
    "live_size_tolerance_pct",
}


def _fingerprint(values: dict[str, Any]) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def settings_fingerprint(settings: Settings) -> str:
    return _fingerprint(settings_signature(settings))


def parity_policy_fingerprint(settings: Settings) -> str:
    values = settings_signature(settings)
    return _fingerprint(
        {key: value for key, value in values.items() if key not in PARITY_ENVIRONMENT_FIELDS}
    )


def parity_environment_fingerprint(settings: Settings) -> str:
    values = settings_signature(settings)
    return _fingerprint(
        {key: values[key] for key in sorted(PARITY_ENVIRONMENT_FIELDS)}
    )


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
    if settings.backup_interval_seconds <= 0:
        errors.append("BACKUP_INTERVAL_SECS must be positive")
    if settings.backup_retention_count < 2:
        errors.append("BACKUP_RETENTION_COUNT must be at least 2")
    if settings.roster_refresh_seconds <= 0 or settings.roster_refresh_batch_seconds <= 0:
        errors.append("roster refresh intervals must be positive")
    if settings.min_order_notional <= 0 or settings.min_slot_usd <= 0:
        errors.append("minimum order and slot values must be positive")
    if not 0 <= settings.live_margin_reserve_pct < 1:
        errors.append("LIVE_MARGIN_RESERVE_PCT must be between 0 and 1")
    if not 0 < settings.live_size_tolerance_pct <= 0.01:
        errors.append("LIVE_SIZE_TOLERANCE_PCT must be greater than 0 and at most 0.01")
    multipliers = (
        settings.scoring_engine_default_candidate_multiplier,
        settings.scoring_engine_candidate_multiplier,
        settings.scoring_engine_proven_candidate_multiplier,
        settings.scoring_engine_core_multiplier,
        settings.scoring_engine_elite_multiplier,
    )
    if settings.scoring_engine_max_slot_multiplier <= 0:
        errors.append("maximum slot multiplier must be positive")
    if settings.scoring_reference_margin_usd <= 0 or settings.scoring_reference_leverage <= 0:
        errors.append("scoring reference margin and leverage must be positive")
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
    leverage: float | None = None


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
    event_id: int | None = None


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

            CREATE TABLE IF NOT EXISTS pending_copy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at TEXT NOT NULL,
                wallet TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                kind TEXT NOT NULL,
                entry_price REAL,
                previous_size REAL,
                current_size REAL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                handled_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_pending_copy_events_status
            ON pending_copy_events(status, id);

            CREATE TABLE IF NOT EXISTS execution_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_key TEXT NOT NULL UNIQUE,
                cloid TEXT NOT NULL UNIQUE,
                operation TEXT NOT NULL,
                coin TEXT NOT NULL,
                side TEXT,
                requested_size REAL NOT NULL,
                leverage REAL,
                pre_side TEXT,
                pre_size REAL NOT NULL DEFAULT 0,
                pre_entry_price REAL,
                state TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_execution_intents_state
            ON execution_intents(state, id);

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
                policy_fingerprint TEXT,
                environment_fingerprint TEXT,
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
        self._ensure_column("execution_intents", "leverage", "REAL")
        self._ensure_column("decision_audit", "policy_fingerprint", "TEXT")
        self._ensure_column("decision_audit", "environment_fingerprint", "TEXT")
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

    def create_verified_backup(
        self, backup_dir: Path, retention_count: int
    ) -> Path:
        """Create an online SQLite snapshot, verify it, then enforce retention."""
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        final_path = backup_dir / f"mockingbot-{stamp}-{suffix}.sqlite3"
        temp_path = backup_dir / f".{final_path.name}.tmp"
        destination: sqlite3.Connection | None = None
        try:
            destination = sqlite3.connect(str(temp_path), timeout=30.0)
            self.conn.backup(destination)
            destination.commit()
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise RuntimeError(
                    f"backup integrity check failed: {integrity[0] if integrity else 'no result'}"
                )
            destination.close()
            destination = None
            temp_path.replace(final_path)
        except Exception:
            if destination is not None:
                destination.close()
            if temp_path.exists():
                temp_path.unlink()
            raise

        backups = sorted(
            backup_dir.glob("mockingbot-*.sqlite3"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for expired in backups[max(2, retention_count):]:
            expired.unlink()
        return final_path

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

    def record_wallet_observation(
        self,
        wallet: str,
        positions: dict[str, Position],
        events: list[CopyEvent],
    ) -> None:
        seeded = set(self.get_json("seeded_wallets", []))
        seeded.add(wallet)
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO pending_copy_events(
                    detected_at, wallet, coin, side, kind, entry_price,
                    previous_size, current_size, status
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """,
                [
                    (
                        utc_now(),
                        event.wallet,
                        event.coin,
                        event.side,
                        event.kind,
                        event.entry_price,
                        event.previous_size,
                        event.current_size,
                    )
                    for event in events
                ],
            )
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
            self.conn.execute(
                "INSERT INTO kv(key, value) VALUES('seeded_wallets', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(sorted(seeded)),),
            )

    def pending_copy_events(self) -> list[CopyEvent]:
        rows = self.conn.execute(
            """
            SELECT id, wallet, coin, side, kind, entry_price, previous_size, current_size
            FROM pending_copy_events
            WHERE status = 'PENDING'
            ORDER BY id
            """
        ).fetchall()
        return [
            CopyEvent(
                kind=str(row["kind"]),
                wallet=str(row["wallet"]),
                coin=str(row["coin"]),
                side=str(row["side"]),
                entry_price=None if row["entry_price"] is None else float(row["entry_price"]),
                previous_size=None if row["previous_size"] is None else float(row["previous_size"]),
                current_size=None if row["current_size"] is None else float(row["current_size"]),
                event_id=int(row["id"]),
            )
            for row in rows
        ]

    def acknowledge_copy_event(self, event_id: int) -> None:
        self.conn.execute(
            """
            UPDATE pending_copy_events
            SET status = 'HANDLED', handled_at = ?
            WHERE id = ? AND status = 'PENDING'
            """,
            (utc_now(), event_id),
        )
        self.conn.commit()

    @staticmethod
    def execution_cloid(intent_key: str) -> str:
        return "0x" + hashlib.sha256(intent_key.encode("utf-8")).hexdigest()[:32]

    def execution_intent(self, intent_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM execution_intents WHERE intent_key = ?", (intent_key,)
        ).fetchone()

    def prepare_execution_intent(
        self,
        intent_key: str,
        operation: str,
        coin: str,
        side: str | None,
        requested_size: float,
        before: Position | None,
        leverage: float | None = None,
    ) -> sqlite3.Row:
        existing = self.execution_intent(intent_key)
        if existing is not None:
            expected = (operation, coin, side, round(requested_size, 12))
            actual = (
                str(existing["operation"]), str(existing["coin"]),
                existing["side"], round(float(existing["requested_size"]), 12),
            )
            if actual != expected:
                raise RuntimeError(
                    f"execution intent collision for {intent_key}: {actual} != {expected}"
                )
            return existing
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO execution_intents(
                    intent_key, cloid, operation, coin, side, requested_size, leverage,
                    pre_side, pre_size, pre_entry_price, state, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?)
                """,
                (
                    intent_key, self.execution_cloid(intent_key), operation, coin, side,
                    requested_size, leverage, before.side if before else None,
                    before.size if before else 0.0,
                    before.entry_price if before else None, now, now,
                ),
            )
        row = self.execution_intent(intent_key)
        if row is None:
            raise RuntimeError(f"failed to persist execution intent {intent_key}")
        return row

    def update_execution_intent(
        self, intent_key: str, state: str, result: ExecutionResult | None = None
    ) -> None:
        result_json = None
        if result is not None:
            result_json = json.dumps(
                {
                    "accepted": result.accepted,
                    "requested_size": result.requested_size,
                    "filled_size": result.filled_size,
                    "avg_fill_price": result.avg_fill_price,
                    "order_id": result.order_id,
                    "status": result.status,
                    "confirmed": result.confirmed,
                    "detail": result.detail,
                }
            )
        with self.conn:
            self.conn.execute(
                """
                UPDATE execution_intents
                SET state = ?, result_json = COALESCE(?, result_json), updated_at = ?
                WHERE intent_key = ?
                """,
                (state, result_json, utc_now(), intent_key),
            )

    def unresolved_execution_intents(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM execution_intents
            WHERE state IN ('PREPARED', 'SUBMITTING', 'AMBIGUOUS')
            ORDER BY id
            """
        ).fetchall()

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

    def commit_paper_open(
        self,
        acct: dict[str, Any],
        coin: str,
        side: str,
        entry_price: float,
        cost_basis: float,
        source_wallet: str,
        leverage: float,
    ) -> None:
        """Commit cash, allocation slice, and aggregate position as one ledger change."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO kv(key, value) VALUES('paper_account', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(acct),),
            )
            self.conn.execute(
                """
                INSERT INTO paper_position_slices(
                    coin, side, source_wallet, entry_price, cost_basis, leverage, opened_at, status
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'OPEN')
                """,
                (coin, side, source_wallet, entry_price, cost_basis, leverage, utc_now()),
            )
            self._sync_paper_position_tx(coin)

    def commit_paper_close(
        self,
        acct: dict[str, Any],
        slice_id: int,
        coin: str,
        exit_price: float | None,
        paper_gain: float,
        pnl_pct: float | None,
    ) -> None:
        """Commit proceeds, closed allocation, and aggregate position atomically."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO kv(key, value) VALUES('paper_account', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(acct),),
            )
            cursor = self.conn.execute(
                """
                UPDATE paper_position_slices
                SET status = 'CLOSED', closed_at = ?, exit_price = ?,
                    paper_gain = ?, pnl_pct = ?
                WHERE id = ? AND status = 'OPEN'
                """,
                (utc_now(), exit_price, paper_gain, pnl_pct, slice_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"paper allocation {slice_id} is no longer open")
            self._sync_paper_position_tx(coin)

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
        with self.conn:
            self._sync_paper_position_tx(coin)

    def _sync_paper_position_tx(self, coin: str) -> None:
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
            self.conn.execute("DELETE FROM paper_positions WHERE coin = ?", (coin,))
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
            # Positive is adverse, negative is price improvement. Entry and exit
            # have opposite economics for the same position side.
            direction = (
                1.0
                if (operation == "OPEN" and side == "LONG")
                or (operation == "CLOSE" and side == "SHORT")
                else -1.0
            )
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
                scoring_seed_fingerprint, policy_fingerprint, environment_fingerprint
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                parity_policy_fingerprint(settings),
                parity_environment_fingerprint(settings),
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
        self._cache_initialized = False
        self._refreshed_at = 0.0
        self._symbols: set[str] = set()
        self._ranks: dict[str, int] = {}
        self._refresh_thread: threading.Thread | None = None
        self._refresh_result: tuple[set[str], dict[str, int], Exception | None] | None = None

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

    def _fetch_cache(self) -> tuple[set[str], dict[str, int], Exception | None]:
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
            except Exception as exc:
                return symbols, ranks, exc
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
        return symbols, ranks, None

    def _background_refresh(self) -> None:
        self._refresh_result = self._fetch_cache()

    def maintain(self) -> None:
        """Persist completed refreshes and start a stale refresh without blocking."""
        if not self.settings.token_risk_logging:
            return
        if not self._cache_initialized:
            self._refreshed_at, self._symbols, self._ranks = self._load_cache()
            self._cache_loaded = bool(self._symbols)
            self._cache_initialized = True

        completed = self._refresh_result
        if completed is not None:
            self._refresh_result = None
            symbols, ranks, error = completed
            if symbols:
                self._symbols = symbols
                self._ranks = ranks
                self._cache_loaded = True
                self._save_cache(symbols, ranks)
                self._refreshed_at = unix_now()
            if error is not None:
                self.store.set_json(
                    "token_risk_retry_after",
                    {
                        "ts": unix_now() + max(300, self.settings.token_risk_retry_seconds),
                        "error": str(error),
                    },
                )
                self.store.log_api_failure(
                    "coingecko", "token_risk_cache", "", str(error)
                )

        if (
            self._symbols
            and unix_now() - self._refreshed_at < self.settings.token_risk_refresh_seconds
        ):
            return
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return
        retry_state = self.store.get_json("token_risk_retry_after", {})
        retry_after = float(retry_state.get("ts", 0) or 0)
        if retry_after and unix_now() < retry_after:
            return
        self._refresh_thread = threading.Thread(
            target=self._background_refresh,
            name="token-risk-refresh",
            daemon=True,
        )
        self._refresh_thread.start()

    def observe(self, event: CopyEvent) -> None:
        if not self.settings.token_risk_logging or event.kind not in {"ENTRY", "ADD"}:
            return
        # This is intentionally cache-only. Network refreshes are advisory and
        # must never delay a time-sensitive order.
        self.maintain()
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
        intent_key: str | None = None,
    ) -> ExecutionResult:
        raise NotImplementedError

    @abstractmethod
    def close_position(
        self, coin: str, size: float | None = None,
        reference_price: float | None = None, intent_key: str | None = None,
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
        self._account_mode: str | None = None

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
        if role is None:
            raise RuntimeError(
                "Live startup blocked: unable to verify API wallet role"
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
                    leverage=float(pos.get("leverage", {}).get("value") or 0) or None,
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
        snapshot = self.capital_snapshot()
        return snapshot.account_value if snapshot is not None else None

    def _live_account_mode(self) -> str | None:
        if not self.settings.live:
            return "standard"
        if self._account_mode is not None:
            return self._account_mode
        try:
            self._init_sdk()
            mode = self._info.query_user_abstraction_state(  # type: ignore[union-attr]
                self.settings.hl_wallet_address
            )
            if isinstance(mode, str) and mode:
                self._account_mode = mode
                return mode
        except Exception as exc:
            self.store.log_api_failure(self.name, "account_abstraction", "", str(exc))
        return None

    def _unified_capital_snapshot(self) -> CapitalSnapshot | None:
        try:
            self._init_sdk()
            state = self._info.spot_user_state(  # type: ignore[union-attr]
                self.settings.hl_wallet_address
            )
            balances = state.get("balances", []) if isinstance(state, dict) else []
            usdc = next(
                (
                    row
                    for row in balances
                    if int(row.get("token", -1)) == 0
                    or str(row.get("coin", "")).upper() == "USDC"
                ),
                None,
            )
            if not isinstance(usdc, dict):
                return None
            account_value = float(usdc.get("total") or 0)
            available_by_token = dict(state.get("tokenToAvailableAfterMaintenance", []))
            available_raw = available_by_token.get(0, available_by_token.get("0"))
            if available_raw is None:
                return None
            available = float(available_raw)
            if account_value <= 0 or available < 0 or available > account_value + 1e-8:
                return None
            return CapitalSnapshot(
                account_value=account_value,
                total_margin_used=max(0.0, account_value - available),
                withdrawable=available,
                available_margin=available,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            self.store.log_api_failure(self.name, "unified_capital", "", str(exc))
            return None

    def capital_snapshot(self) -> CapitalSnapshot | None:
        mode = self._live_account_mode()
        if mode in {"unifiedAccount", "portfolioMargin"}:
            return self._unified_capital_snapshot()
        if mode is None and self.settings.live:
            return None
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
                    leverage=float(pos.get("leverage", {}).get("value") or 0) or None,
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
        intent_key: str | None = None,
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

        intent_key = intent_key or f"adhoc:open:{uuid.uuid4().hex}"
        prior_intent = self.store.execution_intent(intent_key)
        if prior_intent is not None and prior_intent["state"] == "CONFIRMED":
            saved = self._execution_result_from_json(prior_intent["result_json"])
            if saved is not None:
                return saved

        pre_state_available, current_before = self._confirmed_position(coin)
        if not pre_state_available:
            detail = "pre-order position state unavailable; no order submitted"
            self.store.quarantine_coin(coin, "pre-order state unavailable", detail)
            return reject(detail, requested_size=size, status="state_unavailable")
        if prior_intent is not None:
            before = self._intent_pre_position(prior_intent)
            recovered = self._recover_open_intent(
                prior_intent, current_before, side, price, leverage, decimals
            )
            if recovered is not None:
                return recovered
        else:
            before = current_before

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
        if before is not None and (
            before.leverage is None or abs(before.leverage - leverage) > 1e-8
        ):
            detail = (
                f"pre-order exchange leverage={before.leverage}x "
                f"local={leverage}x; no order submitted"
            )
            self.store.quarantine_coin(coin, "exchange leverage mismatch", detail)
            return reject(detail, requested_size=size, status="leverage_mismatch")

        intent = self.store.prepare_execution_intent(
            intent_key, "OPEN", coin, side, size, before, leverage
        )
        cloid = self._cloid(str(intent["cloid"]))

        try:
            if existing_leverage is None:
                leverage_response = self._exchange.update_leverage(  # type: ignore[union-attr]
                    leverage, coin, is_cross=True
                )
                if not isinstance(leverage_response, dict) or leverage_response.get("status") != "ok":
                    return reject(
                        f"Hyperliquid leverage update rejected: {str(leverage_response)[:300]}",
                        requested_size=size,
                        status="leverage_update_rejected",
                    )
            self.store.update_execution_intent(intent_key, "SUBMITTING")
            result = self._exchange.market_open(  # type: ignore[union-attr]
                coin, side == "LONG", size, slippage=self.settings.slippage,
                cloid=cloid,
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
                leverage_confirmed = (
                    confirmed is not None
                    and confirmed.leverage is not None
                    and abs(confirmed.leverage - leverage) <= 1e-8
                )
                if fill_size > tolerance and not leverage_confirmed:
                    leverage_detail = (
                        f"exchange leverage={confirmed.leverage if confirmed else None}x "
                        f"expected={leverage}x after measured fill={fill_size:g}"
                    )
                    execution = ExecutionResult(
                        True, size, fill_size, fill_price or execution.avg_fill_price or price,
                        execution.order_id, "leverage_mismatch", False, leverage_detail,
                    )
                    self.store.quarantine_coin(
                        coin, "post-entry leverage mismatch", leverage_detail
                    )
                elif fill_size > tolerance:
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
            self.store.update_execution_intent(
                intent_key, self._execution_intent_state(execution), execution
            )
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
                leverage_confirmed = (
                    confirmed is not None
                    and confirmed.leverage is not None
                    and abs(confirmed.leverage - leverage) <= 1e-8
                )
                if fill_size > tolerance and not leverage_confirmed:
                    detail = (
                        f"recovered fill after error but exchange leverage="
                        f"{confirmed.leverage if confirmed else None}x expected={leverage}x"
                    )
                    self.store.quarantine_coin(
                        coin, "post-entry leverage mismatch", detail
                    )
                    result = ExecutionResult(
                        True, size, fill_size, fill_price or price,
                        status="leverage_mismatch", confirmed=False, detail=detail,
                    )
                elif fill_size > tolerance:
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
            self.store.update_execution_intent(
                intent_key, self._execution_intent_state(result), result
            )
            self.store.log_execution(coin, side, "OPEN", result, leverage, requested_leverage)
            return result

    @staticmethod
    def _cloid(raw: str) -> Any:
        from hyperliquid.utils.types import Cloid
        return Cloid.from_str(raw)

    @staticmethod
    def _execution_result_from_json(raw: str | None) -> ExecutionResult | None:
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return ExecutionResult(**data)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _execution_intent_state(result: ExecutionResult) -> str:
        if result.confirmed:
            return "CONFIRMED"
        if not result.accepted and result.filled_size <= 0 and result.status in {
            "exception", "rejected", "unfilled", "leverage_update_rejected",
        }:
            return "FAILED"
        return "AMBIGUOUS"

    @staticmethod
    def _intent_pre_position(intent: sqlite3.Row) -> Position | None:
        if not intent["pre_side"] or float(intent["pre_size"]) <= 0:
            return None
        return Position(
            str(intent["coin"]), str(intent["pre_side"]),
            float(intent["pre_size"]), float(intent["pre_entry_price"] or 0),
        )

    def _recover_open_intent(
        self, intent: sqlite3.Row, current: Position | None, side: str,
        fallback_price: float, leverage: int, decimals: int,
    ) -> ExecutionResult | None:
        before = self._intent_pre_position(intent)
        fill_size, fill_price, detail = self._entry_position_delta(before, current, side)
        tolerance = 10 ** (-decimals) / 2
        if fill_size > tolerance:
            leverage_ok = (
                current is not None and current.leverage is not None
                and abs(current.leverage - leverage) <= 1e-8
            )
            result = ExecutionResult(
                True, float(intent["requested_size"]), fill_size,
                fill_price or fallback_price, status="recovered_intent",
                confirmed=leverage_ok,
                detail=f"recovered durable intent; {detail}",
            )
            self.store.update_execution_intent(
                str(intent["intent_key"]),
                "CONFIRMED" if leverage_ok else "AMBIGUOUS", result,
            )
            if leverage_ok:
                self.store.clear_coin_quarantine(str(intent["coin"]))
            else:
                self.store.quarantine_coin(
                    str(intent["coin"]), "recovered intent leverage mismatch", result.detail
                )
            return result
        if str(intent["state"]) == "PREPARED":
            return None
        lookup = self._lookup_intent_order(intent)
        if lookup == "NOT_FOUND":
            # The durable marker was written before submission. Reusing the same
            # cloid preserves idempotency if the exchange view races with us.
            return None
        if lookup == "TERMINAL_NO_FILL":
            result = ExecutionResult(
                False, float(intent["requested_size"]), status="unfilled",
                detail="durable intent reached a terminal exchange state without a fill",
            )
            self.store.update_execution_intent(str(intent["intent_key"]), "FAILED", result)
            return result
        detail = "durable open intent exists but no position delta can be verified"
        self.store.quarantine_coin(str(intent["coin"]), "ambiguous execution intent", detail)
        result = ExecutionResult(
            False, float(intent["requested_size"]), status="ambiguous_intent", detail=detail
        )
        self.store.update_execution_intent(str(intent["intent_key"]), "AMBIGUOUS", result)
        return result

    def _lookup_intent_order(self, intent: sqlite3.Row) -> str:
        """Return NOT_FOUND, TERMINAL_NO_FILL, PRESENT, or UNAVAILABLE."""
        try:
            self._init_sdk()
            response = self._info.query_order_by_cloid(  # type: ignore[union-attr]
                self.settings.hl_wallet_address, self._cloid(str(intent["cloid"]))
            )
        except Exception as exc:
            self.store.log_api_failure(
                self.name, "query_order_by_cloid", str(intent["coin"]), str(exc)
            )
            return "UNAVAILABLE"
        if not isinstance(response, dict):
            return "UNAVAILABLE"
        status = str(response.get("status", "")).lower()
        if status == "unknownoid":
            return "NOT_FOUND"
        order_status = ""
        order = response.get("order")
        if isinstance(order, dict):
            order_status = str(order.get("status", "")).lower()
        if order_status in {
            "canceled", "rejected", "margincanceled", "vaultwithdrawal",
            "openinterestcapcanceled", "selftradecanceled", "reduceonlycanceled",
            "siblingfilledcanceled", "delistedcanceled", "scheduledcancel",
        }:
            return "TERMINAL_NO_FILL"
        return "PRESENT"

    def _intent_fill_price(self, intent: sqlite3.Row) -> float | None:
        try:
            self._init_sdk()
            fills = self._info.user_fills(  # type: ignore[union-attr]
                self.settings.hl_wallet_address
            )
        except Exception as exc:
            self.store.log_api_failure(
                self.name, "user_fills_intent", str(intent["coin"]), str(exc)
            )
            return None
        target = str(intent["cloid"]).lower()
        matched: list[tuple[float, float]] = []
        for fill in fills if isinstance(fills, list) else []:
            if not isinstance(fill, dict):
                continue
            if str(fill.get("cloid", "")).lower() != target:
                continue
            try:
                quantity = abs(float(fill.get("sz") or 0))
                price = float(fill.get("px") or 0)
            except (TypeError, ValueError):
                continue
            if quantity > 0 and price > 0:
                matched.append((quantity, price))
        total = sum(quantity for quantity, _ in matched)
        if total <= 0:
            return None
        return sum(quantity * price for quantity, price in matched) / total

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
        reference_price: float | None = None, intent_key: str | None = None,
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
        intent_key = intent_key or f"adhoc:close:{uuid.uuid4().hex}"
        prior_intent = self.store.execution_intent(intent_key)
        if prior_intent is not None and prior_intent["state"] == "CONFIRMED":
            saved = self._execution_result_from_json(prior_intent["result_json"])
            if saved is not None:
                return saved
        if not state_available:
            result = ExecutionResult(
                False, status="state_unavailable", detail="pre-close position state unavailable"
            )
            self.store.quarantine_coin(coin, "close state unavailable", result.detail)
            self._log_close_execution(coin, None, result, reference_price)
            return result
        if before is None:
            if prior_intent is not None:
                baseline = self._intent_pre_position(prior_intent)
                if baseline is not None:
                    fill_price = self._intent_fill_price(prior_intent)
                    if fill_price is None:
                        detail = "close completed but fill price is unavailable by client order ID"
                        result = ExecutionResult(
                            False, float(prior_intent["requested_size"]), baseline.size,
                            status="ambiguous_intent", detail=detail,
                        )
                        self.store.update_execution_intent(
                            intent_key, "AMBIGUOUS", result
                        )
                        self.store.quarantine_coin(
                            coin, "close fill price unavailable", detail
                        )
                        return result
                    result = ExecutionResult(
                        True, float(prior_intent["requested_size"]), baseline.size,
                        fill_price, status="recovered_intent", confirmed=True,
                        detail="recovered durable close intent; exchange is flat",
                    )
                    self.store.update_execution_intent(intent_key, "CONFIRMED", result)
                    self.store.clear_coin_quarantine(coin)
                    return result
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

        if prior_intent is not None:
            baseline = self._intent_pre_position(prior_intent)
            if baseline is None:
                raise RuntimeError(f"close intent {intent_key} has no pre-order position")
            reduction = baseline.size - (before.size if before.side == baseline.side else 0.0)
            if abs(reduction - float(prior_intent["requested_size"])) <= tolerance:
                fill_price = self._intent_fill_price(prior_intent)
                if fill_price is None:
                    detail = "close delta verified but fill price is unavailable by client order ID"
                    result = ExecutionResult(
                        False, float(prior_intent["requested_size"]), reduction,
                        status="ambiguous_intent", detail=detail,
                    )
                    self.store.update_execution_intent(intent_key, "AMBIGUOUS", result)
                    self.store.quarantine_coin(coin, "close fill price unavailable", detail)
                    return result
                result = ExecutionResult(
                    True, float(prior_intent["requested_size"]), reduction,
                    fill_price, status="recovered_intent", confirmed=True,
                    detail="recovered durable close intent from exchange position delta",
                )
                self.store.update_execution_intent(intent_key, "CONFIRMED", result)
                return result
            if str(prior_intent["state"]) != "PREPARED":
                lookup = self._lookup_intent_order(prior_intent)
                if lookup == "NOT_FOUND" and reduction <= tolerance:
                    before = baseline
                    requested_size = float(prior_intent["requested_size"])
                    prior_intent = self.store.execution_intent(intent_key)
                elif lookup == "TERMINAL_NO_FILL":
                    result = ExecutionResult(
                        False, float(prior_intent["requested_size"]),
                        status="unfilled",
                        detail="durable close intent terminated without a fill",
                    )
                    self.store.update_execution_intent(intent_key, "FAILED", result)
                    return result
                else:
                    detail = (
                        f"durable close intent delta unresolved: expected="
                        f"{float(prior_intent['requested_size']):g} measured={reduction:g}"
                    )
                    result = ExecutionResult(
                        False, float(prior_intent["requested_size"]), max(0.0, reduction),
                        status="ambiguous_intent", detail=detail,
                    )
                    self.store.update_execution_intent(intent_key, "AMBIGUOUS", result)
                    self.store.quarantine_coin(coin, "ambiguous execution intent", detail)
                    return result
            before = baseline
            requested_size = float(prior_intent["requested_size"])

        intent = self.store.prepare_execution_intent(
            intent_key, "CLOSE", coin, before.side, requested_size, before,
            before.leverage,
        )
        cloid = self._cloid(str(intent["cloid"]))

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
            self.store.update_execution_intent(intent_key, "SUBMITTING")
            response = self._exchange.market_close(  # type: ignore[union-attr]
                coin, sz=requested_size, slippage=self.settings.slippage, cloid=cloid
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
                    coin, sz=retry_size, slippage=self.settings.slippage,
                    cloid=self._cloid(self.store.execution_cloid(intent_key + ":residual")),
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
        self.store.update_execution_intent(
            intent_key, self._execution_intent_state(result), result
        )
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
        requested_position_leverage = float(
            leverage if leverage is not None else self.settings.leverage
        )
        existing_position_leverage = self.store.position_leverage(coin)
        position_leverage = (
            existing_position_leverage
            if existing_position_leverage is not None
            else requested_position_leverage
        )
        self.store.commit_paper_open(
            acct, coin, side, price, round(slot, 2), wallet, position_leverage
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
        self.store.commit_paper_close(
            acct, int(pos["id"]), coin, price, gain, pnl_pct
        )
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
            current = platform.account_value()
            if current is None:
                return None
            stored_reference = 0.0
            if stored.get("wallet") == self.settings.hl_wallet_address:
                stored_reference = float(
                    stored.get("high_water_value", stored.get("start_value", 0)) or 0
                )
            reference = max(current, stored_reference)
            self.store.set_json(
                "live_risk_baseline",
                {
                    "started": stored.get("started") or utc_now(),
                    "wallet": self.settings.hl_wallet_address,
                    "start_value": float(stored.get("start_value", 0) or current),
                    "high_water_value": reference,
                    "high_water_updated": utc_now(),
                },
            )
            return reference

        start = portfolio.value(platform.mid_price)
        self.store.set_json("session", {"started": utc_now(), "paper_start": start})
        return start

    def update_live_high_water(
        self, current_value: float, current_reference: float | None
    ) -> float:
        if not self.settings.live:
            return current_reference if current_reference is not None else current_value
        reference = float(current_reference or 0)
        if current_value <= reference:
            return reference
        stored = self.store.get_json("live_risk_baseline", {})
        self.store.set_json(
            "live_risk_baseline",
            {
                "started": stored.get("started") or utc_now(),
                "wallet": self.settings.hl_wallet_address,
                "start_value": float(stored.get("start_value", 0) or current_value),
                "high_water_value": current_value,
                "high_water_updated": utc_now(),
            },
        )
        print(f"[LIVE-RISK] New equity high-water mark ${current_value:,.2f}")
        return current_value

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
        if self.settings.live and existing is not None and coin not in live_held:
            return TradeDecision("SKIP", "local position missing live")
        if coin in live_held and existing is None:
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

        actual_realized = sum(float(row["paper_gain"] or 0.0) for row in rows)
        pnls = [float(row["pnl_pct"]) for row in rows]
        normalized_return_pct = sum(pnls)
        realized = (
            normalized_return_pct
            / 100.0
            * self.settings.scoring_reference_margin_usd
            * self.settings.scoring_reference_leverage
        )
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

        parts = [
            f"sample={sample_size}",
            f"normalized=${realized:.2f}",
            f"return={normalized_return_pct:+.3f}%",
            f"actual=${actual_realized:.2f}",
            f"score={total:.1f}",
        ]
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

    def load_or_refresh(self, force: bool = False) -> list[str]:
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
        return self.refresh()

    def refresh(self) -> list[str]:
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
                self.store.record_wallet_observation(wallet, current, [])
                print(f"[MONITOR] Seeded {wallet[:16]} baseline")
                time.sleep(self.settings.wallet_poll_delay)
                continue

            wallet_events: list[CopyEvent] = []
            for coin, pos in current.items():
                old = previous.get(coin)
                if old is None:
                    wallet_events.append(
                        CopyEvent(
                            "ENTRY", wallet, coin, pos.side, pos.entry_price,
                            previous_size=0.0, current_size=pos.size,
                        )
                    )
                elif old.side != pos.side:
                    wallet_events.append(
                        CopyEvent(
                            "EXIT", wallet, coin, old.side,
                            previous_size=old.size, current_size=0.0,
                        )
                    )
                    wallet_events.append(
                        CopyEvent(
                            "ENTRY", wallet, coin, pos.side, pos.entry_price,
                            previous_size=0.0, current_size=pos.size,
                        )
                    )
                elif old.size > 0:
                    size_increase_pct = (pos.size - old.size) / old.size * 100
                    if size_increase_pct >= self.settings.same_wallet_add_threshold_pct:
                        wallet_events.append(
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
                    wallet_events.append(
                        CopyEvent(
                            "EXIT", wallet, coin, old.side,
                            previous_size=old.size, current_size=0.0,
                        )
                    )

            self.store.record_wallet_observation(wallet, current, wallet_events)
            time.sleep(self.settings.wallet_poll_delay)

        fail_ratio = failures / checked if checked else 0.0
        return self.store.pending_copy_events(), fail_ratio


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

    def run(self, wallets: list[str]) -> None:
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
                self._force_close(
                    coin, pos["source_wallet"], pos["side"], "position timeout",
                    f"reconcile:slice:{int(pos['id'])}:close",
                )

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
                self._force_close(
                    coin, wallet, pos["side"], f"reconcile: {reason}",
                    f"reconcile:slice:{int(pos['id'])}:close",
                )

    def _force_close(
        self, coin: str, wallet: str, side: str, reason: str,
        intent_key: str | None = None,
    ) -> None:
        recovering = (
            intent_key is not None
            and self.store.execution_intent(intent_key) is not None
        )
        if self.settings.live and not recovering:
            quarantine = self.store.coin_quarantine(coin)
            if quarantine is not None:
                detail = f"{reason}; coin quarantined: {quarantine['reason']}"
                self.store.log_signal(
                    wallet, coin, side, "EXIT", None, "SKIPPED", detail
                )
                print(f"[RECONCILE] Skip {coin} {side}: {detail}")
                return
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
        if isinstance(self.platform, HyperliquidAdapter):
            execution = self.platform.close_position(
                coin, close_size, price, intent_key
            )
        else:
            execution = self.platform.close_position(coin, close_size, price)
        if not execution:
            current_intent = (
                self.store.execution_intent(intent_key) if intent_key is not None else None
            )
            if current_intent is not None and current_intent["state"] in {
                "PREPARED", "SUBMITTING", "AMBIGUOUS",
            }:
                raise RuntimeError(
                    f"reconciliation intent {intent_key} remains unresolved: "
                    f"{execution.detail or execution.status}"
                )
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
        self._last_backup_attempt = float(
            self.store.get_json("backup_status", {}).get("attempted_unix", 0) or 0
        )

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
            self._maintain_live_backup(force=True)
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
            self._quarantine_unresolved_execution_intents()
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

    def _quarantine_unresolved_execution_intents(self) -> None:
        unresolved = self.store.unresolved_execution_intents()
        risky = [row for row in unresolved if row["state"] != "PREPARED"]
        for row in risky:
            self.store.quarantine_coin(
                str(row["coin"]),
                "unresolved execution intent",
                f"{row['operation']} {row['state']} cloid={row['cloid']}",
            )
        if risky:
            print(
                f"[RECOVERY] {len(risky)} unresolved exchange intent(s) quarantined; "
                "pending copy events will reconcile them from live position deltas"
            )

    def _maintain_live_backup(self, force: bool = False) -> None:
        if not self.settings.live:
            return
        now = unix_now()
        if (
            not force
            and now - self._last_backup_attempt < self.settings.backup_interval_seconds
        ):
            return
        self._last_backup_attempt = now
        previous = self.store.get_json("backup_status", {})
        try:
            path = self.store.create_verified_backup(
                self.settings.backup_dir, self.settings.backup_retention_count
            )
            status = {
                "attempted_at": utc_now(),
                "attempted_unix": now,
                "successful_at": utc_now(),
                "successful_unix": unix_now(),
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "integrity": "ok",
                "error": "",
            }
            self.store.set_json("backup_status", status)
            print(f"[BACKUP] Verified SQLite snapshot: {path.name}")
        except Exception as exc:
            status = dict(previous)
            status.update(
                {
                    "attempted_at": utc_now(),
                    "attempted_unix": now,
                    "error": str(exc)[:500],
                }
            )
            self.store.set_json("backup_status", status)
            self.store.log_api_failure("sqlite", "live_backup", "", str(exc))
            self.notifier.send(f"MockingBot backup failed: {exc}")
            print(f"[BACKUP] Failed: {exc}")
    def _run_loop(self) -> None:
        wallets = self.roster.load_or_refresh(force=False)
        session_start = self.risk.session_start_value(self.paper, self.platform)
        last_reconcile = 0.0
        last_roster_check = unix_now()

        while self.running:
            cycle_start = unix_now()
            self.token_risk.maintain()
            self._maintain_live_backup()
            paper_value = self.paper.value(self.platform.mid_price)
            risk_value = self.risk.current_value(self.paper, self.platform)
            if self.settings.live:
                self.store.set_json(
                    "live_equity_snapshot",
                    {
                        "observed_at": utc_now(),
                        "observed_unix": unix_now(),
                        "account_value": risk_value,
                        "available": risk_value is not None,
                        "source": "risk-manager",
                    },
                )
            if session_start is None and risk_value is not None:
                session_start = self.risk.session_start_value(self.paper, self.platform)
            if self.settings.live and risk_value is not None:
                session_start = self.risk.update_live_high_water(risk_value, session_start)
            equity_available = session_start is not None and risk_value is not None
            self.risk.live_equity_available(equity_available)
            dd = self.risk.drawdown(session_start, risk_value) if equity_available else 0.0
            self.risk.check_warning(dd)
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
                wallets = self.roster.load_or_refresh(force=True)
                last_roster_check = unix_now()

            scan_wallets = self._effective_wallets(wallets)
            live_held: set[str] | None = set()
            if self.settings.live:
                live_positions = self.platform.live_positions()
                if live_positions is None:
                    print(
                        "[LIVE] Unable to read live positions; blocking entries and "
                        "source reconciliation this cycle"
                    )
                    live_held = None
                else:
                    self._reconcile_live_book(live_positions)
                    live_held = set(live_positions.keys())
            if unix_now() - last_reconcile >= self.settings.reconcile_seconds:
                if not self.settings.live or live_held is not None:
                    self.reconciler.run(scan_wallets)
                    last_reconcile = unix_now()
                else:
                    print("[RECONCILE] Skipped: live book unavailable")

            events, fail_ratio = self.monitor.scan(scan_wallets)
            if fail_ratio > self.settings.api_degraded_max_fail_ratio:
                print(f"[API] Degraded scan ({fail_ratio:.0%} failed); skipping signal execution this cycle")
                self._sleep_remaining(cycle_start)
                continue

            for event in events:
                if event.kind in {"ENTRY", "ADD"}:
                    self._handle_entry(event, wind_down, live_held)
                    if (
                        live_held is not None
                        and self.paper.position(event.coin) is not None
                    ):
                        live_held.add(event.coin)
                elif event.kind == "EXIT":
                    self._handle_exit(event)
                    # A flip is persisted as EXIT then ENTRY. Refresh the local
                    # held-set after a successful full close so the following
                    # opposite-side ENTRY is not rejected using stale state.
                    if (
                        live_held is not None
                        and self.paper.position(event.coin) is None
                    ):
                        live_held.discard(event.coin)
                if event.event_id is not None:
                    self.store.acknowledge_copy_event(event.event_id)

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
        live_snapshot = {
            coin: {
                "side": position.side,
                "size": position.size,
                "entry_price": position.entry_price,
                "leverage": position.leverage,
            }
            for coin, position in live_positions.items()
        }
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
            local_leverage = self.store.position_leverage(coin)
            if (
                local_leverage is None
                or live.leverage is None
                or abs(local_leverage - live.leverage) > 1e-8
            ):
                self.store.quarantine_coin(
                    coin,
                    "exchange leverage mismatch",
                    f"local={local_leverage}x live={live.leverage}x",
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
            tolerance = max(
                expected_size * self.settings.live_size_tolerance_pct,
                rounding_tolerance,
            )
            size_difference = abs(live.size - expected_size)
            live_snapshot[coin].update(
                {
                    "expected_size": expected_size,
                    "size_difference": size_difference,
                    "size_tolerance": tolerance,
                    "size_synchronized": size_difference <= tolerance,
                }
            )
            if expected_size > 0 and size_difference > tolerance:
                self.store.quarantine_coin(
                    coin,
                    "live size mismatch",
                    f"local={expected_size:.10g} live={live.size:.10g} "
                    f"difference={size_difference:.10g} tolerance={tolerance:.10g}",
                )
                continue
            self.store.clear_coin_quarantine(coin)
        self.store.set_json("live_position_snapshot", live_snapshot)

    def _handle_entry(self, event: CopyEvent, wind_down: bool, live_held: set[str] | None) -> None:
        self.token_risk.observe(event)
        recovery_key = (
            f"copy-event:{event.event_id}:open" if event.event_id is not None else None
        )
        recovery_intent = (
            self.store.execution_intent(recovery_key) if recovery_key is not None else None
        )
        if recovery_intent is not None:
            self._recover_entry_event(event, recovery_key, recovery_intent)
            return
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
        execution_key = (
            f"copy-event:{event.event_id}:open" if event.event_id is not None else None
        )
        if isinstance(self.platform, HyperliquidAdapter):
            execution = self.platform.open_position(
                event.coin, event.side, notional, price, effective_leverage,
                tier_leverage, execution_key,
            )
        else:
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
            rollback_succeeded = False
            if self.settings.live and execution.accepted and execution.filled_size > 0:
                rollback_detail = self._rollback_uncommitted_entry(
                    event, execution, price, existing_leverage is not None
                )
                failure_reason += rollback_detail
                rollback_succeeded = "rollback confirmed" in rollback_detail.lower()
                if rollback_succeeded and execution_key is not None:
                    self.store.update_execution_intent(
                        execution_key, "ROLLED_BACK", execution
                    )
            if execution_key is not None:
                intent = self.store.execution_intent(execution_key)
                if (
                    intent is not None
                    and intent["state"] in {"PREPARED", "SUBMITTING", "AMBIGUOUS"}
                    and not rollback_succeeded
                ):
                    raise RuntimeError(
                        f"execution intent {execution_key} remains unresolved: {failure_reason}"
                    )
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
            recovery_key = (
                f"copy-event:{event.event_id}:paper-commit-rollback"
                if event.event_id is not None else None
            )
            if isinstance(self.platform, HyperliquidAdapter):
                recovery_close = self.platform.close_position(
                    event.coin, execution.filled_size, price, recovery_key
                )
            else:
                recovery_close = self.platform.close_position(
                    event.coin, execution.filled_size, price
                )
            if not recovery_close:
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

    def _recover_entry_event(
        self, event: CopyEvent, intent_key: str, intent: sqlite3.Row
    ) -> None:
        if self.paper.owns_position(event.wallet, event.coin, event.side):
            self.store.clear_coin_quarantine(event.coin)
            return
        price = self.platform.mid_price(event.coin) or event.entry_price
        leverage = int(float(intent["leverage"] or self.settings.leverage))
        if not price or price <= 0 or leverage <= 0:
            raise RuntimeError(
                f"cannot recover {intent_key}: price or leverage unavailable"
            )
        requested_size = float(intent["requested_size"])
        if not isinstance(self.platform, HyperliquidAdapter):
            raise RuntimeError(f"cannot recover {intent_key}: unsupported platform")
        execution = self.platform.open_position(
            event.coin, event.side, requested_size * price, price,
            leverage, leverage, intent_key,
        )
        if not execution:
            raise RuntimeError(
                f"execution intent {intent_key} remains unresolved: "
                f"{execution.detail or execution.status}"
            )
        actual_price = execution.avg_fill_price or price
        actual_cost = execution.filled_size * actual_price / leverage
        opened = self.paper.open(
            event.wallet, event.coin, event.side, actual_price, actual_cost,
            allow_same_wallet_add=event.kind == "ADD", leverage=leverage,
        )
        if opened is None:
            self.store.quarantine_coin(
                event.coin, "recovered execution ledger commit failed", intent_key
            )
            raise RuntimeError(
                f"recovered live fill for {intent_key}, but local ledger commit failed"
            )
        self.store.clear_coin_quarantine(event.coin)
        score = self.scoring_engine.score_wallet(event.wallet)
        reason = f"recovered durable exchange intent; {score.tier} {score.total_score:.1f}"
        signal_id = self.store.log_signal(
            event.wallet, event.coin, event.side, event.kind, actual_price,
            "EXECUTED", reason,
        )
        self.scoring_engine.observe_signal(
            event, signal_id, "EXECUTED", reason, actual_price
        )
        print(f"[RECOVERY] {event.coin} {event.side}: local ledger completed")

    def _rollback_uncommitted_entry(
        self,
        event: CopyEvent,
        execution: ExecutionResult,
        reference_price: float,
        had_local_position: bool,
    ) -> str:
        rollback_key = (
            f"copy-event:{event.event_id}:entry-rollback"
            if event.event_id is not None else None
        )
        if isinstance(self.platform, HyperliquidAdapter):
            rollback = self.platform.close_position(
                event.coin, execution.filled_size, reference_price, rollback_key
            )
        else:
            rollback = self.platform.close_position(
                event.coin, execution.filled_size, reference_price
            )
        payload = {
            "ts": utc_now(),
            "coin": event.coin,
            "side": event.side,
            "filled_size": execution.filled_size,
            "trigger_status": execution.status,
            "rollback_status": rollback.status,
            "rollback_confirmed": bool(rollback),
            "detail": rollback.detail,
        }
        self.store.set_json("last_entry_rollback", payload)
        if rollback:
            if had_local_position:
                self.store.quarantine_coin(
                    event.coin,
                    "entry rollback pending reconciliation",
                    "measured fill was reversed; existing position leverage/size "
                    "must reconcile before new entries",
                )
            else:
                self.store.clear_coin_quarantine(event.coin)
            print(
                f"[ROLLBACK] {event.coin} {event.side}: reversed uncommitted "
                f"fill size={execution.filled_size:g}"
            )
            return "; automatic rollback confirmed"

        detail = (
            f"trigger={execution.status} rollback={rollback.status}: "
            f"{rollback.detail or 'not confirmed'}"
        )
        self.store.quarantine_coin(event.coin, "ENTRY ROLLBACK FAILED", detail)
        self.notifier.send(
            f"MockingBot ALERT: {event.coin} {event.side} entry rollback failed; "
            "coin quarantined."
        )
        print(f"[ALERT] {event.coin} {event.side}: ENTRY ROLLBACK FAILED - {detail}")
        return "; AUTOMATIC ROLLBACK FAILED; coin quarantined"

    def _handle_exit(self, event: CopyEvent) -> None:
        close_intent_key = (
            f"copy-event:{event.event_id}:close" if event.event_id is not None else None
        )
        recovering_close = (
            close_intent_key is not None
            and self.store.execution_intent(close_intent_key) is not None
        )
        if self.settings.live and not recovering_close:
            quarantine = self.store.coin_quarantine(event.coin)
            if quarantine is not None:
                reason = f"coin quarantined: {quarantine['reason']}"
                signal_id = self.store.log_signal(
                    event.wallet, event.coin, event.side, "EXIT", None, "SKIPPED", reason
                )
                self.scoring_engine.observe_signal(
                    event, signal_id, "SKIPPED", reason, event.entry_price
                )
                print(f"[EXIT] {event.coin} {event.side}: {reason}")
                return
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
        execution_key = (
            f"copy-event:{event.event_id}:close" if event.event_id is not None else None
        )
        if isinstance(self.platform, HyperliquidAdapter):
            execution = self.platform.close_position(
                event.coin, close_size, price, execution_key
            )
        else:
            execution = self.platform.close_position(event.coin, close_size, price)
        if not execution:
            current_intent = (
                self.store.execution_intent(close_intent_key)
                if close_intent_key is not None else None
            )
            if current_intent is not None and current_intent["state"] in {
                "PREPARED", "SUBMITTING", "AMBIGUOUS",
            }:
                raise RuntimeError(
                    f"execution intent {close_intent_key} remains unresolved: "
                    f"{execution.detail or execution.status}"
                )
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


def live_command_settings() -> Settings:
    base = Settings()
    explicit_data_dir = os.getenv("MOCKINGBOT_LIVE_DATA_DIR", "").strip()
    explicit_slots = os.getenv("MOCKINGBOT_LIVE_MAX_POSITIONS", "").strip()
    return replace(
        base,
        live=True,
        data_dir=(
            Path(explicit_data_dir)
            if explicit_data_dir
            else ROOT / "MockingBot_Main_Live_Test_Data"
        ),
        instance_id="live-main",
        max_positions=int(explicit_slots) if explicit_slots else 4,
    )


def run_live_preflight(
    settings: Settings,
    platform: HyperliquidAdapter | Any | None = None,
    *,
    check_instance_lock: bool = True,
) -> bool:
    results: list[tuple[str, str, str]] = []

    def record(ok: bool, name: str, detail: str, *, blocking: bool = True) -> None:
        status = ("PASS" if ok else "FAIL") if blocking else "INFO"
        results.append((status, name, detail))

    try:
        validate_settings(settings)
        record(True, "configuration", "validated")
    except Exception as exc:
        record(False, "configuration", str(exc))

    if check_instance_lock:
        lock = InstanceLock(settings)
        try:
            lock.acquire()
            record(True, "instance lock", "no duplicate live bot detected")
        except Exception as exc:
            record(False, "instance lock", str(exc))
        finally:
            lock.release()
    else:
        record(True, "instance lock", "held by this live startup")

    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / f".preflight-{uuid.uuid4().hex}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record(True, "runtime paths", f"writable: {settings.data_dir}")
    except Exception as exc:
        record(False, "runtime paths", str(exc))

    record(
        not settings.circuit_breaker_file.exists(),
        "circuit breaker",
        "clear" if not settings.circuit_breaker_file.exists() else "wind-down marker is active",
    )

    if settings.scoring_seed_db_path.resolve() == settings.db_path.resolve():
        record(False, "database isolation", "live and scoring databases are identical")
    elif not settings.scoring_seed_db_path.exists():
        record(False, "scoring seed", f"missing: {settings.scoring_seed_db_path}")
    else:
        try:
            source = sqlite3.connect(
                f"file:{settings.scoring_seed_db_path.as_posix()}?mode=ro", uri=True
            )
            source.execute("SELECT COUNT(*) FROM signals").fetchone()
            source.close()
            record(True, "database isolation", "paper scoring seed readable and separate")
        except Exception as exc:
            record(False, "scoring seed", str(exc))

    store: Store | None = None
    try:
        store = Store(settings.db_path)
        adapter = platform or HyperliquidAdapter(settings, store)
        adapter.validate_live_credentials()
        record(
            True,
            "credentials",
            f"account={settings.hl_wallet_address[:8]}...{settings.hl_wallet_address[-4:]} "
            f"api_wallet={settings.hl_api_wallet_address[:8]}...{settings.hl_api_wallet_address[-4:]}",
        )
        adapter._init_sdk()
        asset_count = len(adapter._sz_decimals)
        record(asset_count > 0, "asset metadata", f"{asset_count} assets loaded")

        capital = adapter.capital_snapshot()
        if capital is None:
            record(False, "capital", "account value / margin state unavailable")
        else:
            reserve = capital.account_value * settings.live_margin_reserve_pct
            usable = max(0.0, capital.available_margin - reserve)
            record(
                usable > 0,
                "capital",
                f"equity=${capital.account_value:.2f} available=${capital.available_margin:.2f} "
                f"usable=${usable:.2f} reserve=${reserve:.2f}",
            )
            multipliers = {
                "default Candidate": settings.scoring_engine_default_candidate_multiplier,
                "Candidate": settings.scoring_engine_candidate_multiplier,
                "proven Candidate": settings.scoring_engine_proven_candidate_multiplier,
                "Core": settings.scoring_engine_core_multiplier,
                "Elite": settings.scoring_engine_elite_multiplier,
            }
            leverages = {
                "default Candidate": settings.scoring_engine_default_candidate_leverage,
                "Candidate": settings.scoring_engine_candidate_leverage,
                "proven Candidate": settings.scoring_engine_proven_candidate_leverage,
                "Core": settings.scoring_engine_core_leverage,
                "Elite": settings.scoring_engine_elite_leverage,
            }
            for slots in sorted({settings.max_positions, 5, 6}):
                base_slot = capital.account_value / slots
                notionals = {
                    tier: base_slot * multiplier * leverages[tier]
                    for tier, multiplier in multipliers.items()
                    if multiplier > 0
                }
                margins = {
                    tier: base_slot * multiplier
                    for tier, multiplier in multipliers.items()
                    if multiplier > 0
                }
                viable = (
                    min(notionals.values()) >= settings.min_order_notional
                    and max(margins.values()) <= usable
                )
                configured = slots == settings.max_positions
                record(
                    viable,
                    f"{slots}-slot sizing" + (" (configured)" if configured else " (expansion)"),
                    f"{'viable; ' if viable else 'not viable; '}"
                    f"notional range=${min(notionals.values()):.2f}-${max(notionals.values()):.2f}; "
                    f"largest margin=${max(margins.values()):.2f}",
                    blocking=configured,
                )

        live_positions = adapter.live_positions()
        if live_positions is None:
            record(False, "position state", "unable to read live positions")
        else:
            identity = store.get_json("live_account_identity", {})
            local_slices = store.open_position_slices()
            if not identity:
                record(
                    not live_positions and not local_slices,
                    "position state",
                    "first initialization is flat" if not live_positions and not local_slices
                    else "first initialization requires both exchange and local state flat",
                )
            else:
                identity_ok = str(identity.get("wallet", "")).lower() == settings.hl_wallet_address.lower()
                record(identity_ok, "database account", "wallet matches" if identity_ok else "database belongs to another wallet")
                local_coins = {str(row["coin"]) for row in local_slices}
                synchronized = local_coins == set(live_positions)
                details: list[str] = []
                for coin in sorted(local_coins & set(live_positions)):
                    rows = [row for row in local_slices if str(row["coin"]) == coin]
                    sides = {str(row["side"]) for row in rows}
                    leverages = {float(row["leverage"]) for row in rows}
                    expected_size = sum(
                        float(row["cost_basis"]) * float(row["leverage"]) / float(row["entry_price"])
                        for row in rows
                        if float(row["entry_price"]) > 0
                    )
                    live = live_positions[coin]
                    rounding_tolerance = 2 * 10 ** (-adapter._sz_decimals.get(coin, 4))
                    tolerance = max(expected_size * settings.live_size_tolerance_pct, rounding_tolerance)
                    coin_ok = (
                        sides == {live.side}
                        and len(leverages) == 1
                        and live.leverage is not None
                        and abs(next(iter(leverages)) - live.leverage) <= 1e-8
                        and abs(expected_size - live.size) <= tolerance
                    )
                    synchronized = synchronized and coin_ok
                    if not coin_ok:
                        details.append(
                            f"{coin}: expected side={sorted(sides)} size={expected_size:.10g} "
                            f"leverage={sorted(leverages)}; live side={live.side} size={live.size:.10g} "
                            f"leverage={live.leverage}"
                        )
                if local_coins != set(live_positions):
                    details.append(f"local coins={sorted(local_coins)} live coins={sorted(live_positions)}")
                record(synchronized, "position state", "synchronized" if synchronized else "; ".join(details))
    except Exception as exc:
        record(False, "live connectivity", str(exc))
    finally:
        if store is not None:
            store.conn.close()

    print("\n=== MOCKINGBOT LIVE PREFLIGHT ===\n")
    for status, name, detail in results:
        print(f"[{status}] {name}: {detail}")
    passed = bool(results) and not any(status == "FAIL" for status, _, _ in results)
    print(f"\nRESULT: {'PASS - no orders submitted' if passed else 'FAIL - live start blocked'}\n")
    return passed


def run_bot(settings: Settings) -> None:
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


def start_live() -> int:
    settings = live_command_settings()
    with InstanceLock(settings):
        print("\n*** MOCKINGBOT LIVE START: REAL ORDERS ENABLED AFTER PREFLIGHT ***\n")
        if not run_live_preflight(settings, check_instance_lock=False):
            return 2
        log_handle, original_stdout, original_stderr = enable_monitor_log(settings)
        try:
            print(
                f"[LIVE] Starting account={settings.hl_wallet_address[:8]}..."
                f"{settings.hl_wallet_address[-4:]} slots={settings.max_positions} "
                f"data={settings.data_dir}"
            )
            bot = CopyTradingBot(settings)
            bot.run_forever()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            log_handle.close()
    return 0


def reset_live_risk_baseline(
    settings: Settings,
    platform: HyperliquidAdapter | Any | None = None,
) -> bool:
    with InstanceLock(settings):
        store = Store(settings.db_path)
        try:
            adapter = platform or HyperliquidAdapter(settings, store)
            adapter.validate_live_credentials()
            capital = adapter.capital_snapshot()
            if capital is None or capital.account_value <= 0:
                print("Live risk reset blocked: verified account equity is unavailable.")
                return False
            identity = store.get_json("live_account_identity", {})
            if identity and str(identity.get("wallet", "")).lower() != settings.hl_wallet_address.lower():
                print("Live risk reset blocked: database belongs to another wallet.")
                return False
            now = utc_now()
            store.set_json(
                "live_risk_baseline",
                {
                    "started": now,
                    "wallet": settings.hl_wallet_address,
                    "start_value": capital.account_value,
                    "high_water_value": capital.account_value,
                    "high_water_updated": now,
                    "manual_reset": True,
                },
            )
            print(
                f"Live risk baseline reset to verified equity "
                f"${capital.account_value:,.2f}; no orders submitted."
            )
            return True
        finally:
            store.conn.close()


def main(argv: list[str]) -> int:
    settings = Settings()
    if len(argv) > 1 and argv[1] == "preflight-live":
        return 0 if run_live_preflight(live_command_settings()) else 2
    if len(argv) > 1 and argv[1] == "start-live":
        return start_live()
    if len(argv) > 1 and argv[1] == "reset-live-risk-baseline":
        return 0 if reset_live_risk_baseline(live_command_settings()) else 2
    if len(argv) > 1 and argv[1] == "status":
        print_status(settings)
        return 0
    if len(argv) > 1 and argv[1] == "export-signals":
        output = Path(argv[2]) if len(argv) > 2 else settings.data_dir / "signals.csv"
        export_signals_csv(settings, output)
        return 0
    if settings.live:
        print(
            "Live startup blocked: use 'python .\\MockingBot.py start-live' "
            "or Start-MockingBot_Live.ps1 so preflight cannot be bypassed."
        )
        return 2
    run_bot(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
