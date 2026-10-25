"""Execute one explicit F2 financial-factor vertical slice.

The slice consumes only an explicit JSON fixture/source bundle containing
current, prior and asset facts plus same-day raw RMB quotes.  It computes F01,
creates the existing typed SignalFrame, builds equal-weight targets and sends
them through the shared paper target package (Engine/Ledger path).  Missing or
late facts remain BLOCKED_DATA and never become zero scores or paper orders.
No provider, broker or THS client is contacted.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import RunContext, TradingMode
from .f01_financial_factor import FinancialFact, F01Result, compute_f01
from .f01_signal import signal_frame, signal_provenance
from .financial_factor_contracts import TimeWindow
from .stockdb_paper_target_package import build_paper_target_package

SCHEMA = "f2_financial_vertical_slice_v1"
COMPLETED = "COMPLETED_RESEARCH_ONLY_F2_VERTICAL_SLICE"
BLOCKED = "BLOCKED_DATA"
STAGES = ("Data", "Signal", "Target Portfolio", "Orders", "BrokerAdapter", "Fill", "Cash/Position")


def _day(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return value


def _fact(raw: Mapping[str, Any], *, symbol: str, role: str) -> FinancialFact:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{symbol}/{role} fact must be an object")
    available = raw.get("available_at")
    if isinstance(available, Mapping):
        available = available.get("start")
    if not isinstance(available, str):
        raise ValueError(f"{symbol}/{role} requires exact available_at")
    period_start = date.fromisoformat(str(raw["period_start"]))
    period_end = date.fromisoformat(str(raw["period_end"]))
    return FinancialFact(
        fact_id=str(raw.get("fact_id") or f"{symbol}:{role}"),
        issuer_id=str(raw.get("issuer_id") or symbol),
        metric_id=str(raw.get("metric_id") or ("parent_net_profit" if role != "assets" else "total_assets")),
        period_start=period_start,
        period_end=period_end,
        period_kind=str(raw.get("period_kind") or "single_quarter"),
        scope_group=str(raw.get("scope_group") or "consolidated_parent"),
        currency=str(raw.get("currency") or "CNY"),
        unit=str(raw.get("unit") or "yuan"),
        value=raw.get("value"),
        available=TimeWindow.exact(available),
        status=str(raw.get("status") or "OBSERVED"),
    )


def _event_time(decision_day: str) -> TimeWindow:
    return TimeWindow.exact(f"{decision_day}T15:00:00+08:00")


def _target_payload(day: str, signal_rows: Sequence[Mapping[str, Any]], strategy_id: str) -> dict[str, Any]:
    if not signal_rows:
        raise ValueError("At least one admitted F01 signal is required")
    if len(signal_rows) > 4:
        signal_rows = signal_rows[:4]
    weight = min(0.2, 0.8 / len(signal_rows))
    weights = {str(row["symbol"]): weight for row in signal_rows}
    signals = []
    for rank, row in enumerate(signal_rows, 1):
        symbol = str(row["symbol"])
        code, exchange = symbol.split(".", 1)
        signals.append({"security": {"code": code, "exchange": exchange},
                        "diagnostics": {"rank": rank, "score": float(row["score"]),
                                        "factor": "F01_asset_normalized_surprise_v1"}})
    return {
        "schema": "stockdb_research_target_v1",
        "strategy_id": strategy_id,
        "decision_day": day,
        "research_only": True,
        "execution_admitted": False,
        "weights": weights,
        "signal_frame": {"signals": signals},
    }


def run_vertical_slice(spec: Mapping[str, Any], output_dir: str | Path | None = None) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise ValueError("spec must be an object")
    decision_day = _day(spec.get("decision_day"), "decision_day")
    execution_day = _day(spec.get("execution_day"), "execution_day")
    settlement_day = _day(spec.get("settlement_day"), "settlement_day")
    if not decision_day < execution_day < settlement_day:
        raise ValueError("Require decision_day < execution_day < settlement_day")
    initial_cash = float(spec.get("initial_cash", 0))
    quotes = spec.get("quotes")
    rows = spec.get("events")
    if not isinstance(quotes, Mapping) or not isinstance(rows, list) or not rows:
        raise ValueError("spec requires quotes and non-empty events")
    context = RunContext("f2_vertical_slice", TradingMode.REPLAY)
    event = _event_time(decision_day)
    results: dict[str, F01Result] = {}
    facts_meta: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, Mapping) or not isinstance(item.get("symbol"), str):
            raise ValueError("Each event requires a symbol")
        symbol = item["symbol"]
        current = _fact(item.get("current"), symbol=symbol, role="current")
        prior = _fact(item.get("prior"), symbol=symbol, role="prior")
        assets = _fact(item.get("assets"), symbol=symbol, role="assets")
        result = compute_f01(current, prior, assets, event)
        results[symbol] = result
        facts_meta.append({"symbol": symbol, "fact_ids": list(result.fact_ids),
                           "result": result.to_dict()})
    signal = signal_frame(results, decision_day, context)
    receipt: dict[str, Any] = {
        "schema": SCHEMA, "status": BLOCKED, "decision_day": decision_day,
        "execution_day": execution_day, "settlement_day": settlement_day,
        "stages": list(STAGES), "execution_admitted": False,
        "broker_interacted": False, "ths_interacted": False,
        "persistent_ledger_mutated": False, "account_replay": False,
        "facts": facts_meta, "signal": signal.to_dict() if hasattr(signal, "to_dict") else {"status": "UNAVAILABLE", "reason": signal.reason},
        "signal_provenance": signal_provenance(results),
        "package": None,
    }
    if not hasattr(signal, "ranked_candidates"):
        receipt["reason"] = signal.reason
        receipt["limitations"] = ["No target/order/account projection is created until one F01 signal is ready at the decision clock."]
        return _write_receipt(receipt, output_dir)
    ranked = signal.ranked_candidates()
    target = _target_payload(decision_day, ranked, str(spec.get("strategy_id") or "f2_f01_vertical_slice"))
    price_map = {}
    for symbol in target["weights"]:
        raw = quotes.get(symbol)
        if isinstance(raw, Mapping):
            raw = raw.get("price_rmb")
        price_map[symbol] = {"price_rmb": float(raw), "source": "explicit_same_day_fixture", "observed_date": decision_day}
    quote_payload = {"schema": "ths_raw_quote_observation_v1", "source": "explicit_same_day_fixture",
                     "observed_at": str(spec.get("quote_observed_at") or f"{decision_day}T15:00:00+08:00"),
                     "price_basis": "raw_rmb", "snapshot_date": decision_day,
                     "execution_admitted": False, "client_interacted": False, "prices": price_map}
    if output_dir is None:
        package = build_paper_target_package(target, quote_payload, initial_cash,
                                             execution_day=execution_day, settlement_day=settlement_day)
    else:
        package_dir = Path(output_dir).resolve() / "paper_package"
        package = build_paper_target_package(target, quote_payload, initial_cash,
                                             execution_day=execution_day, settlement_day=settlement_day,
                                             output_dir=package_dir)
    receipt.update({"status": COMPLETED, "target_portfolio": target,
                    "package": package["result"],
                    "account_replay": True,
                    "limitations": ["Facts and quotes are explicit source inputs; no current provider data is fetched.",
                                    "Paper fills are shared Ledger projections, not broker fills or THS instructions."]})
    if output_dir is not None:
        receipt["package_dir"] = str(package_dir)
    return _write_receipt(receipt, output_dir)


def _write_receipt(receipt: dict[str, Any], output_dir: str | Path | None) -> dict[str, Any]:
    if output_dir is not None:
        out = Path(output_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        path = out / "vertical_slice_receipt.json"
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        receipt["receipt_path"] = str(path)
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = run_vertical_slice(json.loads(args.spec.read_text(encoding="utf-8")), args.output_dir)
    print(json.dumps({"status": receipt["status"], "execution_admitted": False,
                      "receipt": receipt.get("receipt_path")}, ensure_ascii=False))
    return 0


__all__ = ["BLOCKED", "COMPLETED", "SCHEMA", "run_vertical_slice"]

if __name__ == "__main__":
    raise SystemExit(main())
