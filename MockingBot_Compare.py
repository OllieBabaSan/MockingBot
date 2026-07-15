"""Read-only parity comparison for the paper and live MockingBot instances."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_PAPER_DB = ROOT / "MockingBot_Data" / "mockingbot_codex.sqlite3"
DEFAULT_LIVE_DB = ROOT / "MockingBot_Main_Live_Test_Data" / "mockingbot_codex.sqlite3"
DEFAULT_LOG = ROOT / "MockingBot_Comparison_Data" / "parity.jsonl"
MATCH_FIELDS = ("wallet", "coin", "side", "signal")
STATE_REASONS = {
    "position cap",
    "coin already held",
    "paper rejected",
    "wallet allocation already held",
    "same-wallet add cap",
    "opposite side already held",
    "wind-down",
    "live positions unknown",
}


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def read_audit(path: Path, since: datetime) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_audit'"
        ).fetchone()
        if not exists:
            return []
        rows = conn.execute(
            "SELECT * FROM decision_audit WHERE ts >= ? ORDER BY ts, id",
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()
    return [dict(row) for row in rows]


def classify(paper: dict[str, Any], live: dict[str, Any], delta_seconds: float) -> tuple[str, str]:
    if paper["config_fingerprint"] != live["config_fingerprint"]:
        return "CONFIG_DIVERGENCE", "decision parameters differ"
    if paper["code_fingerprint"] != live["code_fingerprint"]:
        return "CODE_DIVERGENCE", "source versions differ"
    if paper["wallet_tier"] != live["wallet_tier"] or abs(
        float(paper["wallet_score"]) - float(live["wallet_score"])
    ) > 0.05:
        return "SCORE_DIVERGENCE", "wallet tier or score differs"
    if paper["action"] != live["action"] or (paper.get("reason") or "") != (live.get("reason") or ""):
        reasons = {(paper.get("reason") or ""), (live.get("reason") or "")}
        if reasons & STATE_REASONS:
            return "STATE_DIVERGENCE", "capacity, holdings, or risk state differs"
        return "LOGIC_DIVERGENCE", "same inputs produced a different decision"
    if delta_seconds > 45:
        return "TIMING_VARIANCE", f"observed {delta_seconds:.0f}s apart"
    paper_price = paper.get("observed_price")
    live_price = live.get("observed_price")
    if paper_price and live_price:
        variance = abs(float(paper_price) - float(live_price)) / float(paper_price)
        if variance > 0.002:
            return "EXECUTION_VARIANCE", f"observed prices differ by {variance:.2%}"
    return "MATCH", "same decision chain"


def compare(
    paper_rows: list[dict[str, Any]],
    live_rows: list[dict[str, Any]],
    tolerance_seconds: int,
) -> list[dict[str, Any]]:
    live_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in live_rows:
        live_by_key[tuple(row[field] for field in MATCH_FIELDS)].append(row)
    used: set[int] = set()
    results: list[dict[str, Any]] = []

    for paper in paper_rows:
        key = tuple(paper[field] for field in MATCH_FIELDS)
        paper_ts = parse_ts(str(paper["ts"]))
        candidates = []
        for live in live_by_key.get(key, []):
            if int(live["id"]) in used:
                continue
            delta = abs((parse_ts(str(live["ts"])) - paper_ts).total_seconds())
            if delta <= tolerance_seconds:
                candidates.append((delta, live))
        if not candidates:
            results.append({"classification": "MISSING_SIGNAL", "side": "live", "paper": paper})
            continue
        delta, live = min(candidates, key=lambda item: item[0])
        used.add(int(live["id"]))
        classification, detail = classify(paper, live, delta)
        results.append(
            {
                "classification": classification,
                "detail": detail,
                "delta_seconds": delta,
                "paper": paper,
                "live": live,
            }
        )

    for live in live_rows:
        if int(live["id"]) not in used:
            results.append({"classification": "MISSING_SIGNAL", "side": "paper", "live": live})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-db", type=Path, default=DEFAULT_PAPER_DB)
    parser.add_argument("--live-db", type=Path, default=DEFAULT_LIVE_DB)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--tolerance-seconds", type=int, default=180)
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(hours=max(0.0, args.hours))
    paper_rows = read_audit(args.paper_db, since)
    live_rows = read_audit(args.live_db, since)
    results = compare(paper_rows, live_rows, max(1, args.tolerance_seconds))
    counts = Counter(result["classification"] for result in results)
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "since": since.strftime("%Y-%m-%d %H:%M:%S"),
        "paper_db": str(args.paper_db.resolve()),
        "live_db": str(args.live_db.resolve()),
        "paper_events": len(paper_rows),
        "live_events": len(live_rows),
        "counts": dict(sorted(counts.items())),
        "divergences": [r for r in results if r["classification"] != "MATCH"],
    }
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, separators=(",", ":"), default=str) + "\n")

    print("=== MOCKINGBOT PARITY ===")
    print(f"Paper events: {len(paper_rows)}")
    print(f"Live events:  {len(live_rows)}")
    for name, count in sorted(counts.items()):
        print(f"{name:20} {count}")
    print(f"Log: {args.log}")
    return 2 if any(name.endswith("DIVERGENCE") for name in counts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
