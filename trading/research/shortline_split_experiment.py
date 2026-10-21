"""Run a fixed-configuration temporal split for the shortline panel replay.

The split is descriptive only: early 2026 (through 2026-06-30) and later 2026
(2026-07-01 through 2026-10-09) use the same top-5-per-day, causal first-seen
20-symbol and five-holding configuration.  Next-session open clearance is joined after the
replay from the existing daily OHLC proxy and is never used for score or
selection.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .shortline_panel_replay import replay, write_result


SPLITS = {
    "early_2026": ("2026-01-01", "2026-06-30"),
    "later_2026": ("2026-07-01", "2026-10-09"),
}
TOUCHABILITY_COLUMNS = {
    "event_id", "next_session_observed", "next_open_clearance",
    "next_high_touches_limit_up", "next_open_locked_limit_up",
    "touchability_label_status",
}


def _bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def summarize_touchability(proxy_path: Path | None, selected_events: list[dict]) -> dict:
    """Summarize post-replay next-open clearance for selected events."""
    selected = pd.DataFrame(selected_events)
    selected_count = int(len(selected))
    base = {
        "proxy_source": str(Path(proxy_path).resolve()) if proxy_path else None,
        "selected_event_rows": selected_count,
        "matched_event_rows": 0,
        "next_session_observed": 0,
        "next_open_clearance": 0,
        "next_high_touches_limit_up": 0,
        "next_open_locked_limit_up": 0,
        "coverage": 0.0,
        "next_session_observed_rate": None,
        "next_open_clearance_rate_observed": None,
        "next_high_touches_limit_up_rate_observed": None,
        "next_open_locked_limit_up_rate_observed": None,
        "label_status_counts": {},
        "diagnostic_only": True,
        "used_for_score_or_selection": False,
        "executable_fill_label": False,
    }
    if proxy_path is None or not Path(proxy_path).is_file() or selected_count == 0:
        base["status"] = "unavailable"
        return base
    available_columns = pd.read_csv(Path(proxy_path), nrows=0).columns.tolist()
    missing = sorted(TOUCHABILITY_COLUMNS - set(available_columns))
    if missing:
        raise ValueError(f"Touchability proxy missing columns: {missing}")
    proxy = pd.read_csv(Path(proxy_path), usecols=sorted(TOUCHABILITY_COLUMNS), dtype={"event_id": str})
    proxy["event_id"] = proxy["event_id"].astype(str)
    if proxy["event_id"].duplicated().any():
        raise ValueError("Touchability proxy contains duplicate event_id")
    matched = selected[["event_id"]].drop_duplicates().merge(proxy, on="event_id", how="inner")
    base["matched_event_rows"] = int(len(matched))
    base["coverage"] = float(len(matched) / selected_count) if selected_count else 0.0
    for field in ("next_session_observed", "next_open_clearance",
                  "next_high_touches_limit_up", "next_open_locked_limit_up"):
        matched[field] = matched[field].map(_bool)
        base[field] = int(matched[field].sum())
    observed = matched["next_session_observed"]
    observed_count = int(observed.sum())
    base["next_session_observed_rate"] = float(observed.mean()) if len(matched) else None
    for field, out_field in (
        ("next_open_clearance", "next_open_clearance_rate_observed"),
        ("next_high_touches_limit_up", "next_high_touches_limit_up_rate_observed"),
        ("next_open_locked_limit_up", "next_open_locked_limit_up_rate_observed"),
    ):
        base[out_field] = float(matched.loc[observed, field].mean()) if observed_count else None
    base["label_status_counts"] = {
        str(key): int(value) for key, value in matched["touchability_label_status"].value_counts(dropna=False).items()
    }
    base["status"] = "completed_diagnostic"
    return base


def run_split_experiment(predictions: Path, snapshot: Path, *, annotation_path: Path | None,
                         touchability_path: Path | None, output_root: Path,
                         top_per_day: int = 5, max_symbols: int = 20,
                         holdings: int = 5, initial_cash: float = 100000) -> dict:
    """Run both fixed windows and persist child replay artifacts plus summary."""
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    split_rows = {}
    for name, (start, end) in SPLITS.items():
        result = replay(
            predictions, snapshot, start=start, end=end,
            top_per_day=top_per_day, max_symbols=max_symbols, holdings=holdings,
            initial_cash=initial_cash, annotation_path=annotation_path,
            symbol_cap_method="first_seen",
        )
        result["temporal_split"] = name
        result["touchability_proxy"] = summarize_touchability(
            touchability_path, result.get("selected_events") or []
        )
        child_paths = write_result(result, output_root / name)
        split_rows[name] = {
            "start": start,
            "end": end,
            "result": child_paths["result"],
            "audit": child_paths["audit"],
            "metrics": result["metrics"],
            "symbols": result["symbols"],
            "replay_sessions": result["replay_sessions"],
            "selected_event_rows": result["selected_event_rows"],
            "orders": len(result["orders"]),
            "fills": len(result["fills"]),
            "blocked_order_event_count": result["blocked_order_event_count"],
            "touchability": result["touchability_proxy"],
        }
    summary = {
        "schema": "a_share_shortline_temporal_split_replay_v1",
        "status": "completed_research_only_temporal_split",
        "strategy_id": "shortline_limitup_continuation_v1",
        "fixed_config": {
            "top_per_day": top_per_day,
            "max_symbols": max_symbols,
            "symbol_cap_method": "first_seen",
            "holdings": holdings,
            "initial_cash": initial_cash,
        },
        "splits": split_rows,
        "same_config_across_splits": True,
        "future_labels_used_for_score": False,
        "touchability_used_for_score_or_selection": False,
        "selection_enabled": False,
        "execution_admitted": False,
        "ledger_mutated": False,
        "strategy_validated": False,
        "ths_payload_generated": False,
        "limitations": [
            "Both windows are candidate-panel replays, not full-market validation or an untouched holdout.",
            "The 20-symbol cap is deterministic and fixed across windows; it is not tuned on split returns.",
            "Next-open clearance is a daily OHLC proxy and does not prove queue fills, latency, T+1 availability or broker execution.",
            "PIT membership, corporate actions, intraday queue and point-in-time theme mapping remain unresolved.",
        ],
    }
    summary_path = output_root / "split_result.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Shortline fixed temporal split replay", "",
        "The same top5/day, first-seen max20 symbols and five-holding configuration was used in both windows.",
        "Next-open clearance is a post-replay daily OHLC diagnostic; it does not enter score, selection or orders.",
        "",
        "| Window | Sessions | Symbols | Events | Orders | Fills | Net return | Max drawdown | Open clearance (observed) | Open locked (observed) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, row in split_rows.items():
        touch = row["touchability"]
        lines.append(
            f"| {name} | {row['replay_sessions']} | {row['symbols']} | {row['selected_event_rows']} | "
            f"{row['orders']} | {row['fills']} | {row['metrics']['net_return']:.2%} | "
            f"{row['metrics']['max_drawdown']:.2%} | "
            f"{(touch['next_open_clearance_rate_observed'] if touch['next_open_clearance_rate_observed'] is not None else float('nan')):.2%} | "
            f"{(touch['next_open_locked_limit_up_rate_observed'] if touch['next_open_locked_limit_up_rate_observed'] is not None else float('nan')):.2%} |"
        )
    lines += ["", "Execution admitted: `false`; selection enabled: `false`; strategy validated: `false`.",
              "", f"Summary JSON: `{summary_path}`"]
    (output_root / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--annotation-path", type=Path)
    parser.add_argument("--touchability-path", type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--top-per-day", type=int, default=5)
    parser.add_argument("--max-symbols", type=int, default=20)
    parser.add_argument("--holdings", type=int, default=5)
    parser.add_argument("--initial-cash", type=float, default=100000)
    args = parser.parse_args(argv)
    summary = run_split_experiment(
        args.predictions, args.snapshot, annotation_path=args.annotation_path,
        touchability_path=args.touchability_path, output_root=args.output_root,
        top_per_day=args.top_per_day, max_symbols=args.max_symbols,
        holdings=args.holdings, initial_cash=args.initial_cash,
    )
    print(json.dumps({"result": str((Path(args.output_root).resolve() / "split_result.json")),
                      "status": summary["status"], "splits": list(summary["splits"])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
