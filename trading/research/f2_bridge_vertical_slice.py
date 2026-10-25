"""Adapt a READY F2 typed-fact receipt into the shared paper pipeline.

The publication bridge owns current-period PIT evidence.  This adapter joins
those typed facts with an explicitly supplied prior-period fact and quotes,
then delegates to :mod:`f2_vertical_slice`.  It never infers a prior value
from a latest file and never promotes a blocked bridge receipt to a factor or
paper order.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .f2_fact_bridge import READY as BRIDGE_READY, SCHEMA as BRIDGE_SCHEMA
from .f2_vertical_slice import BLOCKED, COMPLETED, run_vertical_slice


SCHEMA = "f2_bridge_vertical_slice_v1"


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))


def _ref(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": sha256(path.read_bytes()).hexdigest()}


def _day(value: Any, field: str) -> str:
    from datetime import date

    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return value


def _blocked(*, bridge: Mapping[str, Any], reason: str,
             decision_day: str | None = None,
             output_dir: str | Path | None = None,
             details: Any = None) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": BLOCKED,
        "decision_day": decision_day or bridge.get("decision_day"),
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "persistent_ledger_mutated": False,
        "account_replay": False,
        "reason": reason,
        "bridge_status": bridge.get("status"),
        "bridge": {key: bridge.get(key) for key in ("schema", "status", "decision_day", "summary")},
    }
    if details is not None:
        receipt["details"] = details
    return _write(receipt, output_dir)


def _write(receipt: dict[str, Any], output_dir: str | Path | None) -> dict[str, Any]:
    if output_dir is None:
        return receipt
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / "bridge_vertical_slice_receipt.json"
    receipt["receipt_path"] = str(path)
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")
    return receipt


def run_bridge_vertical_slice(bridge: Mapping[str, Any], prior_facts: Mapping[str, Any],
                              quotes: Mapping[str, Any], *, execution_day: str,
                              settlement_day: str, initial_cash: float,
                              output_dir: str | Path | None = None,
                              strategy_id: str = "f2_f01_bridge_vertical_slice") -> dict[str, Any]:
    """Run one bridge-to-paper slice with an explicit prior-fact join."""

    if bridge.get("schema") != BRIDGE_SCHEMA:
        return _blocked(bridge=bridge, reason="bridge_schema_invalid", output_dir=output_dir)
    decision_day = _day(bridge.get("decision_day"), "decision_day")
    execution_day = _day(execution_day, "execution_day")
    settlement_day = _day(settlement_day, "settlement_day")
    if not decision_day < execution_day < settlement_day:
        return _blocked(bridge=bridge, decision_day=decision_day,
                        reason="decision_execution_settlement_order_invalid", output_dir=output_dir)
    if bridge.get("status") != BRIDGE_READY:
        return _blocked(bridge=bridge, decision_day=decision_day,
                        reason="bridge_not_ready", output_dir=output_dir,
                        details={"required_status": BRIDGE_READY})
    rows = bridge.get("rows")
    if not isinstance(rows, list) or not rows:
        return _blocked(bridge=bridge, decision_day=decision_day,
                        reason="bridge_rows_missing", output_dir=output_dir)
    if not isinstance(prior_facts, Mapping) or not isinstance(quotes, Mapping):
        return _blocked(bridge=bridge, decision_day=decision_day,
                        reason="prior_facts_or_quotes_not_mapping", output_dir=output_dir)

    events: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("status") != BRIDGE_READY:
            missing.append({"symbol": str(row.get("symbol", "")) if isinstance(row, Mapping) else "",
                            "reason": "row_not_ready"})
            continue
        symbol = row.get("symbol")
        facts = row.get("facts")
        by_metric = {item.get("metric_id"): item for item in facts or []
                     if isinstance(item, Mapping)}
        prior = prior_facts.get(symbol) if isinstance(symbol, str) else None
        current = by_metric.get("parent_net_profit")
        assets = by_metric.get("total_assets")
        if not isinstance(symbol, str) or not isinstance(current, Mapping) \
                or not isinstance(assets, Mapping) or not isinstance(prior, Mapping):
            missing.append({"symbol": str(symbol or ""),
                            "reason": "explicit_prior_or_required_current_fact_missing"})
            continue
        if symbol not in quotes:
            missing.append({"symbol": symbol, "reason": "same_day_quote_missing"})
            continue
        events.append({"symbol": symbol, "historical_evidence": row.get("historical_evidence"),
                       "current": dict(current), "prior": dict(prior), "assets": dict(assets)})
    if missing or not events:
        return _blocked(bridge=bridge, decision_day=decision_day,
                        reason="explicit_join_inputs_missing", output_dir=output_dir,
                        details={"missing": missing, "event_count": len(events)})

    spec = {
        "decision_day": decision_day,
        "execution_day": execution_day,
        "settlement_day": settlement_day,
        "initial_cash": float(initial_cash),
        "strategy_id": strategy_id,
        "quotes": dict(quotes),
        "events": events,
    }
    vertical = run_vertical_slice(spec, output_dir)
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": vertical.get("status"),
        "decision_day": decision_day,
        "execution_day": execution_day,
        "settlement_day": settlement_day,
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "persistent_ledger_mutated": False,
        "account_replay": vertical.get("account_replay") is True,
        "bridge_status": bridge.get("status"),
        "bridge": dict(bridge.get("bridge") or {}),
        "source_event_count": len(events),
        "vertical_slice": vertical,
    }
    return _write(receipt, output_dir)


def run_from_files(bridge_path: str | Path, prior_facts_path: str | Path,
                   quotes_path: str | Path, **kwargs: Any) -> dict[str, Any]:
    bridge_file = Path(bridge_path).expanduser().resolve()
    bridge = _read_json(bridge_file)
    if not isinstance(bridge, Mapping):
        raise ValueError("bridge receipt must be a JSON object")
    prior_facts = _read_json(prior_facts_path)
    quotes = _read_json(quotes_path)
    if not isinstance(prior_facts, Mapping) or not isinstance(quotes, Mapping):
        raise ValueError("prior facts and quotes must be JSON objects")
    result = run_bridge_vertical_slice(bridge, prior_facts, quotes, **kwargs)
    result["bridge_receipt"] = _ref(bridge_file)
    output_dir = kwargs.get("output_dir")
    if output_dir is not None:
        path = Path(output_dir).expanduser().resolve() / "bridge_vertical_slice_receipt.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                        encoding="utf-8")
        result["receipt_path"] = str(path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--prior-facts", type=Path, required=True)
    parser.add_argument("--quotes", type=Path, required=True)
    parser.add_argument("--execution-day", required=True)
    parser.add_argument("--settlement-day", required=True)
    parser.add_argument("--initial-cash", type=float, required=True)
    parser.add_argument("--strategy-id", default="f2_f01_bridge_vertical_slice")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_from_files(
        args.bridge, args.prior_facts, args.quotes,
        execution_day=args.execution_day, settlement_day=args.settlement_day,
        initial_cash=args.initial_cash, strategy_id=args.strategy_id,
        output_dir=args.output_dir,
    )
    print(json.dumps({"status": result["status"],
                      "execution_admitted": result["execution_admitted"],
                      "receipt": result.get("receipt_path")}, ensure_ascii=False))
    return 0 if result["status"] == COMPLETED else 1


__all__ = ["SCHEMA", "run_bridge_vertical_slice", "run_from_files"]


if __name__ == "__main__":
    raise SystemExit(main())
