"""Discover and replay fixed research candidates without return-based tuning.

The frontier is intentionally small and explicit. It reuses the newest bulk
market-replay input snapshot, runs at most one missing fixed rule per action,
and records failed or unavailable candidates instead of hiding them. It is a
research consumer only: it never promotes a strategy or touches a broker.
"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

from .. import settings
from . import market_replay
from .experiment import write_json


FIXED_CANDIDATES = (
    # (catalogue id, market_replay runner id, source kind, description)
    ("risk_adjusted_momentum", "risk_adjusted_momentum", "fixed_local_rule", "Existing risk-adjusted momentum rule"),
    ("momentum_12_1_20", "momentum", "fixed_local_rule", "Existing 12-1 momentum rule"),
    ("low_vol_momentum", "low_vol_momentum", "fixed_local_rule", "Fixed 12-1 momentum gated to the lower half of the eligible 60-day volatility pool"),
    ("liquid_momentum", "liquid_momentum", "fixed_local_rule", "Fixed 12-1 momentum gated to the upper half of the eligible 20-day median traded-amount pool"),
    ("equal_weight", "equal_weight", "fixed_local_rule", "Existing equal-weight control"),
    ("donchian_trend", "donchian_trend", "fixed_local_rule", "Existing Donchian breakout candidate"),
)


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _records():
    root = settings.ROOT / "outputs" / "market_replay"
    values = []
    for path in root.glob("*/run.json") if root.is_dir() else ():
        record = _read(path)
        if not isinstance(record, dict) or not isinstance(record.get("inputs"), dict):
            continue
        values.append((path, record))
    return sorted(values, key=lambda item: item[0].stat().st_mtime, reverse=True)


def _input_key(record):
    protocol = record.get("protocol") or {}
    portfolio = protocol.get("portfolio") or {}
    execution_fields = (
        "initial_cash", "gross_limit", "single_limit", "commission",
        "minimum_commission", "sell_tax", "transfer_fee", "slippage_bps",
        "participation", "dividend_tax_reserve",
    )
    payload = {
        "inputs": record.get("inputs"),
        "start": record.get("start"),
        "end": record.get("end"),
        # Keep execution assumptions in the input identity, but leave the
        # candidate's signal/lookback/selection rule out so fixed candidates
        # remain comparable on the same data and cost path.
        "execution": {key: portfolio.get(key) for key in execution_fields},
        "adjusted_continuity_override": record.get("adjusted_continuity_override", False),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _latest_source(source=None):
    if source:
        path = Path(source).resolve()
        record = _read(path)
        if not isinstance(record, dict) or not isinstance(record.get("inputs"), dict):
            raise ValueError("Source run must contain explicit bulk replay inputs")
        return path, record
    for path, record in _records():
        if record.get("inputs", {}).get("price_view") and record.get("inputs", {}).get("event_view"):
            return path.resolve(), record
    raise ValueError("No market-replay run with explicit bulk inputs is available")


def _coverage(source_path, source_record):
    key = _input_key(source_record)
    coverage = {signal: [] for signal, _, _, _ in FIXED_CANDIDATES}
    for path, record in _records():
        if _input_key(record) != key:
            continue
        for result in record.get("results") or []:
            signal = result.get("signal")
            candidate = next((item for item in FIXED_CANDIDATES if item[0] == signal or item[1] == signal), None)
            if candidate:
                coverage[candidate[0]].append({
                    "run": str(path.resolve()),
                    "status": result.get("status"),
                    "return": result.get("net_return"),
                    "max_drawdown": result.get("max_drawdown"),
                    "sharpe_zero_rf_252": result.get("sharpe_zero_rf_252"),
                    "error": result.get("error"),
                    "completed_sessions": result.get("completed_sessions"),
                })
    result = []
    for signal, runner, source_kind, description in FIXED_CANDIDATES:
        attempts = coverage[signal]
        completed = next((item for item in attempts if item["status"] == "completed"), None)
        failed = attempts[0] if attempts and completed is None else None
        result.append({
            "candidate": signal,
            "runner": runner,
            "source_kind": source_kind,
            "description": description,
            "status": "completed" if completed else "failed" if failed else "missing",
            "latest": completed or failed,
            "attempts": attempts,
            "research_only": True,
            "execution_admitted": False,
        })
    return result


def discover(source=None):
    source_path, source_record = _latest_source(source)
    return {
        "schema": "quant_fixed_idea_frontier_v1",
        "source_run": str(source_path),
        "source_key": _input_key(source_record),
        "source_period": {key: source_record.get(key) for key in ("start", "end", "sessions")},
        "source_inputs": source_record.get("inputs"),
        "candidates": _coverage(source_path, source_record),
        "research_only": True,
        "execution_admitted": False,
        "fresh_holdout_claimed": False,
        "limitations": [
            "Candidates are fixed rules, not automatically selected from observed returns",
            "The newest available input may already have been inspected and is not an untouched holdout",
            "Missing held corporate-action/reference evidence stops a candidate; no zero is invented",
            "External ideas without a typed local runner remain catalogued but are not treated as backtested",
        ],
    }


def _run_one(frontier):
    missing = next((item for item in frontier["candidates"] if item["status"] == "missing"), None)
    if missing is None:
        return None
    inputs = frontier["source_inputs"]
    out = market_replay.run(
        price_view=inputs["price_view"],
        event_view=inputs["event_view"],
        reference_view=inputs["reference_view"],
        listing_master=inputs.get("listing_master"),
        start=frontier["source_period"]["start"],
        end=frontier["source_period"]["end"],
        initial_cash=(frontier.get("source_record") or {}).get("initial_cash", 140000),
        only_signals=[missing["runner"]],
    )
    return {"candidate": missing["candidate"], "run": str(Path(out).resolve())}


def run(*, execute_missing=False, source=None):
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs" / "auto_research_frontiers" / identifier
    out.mkdir(parents=True, exist_ok=False)
    frontier = discover(source)
    # Keep the explicit source record available for sizing the next fixed run.
    source_record = _read(frontier["source_run"]) or {}
    portfolio = (source_record.get("protocol") or {}).get("portfolio") or {}
    frontier["source_record"] = {"initial_cash": portfolio.get("initial_cash", 140000)}
    actions = []
    if execute_missing:
        action = _run_one(frontier)
        if action:
            actions.append(action)
            frontier = discover(source)
    final = {
        "schema": "quant_research_idea_frontier_run_v1",
        "status": "completed_fixed_idea_frontier",
        "id": identifier,
        "started_at": datetime.now().astimezone().isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
        "execute_missing": bool(execute_missing),
        "actions": actions,
        "frontier": frontier,
        "research_only": True,
        "execution_admitted": False,
        "fresh_holdout_claimed": False,
    }
    write_json(out / "run.json", final)
    lines = ["# Fixed Idea Frontier", "", "Research-only; `NO_TRADE`.", "",
             f"Source: `{frontier['source_run']}`", "", "## Candidates", ""]
    lines.extend(
        f"- `{item['candidate']}`: {item['status']}" +
        (f"; return={item['latest'].get('return')}" if item.get("latest") else "")
        for item in frontier["candidates"]
    )
    lines.extend(["", "## Boundaries", "", "- No return-driven tuning or promotion.",
                  "- Failed candidates remain evidence; missing corporate-action inputs stop replay.",
                  "- No broker submission or Ledger mutation.", ""])
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return out, final


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-missing", action="store_true")
    parser.add_argument("--source", help="Optional explicit market-replay run.json")
    args = parser.parse_args(argv)
    run(execute_missing=args.execute_missing, source=args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
