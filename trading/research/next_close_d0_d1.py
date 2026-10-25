"""Run one closed-session D0/D1 research-only StockDB paper package.

The command deliberately joins two existing contracts instead of creating a
new strategy or account path:

``stockdb_source.freeze_snapshot`` -> same-day target/quote payloads ->
``daily_candidate_paper_batch`` and ``stockdb_paper_target_package``.

Strategy adapters must provide a target whose decision day is the requested
closed session.  A stale target, a missing StockDB row, or a changed input is
preserved as a blocked/failed receipt; no price is carried forward and no
broker/THS state is touched.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from ..stockdb_source import DEFAULT_STOCKDB, freeze_snapshot
from .candidate_shadow_paper import _target_payload
from .daily_candidate_paper_batch import build_batch
from .joint_portfolio_paper_package import (
    CONTINUE_RESEARCH_ONLY,
    _decision_rows,
    build_joint_paper_from_payloads,
)
from .stockdb_paper_target_package import build_paper_target_package
from .exchange_calendar import load_calendar, next_exchange_session
from . import stockdb_fixed_daily


SCHEMA = "stockdb_next_close_d0_d1_contract_v1"
STATUS = "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1"
BLOCKED_STATUS = "BLOCKED_MISSING_SAME_DAY_STOCKDB_ROWS"
FAILED_STATUS = "FAILED_D0_D1_CAPTURE_OR_PROJECTION"
DEFAULT_CATALOG = Path("docs/architecture_v3/candidate_strategy_catalog_20261024.json")


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ref(path: Path) -> dict[str, str]:
    path = path.resolve()
    return {"path": str(path), "sha256": _sha256(path)}


def _calendar_contract(close_date: str, execution_day: str, settlement_day: str,
                       calendar_path: Path | None) -> dict[str, Any]:
    """Bind adjacent D0/D1 dates to the pinned calendar when available."""
    payload = load_calendar(calendar_path)
    if payload is None:
        return {"status": "calendar_artifact_unavailable", "path": str(calendar_path) if calendar_path else None}
    next_execution, execution_meta = next_exchange_session(close_date, calendar_path)
    next_settlement, settlement_meta = next_exchange_session(execution_day, calendar_path)
    if next_execution != execution_day or next_settlement != settlement_day:
        raise ValueError("D0/D1 dates are not adjacent sessions in the pinned exchange calendar")
    return {"status": "resolved_authoritative_static_calendar", "calendar_id": payload.get("calendar_id"),
            "path": str((calendar_path or Path()).resolve()) if calendar_path else None,
            "execution": execution_meta, "settlement": settlement_meta}


def _source_path(item: Mapping[str, Any]) -> tuple[Path, bool]:
    replay = item.get("replay_result")
    terminal = item.get("terminal_account")
    if bool(replay) == bool(terminal):
        raise ValueError("Each source requires exactly one replay_result or terminal_account")
    path = Path(replay or terminal).resolve()
    if not path.is_file():
        raise ValueError(f"Source does not exist: {path}")
    return path, terminal is not None


def _source_target(item: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    direct_target = item.get("target")
    if direct_target:
        path = Path(direct_target).resolve()
        if not path.is_file():
            raise ValueError(f"Target payload does not exist: {path}")
        target = _read(path)
        if target.get("research_only") is not True or target.get("execution_admitted") is not False:
            raise ValueError("Direct target payload must be research-only and unadmitted")
        requested_strategy = item.get("strategy_id")
        if requested_strategy is not None and target.get("strategy_id") != requested_strategy:
            raise ValueError("Direct target strategy_id does not match source manifest")
        return target, {"path": str(path), "sha256": _sha256(path),
                        "source_kind": "direct_target_payload"}
    path, legacy = _source_path(item)
    payload = _read(path)
    target, _ = _target_payload(payload, legacy_account=legacy,
                                 strategy_id=item.get("strategy_id"))
    return target, {"path": str(path), "sha256": _sha256(path),
                    "source_kind": "legacy_terminal_account" if legacy else "nested_replay_result"}


def _candidate_items(path: Path) -> list[Mapping[str, Any]]:
    raw = _read(path)
    values = raw if isinstance(raw, list) else raw.get("candidates")
    if not isinstance(values, list) or not values or not all(isinstance(item, Mapping) for item in values):
        raise ValueError("Candidate source manifest must contain a non-empty candidates list")
    return list(values)


def _decision_bound_candidates(items: Sequence[Mapping[str, Any]], receipt_path: str | Path) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    """Filter D0/D1 candidates using an explicit research decision receipt."""
    decisions, selection = _decision_rows(receipt_path)
    selected: list[Mapping[str, Any]] = []
    for item in items:
        decision_id = str(item.get("decision_id") or item.get("candidate_id") or item.get("strategy_id") or "").strip()
        if not decision_id:
            raise ValueError("Decision-bound candidate requires decision_id or candidate_id")
        decision = decisions.get(decision_id)
        if decision is None:
            raise ValueError(f"research decision is missing: {decision_id}")
        record = {"candidate_id": str(item.get("candidate_id") or item.get("strategy_id") or ""),
                  "decision_id": decision_id, "decision": decision}
        bucket = "included" if decision == CONTINUE_RESEARCH_ONLY else "excluded"
        selection[bucket].append(record)
        if decision == CONTINUE_RESEARCH_ONLY:
            selected.append(item)
    if not selected:
        raise ValueError("research decision receipt excluded every D0/D1 candidate")
    return selected, selection


def _canonical_day(value: str, field: str) -> str:
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO date") from error
    if day.isoformat() != value:
        raise ValueError(f"{field} must use canonical YYYY-MM-DD form")
    return value


def _input_fingerprint(*, close_date: str, execution_day: str, settlement_day: str,
                       stockdb_root: Path, source_manifest: Path, catalog: Path | None,
                       fixed_source: Path, fixed_strategy_id: str, initial_cash: float,
                       minute_time: str, gate_mode: str,
                       research_decision_receipt: Path | None,
                       calendar_path: Path | None,
                       joint_capacity_policy: str | None, joint_max_target_rows: int) -> tuple[str, dict[str, Any]]:
    refs = {
        "source_manifest": _ref(source_manifest),
        "fixed_source": {**_ref(fixed_source), "strategy_id": fixed_strategy_id},
    }
    if catalog is not None:
        refs["catalog"] = _ref(catalog)
    if research_decision_receipt is not None:
        refs["research_decision_receipt"] = _ref(research_decision_receipt)
    if calendar_path is not None and calendar_path.is_file():
        refs["calendar"] = _ref(calendar_path)
    body = {"close_date": close_date, "execution_day": execution_day,
            "settlement_day": settlement_day, "stockdb_root": str(stockdb_root.resolve()),
            "sources": refs, "initial_cash": float(initial_cash),
            "minute_time": minute_time, "gate_mode": gate_mode,
            "joint_capacity_policy": joint_capacity_policy,
            "joint_max_target_rows": joint_max_target_rows,
            "calendar_path": str(calendar_path) if calendar_path else None}
    fingerprint = sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()
    return fingerprint, body


def _snapshot_quote(snapshot_dir: Path, symbols: Sequence[str], close_date: str,
                    *, observed_at: str) -> tuple[dict[str, Any], list[str]]:
    metadata = _read(snapshot_dir / "snapshot.json")
    daily_path = snapshot_dir / str(metadata["files"]["daily"]["path"])
    if _sha256(daily_path) != metadata["files"]["daily"]["sha256"]:
        raise ValueError("StockDB snapshot daily.csv hash changed")
    frame = pd.read_csv(daily_path, dtype={"symbol": str, "date": str})
    frame = frame.loc[frame["date"].eq(close_date)]
    if frame["symbol"].duplicated().any():
        raise ValueError("StockDB snapshot has duplicate same-day symbols")
    by_symbol = {str(row["symbol"]): row for row in frame.to_dict("records")}
    missing = sorted(set(symbols) - set(by_symbol))
    prices: dict[str, Any] = {}
    for symbol in symbols:
        if symbol in missing:
            continue
        value = by_symbol[symbol].get("close")
        try:
            price = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"StockDB close is not numeric: {symbol}") from error
        if not pd.notna(price) or price <= 0:
            raise ValueError(f"StockDB close is not positive: {symbol}")
        prices[symbol] = {"price_rmb": price / 100.0,
                          "source": "local_stockdb_snapshot_daily_close",
                          "observed_date": close_date,
                          "snapshot": str((snapshot_dir / "snapshot.json").resolve())}
    quote = {
        "schema": "ths_raw_quote_observation_v1",
        "source": "local_stockdb_snapshot_daily_close",
        "observed_at": observed_at,
        "price_basis": "raw_rmb",
        "snapshot_date": close_date,
        "snapshot": _ref(snapshot_dir / "snapshot.json"),
        "execution_admitted": False,
        "client_interacted": False,
        "prices": prices,
    }
    return quote, missing


def _write_payloads(root: Path, label: str, target: dict[str, Any], quote: dict[str, Any]) -> dict[str, str]:
    folder = root / label
    folder.mkdir(parents=True, exist_ok=True)
    target_path, quote_path = folder / "target_payload.json", folder / "quote_payload.json"
    target_path.write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    quote_path.write_text(json.dumps(quote, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"target": str(target_path), "quotes": str(quote_path)}


def _quote_for_target(quote: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the shared snapshot provenance while narrowing coverage per leg."""
    prices = quote.get("prices") or {}
    symbols = list((target.get("weights") or {}).keys())
    if any(symbol not in prices for symbol in symbols):
        raise ValueError("Target quote narrowing encountered a missing symbol")
    return {**quote, "prices": {symbol: prices[symbol] for symbol in symbols}}


def _assert_target_day(target: Mapping[str, Any], close_date: str, label: str) -> None:
    if target.get("decision_day") != close_date:
        raise ValueError(f"{label} target decision_day must equal close_date {close_date}")
    for field in ("source_day", "decision_source_day"):
        if field in target and target.get(field) != close_date:
            raise ValueError(f"{label} target {field} must equal close_date {close_date}")
    if "decision_cutoff_at" in target and not isinstance(target.get("decision_cutoff_at"), str):
        raise ValueError(f"{label} target decision_cutoff_at must be an ISO timestamp")
    if target.get("execution_admitted") is not False or target.get("research_only") is not True:
        raise ValueError(f"{label} target must remain research-only/unadmitted")


def _resolve_fixed_source(*, fixed_terminal_account: str | Path | None,
                          fixed_run_dir: str | Path | None,
                          fixed_strategy_id: str | None) -> tuple[Path, str]:
    """Resolve the registered fixed daily head without creating another account path."""
    if (fixed_terminal_account is None) == (fixed_run_dir is None):
        raise ValueError("Provide exactly one of fixed_terminal_account or fixed_run_dir")
    if fixed_run_dir is not None:
        current = stockdb_fixed_daily.status(fixed_run_dir)
        source = current.get("terminal_account")
        signal = fixed_strategy_id or current.get("signal")
        if not isinstance(source, str) or not source:
            raise ValueError("Saved fixed_daily status has no terminal account")
        if not isinstance(signal, str) or not signal:
            raise ValueError("Saved fixed_daily status has no strategy signal")
        return Path(source).resolve(), signal
    if not isinstance(fixed_terminal_account, (str, Path)):
        raise ValueError("fixed_terminal_account is required")
    if not isinstance(fixed_strategy_id, str) or not fixed_strategy_id.strip():
        raise ValueError("fixed_strategy_id is required with fixed_terminal_account")
    return Path(fixed_terminal_account).resolve(), fixed_strategy_id


def run_contract(source_manifest: str | Path, output_dir: str | Path, *,
                 fixed_terminal_account: str | Path | None = None,
                 fixed_run_dir: str | Path | None = None,
                 fixed_strategy_id: str | None = None,
                 stockdb_root: str | Path = DEFAULT_STOCKDB,
                 catalog_path: str | Path | None = DEFAULT_CATALOG,
                 close_date: str, execution_day: str, settlement_day: str,
                 initial_cash: float, minute_time: str = "",
                 gate_mode: str = "research_followup",
                 research_decision_receipt: str | Path | None = None,
                 calendar_path: str | Path | None = None,
                 joint_capacity_policy: str | None = None,
                 joint_max_target_rows: int = 22) -> dict[str, Any]:
    close_date = _canonical_day(close_date, "close_date")
    execution_day = _canonical_day(execution_day, "execution_day")
    settlement_day = _canonical_day(settlement_day, "settlement_day")
    if not close_date < execution_day < settlement_day:
        raise ValueError("Require close_date < execution_day < settlement_day")
    source_manifest = Path(source_manifest).resolve()
    fixed_source, fixed_strategy_id = _resolve_fixed_source(
        fixed_terminal_account=fixed_terminal_account,
        fixed_run_dir=fixed_run_dir,
        fixed_strategy_id=fixed_strategy_id)
    stockdb_root = Path(stockdb_root).resolve()
    catalog = Path(catalog_path).resolve() if catalog_path is not None else None
    decision_receipt = Path(research_decision_receipt).resolve() if research_decision_receipt is not None else None
    calendar_file = Path(calendar_path).resolve() if calendar_path is not None else None
    if not source_manifest.is_file() or not fixed_source.is_file():
        raise ValueError("Source manifest and fixed terminal account must exist")
    if catalog is not None and not catalog.is_file():
        raise ValueError(f"Candidate catalog does not exist: {catalog}")
    if decision_receipt is not None and not decision_receipt.is_file():
        raise ValueError(f"Research decision receipt does not exist: {decision_receipt}")
    fingerprint, fingerprint_inputs = _input_fingerprint(
        close_date=close_date, execution_day=execution_day, settlement_day=settlement_day,
        stockdb_root=stockdb_root, source_manifest=source_manifest, catalog=catalog,
        fixed_source=fixed_source, fixed_strategy_id=fixed_strategy_id,
        initial_cash=initial_cash, minute_time=minute_time, gate_mode=gate_mode,
        research_decision_receipt=decision_receipt,
        calendar_path=calendar_file,
        joint_capacity_policy=joint_capacity_policy, joint_max_target_rows=joint_max_target_rows)
    output = Path(output_dir).resolve()
    receipt_path = output / "contract_receipt.json"
    if receipt_path.is_file():
        existing = _read(receipt_path)
        if existing.get("input_fingerprint") != fingerprint:
            raise ValueError("D0/D1 output already exists for different inputs; use a new output directory")
        if existing.get("execution_admitted") is not False:
            raise ValueError("Saved D0/D1 receipt crossed execution boundary")
        return {**existing, "reused_existing_run": True}
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("D0/D1 output exists without a contract receipt; inspect preserved failure evidence")
    output.mkdir(parents=True, exist_ok=True)
    receipt_base = {
        "schema": SCHEMA, "status": "CAPTURING_D0_D1", "created_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False, "broker_interacted": False, "ths_interacted": False,
        "persistent_ledger_mutated": False, "input_fingerprint": fingerprint,
        "fingerprint_inputs": fingerprint_inputs, "reused_existing_run": False,
    }
    try:
        receipt_base["calendar"] = _calendar_contract(close_date, execution_day, settlement_day, calendar_file)
        fixed_target, fixed_source_ref = _source_target({"terminal_account": str(fixed_source),
                                                           "strategy_id": fixed_strategy_id})
        _assert_target_day(fixed_target, close_date, "fixed")
        candidates = _candidate_items(source_manifest)
        decision_selection = None
        if decision_receipt is not None:
            candidates, decision_selection = _decision_bound_candidates(candidates, decision_receipt)
        candidate_targets: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for item in candidates:
            candidate_id = str(item.get("candidate_id") or item.get("strategy_id") or "").strip()
            if not candidate_id:
                raise ValueError("Each candidate requires candidate_id or strategy_id")
            target, source_ref = _source_target(item)
            _assert_target_day(target, close_date, candidate_id)
            candidate_targets.append((candidate_id, target, source_ref))
        all_targets = [fixed_target, *(item[1] for item in candidate_targets)]
        symbols = list(dict.fromkeys(symbol for target in all_targets for symbol in target["weights"]))
        snapshot_dir = output / "snapshot"
        metadata = freeze_snapshot(stockdb_root, snapshot_dir, symbols, [close_date], minute_time)
        snapshot_ref = _ref(snapshot_dir / "snapshot.json")
        quote, missing = _snapshot_quote(snapshot_dir, symbols, close_date,
                                         observed_at=metadata["captured_at"])
        receipt_base.update({"close_date": close_date, "snapshot": snapshot_ref,
                             "requested_symbol_count": len(symbols), "missing_symbols": missing,
                             "fixed_source": fixed_source_ref,
                             "candidate_source_manifest": _ref(source_manifest),
                             "time_semantics": {
                                 "source_day": close_date,
                                 "decision_cutoff_at": metadata["captured_at"],
                                 "quote_observed_at": metadata["captured_at"],
                                 "artifact_created_at": receipt_base["created_at"],
                                 "plan_published_at": None,
                                 "interpretation": "The contract freezes a close-day input and writes a local artifact; it does not claim a user-facing publication time.",
                             }})
        if decision_selection is not None:
            receipt_base["research_decision_selection"] = decision_selection
        if missing:
            receipt_base.update(status=BLOCKED_STATUS,
                                reason="Missing same-day StockDB rows; no price was carried forward",
                                artifacts={"snapshot": str(snapshot_dir),
                                           "missing_symbols": missing})
            receipt_path.write_text(json.dumps(receipt_base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return receipt_base

        inputs_dir = output / "inputs"
        fixed_quote = _quote_for_target(quote, fixed_target)
        fixed_paths = _write_payloads(inputs_dir, "fixed", fixed_target, fixed_quote)
        candidate_rows = []
        for candidate_id, target, source_ref in candidate_targets:
            paths = _write_payloads(inputs_dir, candidate_id, target, _quote_for_target(quote, target))
            candidate_rows.append({"candidate_id": candidate_id,
                                   "strategy_id": target["strategy_id"], **paths,
                                   "source": source_ref})
        manifest = {"schema": "next_close_d0_d1_target_quote_manifest_v1",
                    "close_date": close_date, "execution_admitted": False,
                    "broker_interacted": False, "ths_interacted": False,
                    "candidates": [{k: row[k] for k in ("candidate_id", "strategy_id", "target", "quotes")}
                                   for row in candidate_rows]}
        manifest_path = inputs_dir / "candidate_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        fixed_package = build_paper_target_package(
            _read(Path(fixed_paths["target"])), _read(Path(fixed_paths["quotes"])), initial_cash,
            execution_day=execution_day, settlement_day=settlement_day,
            output_dir=output / "fixed")
        batch = build_batch(manifest["candidates"], output / "candidate_batch",
                            initial_cash=initial_cash, execution_day=execution_day,
                            settlement_day=settlement_day, catalog_path=catalog,
                            manifest_path=manifest_path, gate_mode=gate_mode)
        joint_result = None
        if joint_capacity_policy is not None:
            payloads = {fixed_target["strategy_id"]: (fixed_target, fixed_quote)}
            for candidate_id, target, _source_ref in candidate_targets:
                payloads[target["strategy_id"]] = (
                    target, _quote_for_target(quote, target))
            joint_result = build_joint_paper_from_payloads(
                payloads, initial_cash_cny=initial_cash,
                capacity_policy=joint_capacity_policy,
                max_target_rows=joint_max_target_rows,
                execution_day=execution_day, settlement_day=settlement_day,
                output_dir=output / "joint_paper",
            )
        cost_ids = [(fixed_package["result"].get("cost_model") or {}).get("id")]
        benchmark_ids = [(fixed_package["result"].get("benchmark_binding") or {}).get("id")]
        cost_ids.extend(item.get("cost_model_id") for item in batch["candidates"])
        benchmark_ids.extend(item.get("benchmark_binding_id") for item in batch["candidates"])
        if len(set(cost_ids)) != 1 or None in cost_ids:
            raise ValueError("Fixed and candidate packages do not share one cost model")
        if len(set(benchmark_ids)) != 1 or None in benchmark_ids:
            raise ValueError("Fixed and candidate packages do not share one benchmark binding")
        receipt = {**receipt_base, "status": STATUS,
                   "fixed": {"strategy_id": fixed_package["result"]["strategy_id"],
                             "target_count": fixed_package["result"]["target_count"],
                             "paper_order_count": fixed_package["result"]["paper_order_count"],
                             "result": str(output / "fixed" / "result.json")},
                   "candidate_batch": {"receipt": batch["receipt_path"],
                                       "candidate_count": batch["candidate_count"]},
                   "joint_paper": ({
                       "status": joint_result["status"],
                       "receipt": joint_result.get("receipt_path"),
                       "paper_order_count": (joint_result.get("paper_package") or {}).get("paper_order_count", 0),
                       "capacity_policy": (joint_result.get("joint") or {}).get("capacity_policy"),
                   } if joint_result is not None else None),
                   "cost_benchmark_binding": {"cost_model_id": cost_ids[0],
                                               "benchmark_binding_id": benchmark_ids[0]},
                   "artifacts": {"snapshot": str(snapshot_dir),
                                 "target_quote_manifest": str(manifest_path)},
                   "limitations": [
                       "Targets are strategy-adapter inputs and must already be same-day outputs.",
                       "StockDB rows are local read-only observations; no PIT or corporate-action promotion occurs.",
                       "Paper fills are shared Ledger projections, not broker fills or THS instructions.",
                   ]}
        receipt["receipt_path"] = str(receipt_path)
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return receipt
    except Exception as error:
        failure = {**receipt_base, "status": FAILED_STATUS,
                   "error_type": type(error).__name__, "error": str(error),
                   "failure_evidence": {"output_dir": str(output),
                                        "snapshot_dir": str(output / "snapshot")}}
        receipt_path.write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    fixed_source = parser.add_mutually_exclusive_group(required=True)
    fixed_source.add_argument("--fixed-terminal-account", type=Path)
    fixed_source.add_argument("--fixed-run-dir", type=Path,
                              help="registered stockdb-fixed-daily directory; uses its current head")
    parser.add_argument("--fixed-strategy-id",
                        help="optional with --fixed-run-dir; required with --fixed-terminal-account")
    parser.add_argument("--stockdb-root", type=Path, default=DEFAULT_STOCKDB)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--close-date", required=True)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--settlement-day", required=True)
    parser.add_argument("--initial-cash", type=float, required=True)
    parser.add_argument("--minute-time", default="")
    parser.add_argument("--gate-mode", choices=("research_followup", "separate_gate"), default="research_followup")
    parser.add_argument("--research-decision-receipt", type=Path,
                        help="optional receipt; only CONTINUE_RESEARCH_ONLY candidates enter D0/D1")
    parser.add_argument("--calendar", type=Path,
                        help="optional pinned SSE calendar JSON for adjacent D0/D1 validation")
    parser.add_argument("--joint-capacity-policy", choices=("strict", "budgeted"), default=None,
                        help="optional direct fixed+candidate joint paper projection")
    parser.add_argument("--joint-max-target-rows", type=int, default=22)
    args = parser.parse_args(argv)
    receipt = run_contract(
        args.source_manifest, args.output_dir,
        fixed_terminal_account=args.fixed_terminal_account, fixed_run_dir=args.fixed_run_dir,
        fixed_strategy_id=args.fixed_strategy_id,
        stockdb_root=args.stockdb_root, catalog_path=args.catalog,
        close_date=args.close_date, execution_day=args.execution_day,
        settlement_day=args.settlement_day, initial_cash=args.initial_cash,
        minute_time=args.minute_time, gate_mode=args.gate_mode,
        research_decision_receipt=args.research_decision_receipt,
        calendar_path=args.calendar,
        joint_capacity_policy=args.joint_capacity_policy,
        joint_max_target_rows=args.joint_max_target_rows)
    print(json.dumps({"status": receipt["status"], "execution_admitted": receipt["execution_admitted"],
                      "reused_existing_run": receipt.get("reused_existing_run", False),
                      "receipt": receipt.get("receipt_path", str(Path(args.output_dir).resolve() / "contract_receipt.json"))},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
