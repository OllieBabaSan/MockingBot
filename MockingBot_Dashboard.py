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

_PRICE_CACHE: dict[str, float] = {}
_PRICE_CACHE_TS = 0.0
_PRICE_CACHE_ERROR = ""
_ACCOUNT_VALUE_CACHE: float | None = None
_ACCOUNT_VALUE_CACHE_TS = 0.0
_ACCOUNT_VALUE_CACHE_ERROR = ""


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


def live_account_value(wallet: str) -> tuple[float | None, str]:
    global _ACCOUNT_VALUE_CACHE, _ACCOUNT_VALUE_CACHE_TS, _ACCOUNT_VALUE_CACHE_ERROR
    if not wallet:
        return None, "missing-wallet"
    now = time.time()
    if _ACCOUNT_VALUE_CACHE is not None and now - _ACCOUNT_VALUE_CACHE_TS < PRICE_CACHE_SECONDS:
        return _ACCOUNT_VALUE_CACHE, "live"
    payload = json.dumps({"type": "clearinghouseState", "user": wallet}).encode("utf-8")
    request = Request(
        HL_INFO_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=6) as response:
            body = json.loads(response.read().decode("utf-8"))
        value = float(body.get("marginSummary", {}).get("accountValue") or 0)
        if value >= 0:
            _ACCOUNT_VALUE_CACHE = value
            _ACCOUNT_VALUE_CACHE_TS = now
            _ACCOUNT_VALUE_CACHE_ERROR = ""
            return value, "live"
    except Exception as exc:
        _ACCOUNT_VALUE_CACHE_ERROR = str(exc)
    return (
        _ACCOUNT_VALUE_CACHE,
        "cached-live" if _ACCOUNT_VALUE_CACHE is not None else "local-estimate",
    )


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
        ORDER BY coin, side, opened_at, id
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
            }
        )
    return sorted(positions, key=lambda p: (p["coin"], p["side"]))


def recent_rows(conn: sqlite3.Connection, table_sql: str, limit: int = 12) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(table_sql, (limit,)).fetchall()]


def dashboard_data() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"ok": False, "error": f"Database not found: {DB_PATH}"}

    with connect() as conn:
        acct = get_json(conn, "paper_account", {"cash": 0.0, "realized_pnl": 0.0})
        identity = get_json(conn, "live_account_identity", {}) if MODE == "live" else {}
        capital = get_json(conn, "live_capital_snapshot", {}) if MODE == "live" else {}
        if MODE == "live":
            risk_baseline = get_json(conn, "live_risk_baseline", {})
            baseline = float(
                risk_baseline.get("start_value")
                or identity.get("initial_account_value")
                or acct.get("cash")
                or 0.0
            )
        else:
            session = get_json(conn, "session", {})
            baseline = float(session.get("paper_start") or 10_000.0)
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
        live_value, equity_source = (
            live_account_value(account_wallet) if MODE == "live" else (None, "paper-ledger")
        )
        estimated_value = live_value if live_value is not None else local_estimate
        drawdown = ((baseline - estimated_value) / baseline * 100.0) if baseline else 0.0

        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM roster WHERE status = 'follow') AS roster,
                (SELECT COUNT(*) FROM paused_wallets) AS paused,
                (SELECT COUNT(*) FROM signals) AS signals,
                (SELECT COUNT(*) FROM signals WHERE signal = 'EXIT' AND action = 'EXECUTED') AS exits,
                (SELECT COUNT(*) FROM api_failures) AS api_failures,
                (SELECT COUNT(*) FROM reconciliation_quarantine) AS quarantined
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
            20,
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
            20,
        )
        recent_executions = recent_rows(
            conn,
            """
            SELECT ts, coin, side, operation, requested_leverage, leverage, requested_size, filled_size,
                   avg_fill_price, reference_price, slippage_bps, price_source,
                   order_id, exchange_status, confirmed, detail
            FROM execution_audit
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
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
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
            "equity_error": _ACCOUNT_VALUE_CACHE_ERROR if MODE == "live" else "",
            "drawdown_pct": max(0.0, drawdown),
            "baseline": baseline,
            "counts": dict(counts) if counts else {},
            "positions": positions,
            "allocations": allocations,
            "recent_closes": recent_closes,
            "recent_failures": recent_failures,
            "token_risk_alerts": token_risk_alerts,
            "quarantines": quarantines,
            "recent_executions": recent_executions,
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
    main { max-width: 1040px; margin: 0 auto; padding: 12px; }
    .stats {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
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
    details { margin-top: 10px; }
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
      .value { font-size: 1.28rem; }
    }
    @media (max-width: 640px) {
      header { padding: 8px 10px; }
      main { padding: 8px; }
      .stats { gap: 7px; }
      .stat { min-height: 58px; padding: 8px; }
      .value { font-size: 1rem; }
      table { min-width: 640px; }
      th, td { padding: 8px; font-size: .78rem; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <img src="/header.png" alt="MockingBot">
      <div class="mode-badge" id="mode-badge">...</div>
      <div class="updated" id="updated">Loading...</div>
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
      <div class="table-wrap"><table id="positions"></table></div>
    </section>
    <section>
      <h2>Recent Closes</h2>
      <div class="table-wrap"><table id="closes"></table></div>
    </section>
    <section>
      <h2>Execution Confirmations</h2>
      <div class="table-wrap"><table id="executions"></table></div>
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
      document.getElementById("updated").textContent = `Updated ${data.generated_at} | read-only | ${priceLabel}${accountLabel}`;
      const c = data.counts || {};
      const capital = data.capital || {};
      const cards = [
        ["Est. Value", fmtMoney(data.estimated_value), clsNum(data.estimated_value - data.baseline)],
        ["Cash", fmtMoney(data.cash), ""],
        ["Realized PnL", fmtMoney(data.realized_pnl), clsNum(data.realized_pnl)],
        ["Positions", String(data.positions.length), ""],
        ["Open PnL", fmtMoney(data.open_pnl), clsNum(data.open_pnl)],
        ["Closed Trades", String(c.exits ?? 0), ""],
        ["Quarantined", String(c.quarantined ?? 0), (c.quarantined ?? 0) > 0 ? "bad" : ""],
        ...(data.mode === "live" ? [
          ["Available Margin", fmtMoney(capital.available_margin), ""],
          ["Usable Margin", fmtMoney(capital.usable_margin), ""],
          ["Ledger Variance", fmtMoney(capital.equity_variance), clsNum(-(capital.equity_variance || 0))],
        ] : []),
      ];
      document.getElementById("stats").innerHTML = cards.map(([label, value, klass]) => `<div class="stat"><div class="label">${label}</div><div class="value ${klass}">${value}</div></div>`).join("");

      const tokenRiskSection = document.getElementById("token-risk-section");
      const tokenRisk = data.token_risk_alerts || [];
      tokenRiskSection.hidden = tokenRisk.length === 0;
      if (tokenRisk.length) {
        table(document.getElementById("token-risk"), ["Time", "Coin", "Side", "Signal", "Wallet", "Reason"],
          tokenRisk.map(t => `<tr>
            <td class="muted">${esc((t.ts || "").replace("T", " ").slice(5, 19))}</td>
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
            <td class="muted">${esc(q.quarantined_at || "")}</td><td class="muted">${esc(q.updated_at || "")}</td>
          </tr>`), "No quarantined coins.");
      }

      table(document.getElementById("positions"), ["Coin", "Side", "Alloc", "Local Lev", "HL Lev", "HL Size", "Δ / Tol", "Cost", "Entry", "Last", "Open PnL", "Wallets", "Status"],
        data.positions.map(p => `<tr>
          <td><strong>${esc(p.coin)}</strong></td><td><span class="pill">${esc(p.side)}</span></td>
          <td>${p.allocation_count}</td><td>${Number(p.leverage).toFixed(1)}x</td>
          <td>${p.exchange_leverage ? `${Number(p.exchange_leverage).toFixed(1)}x` : "n/a"}</td>
          <td>${p.exchange_size === null || p.exchange_size === undefined ? "n/a" : Number(p.exchange_size).toLocaleString()}</td>
          <td class="${p.size_difference > p.size_tolerance ? "bad" : "muted"}">${p.size_difference === null || p.size_difference === undefined ? "n/a" : `${Number(p.size_difference).toPrecision(3)} / ${Number(p.size_tolerance).toPrecision(3)}`}</td>
          <td>${fmtMoney(p.cost_basis)}</td>
          <td>${Number(p.entry_price).toLocaleString(undefined, {maximumFractionDigits: 6})}</td>
          <td>${p.last_price ? Number(p.last_price).toLocaleString(undefined, {maximumFractionDigits: 6}) : "n/a"}</td>
          <td class="${clsNum(p.pnl_usd)}">${fmtMoney(p.pnl_usd)} <span class="muted">${fmtPct(p.pnl_pct)}</span></td>
          <td class="muted">${listCell(p.wallets)}</td>
          <td class="muted">${listCell(p.wallet_statuses)}</td>
        </tr>`), "No open positions.");

      table(document.getElementById("closes"), ["Time", "Coin", "Side", "Wallet", "Status", "Lev", "Cost", "Result"],
        data.recent_closes.map(s => `<tr>
          <td class="muted">${esc((s.ts || "").replace("T", " ").slice(5, 19))}</td>
          <td><strong>${esc(s.coin)}</strong></td><td>${esc(s.side)}</td>
          <td class="muted">${esc(shortWallet(s.wallet))}</td>
          <td class="muted">${esc(s.wallet_tier === "Unscored" ? "Unscored" : `${s.wallet_tier} ${Number(s.wallet_score).toFixed(1)}`)}</td>
          <td>${Number(s.leverage).toFixed(1)}x</td>
          <td>${fmtMoney(s.cost_basis)}</td>
          <td class="${clsNum(s.paper_gain)}">${fmtMoney(s.paper_gain)} <span class="muted">${fmtPct(s.pnl_pct)}</span></td>
        </tr>`), "No executed closes yet.");

      table(document.getElementById("executions"), ["Time", "Coin", "Op", "Req Lev", "Effective", "Requested", "Filled", "Avg Fill", "Quote", "Slip", "Source", "Order", "Confirmed", "Detail"],
        (data.recent_executions || []).map(x => `<tr>
          <td class="muted">${esc((x.ts || "").slice(5, 19))}</td><td><strong>${esc(x.coin)}</strong></td>
          <td>${esc(x.operation)}</td>
          <td>${x.requested_leverage ? `${Number(x.requested_leverage).toFixed(1)}x` : "n/a"}</td>
          <td>${x.leverage ? `${Number(x.leverage).toFixed(1)}x` : "n/a"}</td><td>${Number(x.requested_size || 0).toLocaleString()}</td>
          <td>${Number(x.filled_size || 0).toLocaleString()}</td>
          <td>${x.avg_fill_price ? Number(x.avg_fill_price).toLocaleString(undefined, {maximumFractionDigits: 6}) : "n/a"}</td>
          <td>${x.reference_price ? Number(x.reference_price).toLocaleString(undefined, {maximumFractionDigits: 6}) : "n/a"}</td>
          <td class="${clsNum(-(x.slippage_bps || 0))}">${x.slippage_bps === null || x.slippage_bps === undefined ? "n/a" : `${Number(x.slippage_bps).toFixed(1)} bp`}</td>
          <td class="muted">${esc(x.price_source || "")}</td>
          <td class="muted">${esc(x.order_id || "")}</td>
          <td class="${x.confirmed ? "good" : "bad"}">${x.confirmed ? "yes" : "no"}</td>
          <td class="muted">${esc(x.detail || x.exchange_status || "")}</td>
        </tr>`), "No execution records yet.");

      const failures = document.getElementById("failures");
      if (!data.recent_failures.length) {
        failures.innerHTML = `<div class="empty">No API failures recorded.</div>`;
      } else {
        failures.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Time</th><th>Operation</th><th>Subject</th><th>Error</th></tr></thead><tbody>${data.recent_failures.map(f => `<tr>
          <td class="muted">${esc((f.ts || "").replace("T", " ").slice(5, 19))}</td><td>${esc(f.operation)}</td><td>${esc(f.subject || "")}</td><td class="warn">${esc(f.error || "")}</td>
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
