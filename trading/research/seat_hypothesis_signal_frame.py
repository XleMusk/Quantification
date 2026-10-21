"""Bridge a frozen public-seat hypothesis into the shared replay contracts.

The input contains retrospective forward-return labels.  Those labels are kept
as diagnostics only; the score used by :class:`SignalFrame` is the frozen
event-day concentration ratio.  This module deliberately stops at the shared
signal and target selector because the small paired panel does not contain a
complete, reconciled OHLC/account path for a ledger replay.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import date
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import RunContext, SecurityId, SignalFrame, TradingMode
from ..engine import choose_targets


LAGS = (1, 2)
HYPOTHESIS_ID = "lhb_identifiable_buy_concentration_v1"
REQUIRED_COLUMNS = {
    "trade_date", "symbol", "buy_seats", "sell_seats", "net_buy_cny",
    "net_buy_to_reported_turnover", "hypothesis_h1",
}


def _flag(value):
    return str(value).strip().lower() in {"1", "true", "yes"}


def _number(value, field, *, required=True):
    if value in (None, ""):
        if required:
            raise ValueError(f"Missing {field}")
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {field}: {value!r}") from error
    if not math.isfinite(result):
        raise ValueError(f"Invalid {field}: {value!r}")
    return result


def _day(value, field="trade_date"):
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid {field}: {value!r}") from error


def _canonical_symbol(value):
    """Map the public-source ``.BJ`` spelling to the contract's ``.BSE``."""
    symbol = str(value or "").strip().upper()
    if symbol.endswith(".BJ"):
        symbol = symbol[:-3] + ".BSE"
    return symbol


def _optional_label(row, lag):
    """Read a future label without turning an unavailable outcome into zero."""
    prefix = f"{lag}"
    outcome = _number(row.get(f"stockdb_return_lag{prefix}"),
                      f"stockdb_return_lag{prefix}", required=False)
    independent = _number(row.get(f"baostock_return_lag{prefix}"),
                           f"baostock_return_lag{prefix}", required=False)
    label_date = row.get(f"lag{prefix}_date")
    if label_date not in (None, ""):
        label_date = _day(label_date, f"lag{prefix}_date")
    else:
        label_date = None
    available = float(outcome is not None and independent is not None)
    agreement = float(available and abs(outcome - independent) <= 1e-10)
    result = {
        f"label_available_lag{prefix}": available,
        f"label_source_agreement_lag{prefix}": agreement,
    }
    if outcome is not None:
        result[f"stockdb_return_lag{prefix}"] = outcome
    if independent is not None:
        result[f"baostock_return_lag{prefix}"] = independent
    if label_date is not None:
        # Signal diagnostics are numeric; the date itself is retained in the
        # record below, not smuggled into the typed signal.
        result[f"label_date_ordinal_lag{prefix}"] = float(date.fromisoformat(label_date).toordinal())
    return result


def read_forward_labels(path: Path, lag: int, decision_day: str | None = None):
    """Validate the frozen candidate panel and return rows grouped by day."""
    if lag not in LAGS:
        raise ValueError(f"label lag must be one of {LAGS}")
    path = Path(path).resolve()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = set(reader.fieldnames or ())
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise ValueError(f"Forward-label panel missing columns: {missing}")
        raw = list(reader)
    if not raw:
        raise ValueError("Forward-label panel is empty")
    requested = _day(decision_day, "decision_day") if decision_day is not None else None
    grouped = {}
    seen = set()
    for row in raw:
        day = _day(row.get("trade_date"))
        if requested is not None and day != requested:
            continue
        symbol = _canonical_symbol(row.get("symbol"))
        SecurityId(symbol)
        key = (day, symbol)
        if key in seen:
            raise ValueError(f"Duplicate seat candidate: {day} {symbol}")
        seen.add(key)
        if not _flag(row.get("hypothesis_h1")):
            raise ValueError(f"Row is outside frozen {HYPOTHESIS_ID}: {day} {symbol}")
        buy_seats = _number(row.get("buy_seats"), "buy_seats")
        sell_seats = _number(row.get("sell_seats"), "sell_seats")
        net_buy = _number(row.get("net_buy_cny"), "net_buy_cny")
        ratio = _number(row.get("net_buy_to_reported_turnover"), "net_buy_to_reported_turnover")
        if any(value < 0 or int(value) != value for value in (buy_seats, sell_seats)):
            raise ValueError(f"Seat counts must be nonnegative integers: {day} {symbol}")
        if buy_seats < 2 or net_buy <= 0 or ratio < 0.01:
            raise ValueError(f"Candidate violates frozen formula: {day} {symbol}")
        labels = _optional_label(row, lag)
        record = {
            "trade_date": day,
            "symbol": symbol,
            "score": ratio,
            "buy_seats": float(buy_seats),
            "sell_seats": float(sell_seats),
            "net_buy_cny": net_buy,
            "net_buy_to_reported_turnover": ratio,
            "hypothesis_h1": 1.0,
            **labels,
        }
        grouped.setdefault(day, []).append(record)
    if not grouped:
        raise ValueError("No candidates match decision_day")
    for day, rows in grouped.items():
        rows.sort(key=lambda item: (-item["score"], item["symbol"]))
    return grouped


def build_signal_frames(path: Path, lag: int):
    """Create one typed replay frame per event day; labels remain diagnostics."""
    grouped = read_forward_labels(path, lag)
    context = RunContext("seat_research_bridge", TradingMode.REPLAY)
    frames = {}
    for day, rows in grouped.items():
        signal_rows = [{key: value for key, value in row.items() if key != "trade_date"}
                       for row in rows]
        frames[day] = SignalFrame.from_legacy(
            signal_rows,
            day,
            f"research:{HYPOTHESIS_ID}:label_lag{lag}_outcome_only",
            context,
        )
    return frames


def build_research_target(frame: SignalFrame, lag: int, holdings: int = 5):
    """Apply the existing rank-buffer selector without opening a Ledger."""
    if not isinstance(frame, SignalFrame) or frame.context.mode != TradingMode.REPLAY:
        raise ValueError("Seat target preview requires a replay SignalFrame")
    if lag not in LAGS:
        raise ValueError(f"label lag must be one of {LAGS}")
    count = int(holdings)
    if count < 1 or count > len(frame.signals):
        raise ValueError("Target preview holdings must fit the marked signal count")
    config = StrategyConfig(
        signal="equal_weight", holdings=count, retain_rank=count,
        gross_limit=0.8, single_limit=0.2, initial_cash=100000,
    ).validate()
    weights = choose_targets(frame.ranked_candidates(), [], config)
    labels = [s for s in frame.signals if dict(s.diagnostics).get(f"label_available_lag{lag}") == 1.0]
    return {
        "status": "completed_research_target_weight_preview",
        "strategy_id": HYPOTHESIS_ID,
        "decision_day": frame.day,
        "label_lag_sessions": lag,
        "weights": weights,
        "weight_sum": sum(weights.values()),
        "selector": "trading.engine.choose_targets",
        "config": config.to_dict(),
        "label_rows_available": len(labels),
        "signal_score_definition": "net_buy_to_reported_turnover; future returns are diagnostics only",
        "research_only": True,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "ledger_replay_completed": False,
        "execution_admitted": False,
        "limitations": [
            "Forward returns are retrospective outcome labels and were not used for score or selection.",
            "No complete OHLC/account path exists for all candidate rows, so no Engine/Ledger replay is claimed.",
            "Unknown labels remain missing; they are not imputed as zero.",
            "Public disclosure publication timestamps are unknown; this is an EOD research assumption only.",
            "Public-source .BJ identifiers are canonicalized to the contract .BSE spelling for typed signals.",
        ],
    }


def build_record(path: Path, lag: int, holdings: int = 5, decision_day: str | None = None):
    grouped = read_forward_labels(path, lag, decision_day)
    context = RunContext("seat_research_bridge", TradingMode.REPLAY)
    frames = {}
    targets = {}
    for day, rows in grouped.items():
        signal_rows = [{key: value for key, value in row.items() if key != "trade_date"}
                       for row in rows]
        frame = SignalFrame.from_legacy(
            signal_rows, day, f"research:{HYPOTHESIS_ID}:label_lag{lag}_outcome_only", context)
        frames[day] = frame
        target_holdings = min(int(holdings), len(rows))
        targets[day] = build_research_target(frame, lag, target_holdings)
    signal_count = sum(len(frame.signals) for frame in frames.values())
    return {
        "schema": "seat_hypothesis_signal_frame_v1",
        "status": "completed_research_signal_frame",
        "hypothesis_id": HYPOTHESIS_ID,
        "forward_labels": str(Path(path).resolve()),
        "label_lag_sessions": lag,
        "decision_day": decision_day,
        "signal_count": signal_count,
        "decision_days": sorted(frames),
        "signal_frames": {day: frame.to_dict() for day, frame in frames.items()},
        "research_targets": targets,
        "selector": "trading.engine.choose_targets",
        "run_context": context.to_dict(),
        "future_labels_used_for_score": False,
        "engine_ledger_replay_completed": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "ths_payload_generated": False,
        "execution_admitted": False,
        "limitations": [
            "This consumer enters the shared SignalFrame and target selector only.",
            "Forward labels are retrospective validation diagnostics, never signal inputs.",
            "No missing label was imputed; incomplete source coverage blocks Ledger replay.",
            "No account, broker, THS, order or fill state was accessed.",
            "Public-source .BJ identifiers are canonicalized to the contract .BSE spelling for typed signals.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forward-labels", required=True, type=Path)
    parser.add_argument("--lag", required=True, type=int, choices=LAGS)
    parser.add_argument("--decision-day")
    parser.add_argument("--holdings", type=int, default=5)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    result = build_record(args.forward_labels, args.lag, args.holdings, args.decision_day)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": str(args.out.resolve()), "status": result["status"],
                      "signal_count": result["signal_count"], "decision_days": result["decision_days"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
