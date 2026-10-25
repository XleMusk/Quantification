"""Run one F01 event through the shared Engine/Ledger with explicit limits.

The scoped issuer PDFs contain real Q1 2024 values, but the original network
retrieval and revision history were not captured.  This adapter therefore uses
the immutable local PDF timestamp only as a research availability witness,
marks the slice ``PARTIAL_LOCAL_TIMESTAMP_VERTICAL_SLICE``, and keeps PIT and
promotion closed.  It still proves that one typed F01 event can reach Signal,
Target Portfolio, Orders, simulated BrokerAdapter, Fill and Cash/Position.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

import pandas as pd

from ..config import StrategyConfig
from ..contracts import RunContext, TradingMode, decision_time
from .f01_account_replay import publish_bundle, replay_f01
from .f01_financial_factor import FinancialFact, compute_f01
from .financial_factor_contracts import MARKET_TZ, TimeWindow
from .stockdb_rolling_low_volatility import RollingStockDBFeed, _read_snapshot


SCHEMA = "quantification_f01_local_vertical_slice_v1"
STATUS = "PARTIAL_LOCAL_TIMESTAMP_VERTICAL_SLICE"
DEFAULT_SCOPED = Path("outputs/financials/f01_scoped_field_coverage_20261024.json")
DEFAULT_FACTS = Path(r"D:\QuantificationData\trading\research\financials\20260930_162448_8be1e603\field_checks\20260930_q1_three_issuers\facts.json")
DEFAULT_SOURCE_RECEIPTS = Path(r"D:\QuantificationData\trading\research\financials\20260930_162448_8be1e603\field_checks\20260930_q1_three_issuers\source_receipts.json")
DEFAULT_SNAPSHOT = Path(r"D:\QuantificationData\trading\research\full_market\stockdb_2024_0101_1013_current_cutoff_v1")
DEFAULT_SUPPLEMENT = Path(r"D:\QuantificationData\trading\research\lowvol_supplements\20261024_combined_1023_stockdb_v1\raw_daily.parquet")
DEFAULT_DECISION_DAY = "2026-10-22"
DEFAULT_OUTPUT = Path("outputs/pipeline_receipts/20261024_f01_local_vertical_slice_v1.json")
DEFAULT_REPORT = Path("outputs/research_reports/20261024_f01_local_vertical_slice_v1.md")
DEFAULT_BUNDLE = Path(r"D:\QuantificationData\trading\research\financials\f01_local_vertical_slice\20261024_10-22_v1\bundle")


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number_tokens(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for token in re.findall(r"\(?[\d,]+(?:\.\d+)?\)?", text):
        negative = token.startswith("(") and token.endswith(")")
        clean = token.strip("()").replace(",", "")
        value = Decimal(clean)
        values.append(-value if negative else value)
    return values


def parse_prior_parent_profit(text: str) -> Decimal:
    """Read the comparative parent-profit value from the saved source snippet."""
    values = _number_tokens(text)
    if len(values) < 2:
        raise ValueError("parent-profit source snippet has no comparative value")
    # The source line can begin with the item number ``1.``; the comparative
    # value is the final numeric token after current-period profit.
    return values[-1]


def _local_timestamp(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_ctime, tz=timezone.utc).astimezone(MARKET_TZ)


def _source_index(path: Path) -> dict[str, Mapping[str, Any]]:
    rows = _json(path)
    if not isinstance(rows, list):
        raise ValueError("source receipts must be a list")
    return {str(row["symbol"]): row for row in rows}


def build_facts(scoped_path: Path, facts_path: Path, sources_path: Path,
                *, decision_day: str) -> tuple[dict[str, Any], dict[str, Any]]:
    scoped = _json(scoped_path)
    facts_rows = {str(row["symbol"]): row for row in _json(facts_path)}
    sources = _source_index(sources_path)
    event = TimeWindow.exact(decision_time(decision_day))
    result_by_symbol: dict[str, Any] = {}
    evidence_rows: list[dict[str, Any]] = []
    for row in scoped["rows_detail"]:
        symbol = str(row["symbol"])
        fact_row = facts_rows[symbol]
        source = sources[symbol]
        pdf = Path(str(source["pdf"])).resolve()
        if not pdf.is_file() or _sha256(pdf) != str(source["pdf_sha256"]):
            raise ValueError(f"issuer PDF hash mismatch: {symbol}")
        available_at = _local_timestamp(pdf)
        revision_id = str(source["pdf_sha256"])
        scope = "consolidated_reporting_scope_v1"
        current = FinancialFact(
            f"{symbol}:2024Q1:parent_net_profit:{revision_id[:16]}", symbol,
            "parent_net_profit", date(2024, 1, 1), date(2024, 3, 31), "single_quarter",
            scope, "CNY", "yuan", Decimal(str(fact_row["parent_profit_rmb"])), TimeWindow.exact(available_at),
        )
        prior = FinancialFact(
            f"{symbol}:2023Q1:parent_net_profit:{revision_id[:16]}", symbol,
            "parent_net_profit", date(2023, 1, 1), date(2023, 3, 31), "single_quarter",
            scope, "CNY", "yuan", parse_prior_parent_profit(str(row["parent_net_profit_source"]["snippet"])), TimeWindow.exact(available_at),
        )
        assets = FinancialFact(
            f"{symbol}:2024Q1:total_assets:{revision_id[:16]}", symbol,
            "total_assets", date(2024, 3, 31), date(2024, 3, 31), "point_in_time",
            scope, "CNY", "yuan", Decimal(str(row["total_assets"])), TimeWindow.exact(available_at),
        )
        factor = compute_f01(current, prior, assets, event)
        result_by_symbol[symbol] = factor
        evidence_rows.append({
            "symbol": symbol, "period": row["period"], "publication_date": row["pub_date"],
            "source_file": str(pdf), "source_sha256": revision_id,
            "revision_id": revision_id, "local_file_retrieved_at": available_at.isoformat(),
            "asof_at": available_at.isoformat(), "factor_status": factor.status,
            "factor_value": str(factor.value) if factor.value is not None else None,
            "issuer_network_retrieval_verified": False,
            "pit_safe": False,
        })
    return result_by_symbol, {"decision_day": decision_day, "event": event.to_dict(), "rows": evidence_rows,
                              "issuer_network_retrieval_verified": False, "pit_safe": False}


class _TailFeed:
    """Two-session StockDB slice used for the event and its next-session fill."""

    def __init__(self, source: RollingStockDBFeed, days: list[str], symbols: list[str]):
        self.calendar = days
        self.rows = {day: {symbol: source.rows.get(day, {}).get(symbol)
                           for symbol in symbols if source.rows.get(day, {}).get(symbol) is not None}
                     for day in days}
        self.instruments = {symbol: source.instruments[symbol] for symbol in symbols}
        self.actions = []
        self.instrument_profiles = {}
        self.manifest = {"scope": "f01_local_vertical_slice_stockdb_tail", "execution_admitted": False}
        self.volumes = source.volumes.loc[days, symbols]

    def next_day(self, day: str) -> str:
        index = self.calendar.index(day)
        return self.calendar[index + 1] if index + 1 < len(self.calendar) else "2026-10-24"


def run(scoped_path: Path = DEFAULT_SCOPED, facts_path: Path = DEFAULT_FACTS,
        sources_path: Path = DEFAULT_SOURCE_RECEIPTS, snapshot: Path = DEFAULT_SNAPSHOT,
        supplement: Path = DEFAULT_SUPPLEMENT, *, decision_day: str = DEFAULT_DECISION_DAY,
        bundle_path: Path = DEFAULT_BUNDLE) -> dict[str, Any]:
    results, evidence = build_facts(scoped_path.resolve(), facts_path.resolve(), sources_path.resolve(), decision_day=decision_day)
    source_snapshot, _manifest, frame, _supplement = _read_snapshot(snapshot, supplement, end="2026-10-23")
    symbols = sorted(results)
    base = RollingStockDBFeed(frame.loc[frame["symbol"].isin(symbols)].copy(), "2026-10-23", variant="low_volatility_252_20")
    days = [decision_day, "2026-10-23"]
    if any(day not in base.raw_calendar for day in days):
        raise ValueError(f"event/fill days missing from StockDB tail: {days}")
    feed = _TailFeed(base, days, symbols)
    config = StrategyConfig(signal="equal_weight", lookback=21, skip=1, volatility_window=10,
                            rebalance_sessions=1, holdings=3, retain_rank=3, gross_limit=.8,
                            single_limit=.2, initial_cash=1_000_000, min_amount=0,
                            min_listing_days=1, commission=.0003, minimum_commission=5,
                            sell_tax=.0005, transfer_fee=.00001, slippage_bps=0,
                            participation=.1).validate()
    context = RunContext("f01_local_vertical_slice", TradingMode.REPLAY)
    result = replay_f01(feed, {decision_day: results, "2026-10-23": {}}, config, context)
    bundle = publish_bundle(result, bundle_path.resolve())
    output = {
        "schema": SCHEMA, "status": STATUS, "execution_admitted": False,
        "research_only": True, "pit_verified": False, "factor_ready": False,
        "promotion_allowed": False, "decision_day": decision_day,
        "event_evidence": evidence, "source_snapshot": str(source_snapshot),
        "source_supplement": str(supplement.resolve()), "account_replay": True,
        "stages": result["stages"], "replay_sessions": len(result["daily"]),
        "event_signal_days": [row["day"] for row in result["daily"] if row["signal"].get("status") != "UNAVAILABLE"],
        "orders": sum(len(row["orders"]) for row in result["daily"]),
        "fills": sum(len(row["fills"]) for row in result["daily"]),
        "final_cash_cents": result["final_state"]["cash"],
        "final_position_count": len(result["final_state"]["positions"]),
        "bundle": str(bundle), "bundle_manifest_sha256": _sha256(bundle / "manifest.json"),
        "ledger_mutated": False, "broker_or_ths_interacted": False,
        "limitations": [
            "The source facts are issuer-PDF hashes with local filesystem timestamps; original issuer network retrieval and revision history remain unverified.",
            "This proves a local F01 event-to-account vertical slice only; PIT promotion and paper/live selection remain closed.",
            "StockDB tail is a research snapshot and simulated BrokerAdapter events are not broker fills.",
        ],
    }
    return output


def build_report(result: Mapping[str, Any]) -> str:
    return "\n".join([
        "# F01 local vertical slice", "",
        f"Status: `{result['status']}`; decision day `{result['decision_day']}`; execution: `false`.", "",
        f"The event reached `{', '.join(result['stages'])}` with `{result['orders']}` orders and `{result['fills']}` simulated fills in a copied research bundle.", "",
        "PIT/promotion remains closed because the evidence uses local PDF timestamps and does not prove original issuer-network retrieval or revision history.", "",
    ])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scoped", type=Path, default=DEFAULT_SCOPED)
    parser.add_argument("--facts", type=Path, default=DEFAULT_FACTS)
    parser.add_argument("--source-receipts", type=Path, default=DEFAULT_SOURCE_RECEIPTS)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--supplement", type=Path, default=DEFAULT_SUPPLEMENT)
    parser.add_argument("--decision-day", default=DEFAULT_DECISION_DAY)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    result = run(args.scoped, args.facts, args.source_receipts, args.snapshot, args.supplement,
                 decision_day=args.decision_day, bundle_path=args.bundle)
    output, report = args.output.resolve(), args.report.resolve()
    output.parent.mkdir(parents=True, exist_ok=True); report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(build_report(result), encoding="utf-8")
    print(json.dumps({"status": result["status"], "orders": result["orders"], "fills": result["fills"], "event_signal_days": result["event_signal_days"], "pit_verified": False, "execution_admitted": False}, ensure_ascii=False))
    return 0


__all__ = ["SCHEMA", "STATUS", "build_facts", "build_report", "main", "parse_prior_parent_profit", "run"]


if __name__ == "__main__":
    raise SystemExit(main())
