"""
Read-only local dashboard for a MockingBot paper or live instance.

Usage:
  python .\\MockingBot_Dashboard.py

Then open:
  http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
MODE = os.getenv("MOCKINGBOT_DASHBOARD_MODE", "paper").strip().lower()
if MODE not in {"paper", "live"}:
    raise SystemExit("MOCKINGBOT_DASHBOARD_MODE must be 'paper' or 'live'")
DEFAULT_DATA_DIR = ROOT / ("MockingBot_Main_Live_Test_Data" if MODE == "live" else "MockingBot_Data")
DATA_DIR = Path(os.getenv("MOCKINGBOT_DATA_DIR", str(DEFAULT_DATA_DIR)))
DB_PATH = DATA_DIR / "mockingbot_codex.sqlite3"
HEADER_IMAGE_PATH = Path(os.getenv("MOCKINGBOT_HEADER_IMAGE", r"C:\Users\user\Desktop\Header_cr.png"))
HOST = os.getenv("MOCKINGBOT_DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("MOCKINGBOT_DASHBOARD_PORT", "8766" if MODE == "live" else "8765"))
HL_INFO_URL = os.getenv("HL_INFO_URL", "https://api.hyperliquid.xyz/info")
LEVERAGE = float(os.getenv("HL_LEVERAGE", "3"))
PRICE_CACHE_SECONDS = int(os.getenv("MOCKINGBOT_DASHBOARD_PRICE_CACHE_SECS", "15"))
EQUITY_MAX_AGE_SECONDS = int(
    os.getenv("MOCKINGBOT_DASHBOARD_EQUITY_MAX_AGE_SECS", "90")
)
PAPER_STARTING_EQUITY = float(os.getenv("PAPER_STARTING_CASH", "10000"))

_PRICE_CACHE: dict[str, float] = {}
_PRICE_CACHE_TS = 0.0
_PRICE_CACHE_ERROR = ""


def money(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def short_wallet(wallet: str | None) -> str:
    if not wallet:
        return "unknown"
    return f"{wallet[:8]}...{wallet[-4:]}" if len(wallet) > 14 else wallet


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
    finally:
        conn.close()


def get_json(conn: sqlite3.Connection, key: str, default: Any) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return default


def latest_prices(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT s.coin, s.price
        FROM signals s
        JOIN (
            SELECT coin, MAX(id) AS max_id
            FROM signals
            WHERE price IS NOT NULL
            GROUP BY coin
        ) latest ON latest.max_id = s.id
        """
    ).fetchall()
    return {str(row["coin"]): float(row["price"]) for row in rows if row["price"] is not None}


def live_prices() -> tuple[dict[str, float], str]:
    global _PRICE_CACHE, _PRICE_CACHE_TS, _PRICE_CACHE_ERROR
    now = time.time()
    if _PRICE_CACHE and now - _PRICE_CACHE_TS < PRICE_CACHE_SECONDS:
        return _PRICE_CACHE, "live"
    payload = json.dumps({"type": "allMids"}).encode("utf-8")
    request = Request(
        HL_INFO_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=6) as response:
            body = json.loads(response.read().decode("utf-8"))
        if isinstance(body, dict):
            prices = {str(coin): float(price) for coin, price in body.items() if price is not None}
            if prices:
                _PRICE_CACHE = prices
                _PRICE_CACHE_TS = now
                _PRICE_CACHE_ERROR = ""
                return _PRICE_CACHE, "live"
    except Exception as exc:
        _PRICE_CACHE_ERROR = str(exc)
    return _PRICE_CACHE, "cached-live" if _PRICE_CACHE else "stored"


def bot_live_equity(conn: sqlite3.Connection) -> tuple[float | None, str, float | None]:
    snapshot = get_json(conn, "live_equity_snapshot", {})
    observed = float(snapshot.get("observed_unix", 0) or 0)
    age = max(0.0, time.time() - observed) if observed else None
    if not snapshot.get("available") or snapshot.get("account_value") is None:
        return None, "unavailable", age
    if age is None or age > EQUITY_MAX_AGE_SECONDS:
        return None, "stale-bot-snapshot", age
    try:
        return float(snapshot["account_value"]), "bot-risk-feed", age
    except (TypeError, ValueError):
        return None, "invalid-bot-snapshot", age


def wallet_statuses(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT wallet, tier, total_score, sample_size, realized_pnl
        FROM marshal_wallet_scores s
        WHERE id = (
            SELECT MAX(id)
            FROM marshal_wallet_scores
            WHERE wallet = s.wallet
        )
        """
    ).fetchall()
    return {
        str(row["wallet"]): {
            "tier": str(row["tier"]),
            "score": float(row["total_score"]),
            "sample": int(row["sample_size"]),
            "realized": float(row["realized_pnl"]),
        }
        for row in rows
    }


def status_label(status: dict[str, Any] | None) -> str:
    if not status:
        return "Unscored"
    return f"{status['tier']} {float(status['score']):.1f}"


def open_allocations(
    conn: sqlite3.Connection,
    prices: dict[str, float],
    statuses: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, coin, side, source_wallet, entry_price, cost_basis, leverage, opened_at
        FROM paper_position_slices
        WHERE status = 'OPEN'
        ORDER BY opened_at DESC, id DESC
        """
    ).fetchall()
    allocations: list[dict[str, Any]] = []
    for row in rows:
        coin = str(row["coin"])
        side = str(row["side"])
        entry = float(row["entry_price"])
        cost = float(row["cost_basis"])
        leverage = float(row["leverage"])
        last = prices.get(coin)
        pnl_pct = None
        pnl_usd = None
        if last and entry:
            direction = 1.0 if side == "LONG" else -1.0
            pnl_pct = ((last - entry) / entry) * direction * 100.0
            pnl_usd = cost * leverage * (pnl_pct / 100.0)
        allocations.append(
            {
                "id": int(row["id"]),
                "coin": coin,
                "side": side,
                "wallet": str(row["source_wallet"]),
                "wallet_short": short_wallet(str(row["source_wallet"])),
                "wallet_status": status_label(statuses.get(str(row["source_wallet"]))),
                "entry_price": entry,
                "last_price": last,
                "cost_basis": cost,
                "leverage": leverage,
                "opened_at": row["opened_at"],
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
            }
        )
    return allocations


def aggregate_positions(allocations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for alloc in allocations:
        key = (alloc["coin"], alloc["side"])
        group = grouped.setdefault(
            key,
            {
                "coin": alloc["coin"],
                "side": alloc["side"],
                "cost_basis": 0.0,
                "weighted_entry_sum": 0.0,
                "weighted_leverage_sum": 0.0,
                "weighted_pnl_pct_sum": 0.0,
                "pnl_usd": 0.0,
                "pnl_known": True,
                "allocation_count": 0,
                "wallets": [],
                "wallet_statuses": [],
                "opened_times": [],
                "last_price": alloc["last_price"],
            },
        )
        cost = float(alloc["cost_basis"])
        group["cost_basis"] += cost
        group["weighted_entry_sum"] += float(alloc["entry_price"]) * cost
        group["weighted_leverage_sum"] += float(alloc["leverage"]) * cost
        if alloc["pnl_pct"] is not None:
            group["weighted_pnl_pct_sum"] += float(alloc["pnl_pct"]) * cost
        group["allocation_count"] += 1
        group["wallets"].append(alloc["wallet_short"])
        group["wallet_statuses"].append(alloc["wallet_status"])
        group["opened_times"].append(alloc["opened_at"])
        group["last_price"] = alloc["last_price"] or group["last_price"]
        if alloc["pnl_usd"] is None:
            group["pnl_known"] = False
        else:
            group["pnl_usd"] += float(alloc["pnl_usd"])

    positions: list[dict[str, Any]] = []
    for group in grouped.values():
        avg_entry = group["weighted_entry_sum"] / group["cost_basis"] if group["cost_basis"] else 0.0
        pnl_usd = group["pnl_usd"] if group["pnl_known"] else None
        pnl_pct = (
            group["weighted_pnl_pct_sum"] / group["cost_basis"]
            if pnl_usd is not None and group["cost_basis"]
            else None
        )
        positions.append(
            {
                "coin": group["coin"],
                "side": group["side"],
                "cost_basis": group["cost_basis"],
                "entry_price": avg_entry,
                "leverage": group["weighted_leverage_sum"] / group["cost_basis"] if group["cost_basis"] else 0.0,
                "last_price": group["last_price"],
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "allocation_count": group["allocation_count"],
                "wallets": group["wallets"][:4] + (["..."] if len(group["wallets"]) > 4 else []),
                "wallet_statuses": group["wallet_statuses"][:4]
                + (["..."] if len(group["wallet_statuses"]) > 4 else []),
                "opened_times": group["opened_times"][:4]
                + (["..."] if len(group["opened_times"]) > 4 else []),
            }
        )
    # Keep the most recently opened position at the top.  A position can have
    # multiple wallet allocations, so its newest allocation controls the row's
    # recency and the allocation timestamps are newest-first as well.
    for position in positions:
        position["opened_times"].sort(reverse=True)
    return sorted(
        positions,
        key=lambda p: max(p["opened_times"], default=""),
        reverse=True,
    )


def recent_rows(conn: sqlite3.Connection, table_sql: str, limit: int = 12) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(table_sql, (limit,)).fetchall()]


def dashboard_data() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"ok": False, "error": f"Database not found: {DB_PATH}"}

    with connect() as conn:
        acct = get_json(conn, "paper_account", {"cash": 0.0, "realized_pnl": 0.0})
        identity = get_json(conn, "live_account_identity", {}) if MODE == "live" else {}
        capital = get_json(conn, "live_capital_snapshot", {}) if MODE == "live" else {}
        backup_status = get_json(conn, "backup_status", {}) if MODE == "live" else {}
        backup_success_unix = float(backup_status.get("successful_unix", 0) or 0)
        backup_status["age_seconds"] = (
            max(0.0, time.time() - backup_success_unix)
            if backup_success_unix else None
        )
        last_entry_rollback = get_json(conn, "last_entry_rollback", {}) if MODE == "live" else {}
        if MODE == "live":
            risk_baseline = get_json(conn, "live_risk_baseline", {})
            starting_equity = float(
                identity.get("initial_account_value")
                or risk_baseline.get("start_value")
                or acct.get("cash")
                or 0.0
            )
            risk_reference = float(
                risk_baseline.get("high_water_value")
                or risk_baseline.get("start_value")
                or starting_equity
                or acct.get("cash")
                or 0.0
            )
            baseline_source = "live-initial-equity"
        else:
            starting_equity = PAPER_STARTING_EQUITY
            risk_reference = starting_equity
            baseline_source = "paper-starting-equity"
        stored_prices = latest_prices(conn)
        live_price_map, price_source = live_prices()
        prices = dict(stored_prices)
        prices.update(live_price_map)
        statuses = wallet_statuses(conn)
        allocations = open_allocations(conn, prices, statuses)
        positions = aggregate_positions(allocations)
        live_position_snapshot = (
            get_json(conn, "live_position_snapshot", {}) if MODE == "live" else {}
        )
        for position in positions:
            live_position = live_position_snapshot.get(position["coin"], {})
            position["exchange_leverage"] = live_position.get("leverage")
            position["exchange_size"] = live_position.get("size")
            position["size_difference"] = live_position.get("size_difference")
            position["size_tolerance"] = live_position.get("size_tolerance")
        open_cost = sum(float(a["cost_basis"]) for a in allocations)
        open_pnl_known = all(a["pnl_usd"] is not None for a in allocations)
        open_pnl = sum(float(a["pnl_usd"] or 0.0) for a in allocations) if open_pnl_known else None
        cash = float(acct.get("cash", 0.0))
        realized = float(acct.get("realized_pnl", 0.0))
        local_estimate = cash + open_cost + (open_pnl or 0.0)
        account_wallet = str(identity.get("wallet", ""))
        if MODE == "live":
            live_value, equity_source, equity_age_seconds = bot_live_equity(conn)
            estimated_value = live_value
        else:
            equity_source = "paper-ledger"
            equity_age_seconds = None
            estimated_value = local_estimate
        drawdown = (
            ((risk_reference - estimated_value) / risk_reference * 100.0)
            if risk_reference and estimated_value is not None
            else None
        )

        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM roster WHERE status = 'follow') AS roster,
                (SELECT COUNT(*) FROM signals) AS signals,
                (SELECT COUNT(*) FROM signals WHERE signal = 'EXIT' AND action = 'EXECUTED') AS exits,
                (SELECT COUNT(*) FROM api_failures) AS api_failures,
                (SELECT COUNT(*) FROM reconciliation_quarantine) AS quarantined,
                (SELECT COUNT(*) FROM execution_intents
                 WHERE state IN ('PREPARED', 'SUBMITTING', 'AMBIGUOUS')) AS unresolved_intents
            """
        ).fetchone()

        recent_closes = recent_rows(
            conn,
            """
            SELECT
                slices.closed_at AS ts,
                slices.source_wallet AS wallet,
                slices.coin,
                slices.side,
                slices.cost_basis,
                slices.leverage,
                slices.paper_gain,
                slices.pnl_pct,
                COALESCE(scores.tier, 'Unscored') AS wallet_tier,
                scores.total_score AS wallet_score
            FROM paper_position_slices slices
            LEFT JOIN (
                SELECT wallet, tier, total_score
                FROM marshal_wallet_scores s
                WHERE id = (
                    SELECT MAX(id)
                    FROM marshal_wallet_scores
                    WHERE wallet = s.wallet
                )
            ) scores ON scores.wallet = slices.source_wallet
            WHERE slices.status = 'CLOSED'
            ORDER BY slices.id DESC
            LIMIT ?
            """,
            10,
        )
        recent_failures = recent_rows(
            conn,
            """
            SELECT ts, operation, subject, error
            FROM api_failures
            ORDER BY id DESC
            LIMIT ?
            """,
            6,
        )
        token_risk_alerts = recent_rows(
            conn,
            """
            SELECT ts, coin, wallet, side, signal, reason, market_cap_rank, source
            FROM token_risk_events
            ORDER BY id DESC
            LIMIT ?
            """,
            8,
        )
        quarantines = recent_rows(
            conn,
            """
            SELECT coin, reason, details, quarantined_at, updated_at
            FROM reconciliation_quarantine
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            8,
        )
        recent_executions = recent_rows(
            conn,
            """
            SELECT
                executions.ts,
                executions.coin,
                executions.side,
                executions.operation,
                executions.requested_leverage,
                executions.leverage,
                executions.requested_size,
                executions.filled_size,
                executions.avg_fill_price,
                executions.reference_price,
                executions.slippage_bps,
                executions.price_source,
                executions.order_id,
                executions.exchange_status,
                executions.confirmed,
                executions.detail,
                COALESCE((
                    SELECT decisions.wallet_tier
                    FROM decision_audit decisions
                    WHERE decisions.coin = executions.coin
                      AND (executions.side IS NULL OR decisions.side = executions.side)
                      AND decisions.action = 'EXECUTED'
                      AND (
                        (executions.operation = 'OPEN' AND decisions.signal IN ('ENTRY', 'ADD'))
                        OR (executions.operation = 'CLOSE' AND decisions.signal = 'EXIT')
                      )
                      AND ABS(strftime('%s', decisions.ts) - strftime('%s', executions.ts)) <= 120
                    ORDER BY decisions.id DESC
                    LIMIT 1
                ), 'System') AS wallet_tier,
                (
                    SELECT decisions.wallet_score
                    FROM decision_audit decisions
                    WHERE decisions.coin = executions.coin
                      AND (executions.side IS NULL OR decisions.side = executions.side)
                      AND decisions.action = 'EXECUTED'
                      AND (
                        (executions.operation = 'OPEN' AND decisions.signal IN ('ENTRY', 'ADD'))
                        OR (executions.operation = 'CLOSE' AND decisions.signal = 'EXIT')
                      )
                      AND ABS(strftime('%s', decisions.ts) - strftime('%s', executions.ts)) <= 120
                    ORDER BY decisions.id DESC
                    LIMIT 1
                ) AS wallet_score
            FROM execution_audit executions
            ORDER BY executions.id DESC
            LIMIT ?
            """,
            8,
        )
        execution_intents = recent_rows(
            conn,
            """
            SELECT updated_at, coin, side, operation, requested_size, leverage,
                   cloid, state
            FROM execution_intents
            WHERE state IN ('PREPARED', 'SUBMITTING', 'AMBIGUOUS')
            ORDER BY id DESC
            LIMIT ?
            """,
            20,
        )

        return {
            "ok": True,
            "mode": MODE,
            "instance_label": "LIVE" if MODE == "live" else "PAPER",
            "account_wallet": short_wallet(account_wallet),
            "capital": capital,
            "backup_status": backup_status,
            "last_entry_rollback": last_entry_rollback,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "db_path": str(DB_PATH),
            "price_source": price_source,
            "price_error": _PRICE_CACHE_ERROR,
            "cash": cash,
            "realized_pnl": realized,
            "open_cost": open_cost,
            "open_pnl": open_pnl,
            "estimated_value": estimated_value,
            "local_estimate": local_estimate,
            "equity_source": equity_source,
            "equity_age_seconds": equity_age_seconds,
            "equity_error": (
                "Bot risk equity is unavailable or stale"
                if MODE == "live" and estimated_value is None else ""
            ),
            "drawdown_pct": None if drawdown is None else max(0.0, drawdown),
            "baseline": starting_equity,
            "baseline_source": baseline_source,
            "risk_reference": risk_reference,
            "counts": dict(counts) if counts else {},
            "positions": positions,
            "allocations": allocations,
            "recent_closes": recent_closes,
            "recent_failures": recent_failures,
            "token_risk_alerts": token_risk_alerts,
            "quarantines": quarantines,
            "recent_executions": recent_executions,
            "execution_intents": execution_intents,
        }


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MockingBot Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101114;
      --panel: #181a20;
      --line: #2a2e38;
      --text: #f1f3f7;
      --muted: #9aa3b2;
      --good: #55d28f;
      --bad: #ff6b6b;
      --warn: #f4bf5f;
      --accent: #78a8ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
      letter-spacing: 0;
    }
    header {
      background: rgba(16, 17, 20, 0.96);
      border-bottom: 1px solid var(--line);
      padding: 10px 14px;
    }
    .brand {
      display: grid;
      gap: 6px;
      min-width: 0;
      justify-items: center;
      text-align: center;
    }
    .brand img {
      width: clamp(150px, 36vw, 280px);
      height: auto;
      max-height: 64px;
      object-fit: contain;
    }
    .updated { color: var(--muted); font-size: .78rem; }
    .timezone-control {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: .72rem;
    }
    .timezone-control select {
      max-width: min(310px, 70vw);
      background: var(--panel);
      color: var(--text);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 7px;
      font: inherit;
    }
    .mode-badge {
      display: inline-block;
      border: 1px solid var(--accent);
      border-radius: 999px;
      padding: 4px 12px;
      color: var(--accent);
      font-size: .78rem;
      font-weight: 900;
      letter-spacing: .12em;
    }
    .mode-badge.live { color: var(--bad); border-color: var(--bad); }
    main { max-width: 1560px; margin: 0 auto; padding: 12px; }
    .stats {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .ops-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      padding: 8px;
    }
    .stat, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .stat { padding: 10px; min-height: 66px; }
    .label { color: var(--muted); font-size: .7rem; text-transform: uppercase; }
    .value { font-size: 1.12rem; font-weight: 800; margin-top: 4px; overflow-wrap: anywhere; }
    section { margin-top: 10px; overflow: hidden; }
    h2 {
      font-size: .88rem;
      margin: 0;
      padding: 10px;
      border-bottom: 1px solid var(--line);
    }
    details { margin: 0; }
    summary {
      cursor: pointer;
      list-style: none;
      font-size: .88rem;
      font-weight: 700;
      padding: 10px;
      border-bottom: 1px solid var(--line);
    }
    summary::-webkit-details-marker { display: none; }
    summary::after {
      content: "v";
      float: right;
      color: var(--muted);
    }
    details[open] summary::after { content: "^"; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 720px; }
    th, td {
      text-align: left;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      font-size: .84rem;
      white-space: nowrap;
    }
    th { color: var(--muted); font-size: .68rem; text-transform: uppercase; }
    tr:last-child td { border-bottom: 0; }
    .good { color: var(--good); }
    .bad { color: var(--bad); }
    .warn { color: var(--warn); }
    .muted { color: var(--muted); }
    .cell-list {
      display: grid;
      gap: 3px;
      line-height: 1.25;
    }
    .cell-list span { display: block; }
    .pill {
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      font-size: .78rem;
      color: var(--muted);
    }
    .alert-panel {
      border-color: rgba(244, 191, 95, .55);
    }
    .alert-panel h2 {
      color: var(--warn);
    }
    .empty { color: var(--muted); padding: 14px; }
    @media (min-width: 760px) {
      .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .ops-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .value { font-size: 1.28rem; }
    }
    @media (min-width: 1100px) {
      .stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
      .stat { min-height: 62px; padding: 9px 10px; }
      .label { font-size: .67rem; }
      .value { font-size: 1.16rem; }
    }
    @media (max-width: 640px) {
      header { padding: 8px 10px; }
      main { padding: 8px; }
      .stats { gap: 7px; }
      .stat { min-height: 58px; padding: 8px; }
      .value { font-size: 1rem; }
      .compact-table { min-width: 0; }
      .compact-table thead { display: none; }
      .compact-table, .compact-table tbody, .compact-table tr,
      .compact-table td { display: block; width: 100%; }
      .compact-table tr {
        padding: 7px 9px;
        border-bottom: 1px solid var(--line);
      }
      .compact-table tr:last-child { border-bottom: 0; }
      .compact-table td {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 14px;
        padding: 4px 0;
        border: 0;
        text-align: right;
        white-space: normal;
        font-size: .8rem;
      }
      .compact-table td::before {
        content: attr(data-label);
        color: var(--muted);
        flex: 0 0 auto;
        font-size: .65rem;
        font-weight: 700;
        text-transform: uppercase;
      }
      .compact-table .cell-list { justify-items: end; }
      table:not(.compact-table) { min-width: 620px; }
      th, td { padding: 8px; font-size: .78rem; }
      .updated { max-width: 94vw; line-height: 1.35; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <img src="/header.png" alt="MockingBot">
      <div class="mode-badge" id="mode-badge">...</div>
      <div class="updated" id="updated">Loading...</div>
      <label class="timezone-control">Timezone
        <select id="timezone"></select>
      </label>
    </div>
  </header>
  <main>
    <div class="stats" id="stats"></div>
    <section class="alert-panel" id="token-risk-section" hidden>
      <h2>Token Risk Alerts</h2>
      <div class="table-wrap"><table id="token-risk"></table></div>
    </section>
    <section class="alert-panel" id="quarantine-section" hidden>
      <h2>Quarantined Coins</h2>
      <div class="table-wrap"><table id="quarantines"></table></div>
    </section>
    <section>
      <h2>Open Positions</h2>
      <div class="table-wrap"><table class="compact-table" id="positions"></table></div>
    </section>
    <section>
      <h2>Recent Closes</h2>
      <div class="table-wrap"><table class="compact-table" id="closes"></table></div>
    </section>
    <section id="live-operations-section" hidden>
      <details>
        <summary>Live Diagnostics</summary>
        <div class="ops-grid" id="live-operations"></div>
      </details>
    </section>
    <section>
      <h2>Execution Confirmations</h2>
      <div class="table-wrap"><table class="compact-table" id="executions"></table></div>
    </section>
    <section class="alert-panel" id="execution-intents-section" hidden>
      <h2>Execution Attention Required</h2>
      <div class="table-wrap"><table class="compact-table" id="execution-intents"></table></div>
    </section>
    <section class="api-panel">
      <details>
      <summary>API Health</summary>
      <div id="failures"></div>
      </details>
    </section>
  </main>
  <script>
    const fmtMoney = v => v === null || v === undefined ? "n/a" : `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    const fmtPct = v => v === null || v === undefined ? "n/a" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
    const clsNum = v => v > 0 ? "good" : v < 0 ? "bad" : "";
    const shortWallet = w => !w ? "unknown" : (w.length > 14 ? `${w.slice(0, 8)}...${w.slice(-4)}` : w);
    const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const listCell = values => `<div class="cell-list">${(Array.isArray(values) ? values : [values]).map(v => `<span>${esc(v)}</span>`).join("")}</div>`;
    const timezoneSelect = document.getElementById("timezone");
    const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    const availableTimezones = typeof Intl.supportedValuesOf === "function"
      ? Intl.supportedValuesOf("timeZone")
      : [browserTimezone, "UTC"];
    const savedTimezone = localStorage.getItem("mockingbot-timezone") || browserTimezone;
    [...new Set([browserTimezone, "UTC", ...availableTimezones])].forEach(zone => {
      const option = document.createElement("option");
      option.value = zone;
      option.textContent = zone.replaceAll("_", " ");
      option.selected = zone === savedTimezone;
      timezoneSelect.appendChild(option);
    });
    if (![...timezoneSelect.options].some(option => option.selected)) {
      timezoneSelect.value = browserTimezone;
    }
    const fmtTime = (value, compact = false) => {
      if (!value || value === "...") return value || "";
      let raw = String(value).trim();
      if (!/[zZ]$|[+-]\d\d:?\d\d$/.test(raw)) raw = `${raw.replace(" ", "T")}Z`;
      const parsed = new Date(raw);
      if (Number.isNaN(parsed.getTime())) return String(value);
      return new Intl.DateTimeFormat(undefined, {
        timeZone: timezoneSelect.value || browserTimezone,
        month: "short", day: "numeric",
        ...(compact ? {} : {year: "numeric", second: "2-digit"}),
        hour: "numeric", minute: "2-digit", hour12: true,
      }).format(parsed);
    };
    const fmtTradeTime = value => fmtTime(value, true);
    timezoneSelect.addEventListener("change", () => {
      localStorage.setItem("mockingbot-timezone", timezoneSelect.value);
      load();
    });

    function table(el, headers, rows, emptyText) {
      if (!rows.length) {
        el.innerHTML = `<tbody><tr><td colspan="${headers.length}" class="empty">${emptyText}</td></tr></tbody>`;
        return;
      }
      el.innerHTML = `<thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody>`;
    }

    async function load() {
      const res = await fetch("/api/status", {cache: "no-store"});
      const data = await res.json();
      if (!data.ok) {
        document.getElementById("updated").textContent = data.error || "Dashboard error";
        return;
      }
      const priceLabel = data.price_source === "live"
        ? "live mids cached"
        : data.price_source === "cached-live"
          ? "cached mids"
          : "stored prices";
      const badge = document.getElementById("mode-badge");
      badge.textContent = data.instance_label;
      badge.className = `mode-badge ${data.mode === "live" ? "live" : "paper"}`;
      const accountLabel = data.account_wallet ? ` | account ${data.account_wallet}` : "";
      document.title = `MockingBot ${data.instance_label} Dashboard`;
      const equityLabel = data.mode === "live"
        ? ` | equity ${data.equity_source}${data.equity_age_seconds === null || data.equity_age_seconds === undefined ? "" : ` (${Math.round(data.equity_age_seconds)}s old)`}`
        : "";
      document.getElementById("updated").textContent = `Updated ${fmtTime(data.generated_at)} | read-only | ${priceLabel}${accountLabel}${equityLabel}`;
      const c = data.counts || {};
      const capital = data.capital || {};
      const rollback = data.last_entry_rollback || {};
      const backup = data.backup_status || {};
      const cards = [
        [data.mode === "live" ? "Live Equity" : "Paper Value", fmtMoney(data.estimated_value), data.estimated_value === null || data.estimated_value === undefined ? "bad" : clsNum(data.estimated_value - data.baseline)],
        ["Starting Equity", fmtMoney(data.baseline), ""],
        ["Drawdown", fmtPct(data.drawdown_pct === null || data.drawdown_pct === undefined ? null : -data.drawdown_pct), data.drawdown_pct > 0 ? "bad" : ""],
        ["Cash", fmtMoney(data.cash), ""],
        ["Positions", String(data.positions.length), ""],
        ["Realized PnL", fmtMoney(data.realized_pnl), clsNum(data.realized_pnl)],
        ["Open PnL", fmtMoney(data.open_pnl), clsNum(data.open_pnl)],
        ["Closed Trades", String(c.exits ?? 0), ""],
      ];
      document.getElementById("stats").innerHTML = cards.map(([label, value, klass]) => `<div class="stat"><div class="label">${label}</div><div class="value ${klass}">${value}</div></div>`).join("");

      const liveOperationsSection = document.getElementById("live-operations-section");
      liveOperationsSection.hidden = data.mode !== "live";
      if (data.mode === "live") {
        const liveOperations = [
          ["Local Ledger Estimate", fmtMoney(data.local_estimate), ""],
          ["Available Margin", fmtMoney(capital.available_margin), ""],
          ["Usable Margin", fmtMoney(capital.usable_margin), ""],
          ["Ledger Variance", fmtMoney(capital.equity_variance), clsNum(-(capital.equity_variance || 0))],
          ["Last Rollback", !rollback.ts ? "none" : (rollback.rollback_confirmed ? "confirmed" : "FAILED"), rollback.ts && !rollback.rollback_confirmed ? "bad" : ""],
          ["Last Backup", !backup.successful_at ? "none" : `${Math.round((backup.age_seconds || 0) / 60)}m ago`, !backup.successful_at || backup.error ? "bad" : "good"],
        ];
        document.getElementById("live-operations").innerHTML = liveOperations.map(([label, value, klass]) => `<div class="stat"><div class="label">${label}</div><div class="value ${klass}">${value}</div></div>`).join("");
      }

      const tokenRiskSection = document.getElementById("token-risk-section");
      const tokenRisk = data.token_risk_alerts || [];
      tokenRiskSection.hidden = tokenRisk.length === 0;
      if (tokenRisk.length) {
        table(document.getElementById("token-risk"), ["Time", "Coin", "Side", "Signal", "Wallet", "Reason"],
          tokenRisk.map(t => `<tr>
            <td class="muted">${esc(fmtTime(t.ts))}</td>
            <td><strong>${esc(t.coin)}</strong></td><td>${esc(t.side)}</td><td>${esc(t.signal)}</td>
            <td class="muted">${esc(shortWallet(t.wallet))}</td>
            <td class="warn">${esc(t.reason || "")}</td>
          </tr>`), "No token risk alerts.");
      }

      const quarantineSection = document.getElementById("quarantine-section");
      const quarantines = data.quarantines || [];
      quarantineSection.hidden = quarantines.length === 0;
      if (quarantines.length) {
        table(document.getElementById("quarantines"), ["Coin", "Reason", "Details", "Since", "Updated"],
          quarantines.map(q => `<tr>
            <td><strong>${esc(q.coin)}</strong></td><td class="bad">${esc(q.reason)}</td>
            <td class="warn">${esc(q.details || "")}</td>
            <td class="muted">${esc(fmtTime(q.quarantined_at))}</td><td class="muted">${esc(fmtTime(q.updated_at))}</td>
          </tr>`), "No quarantined coins.");
      }

      table(document.getElementById("positions"), ["Market", "Opened", "Margin", "Entry", "Last", "Open PnL", "Source", "Tier"],
        data.positions.map(p => `<tr>
          <td data-label="Market"><strong>${esc(p.coin)}</strong> <span class="pill">${esc(p.side)}</span>${p.allocation_count > 1 ? ` <span class="muted">×${p.allocation_count}</span>` : ""}</td>
          <td data-label="Opened" class="muted">${listCell((p.opened_times || []).map(fmtTradeTime))}</td>
          <td data-label="Margin">${fmtMoney(p.cost_basis)}</td>
          <td data-label="Entry">${Number(p.entry_price).toLocaleString(undefined, {maximumFractionDigits: 6})}</td>
          <td data-label="Last">${p.last_price ? Number(p.last_price).toLocaleString(undefined, {maximumFractionDigits: 6}) : "n/a"}</td>
          <td data-label="Open PnL" class="${clsNum(p.pnl_usd)}">${fmtMoney(p.pnl_usd)} <span class="muted">${fmtPct(p.pnl_pct)}</span></td>
          <td data-label="Source" class="muted">${listCell(p.wallets || [])}</td>
          <td data-label="Tier" class="muted">${listCell(p.wallet_statuses || [])}</td>
        </tr>`), "No open positions.");

      table(document.getElementById("closes"), ["Closed", "Market", "Margin", "Result", "Source", "Tier"],
        data.recent_closes.map(s => `<tr>
          <td data-label="Closed" class="muted">${esc(fmtTradeTime(s.ts))}</td>
          <td data-label="Market"><strong>${esc(s.coin)}</strong> <span class="pill">${esc(s.side)}</span></td>
          <td data-label="Margin">${fmtMoney(s.cost_basis)}</td>
          <td data-label="Result" class="${clsNum(s.paper_gain)}">${fmtMoney(s.paper_gain)} <span class="muted">${fmtPct(s.pnl_pct)}</span></td>
          <td data-label="Source" class="muted">${esc(shortWallet(s.wallet))}</td>
          <td data-label="Tier" class="muted">${esc(s.wallet_tier === "Unscored" ? "Unscored" : `${s.wallet_tier} ${Number(s.wallet_score).toFixed(1)}`)}</td>
        </tr>`), "No executed closes yet.");

      table(document.getElementById("executions"), ["Time", "Market", "Action", "Tier", "Filled", "Avg Fill", "Status"],
        (data.recent_executions || []).map(x => `<tr>
          <td data-label="Time" class="muted">${esc(fmtTradeTime(x.ts))}</td>
          <td data-label="Market"><strong>${esc(x.coin)}</strong> <span class="pill">${esc(x.side || "")}</span></td>
          <td data-label="Action">${esc(x.operation)}</td>
          <td data-label="Tier" class="muted">${esc(x.wallet_tier === "System" ? "System" : `${x.wallet_tier} ${Number(x.wallet_score).toFixed(1)}`)}</td>
          <td data-label="Filled">${Number(x.filled_size || 0).toLocaleString(undefined, {maximumFractionDigits: 8})}</td>
          <td data-label="Avg Fill">${x.avg_fill_price ? Number(x.avg_fill_price).toLocaleString(undefined, {maximumFractionDigits: 6}) : "n/a"}</td>
          <td data-label="Status" class="${x.confirmed ? "good" : "bad"}">${x.confirmed ? "Confirmed" : `FAILED${x.detail ? ` · ${esc(x.detail)}` : ""}`}</td>
        </tr>`), "No execution records yet.");

      const unresolvedIntents = data.execution_intents || [];
      document.getElementById("execution-intents-section").hidden = unresolvedIntents.length === 0;
      table(document.getElementById("execution-intents"), ["Updated", "Market", "Action", "State"],
        unresolvedIntents.map(x => `<tr>
          <td data-label="Updated" class="muted">${esc(fmtTradeTime(x.updated_at))}</td>
          <td data-label="Market"><strong>${esc(x.coin)}</strong> <span class="pill">${esc(x.side || "")}</span></td>
          <td data-label="Action">${esc(x.operation)}</td>
          <td data-label="State" class="bad">${esc(x.state)}</td>
        </tr>`), "No unresolved exchange intents.");

      const failures = document.getElementById("failures");
      if (!data.recent_failures.length) {
        failures.innerHTML = `<div class="empty">No API failures recorded.</div>`;
      } else {
        failures.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Time</th><th>Operation</th><th>Subject</th><th>Error</th></tr></thead><tbody>${data.recent_failures.map(f => `<tr>
          <td class="muted">${esc(fmtTime(f.ts))}</td><td>${esc(f.operation)}</td><td>${esc(f.subject || "")}</td><td class="warn">${esc(f.error || "")}</td>
        </tr>`).join("")}</tbody></table></div>`;
      }
    }
    load();
    setInterval(load, 5000);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            payload = json.dumps(dashboard_data(), default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path in {"/", "/index.html"}:
            payload = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/header.png":
            if not HEADER_IMAGE_PATH.exists():
                self.send_error(404, f"Header image not found: {HEADER_IMAGE_PATH}")
                return
            payload = HEADER_IMAGE_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)
        return


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"MockingBot {MODE.upper()} dashboard running at http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
