"""Build a paper target reconciliation from a pinned StockDB close snapshot.

This is the local, research-only side of the target-holdings workflow.  It
uses the same StrategyConfig, Ledger and stock-lot allocator as replay, while
keeping the THS read-only request artifact separate.  A StockDB close is a
historical paper mark; it is never treated as a current broker quote or a
prefillable order price.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode
from ..ledger import Ledger
from ..portfolio_allocation import stock_lot_allocation
from ..rules import quote_value


SCHEMA = "quantification_paper_close_target_reconciliation_v1"
STATUS_READY = "PAPER_CLOSE_REFERENCE_READY"
STATUS_INCOMPLETE = "PAPER_CLOSE_REFERENCE_INCOMPLETE"
PREFILL_STATUS = "reference_only_not_prefillable"
DEFAULT_PLAN = Path("outputs/paper_plans/20261024_lowvol_current_paper_plan_receipt.json")
DEFAULT_OUTPUT = Path("outputs/paper_plans/20261024_lowvol_current_paper_reconciliation")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _code(value: Any) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    match = re.search(r"(\d{1,6})$", text)
    if not match:
        raise ValueError(f"StockDB row has no six-digit code: {value!r}")
    return match.group(1).zfill(6)


def _symbol(code: str) -> str:
    if code.startswith(("600", "601", "603", "605", "688")):
        return f"{code}.SSE"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return f"{code}.SZSE"
    # The full raw file also contains BSE and other instruments; they are
    # filtered before target validation and are irrelevant to this slice.
    return ""


def read_stockdb_close(raw_path: Path, day: str, target_symbols: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read and validate only the requested day and target symbols."""
    columns = ["date", "code", "close", "tradestatus", "adjustflag"]
    frame = pd.read_parquet(raw_path, columns=columns)
    frame["date"] = frame["date"].astype(str).str[:10]
    frame = frame.loc[frame["date"] == day].copy()
    frame["code"] = frame["code"].map(_code)
    frame["symbol"] = frame["code"].map(_symbol)
    frame = frame.loc[frame["symbol"].isin(target_symbols)].copy()
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("StockDB close contains duplicate target/date rows")
    if not frame["adjustflag"].astype(str).eq("3").all():
        raise ValueError("Paper target reconciliation requires raw adjustflag=3")
    status = frame["tradestatus"].astype(str)
    if not status.isin(["0", "1"]).all():
        raise ValueError("Paper target reconciliation requires explicit tradestatus 0/1")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if not frame["close"].map(lambda value: math.isfinite(float(value)) and float(value) > 0).all():
        raise ValueError("StockDB target close must be finite and positive")
    rows = {
        row.symbol: {
            "close": float(row.close),
            "tradestatus": str(row.tradestatus),
            "adjustflag": str(row.adjustflag),
            "date": day,
            "basis": "StockDB raw_daily close; adjustflag=3; explicit tradestatus",
        }
        for row in frame.itertuples(index=False)
    }
    return rows, {
        "path": str(raw_path),
        "day": day,
        "rows_scanned": int(len(frame)),
        "target_rows_found": int(len(rows)),
        "target_rows_expected": len(target_symbols),
        "missing_symbols": [symbol for symbol in target_symbols if symbol not in rows],
    }


def read_native_stockdb_close(snapshot_path: Path, day: str,
                              target_symbols: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read a frozen native StockDB daily.csv close slice."""
    snapshot_path = Path(snapshot_path).resolve()
    daily_path = snapshot_path / "daily.csv" if snapshot_path.is_dir() else snapshot_path
    if not daily_path.is_file():
        raise ValueError(f"Native StockDB daily.csv is missing: {daily_path}")
    frame = pd.read_csv(daily_path, dtype={"date": "string", "symbol": "string", "code": "string"})
    required = {"date", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Native StockDB close is missing fields: {missing}")
    frame["date"] = frame["date"].astype(str).str[:10]
    if "symbol" not in frame.columns:
        if "code" not in frame.columns:
            raise ValueError("Native StockDB close requires symbol or code")
        frame["code"] = frame["code"].map(_code)
        frame["symbol"] = frame["code"].map(_symbol)
    else:
        frame["symbol"] = frame["symbol"].astype(str).str.strip()
        missing_suffix = ~frame["symbol"].str.contains(r"\.(?:SSE|SZSE)$", regex=True, na=False)
        if "code" in frame.columns:
            frame.loc[missing_suffix, "code"] = frame.loc[missing_suffix, "code"].map(_code)
            frame.loc[missing_suffix, "symbol"] = frame.loc[missing_suffix, "code"].map(_symbol)
    frame = frame.loc[(frame["date"] == day) & frame["symbol"].isin(target_symbols)].copy()
    if frame.duplicated(["date", "symbol"]).any():
        raise ValueError("Native StockDB close contains duplicate target/date rows")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if not frame["close"].map(lambda value: math.isfinite(float(value)) and float(value) > 0).all():
        raise ValueError("Native StockDB close must be finite and positive")
    rows = {
        row.symbol: {"close": float(row.close), "date": day,
                     "basis": "native StockDB snapshot daily.csv close"}
        for row in frame.itertuples(index=False)
    }
    return rows, {
        "path": str(daily_path), "day": day,
        "rows_scanned": int(len(frame)), "target_rows_found": int(len(rows)),
        "target_rows_expected": len(target_symbols),
        "missing_symbols": [symbol for symbol in target_symbols if symbol not in rows],
        "source": "native StockDB snapshot",
    }


def compare_native_stockdb_close(plan_path: Path, snapshot_path: Path, raw_path: Path,
                                 *, day: str, output_dir: Path | None = None,
                                 tolerance: float = 1e-9) -> dict[str, Any]:
    """Compare one paper target close against native and raw local sources."""
    plan_path = Path(plan_path).resolve()
    plan = _json(plan_path)
    if plan.get("execution_admitted") is not False or plan.get("prefill_allowed") is not False:
        raise ValueError("Native reference requires an unadmitted, non-prefill paper plan")
    weights = dict(plan.get("historical_target", {}).get("weights") or {})
    if not weights:
        raise ValueError("Native reference requires non-empty historical target weights")
    target_symbols = sorted(weights)
    native_rows, native_meta = read_native_stockdb_close(Path(snapshot_path), day, target_symbols)
    raw_rows, raw_meta = read_stockdb_close(Path(raw_path), day, target_symbols)
    rows = []
    for symbol in target_symbols:
        native = native_rows.get(symbol)
        raw = raw_rows.get(symbol)
        native_close = native.get("close") if native else None
        raw_close = raw.get("close") if raw else None
        difference = (abs(native_close - raw_close)
                      if native_close is not None and raw_close is not None else None)
        rows.append({
            "symbol": symbol, "reference_date": day,
            "native_close_rmb": native_close, "raw_close_rmb": raw_close,
            "abs_close_diff_rmb": difference,
            "close_match": difference is not None and difference <= tolerance,
            "native_status": "found" if native else "missing",
            "raw_status": "found" if raw else "missing",
            "execution_admitted": False,
        })
    matches = sum(bool(row["close_match"]) for row in rows)
    complete = matches == len(rows) and len(rows) > 0
    receipt = {
        "schema": "quantification_paper_target_native_stockdb_reference_v1",
        "status": ("COMPLETED_RESEARCH_ONLY_CROSS_SOURCE_CLOSE_REFERENCE"
                   if complete else "INCOMPLETE_RESEARCH_ONLY_CROSS_SOURCE_CLOSE_REFERENCE"),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False, "client_interacted": False, "prefill_allowed": False,
        "source_paper_plan": str(plan_path), "reference_date": day,
        "target_symbols": len(target_symbols), "target_symbols_list": target_symbols,
        "native_snapshot": {**native_meta, "capture_complete": True,
                            "execution_admitted": False},
        "raw_reference": {**raw_meta, "execution_admitted": False},
        "comparison": {
            "close_rows_compared": len(rows), "close_matches": matches,
            "close_mismatches": len(rows) - matches,
            "max_abs_close_diff": max((row["abs_close_diff_rmb"] or 0.0 for row in rows), default=0.0),
            "tolerance_rmb": tolerance,
        },
        "rows": rows,
        "interpretation": "Local research cross-source close check only; it is not a THS quote, account observation or prefill payload.",
        "limitations": [
            "Native StockDB is not PIT/revision-safe or corporate-action proof.",
            "No login, prefill, submit, cancel, transfer or Ledger mutation is performed.",
            "A close match does not establish broker/account state or execution readiness.",
        ],
    }
    if output_dir is not None:
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        comparison_csv = output_dir / "comparison.csv"
        with comparison_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        receipt["comparison"]["comparison_csv"] = str(comparison_csv)
        receipt["artifacts"] = {"comparison_csv": str(comparison_csv),
                                "receipt_json": str(output_dir / "receipt.json")}
        (output_dir / "receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def build_reconciliation(
    plan: Mapping[str, Any],
    run: Mapping[str, Any],
    account: Mapping[str, Any],
    raw_rows: Mapping[str, Mapping[str, Any]],
    *,
    day: str,
    source_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compute target quantities without mutating the supplied account state."""
    if plan.get("execution_admitted") is not False or plan.get("prefill_allowed") is not False:
        raise ValueError("Paper reconciliation requires an unadmitted, non-prefill paper plan")
    if run.get("execution_admitted") is not False or run.get("research_only") is not True:
        raise ValueError("Paper reconciliation requires an unadmitted research-only run")
    weights = dict(plan.get("historical_target", {}).get("weights") or account.get("targets") or {})
    if not weights:
        raise ValueError("Paper reconciliation requires non-empty target weights")
    if any(not isinstance(symbol, str) or not isinstance(weight, (int, float)) for symbol, weight in weights.items()):
        raise ValueError("Target weights must be numeric canonical symbols")
    config = StrategyConfig(**dict(run["config"])).validate()
    source_state = deepcopy(dict(account))
    valuation_state = deepcopy(source_state)
    context = RunContext("paper_close_reconciliation", TradingMode.REPLAY)
    book = Ledger(valuation_state, config, context)
    prices: dict[str, int] = {}
    rules = {}
    missing = []
    inactive = []
    mark_matches = 0
    for symbol in weights:
        row = raw_rows.get(symbol)
        if row is None:
            missing.append(symbol)
            continue
        rule = book.rule(symbol, day)
        if rule.price_unit != "cent":
            raise ValueError(f"Target is not an ordinary cent-stock: {symbol}")
        prices[symbol] = quote_value(row["close"], rule.price_unit)
        rules[symbol] = rule
        if source_state.get("prices", {}).get(symbol) == prices[symbol]:
            mark_matches += 1
        if row.get("tradestatus") != "1":
            inactive.append(symbol)
        # Revalue only the clone, never the persisted source account.
        valuation_state.setdefault("prices", {})[symbol] = prices[symbol]
    complete = not missing and not inactive
    if not complete:
        rows = []
        for symbol, weight in weights.items():
            row = raw_rows.get(symbol)
            rows.append({
                "symbol": symbol, "target_weight": weight,
                "paper_close_date": day if row else None,
                "paper_close_rmb": row.get("close") if row else None,
                "current_qty": book.quantity(symbol),
                "sellable_qty": book.available(symbol, day),
                "estimated_target_qty": None,
                "delta_qty": None,
                "reconciliation_status": STATUS_INCOMPLETE,
                "ths_prefill_status": PREFILL_STATUS,
                "execution_admitted": False,
            })
        return {
            "schema": SCHEMA, "status": STATUS_INCOMPLETE, "day": day,
            "target_count": len(weights), "target_weight_sum": sum(weights.values()),
            "rows": rows, "missing_symbols": missing, "inactive_symbols": inactive,
            "account_equity_cents": book.equity(), "paper_account_mark_matches": mark_matches,
            "source_hashes": dict(source_hashes or {}), "execution_admitted": False,
            "client_interacted": False, "prefill_allowed": False,
            "orders_created": False, "ledger_mutated": False,
            "limitations": [
                "StockDB close is a historical paper mark, not a current THS quote or account observation.",
                "This artifact is reference-only and cannot be used to prefill, submit, cancel or place orders.",
            ],
        }
    entries, budget, spent = stock_lot_allocation(
        weights, book.equity(), prices, rules, config.single_limit, config.gross_limit
    )
    by_symbol = {entry["symbol"]: entry for entry in entries}
    rows = []
    for symbol, weight in weights.items():
        entry = by_symbol[symbol]
        current = book.quantity(symbol)
        sellable = book.available(symbol, day)
        delta = entry["quantity"] - current
        flags = []
        if delta < 0 and -delta > sellable:
            flags.append("sellable_T1_shortfall")
        rows.append({
            "symbol": symbol, "target_weight": weight,
            "paper_close_date": day, "paper_close_rmb": prices[symbol] / 100,
            "current_qty": current, "sellable_qty": sellable,
            "estimated_target_qty": entry["quantity"], "delta_qty": delta,
            "estimated_target_notional_cny": entry["quantity"] * prices[symbol] / 100,
            "estimated_buy_fee_cny": book.fee(prices[symbol] * max(delta, 0), "buy", symbol, day) / 100 if delta > 0 else 0,
            "constraint_flags": flags,
            "reconciliation_status": STATUS_READY if not flags else "PAPER_CLOSE_REFERENCE_CONSTRAINT",
            "ths_prefill_status": PREFILL_STATUS,
            "execution_admitted": False,
        })
    # The clone was only used for valuation; the caller-supplied account stays unchanged.
    if source_state != dict(account):
        raise AssertionError("source account changed during paper reconciliation")
    return {
        "schema": SCHEMA, "status": STATUS_READY, "day": day,
        "target_count": len(weights), "target_weight_sum": sum(weights.values()),
        "account_equity_cents": book.equity(), "account_equity_cny": book.equity() / 100,
        "stock_budget_cents": int(budget), "estimated_allocated_notional_cents": int(spent),
        "paper_account_mark_matches": mark_matches,
        "paper_close_rows_found": len(raw_rows), "missing_symbols": [], "inactive_symbols": [],
        "rows": rows, "source_hashes": dict(source_hashes or {}),
        "execution_admitted": False, "client_interacted": False, "prefill_allowed": False,
        "orders_created": False, "ledger_mutated": False,
        "limitations": [
            "StockDB close is a historical paper mark, not a current THS quote or account observation.",
            "This artifact is reference-only and cannot be used to prefill, submit, cancel or place orders.",
            "The separate THS read-only request keeps current quote/account/readback blank until an authorized observation exists.",
        ],
    }


def run(plan_path: Path, output_dir: Path, *, day: str | None = None,
        account_path: Path | None = None, run_path: Path | None = None,
        raw_path: Path | None = None) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    plan = _json(plan_path)
    run_dir = Path(plan["run_dir"])
    head_path = run_dir / "head.json"
    head = _json(head_path)
    run_path = (run_path or Path(plan["source_run"]["path"])).resolve()
    run_payload = _json(run_path)
    account_path = (account_path or Path(head["source"]["account"]["path"])).resolve()
    account = _json(account_path)
    raw_path = (raw_path or Path(head["supplement"]["path"])).resolve()
    if raw_path.is_dir():
        raw_path = raw_path / "raw_daily.parquet"
    target_weights = dict(plan.get("historical_target", {}).get("weights") or account.get("targets") or {})
    day = day or str(plan.get("seed_last_completed_day") or account.get("day"))
    rows, data_meta = read_stockdb_close(raw_path, day, list(target_weights))
    receipt = build_reconciliation(
        plan, run_payload, account, rows, day=day,
        source_hashes={
            "paper_plan_sha256": _sha256(plan_path), "head_sha256": _sha256(head_path),
            "source_run_sha256": _sha256(run_path), "paper_account_sha256": _sha256(account_path),
            "raw_daily_sha256": _sha256(raw_path),
        },
    )
    receipt["observed_at"] = datetime.now(timezone.utc).isoformat()
    receipt["source_paths"] = {
        "paper_plan": str(plan_path), "head": str(head_path), "source_run": str(run_path),
        "paper_account": str(account_path), "raw_daily": str(raw_path),
    }
    receipt["stockdb_close"] = data_meta
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "target_reconciliation.csv"
    fieldnames = list(receipt["rows"][0].keys()) if receipt["rows"] else []
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(receipt["rows"])
    receipt_path = output_dir / "receipt.json"
    receipt["artifacts"] = {
        "target_reconciliation_csv": str(csv_path),
        "target_reconciliation_sha256": _sha256(csv_path),
        "receipt_json": str(receipt_path),
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--day")
    parser.add_argument("--account", type=Path)
    parser.add_argument("--source-run", type=Path)
    parser.add_argument("--raw-daily", type=Path)
    args = parser.parse_args(argv)
    result = run(args.paper_plan, args.output_dir, day=args.day, account_path=args.account,
                 run_path=args.source_run, raw_path=args.raw_daily)
    print(json.dumps({"status": result["status"], "day": result["day"],
                      "target_count": result["target_count"],
                      "output_dir": str(args.output_dir.resolve()),
                      "execution_admitted": result["execution_admitted"]}, ensure_ascii=False))
    return 0


def native_reference_main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare a paper target close against native and raw local StockDB sources")
    parser.add_argument("--paper-plan", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--raw-daily", type=Path, required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = compare_native_stockdb_close(
        args.paper_plan, args.snapshot, args.raw_daily,
        day=args.day, output_dir=args.output_dir,
    )
    print(json.dumps({
        "status": result["status"], "day": result["reference_date"],
        "target_count": result["target_symbols"],
        "close_matches": result["comparison"]["close_matches"],
        "close_mismatches": result["comparison"]["close_mismatches"],
        "output_dir": str(args.output_dir.resolve()),
        "execution_admitted": result["execution_admitted"],
    }, ensure_ascii=False))
    return 0 if result["status"].startswith("COMPLETED_") else 2


__all__ = [
    "SCHEMA", "STATUS_READY", "STATUS_INCOMPLETE",
    "build_reconciliation", "read_stockdb_close", "read_native_stockdb_close",
    "compare_native_stockdb_close", "run", "main", "native_reference_main",
]

