from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


API_URL = "https://api.hyperliquid.xyz/info"
INTERVAL_MS = 15 * 60 * 1000


def parse_utc(value: str) -> int:
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def load_trades(
    db_path: Path, opened_after: str | None, closed_before: str | None
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        filters = [
            "status = 'CLOSED'",
            "marshal_tier IN ('Core', 'Elite')",
            "entry_price > 0",
            "exit_price IS NOT NULL",
            "closed_at IS NOT NULL",
        ]
        params: list[str] = []
        if opened_after:
            filters.append("datetime(opened_at) > datetime(?)")
            params.append(opened_after)
        if closed_before:
            filters.append("datetime(closed_at) <= datetime(?)")
            params.append(closed_before)
        rows = conn.execute(
            """
            SELECT id, wallet, coin, side, entry_price, opened_at, exit_price,
                   closed_at, pnl_pct, marshal_score, marshal_tier
            FROM marshal_shadow_positions
            WHERE """ + " AND ".join(filters) + """
            ORDER BY opened_at, id
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def cache_path(cache_dir: Path, coin: str) -> Path:
    safe = coin.replace(":", "_").replace("/", "_")
    return cache_dir / f"{safe}-15m.json"


def fetch_candles(
    coin: str,
    start_ms: int,
    end_ms: int,
    cache_dir: Path,
    throttle_seconds: float,
) -> list[dict[str, Any]]:
    path = cache_path(cache_dir, coin)
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("start_ms", 0) <= start_ms and cached.get("end_ms", 0) >= end_ms:
            return list(cached["candles"])

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": "15m",
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    error: Exception | None = None
    for attempt in range(5):
        try:
            response = requests.post(API_URL, json=payload, timeout=30)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, list):
                raise RuntimeError(f"unexpected candle response for {coin}: {body!r}")
            candles = sorted(body, key=lambda row: int(row["t"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "coin": coin,
                        "interval": "15m",
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "downloaded_at": datetime.now(timezone.utc).isoformat(),
                        "candles": candles,
                    }
                ),
                encoding="utf-8",
            )
            time.sleep(throttle_seconds)
            return candles
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            error = exc
            time.sleep(max(throttle_seconds, 2**attempt))
    raise RuntimeError(f"unable to download candles for {coin}: {error}")


def stop_result(
    trade: dict[str, Any],
    candles: list[dict[str, Any]],
    threshold_pct: float,
    slippage_bps: float,
) -> dict[str, Any]:
    opened_ms = parse_utc(str(trade["opened_at"]))
    closed_ms = parse_utc(str(trade["closed_at"]))
    entry = float(trade["entry_price"])
    side = str(trade["side"])
    threshold = threshold_pct / 100.0
    stop_price = entry * (1.0 - threshold if side == "LONG" else 1.0 + threshold)
    first_full_candle = math.ceil(opened_ms / INTERVAL_MS) * INTERVAL_MS
    slip = slippage_bps / 10_000.0

    for candle in candles:
        candle_start = int(candle["t"])
        if candle_start < first_full_candle or candle_start > closed_ms:
            continue
        candle_open = float(candle["o"])
        adverse = float(candle["l"] if side == "LONG" else candle["h"])
        hit = adverse <= stop_price if side == "LONG" else adverse >= stop_price
        if not hit:
            continue
        if side == "LONG":
            base_fill = min(stop_price, candle_open)
            fill = base_fill * (1.0 - slip)
            stopped_return = (fill - entry) / entry * 100.0
        else:
            base_fill = max(stop_price, candle_open)
            fill = base_fill * (1.0 + slip)
            stopped_return = (entry - fill) / entry * 100.0
        return {
            "stopped": True,
            "stop_time_ms": candle_start,
            "stop_fill": fill,
            "return_pct": stopped_return,
        }
    return {
        "stopped": False,
        "stop_time_ms": None,
        "stop_fill": None,
        "return_pct": float(trade["pnl_pct"]),
    }


def summarize(rows: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    returns = [float(row[f"{policy}_return_pct"]) for row in rows]
    wins = sum(value > 0 for value in returns)
    stopped = [row for row in rows if row.get(f"{policy}_stopped", False)]
    no_stop_returns = [float(row["no_stop_return_pct"]) for row in rows]
    ordered_equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in returns:
        ordered_equity += value
        peak = max(peak, ordered_equity)
        max_drawdown = max(max_drawdown, peak - ordered_equity)
    return {
        "trades": len(rows),
        "total_normalized_return_pct": round(sum(returns), 4),
        "mean_return_pct": round(sum(returns) / len(returns), 4),
        "median_return_pct": round(sorted(returns)[len(returns) // 2], 4),
        "win_rate_pct": round(wins / len(rows) * 100.0, 2),
        "worst_trade_pct": round(min(returns), 4),
        "path_max_drawdown_pct_points": round(max_drawdown, 4),
        "stops_triggered": len(stopped),
        "stopped_eventual_winners": sum(
            float(row["no_stop_return_pct"]) > 0 for row in stopped
        ),
        "stopped_trades_that_recovered_above_stop": sum(
            float(row["no_stop_return_pct"]) > float(row[f"{policy}_return_pct"])
            for row in stopped
        ),
        "return_delta_vs_no_stop_pct_points": round(
            sum(returns) - sum(no_stop_returns), 4
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare 12%, 15%, and no-stop policies")
    parser.add_argument("--db", type=Path, default=Path("MockingBot_Data/mockingbot_codex.sqlite3"))
    parser.add_argument("--output-dir", type=Path, default=Path("MockingBot_Data/stop_loss_analysis"))
    parser.add_argument("--slippage-bps", type=float, nargs="+", default=[0.0, 10.0, 25.0])
    parser.add_argument("--throttle-seconds", type=float, default=3.5)
    parser.add_argument("--opened-after")
    parser.add_argument("--closed-before")
    args = parser.parse_args()

    trades = load_trades(args.db, args.opened_after, args.closed_before)
    if not trades:
        raise SystemExit("No completed Core/Elite shadow trades found")
    ranges: dict[str, tuple[int, int]] = {}
    for trade in trades:
        start = parse_utc(str(trade["opened_at"]))
        end = parse_utc(str(trade["closed_at"]))
        old = ranges.get(str(trade["coin"]))
        ranges[str(trade["coin"])] = (
            min(start, old[0]) if old else start,
            max(end, old[1]) if old else end,
        )

    cache_dir = args.output_dir / "candle_cache"
    candle_map: dict[str, list[dict[str, Any]]] = {}
    for index, (coin, (start, end)) in enumerate(sorted(ranges.items()), 1):
        print(f"[{index}/{len(ranges)}] candles {coin}", flush=True)
        candle_map[coin] = fetch_candles(
            coin, start - INTERVAL_MS, end + INTERVAL_MS,
            cache_dir, args.throttle_seconds,
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    all_reports: dict[str, Any] = {
        "sample": {
            "trades": len(trades),
            "coins": len(ranges),
            "first_opened_at": min(str(row["opened_at"]) for row in trades),
            "last_closed_at": max(str(row["closed_at"]) for row in trades),
            "tiers": dict(
                sorted(
                    (tier, sum(row["marshal_tier"] == tier for row in trades))
                    for tier in {str(row["marshal_tier"]) for row in trades}
                )
            ),
            "entry_candle_policy": "exclude partial entry candle",
            "candle_interval": "15m",
            "opened_after": args.opened_after,
            "closed_before": args.closed_before,
            "trade_ids": [int(row["id"]) for row in trades],
        },
        "scenarios": {},
    }
    detail_rows: list[dict[str, Any]] = []
    for slippage in args.slippage_bps:
        rows: list[dict[str, Any]] = []
        for trade in trades:
            row = dict(trade)
            row["no_stop_return_pct"] = float(trade["pnl_pct"])
            for threshold in (12.0, 15.0, 20.0):
                result = stop_result(trade, candle_map[str(trade["coin"])], threshold, slippage)
                key = f"stop_{int(threshold)}"
                row[f"{key}_stopped"] = result["stopped"]
                row[f"{key}_return_pct"] = result["return_pct"]
                row[f"{key}_time_ms"] = result["stop_time_ms"]
            row["slippage_bps"] = slippage
            rows.append(row)
        scenario = {
            "no_stop": summarize(rows, "no_stop"),
            "stop_12": summarize(rows, "stop_12"),
            "stop_15": summarize(rows, "stop_15"),
            "stop_20": summarize(rows, "stop_20"),
            "by_entry_tier": {},
        }
        worst_no_stop = min(rows, key=lambda row: float(row["no_stop_return_pct"]))
        without_worst = [row for row in rows if row["id"] != worst_no_stop["id"]]
        leave_worst_out = {
            "omitted_trade": {
                key: worst_no_stop[key]
                for key in ("id", "coin", "side", "marshal_tier", "no_stop_return_pct")
            }
        }
        leave_worst_out.update({
            policy: summarize(without_worst, policy)
            for policy in ("no_stop", "stop_12", "stop_15", "stop_20")
        })
        scenario["leave_worst_trade_out"] = leave_worst_out
        for tier in ("Core", "Elite"):
            subset = [row for row in rows if row["marshal_tier"] == tier]
            scenario["by_entry_tier"][tier] = {
                policy: summarize(subset, policy)
                for policy in ("no_stop", "stop_12", "stop_15", "stop_20")
            }
        all_reports["scenarios"][f"slippage_{slippage:g}_bps"] = scenario
        detail_rows.extend(rows)

    report_path = args.output_dir / "summary.json"
    report_path.write_text(json.dumps(all_reports, indent=2), encoding="utf-8")
    detail_path = args.output_dir / "trade_details.csv"
    with detail_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(detail_rows[0]))
        writer.writeheader()
        writer.writerows(detail_rows)
    print(json.dumps(all_reports, indent=2))
    print(f"Wrote {report_path} and {detail_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
