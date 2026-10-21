"""Convert a validated shortline candidate preview into the shared SignalFrame contract.

This is a research/replay bridge only.  It does not create prices, targets,
orders, account state or Tonghuashun payloads.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import date
from pathlib import Path

from ..config import StrategyConfig
from ..contracts import RunContext, SignalFrame, TradingMode
from ..engine import choose_targets


REQUIRED_COLUMNS = {"symbol", "date", "stage", "model_hit5_probability", "paper_candidate_only"}


def _bool_flag(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _finite_float(value: str, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"Invalid {field}: {value!r}")
    return parsed


def read_candidate_rows(path: Path, decision_day: str | None = None) -> tuple[str, list[dict]]:
    """Read only rows explicitly marked as paper candidates, with strict identity checks."""
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = set(reader.fieldnames or ())
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise ValueError(f"Candidate preview missing columns: {missing}")
        raw = list(reader)
    if not raw:
        raise ValueError("Candidate preview is empty")
    days = {date.fromisoformat(str(row["date"]).strip()).isoformat() for row in raw}
    if len(days) != 1:
        raise ValueError(f"Candidate preview must contain one decision day: {sorted(days)}")
    day = next(iter(days))
    if decision_day is not None and day != date.fromisoformat(decision_day).isoformat():
        raise ValueError(f"Candidate decision day {day} does not match requested {decision_day}")

    rows = []
    seen = set()
    for row in raw:
        if not _bool_flag(row.get("paper_candidate_only")):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        # SecurityId performs the canonical six-digit exchange validation below.
        score = _finite_float(row.get("model_hit5_probability"), "model_hit5_probability")
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"model_hit5_probability must be within [0, 1]: {score}")
        if symbol in seen:
            raise ValueError(f"Duplicate shortline candidate symbol: {symbol}")
        seen.add(symbol)
        diagnostics = {"paper_candidate_only": 1.0, "stage": _finite_float(row.get("stage"), "stage")}
        for name in ("candidate_rank_pct", "train_baseline_hit5_probability"):
            if row.get(name) not in (None, ""):
                diagnostics[name] = _finite_float(row[name], name)
        rows.append({"symbol": symbol, "score": score, **diagnostics})
    if not rows:
        raise ValueError("Candidate preview contains no paper_candidate_only=true rows")
    rows.sort(key=lambda item: (-item["score"], item["symbol"]))
    return day, rows


def build_signal_frame(path: Path, decision_day: str | None = None) -> tuple[SignalFrame, dict]:
    day, rows = read_candidate_rows(path, decision_day)
    frame = SignalFrame.from_legacy(
        rows,
        day,
        "research:shortline_limitup_continuation_v1",
        RunContext("shortline_research_bridge", TradingMode.REPLAY),
    )
    return frame, {
        "schema": "shortline_signal_frame_v1",
        "status": "completed_research_signal_frame",
        "candidate_preview": str(Path(path).resolve()),
        "decision_day": day,
        "signal_count": len(rows),
        "signal_frame": frame.to_dict(),
        "research_only": True,
        "paper_target_generated": False,
        "paper_orders_generated": False,
        "ths_payload_generated": False,
        "execution_admitted": False,
        "limitations": [
            "Scores are close-based research probabilities, not executable fill predictions.",
            "No prices, target weights, account state, orders, Ledger or THS client state was accessed.",
        ],
    }


def build_research_target(frame: SignalFrame, holdings: int | None = None) -> dict:
    """Apply the existing rank-buffer selector to a replay-only signal frame."""
    if not isinstance(frame, SignalFrame) or frame.context.mode != TradingMode.REPLAY:
        raise ValueError("Shortline target preview requires a replay SignalFrame")
    count = len(frame.signals) if holdings is None else int(holdings)
    if count < 1 or count > len(frame.signals):
        raise ValueError("Target preview holdings must fit the marked signal count")
    config = StrategyConfig(
        signal="equal_weight", holdings=count, retain_rank=count,
        gross_limit=0.8, single_limit=0.2, initial_cash=100000,
    ).validate()
    weights = choose_targets(frame.ranked_candidates(), [], config)
    return {
        "status": "completed_research_target_weight_preview",
        "strategy_id": frame.source,
        "decision_day": frame.day,
        "weights": weights,
        "weight_sum": sum(weights.values()),
        "selector": "trading.engine.choose_targets",
        "config": config.to_dict(),
        "research_only": True,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "execution_admitted": False,
        "limitations": [
            "Weights use only marked shortline scores; no prices, cash, held positions or T+1 state were supplied.",
            "This is not a Portfolio/Ledger target and cannot be sent to a broker or Tonghuashun.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--decision-day")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target-preview", action="store_true",
                        help="Also apply the existing replay rank selector to emit research-only weights")
    parser.add_argument("--target-out", type=Path,
                        help="Optional standalone JSON path for the research target preview")
    args = parser.parse_args(argv)
    frame, record = build_signal_frame(args.candidates, args.decision_day)
    if args.target_preview:
        target = build_research_target(frame)
        record["research_target"] = target
        if args.target_out:
            args.target_out.parent.mkdir(parents=True, exist_ok=True)
            args.target_out.write_text(json.dumps(target, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif args.target_out:
        raise ValueError("--target-out requires --target-preview")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": str(args.out.resolve()), "status": record["status"],
                      "signal_count": record["signal_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
