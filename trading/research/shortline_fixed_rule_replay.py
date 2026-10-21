"""Replay a frozen emotion rule through the shared shortline Engine/Ledger.

The rule is intentionally binary and frozen before replay:
``amount_rel20_pre <= 1.0`` and ``market_breadth >= 0``.  Both fields are
attached to the event's signal date and are the only fields used to mark a
candidate. The corrected event panel is used for identity reconciliation and
signal-date full-market breadth checks;
its continuation labels are never loaded into the prediction frame or used
for selection.  Outputs are a research-only candidate panel and never mutate
the persistent Ledger or Tonghuashun state.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from .shortline_panel_replay import replay, write_result
from .shortline_style_quality import MARKET_COUNT_COLUMNS, reject_known_legacy_sidecar, validate_sidecar_breadth


DEFAULT_RULE_AMOUNT_MAX = 1.0
DEFAULT_RULE_BREADTH_MIN = 0.0
REQUIRED_EMOTION_COLUMNS = {
    "symbol", "date", "stage", "amount_rel20_pre", "market_breadth",
}
PREDICTION_COLUMNS = [
    "event_id", "symbol", "date", "stage", "amount_rel20_pre",
    "market_breadth", "fixed_rule_pass", "model_hit5_probability",
    "paper_candidate_only",
]


def _normalise_events(frame: pd.DataFrame, source: Path) -> pd.DataFrame:
    missing = sorted(REQUIRED_EMOTION_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError(f"Emotion feature file missing columns: {missing}")
    frame = frame.copy()
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
    frame["stage"] = pd.to_numeric(frame["stage"], errors="raise")
    if frame["stage"].isna().any() or frame["stage"].mod(1).ne(0).any() or frame["stage"].lt(1).any():
        raise ValueError("Emotion feature stage must be a positive integer")
    frame["stage"] = frame["stage"].astype(int)
    if frame[["symbol", "date", "stage"]].duplicated().any():
        raise ValueError(f"Emotion feature has duplicate symbol/date/stage keys: {source}")
    for col in ("amount_rel20_pre", "market_breadth"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["event_id"] = (
        frame["symbol"] + "|" + frame["date"] + "|s" + frame["stage"].astype(str)
    )
    if frame["event_id"].duplicated().any():
        raise ValueError("Derived emotion event_id is duplicated")
    return frame


def load_fixed_rule_predictions(
    emotion_path: Path,
    annotation_path: Path,
    *,
    start: str | None = None,
    end: str | None = None,
    amount_max: float = DEFAULT_RULE_AMOUNT_MAX,
    breadth_min: float = DEFAULT_RULE_BREADTH_MIN,
) -> tuple[pd.DataFrame, dict]:
    """Create binary prediction rows from signal-date emotion fields.

    Only annotation identity and signal-date market counts are read. Future continuation labels
    are deliberately omitted from the load and cannot influence the rule.
    """
    emotion_path, annotation_path = Path(emotion_path), Path(annotation_path)
    reject_known_legacy_sidecar(emotion_path)
    frame = _normalise_events(pd.read_csv(emotion_path), emotion_path)
    if not annotation_path.is_file():
        raise FileNotFoundError(annotation_path)
    available = set(pq.read_schema(annotation_path).names)
    missing_counts = sorted(set(MARKET_COUNT_COLUMNS) - available)
    if missing_counts:
        raise ValueError(f"Corrected annotation lacks full-market breadth evidence: {missing_counts}")
    ann = pd.read_parquet(annotation_path, columns=["event_id", "symbol", "date", "stage", *MARKET_COUNT_COLUMNS])
    sidecar_quality = validate_sidecar_breadth(frame, ann, emotion_path)
    ann["event_id"] = ann["event_id"].astype(str)
    ann["symbol"] = ann["symbol"].astype(str).str.strip().str.upper()
    ann["date"] = pd.to_datetime(ann["date"], errors="raise").dt.strftime("%Y-%m-%d")
    ann["stage"] = pd.to_numeric(ann["stage"], errors="raise").astype(int)
    if ann[["event_id", "symbol", "date", "stage"]].duplicated().any():
        raise ValueError(f"Annotation identity rows are duplicated: {annotation_path}")
    expected_ids = ann["symbol"] + "|" + ann["date"] + "|s" + ann["stage"].astype(str)
    if (expected_ids != ann["event_id"]).any():
        raise ValueError("Annotation event_id does not match symbol/date/stage identity")
    annotation_ids = set(ann["event_id"])
    matched = frame.loc[frame["event_id"].isin(annotation_ids)].copy()
    missing_identity_rows = int(len(frame) - len(matched))
    if start is not None:
        matched = matched.loc[matched["date"] >= str(start)]
    if end is not None:
        matched = matched.loc[matched["date"] <= str(end)]
    finite = matched["amount_rel20_pre"].notna() & matched["market_breadth"].notna()
    missing_signal_rows = int((~finite).sum())
    matched = matched.loc[finite].copy()
    matched["fixed_rule_pass"] = (
        matched["amount_rel20_pre"].le(float(amount_max))
        & matched["market_breadth"].ge(float(breadth_min))
    )
    candidates = matched.loc[matched["fixed_rule_pass"]].copy()
    # This is a binary rule, rather than a fitted probability.  A unit score
    # is required by the shared panel consumer and only breaks ties
    # deterministically by date/symbol/stage.
    candidates["model_hit5_probability"] = 1.0
    candidates["paper_candidate_only"] = True
    candidates = candidates.sort_values(["date", "symbol", "stage"], kind="stable")
    predictions = candidates[PREDICTION_COLUMNS].reset_index(drop=True)
    record = {
        "schema": "a_share_shortline_fixed_emotion_rule_predictions_v1",
        "emotion_source": str(emotion_path.resolve()),
        "annotation_identity_source": str(annotation_path.resolve()),
        "source_rows": int(len(frame)),
        "annotation_identity_rows": int(len(ann)),
        "annotation_identity_matched_rows": int(len(frame) - missing_identity_rows),
        "missing_annotation_identity_rows": missing_identity_rows,
        "rows_in_requested_range": int(len(matched)),
        "missing_signal_field_rows": missing_signal_rows,
        "candidate_rows": int(len(predictions)),
        "candidate_days": int(predictions["date"].nunique()),
        "candidate_symbols": int(predictions["symbol"].nunique()),
        "rule": {
            "amount_rel20_pre_max": float(amount_max),
            "market_breadth_min": float(breadth_min),
            "amount_rel20_pre": f"<= {float(amount_max):g}",
            "market_breadth": f">= {float(breadth_min):g}",
            "signal_fields": ["amount_rel20_pre", "market_breadth"],
            "decision_horizon": "signal_date_close",
        },
        "selection_fields": ["amount_rel20_pre", "market_breadth"],
        "future_labels_loaded": False,
        "future_labels_used_for_selection": False,
        "prediction_future_label_columns": [],
        "signal_sidecar_quality": sidecar_quality,
        "paper_candidate_only": True,
    }
    if not len(predictions):
        raise ValueError("Fixed rule produced no annotation-backed candidates")
    return predictions, record


def run(
    emotion_path: Path,
    annotation_path: Path,
    snapshot: Path,
    output: Path,
    *,
    start: str = "2026-01-01",
    end: str = "2026-10-09",
    top_per_day: int = 5,
    holdings: int = 5,
    initial_cash: float = 100000,
    max_symbols: int | None = 20,
    symbol_cap_method: str = "first_seen",
    amount_max: float = DEFAULT_RULE_AMOUNT_MAX,
    breadth_min: float = DEFAULT_RULE_BREADTH_MIN,
) -> tuple[Path, dict]:
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    predictions, prediction_info = load_fixed_rule_predictions(
        emotion_path, annotation_path, start=start, end=end,
        amount_max=amount_max, breadth_min=breadth_min,
    )
    # The panel writer creates its output directory.  Keep the input staging
    # file outside it, then copy the durable prediction artifact into output.
    staging = output.parent / f".{output.name}.fixed_rule_predictions.csv"
    predictions.to_csv(staging, index=False, encoding="utf-8-sig")
    try:
        result = replay(
            staging, snapshot, start=start, end=end, top_per_day=top_per_day,
            holdings=holdings, initial_cash=initial_cash,
            # Identity and signal-date breadth checks were completed above
            # without forward labels. Passing no annotation path keeps the shared consumer
            # from loading continuation-label columns even diagnostically.
            annotation_path=None, max_symbols=max_symbols,
            symbol_cap_method=symbol_cap_method,
        )
        result.update({
            "strategy_id": "shortline_fixed_emotion_rule_v1",
            "fixed_rule": prediction_info["rule"],
            "fixed_rule_prediction_info": prediction_info,
            "prediction_source": str((output / "fixed_rule_predictions.csv").resolve()),
            "prediction_rows": int(len(predictions)),
            "prediction_future_label_columns": [],
            "future_labels_loaded_for_rule": False,
            "future_labels_used_for_score": False,
            "candidate_panel_only": True,
            "selection_enabled": False,
            "execution_admitted": False,
            "ledger_mutated": False,
            "strategy_validated": False,
            "ths_payload_generated": False,
            "limitations": [
                "Candidate-panel replay is not full-market or untouched validation.",
                "Candidates are selected from close-available fixed signal-date fields; no intraday queue, order-book or executable-fill data is available.",
                "Raw OHLCV is used; preclose for limit rules is reconstructed from physical pct_chg, and PIT membership/company actions remain unresolved.",
                "tradestatus is a positive-volume proxy; broker/account/THS state is never read or changed.",
            ],
        })
        paths = write_result(result, output)
        predictions.to_csv(output / "fixed_rule_predictions.csv", index=False, encoding="utf-8-sig")
        audit_path = output / "audit.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        audit["schema"] = "a_share_shortline_fixed_emotion_rule_replay_audit_v1"
        audit["checks"].update({
            "fixed_rule_signal_day_fields_only": True,
            "future_labels_loaded_for_rule": False,
            "future_labels_used_for_selection": False,
            "prediction_has_future_label_columns": False,
            "annotation_identity_only_for_selection": True,
            "annotation_signal_date_counts_for_quality": True,
            "future_annotation_label_columns_read": False,
            "candidate_panel_only": True,
        })
        audit["prediction"] = prediction_info
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Add the fixed-rule interpretation to the generic replay report.
        report_path = output / "REPORT.md"
        report = report_path.read_text(encoding="utf-8")
        report_path.write_text(
            "# Fixed emotion-rule shortline candidate replay\n\n"
            "Frozen signal-date rule: `amount_rel20_pre <= 1.0` and "
            "`market_breadth >= 0`. The corrected event panel is used for "
            "identity and signal-date breadth quality checks; continuation labels are not loaded into the rule.\n\n"
            f"Prediction rows: `{len(predictions)}`; candidate days: "
            f"`{prediction_info['candidate_days']}`; candidate symbols: "
            f"`{prediction_info['candidate_symbols']}`.\n\n" + report +
            "\nThe output is a binary research rule, not a fitted hit probability, "
            "and is not admitted to paper trading, THS prefill, or real execution.\n",
            encoding="utf-8",
        )
        # Rewrite result after the writer has persisted all fields and add the
        # prediction path that now exists inside the output directory.
        (output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        paths["predictions"] = str((output / "fixed_rule_predictions.csv").resolve())
        return output / "result.json", {**result, "paths": paths}
    finally:
        if staging.exists():
            staging.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emotion-path", required=True, type=Path)
    parser.add_argument("--annotation-path", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-10-09")
    parser.add_argument("--top-per-day", type=int, default=5)
    parser.add_argument("--max-symbols", type=int, default=20,
                        help="Causal first_seen cap; omit with --uncapped for stress replay")
    parser.add_argument("--symbol-cap-method", choices=("score_max", "first_seen"), default="first_seen")
    parser.add_argument("--uncapped", action="store_true", help="Run the same fixed rule without a symbol cap")
    parser.add_argument("--holdings", type=int, default=5)
    parser.add_argument("--initial-cash", type=float, default=100000)
    parser.add_argument("--amount-max", type=float, default=DEFAULT_RULE_AMOUNT_MAX)
    parser.add_argument("--breadth-min", type=float, default=DEFAULT_RULE_BREADTH_MIN)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    max_symbols = None if args.uncapped else args.max_symbols
    path, result = run(
        args.emotion_path, args.annotation_path, args.snapshot, args.out,
        start=args.start, end=args.end, top_per_day=args.top_per_day,
        holdings=args.holdings, initial_cash=args.initial_cash,
        max_symbols=max_symbols, symbol_cap_method=args.symbol_cap_method,
        amount_max=args.amount_max, breadth_min=args.breadth_min,
    )
    print(json.dumps({"result": str(path), "audit": str(args.out / 'audit.json'),
                      "predictions": str(args.out / 'fixed_rule_predictions.csv'),
                      "status": result["status"], "orders": len(result["orders"]),
                      "fills": len(result["fills"]),
                      "net_return": result["metrics"]["net_return"],
                      "max_symbols": result["max_symbols"],
                      "symbol_cap_method": result["symbol_cap_method"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
