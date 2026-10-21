"""Fixed saved-environment -> subsequent raw-price risk diagnostic.

This does not run a strategy, construct an investable equal-weight portfolio,
or change any Engine/Ledger account. Historical periods were already inspected.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..contracts import MARKET_TZ
from .leader_eight_type_panel_replay import _environment
from .leader_daily_signals import _calendar_view
from .leader_environment_policy import STRENGTH, MIXED, WEAKNESS


METHOD = "fixed_saved_environment_subsequent_raw_price_risk_v1"
INPUT_SCHEMA = "leader_eight_type_observed_native_historical_input_v1"
FLAGS = {"candidate_panel_only": True, "strategy_validated": False,
    "selection_enabled": False, "execution_admitted": False, "returns_reportable": False,
    "pit_verified": False, "provider_contacted": False, "model_refit": False,
    "engine_advanced": False, "account_created": False, "ledger_mutated": False,
    "investable_portfolio_return": False, "independent_holdout": False,
    "historical_signal_publication_verified": False}
PHASE_ORDER = (STRENGTH, MIXED, WEAKNESS)
SPEC = {
    "id": METHOD,
    "label": "original_saved_environment_and_exact_original_two_close_confirmation_and_mode",
    "environment_function": "leader_eight_type_panel_replay._environment",
    "environment_thresholds_changed": False,
    "origin_days": "every_exact_source_session_within_manifest_start_end",
    "outcome_scope": "fixed_manifest_scope_members_then_D_active_nonST_positive_close_and_volume",
    "environment_denominator": "unchanged_saved_panel_denominator_may_differ_from_fixed_candidate_scope",
    "future_status_filter": "no_future_ST_exclusion;future_ST_count_retained;any_halt_missing_or_invalid_bar_is_unknown_not_zero",
    "entry": "exact_calendar_D_plus_1_raw_open",
    "horizons_sessions": [1, 5],
    "endpoint_price_change_pct": "100*(raw_close_at_horizon/raw_open_E-1)",
    "breadth": "(positive_endpoint_count-negative_endpoint_count)/known_complete_path_count;ties_zero_but_not_missing",
    "mean_stock_adverse_excursion_pct": "mean(100*min(0,min(raw_low_E_through_horizon)/raw_open_E-1))",
    "mean_stock_close_drawdown_pct": "mean(min_j(100*(close_j/max(open_E,close_E..close_j)-1)))",
    "valid_outcome": "every_exact_horizon_session_has_one_actual_active_positive_volume_positive_consistent_OHLC_bar",
    "missing_policy": "keep_D_eligible_denominator_and_explicit_first_issue;no_forward_fill_halt_carry_or_missing_zero",
    "five_session_primary": "origin_index_from_original_first_D_mod_5_equals_0;never_shift_for_missing_or_phase",
    "five_session_secondary": "all_origins_overlapping_not_independent_samples",
    "aggregation": "first_each_origin_crosssection_then_unweighted_origin_means_within_each_source_period_phase_and_confirmation",
    "primary_groups": "two_close_confirmed_strength_mixed_weakness;unconfirmed_and_unknown_retained_separately",
    "risk_ordering": "confirmed_strength>=confirmed_mixed>=confirmed_weakness_for_mean_adverse_excursion_and_endpoint_breadth;null_if_any_group_absent",
    "mode_semantics": "original_weakness_EXIT_ALL_can_precede_confirmation;no_reinterpretation_or_account_change",
    "independent_unit": "origin_date_not_security;nonoverlap_avoids_same_future_sessions_but_does_not_prove_independence",
    "metrics_selected_by_results": False,
    "primary_and_secondary_metrics_all_retained": True,
    "year_or_capacity_selected_by_results": False,
    "net_value_sharpe_cagr_significance_claimed": False,
    "price_basis": "unadjusted_native_raw_price_changes_not_economic_total_returns;corporate_action_discontinuities_not_repaired",
    "historical_intervals_already_inspected": True,
    "cross_period_pooling": False,
    "source_recollection_or_notice_review": False,
}
COLUMNS = ("date", "symbol", "open", "high", "low", "close", "volume", "tradestatus", "isST")
STOCK_SCHEMA = pa.schema([
    ("period", pa.string()), ("origin_day", pa.string()), ("symbol", pa.string()),
    ("horizon_sessions", pa.int8()), ("execution_day", pa.string()), ("horizon_end_day", pa.string()),
    ("five_session_primary_origin", pa.bool_()), ("status", pa.string()), ("first_issue_day", pa.string()),
    ("raw_open_E", pa.float64()), ("endpoint_price_change_pct", pa.float64()),
    ("adverse_excursion_pct", pa.float64()), ("close_drawdown_pct", pa.float64()),
    ("future_ST_observed", pa.bool_()),
])
_DAY_LINE = re.compile(rb'^  "(\d{4}-\d{2}-\d{2})": \{\s*$')


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _digest(path):
    result = sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _pin(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": _digest(path)}


def _checked(ref):
    path = Path(ref["path"]).resolve()
    if _digest(path) != ref["sha256"]:
        raise ValueError("Registered diagnostic input changed: " + str(path))
    return path


def calendar_sessions(payload):
    year = int(payload["year"])
    sessions, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar" or not sessions:
        raise ValueError("Original pinned exchange calendar interpretation required")
    return sessions


def register(inputs, out):
    """Pin explicit existing manifests without reading price outcomes."""
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Diagnostic registration requires a new directory")
    periods, seen = [], set()
    for label, manifest in inputs:
        if not label or label in seen:
            raise ValueError("Distinct explicit source-period labels required")
        seen.add(label)
        manifest = Path(manifest).resolve()
        saved = _read(manifest)
        if (saved.get("schema") != INPUT_SCHEMA or not saved.get("source_days")
                or not saved.get("scope_members") or saved.get("execution_admitted") is not False
                or saved["source_days"] != sorted(set(saved["source_days"]))
                or len(saved["scope_members"]) != len(set(saved["scope_members"]))):
            raise ValueError("Existing exact native historical manifest and fixed scope required")
        periods.append({"period": label, "manifest": _pin(manifest),
            "raw": {"path": saved["raw_file"], "sha256": saved["raw_sha256"]},
            "panels": {"path": saved["panels_file"], "sha256": saved["panels_sha256"]},
            "calendar": {"path": saved["calendar"], "sha256": saved["calendar_sha256"]},
            "source_created_at": saved["created_at"], "start": saved["start"], "end": saved["end"],
            "through": saved["through"], "source_sessions": len(saved["source_days"]),
            "fixed_scope_count": len(saved["scope_members"]), "scope_basis": saved.get("scope_basis"),
            "raw_rows": saved["raw_rows"], "historical_input_schema": saved["schema"],
            "historical_listing_classification": saved.get("historical_listing_classification"),
            "identity_overlay_used_for_outcome": False})
    if not periods:
        raise ValueError("At least one explicit existing source period required")
    research = Path(__file__).resolve().parent
    record = {"schema": METHOD + "_registration", "method_id": METHOD, "registered_at": _now(),
        "spec": SPEC, "periods": periods,
        "code": [_pin(__file__), _pin(research / "leader_eight_type_panel_replay.py"),
            _pin(research / "leader_environment_policy.py"), _pin(research / "leader_daily_signals.py")], **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "registration.json", record)
    return record


def read_environment_projection(path, expected_sha256):
    """One streaming pass over saved pretty JSON; retain only top-level headers.

    Directly pinned original bytes are hashed in the same pass. Large type,
    identity and per-stock feature payloads are never materialized or changed.
    """
    result, current, environment_lines = {}, None, None
    hasher = sha256()
    with Path(path).open("rb") as stream:
        for line in stream:
            hasher.update(line)
            match = _DAY_LINE.match(line)
            if match:
                if current is not None and (set(current) != {"day", "available_at", "environment"}):
                    raise ValueError("Incomplete saved environment header")
                day = match.group(1).decode("ascii")
                if day in result:
                    raise ValueError("Duplicate saved environment day")
                current = {}; result[day] = current
                continue
            if current is None:
                continue
            if environment_lines is not None:
                environment_lines.append(line)
                if line.rstrip() in (b"    },", b"    }"):
                    current["environment"] = json.loads(b"".join(environment_lines).rstrip(b",\r\n"))
                    environment_lines = None
                continue
            if line.startswith(b'    "day":'):
                current["day"] = json.loads(line.split(b":", 1)[1].strip().rstrip(b","))
            elif line.startswith(b'    "available_at":'):
                current["available_at"] = json.loads(line.split(b":", 1)[1].strip().rstrip(b","))
            elif line.startswith(b'    "environment": {'):
                environment_lines = [b"{\n"]
    if (hasher.hexdigest() != expected_sha256 or environment_lines is not None or not result
            or current is None or set(current) != {"day", "available_at", "environment"}
            or any(day != row["day"] for day, row in result.items())):
        raise ValueError("Pinned environment bytes or original pretty-JSON header contract differ")
    return result


def prepare_raw(raw, source_days, members):
    """Numeric views preserve missing/status values; they do not fill observations."""
    if raw.duplicated(["date", "symbol"]).any() or sorted(raw.date.unique()) != list(source_days):
        raise ValueError("Raw observations must be unique on exact registered source days")
    members = sorted(members)
    scope = raw.loc[raw.symbol.isin(members), list(COLUMNS)].copy()
    scope["present"] = 1.0
    for name in COLUMNS[2:]:
        scope[name] = pd.to_numeric(scope[name], errors="coerce")
    views = {name: scope.pivot(index="date", columns="symbol", values=name)
        .reindex(index=source_days, columns=members).to_numpy(dtype=float)
        for name in (*COLUMNS[2:], "present")}
    return views, members


def origin_eligibility(views, index):
    status = np.full(views["present"].shape[1], "eligible", dtype=object)
    # First issue is explicit and mutually exclusive; no unknown means zero.
    tests = [(~np.isfinite(views["present"][index]), "D_missing_row"),
        (~np.isfinite(views["tradestatus"][index]), "D_unknown_trading_status"),
        (np.isfinite(views["tradestatus"][index]) & (views["tradestatus"][index] != 1), "D_inactive_or_halted"),
        (~np.isfinite(views["isST"][index]), "D_unknown_ST_status"),
        (np.isfinite(views["isST"][index]) & (views["isST"][index] != 0), "D_ST_or_nonordinary_status"),
        (~np.isfinite(views["close"][index]) | (views["close"][index] <= 0), "D_invalid_close"),
        (~np.isfinite(views["volume"][index]), "D_unknown_volume"),
        (np.isfinite(views["volume"][index]) & (views["volume"][index] <= 0), "D_nonpositive_volume")]
    for mask, reason in tests:
        status[(status == "eligible") & mask] = reason
    return status == "eligible", dict(Counter(status.tolist()))


def outcomes(views, *, source_days, members, calendar, day, eligible, horizon, period, primary):
    origin = calendar.index(day)
    future = calendar[origin + 1:origin + horizon + 1]
    count = len(members)
    status = np.full(count, "known_complete_raw_price_path", dtype=object)
    issue_day = np.full(count, None, dtype=object)
    future_ST = np.full(count, False, dtype=bool)
    open_E = np.full(count, np.nan)
    lows, closes = [], []
    indices = {item: index for index, item in enumerate(source_days)}
    if len(future) != horizon:
        status[:] = "right_censored_exchange_calendar"
    for position, future_day in enumerate(future):
        if future_day not in indices:
            mask = status == "known_complete_raw_price_path"
            status[mask] = "missing_exact_source_day"; issue_day[mask] = future_day
            continue
        idx = indices[future_day]
        op, hi, lo, cl = (views[name][idx] for name in ("open", "high", "low", "close"))
        trade, volume = views["tradestatus"][idx], views["volume"][idx]
        future_ST |= views["isST"][idx] == 1
        if position == 0:
            open_E = op.copy()
        tests = [(~np.isfinite(views["present"][idx]), "missing_raw_row"),
            (~np.isfinite(trade), "unknown_trading_status"),
            (np.isfinite(trade) & (trade != 1), "inactive_or_halted_path"),
            (~np.isfinite(volume), "unknown_volume"),
            (np.isfinite(volume) & (volume <= 0), "nonpositive_volume"),
            (~(np.isfinite(op) & np.isfinite(hi) & np.isfinite(lo) & np.isfinite(cl))
                | (op <= 0) | (hi <= 0) | (lo <= 0) | (cl <= 0), "invalid_positive_OHLC"),
            ((lo > np.minimum(op, cl)) | (hi < np.maximum(op, cl)) | (lo > hi), "inconsistent_OHLC")]
        for mask, reason in tests:
            affected = (status == "known_complete_raw_price_path") & mask
            status[affected] = reason; issue_day[affected] = future_day
        lows.append(lo); closes.append(cl)
    valid = eligible & (status == "known_complete_raw_price_path")
    change = np.full(count, np.nan); adverse = np.full(count, np.nan); drawdown = np.full(count, np.nan)
    if np.any(valid):
        if len(lows) != horizon or len(closes) != horizon:
            raise ValueError("Valid outcome cannot omit any exact future session")
        close_path = np.stack(closes)[:, valid]
        start = open_E[valid]
        change[valid] = (close_path[-1] / start - 1) * 100
        adverse[valid] = np.minimum(0, np.min(np.stack(lows)[:, valid], axis=0) / start - 1) * 100
        running_peak = np.maximum.accumulate(np.vstack([start, close_path]), axis=0)[1:]
        drawdown[valid] = np.min(np.minimum(0, close_path / running_peak - 1) * 100, axis=0)
    chosen = np.flatnonzero(eligible)
    status_counts = dict(Counter(status[eligible].tolist()))
    n = int(np.sum(valid)); up = int(np.sum(change[valid] > 0)); down = int(np.sum(change[valid] < 0))
    summary = {"horizon_sessions": horizon, "execution_day": future[0] if future else None,
        "horizon_end_day": future[-1] if len(future) == horizon else None,
        "five_session_primary_origin": primary, "D_eligible_count": len(chosen),
        "known_complete_path_count": n, "unknown_path_count": len(chosen) - n,
        "complete_path_fraction": n / len(chosen) if len(chosen) else None,
        "status_counts": status_counts, "future_ST_observed_count": int(np.sum(future_ST & eligible)),
        "up_count": up, "down_count": down, "tie_count": n - up - down,
        "breadth": (up - down) / n if n else None,
        "loss_fraction": down / n if n else None,
        "mean_endpoint_price_change_pct": float(np.mean(change[valid])) if n else None,
        "median_endpoint_price_change_pct": float(np.median(change[valid])) if n else None,
        "mean_stock_adverse_excursion_pct": float(np.mean(adverse[valid])) if n else None,
        "mean_stock_close_drawdown_pct": float(np.mean(drawdown[valid])) if n else None}
    def nullable(values):
        return [float(item) if np.isfinite(item) else None for item in values[chosen]]
    stock = {"period": [period] * len(chosen), "origin_day": [day] * len(chosen),
        "symbol": [members[index] for index in chosen], "horizon_sessions": [horizon] * len(chosen),
        "execution_day": [summary["execution_day"]] * len(chosen),
        "horizon_end_day": [summary["horizon_end_day"]] * len(chosen),
        "five_session_primary_origin": [primary] * len(chosen), "status": status[chosen].tolist(),
        "first_issue_day": issue_day[chosen].tolist(), "raw_open_E": nullable(open_E),
        "endpoint_price_change_pct": nullable(change), "adverse_excursion_pct": nullable(adverse),
        "close_drawdown_pct": nullable(drawdown), "future_ST_observed": future_ST[chosen].tolist()}
    return summary, stock


SUMMARY_METRICS = ("breadth", "loss_fraction", "mean_endpoint_price_change_pct",
    "median_endpoint_price_change_pct", "mean_stock_adverse_excursion_pct", "mean_stock_close_drawdown_pct",
    "complete_path_fraction")


def summarize_origins(origins, horizon, *, nonoverlap=False, confirmed_only=False):
    groups = []
    selected = [item for item in origins if not nonoverlap or item["five_session_primary_origin"]]
    for phase in (*PHASE_ORDER, "unknown"):
        subset = [item for item in selected if item["phase"] == phase
            and (not confirmed_only or item["two_close_confirmation"] is True)]
        rows = [item["outcomes"][str(horizon)] for item in subset]
        values = {metric: [row[metric] for row in rows if row[metric] is not None] for metric in SUMMARY_METRICS}
        groups.append({"phase": phase, "origin_count": len(subset),
            "origins_with_known_outcomes": sum(row["known_complete_path_count"] > 0 for row in rows),
            "confirmed_origin_count": sum(item["two_close_confirmation"] is True for item in subset),
            "unconfirmed_origin_count": sum(item["two_close_confirmation"] is False for item in subset),
            "mode_counts": dict(Counter(item["mode"] for item in subset)),
            "eligible_stock_origin_observations_not_independent_N": sum(row["D_eligible_count"] for row in rows),
            "known_stock_origin_observations_not_independent_N": sum(row["known_complete_path_count"] for row in rows),
            "unknown_stock_origin_observations": sum(row["unknown_path_count"] for row in rows),
            "outcome_first_issue_counts": dict(sum((Counter(row["status_counts"]) for row in rows), Counter())),
            "origin_mean": {metric: float(np.mean(series)) if series else None for metric, series in values.items()}})
    known = {row["phase"]: row for row in groups}
    primary = {phase: known[phase]["origin_mean"] for phase in PHASE_ORDER}
    enough = all(known[phase]["origins_with_known_outcomes"] > 0 for phase in PHASE_ORDER)
    ordering = {}
    for metric in ("mean_stock_adverse_excursion_pct", "breadth"):
        values = [primary[phase][metric] for phase in PHASE_ORDER]
        ordering[metric] = bool(values[0] >= values[1] >= values[2]) if enough else None
    return {"horizon_sessions": horizon, "nonoverlap_every_five_origins": nonoverlap,
        "two_close_confirmed_only": confirmed_only, "origin_count_before_phase_filter": len(selected),
        "groups": groups, "all_three_phases_observed": enough,
        "confirmed_risk_ordering_descriptive_only": ordering,
        "independence_or_statistical_significance_claimed": False}


def run(registration, out):
    registration, out = Path(registration).resolve(), Path(out).resolve()
    record = _read(registration)
    if (record.get("schema") != METHOD + "_registration" or record.get("method_id") != METHOD
            or record.get("spec") != SPEC or any(record.get(key) is not value for key, value in FLAGS.items())
            or datetime.fromisoformat(record["registered_at"]) > datetime.now(MARKET_TZ)):
        raise ValueError("Exact unchanged earlier diagnostic registration required")
    for code in record["code"]:
        _checked(code)
    if out.exists():
        raise FileExistsError("Diagnostic output already exists; no automatic replay or retry")
    protected = [registration.parent, *[Path(item["manifest"]["path"]).resolve().parent for item in record["periods"]]]
    if any(out == source or out.is_relative_to(source) or source.is_relative_to(out) for source in protected):
        raise ValueError("New external output must not overlap registration or input directories")
    started = _now(); out.mkdir(parents=True, exist_ok=False)
    result = {"schema": METHOD + "_result", "method_id": METHOD, "status": "running",
        "started_at": started, "registration": _pin(registration), "spec": SPEC, "periods": [], **FLAGS}
    _write(out / "result.json", result)
    for ref in record["periods"]:
        print(json.dumps({"period": ref["period"], "stage": "reading_registered_environment_and_raw", "at": _now()}), flush=True)
        manifest = _read(_checked(ref["manifest"]))
        calendar_path = _checked(ref["calendar"])
        calendar = calendar_sessions(_read(calendar_path))
        if (manifest["raw_file"] != ref["raw"]["path"] or manifest["raw_sha256"] != ref["raw"]["sha256"]
                or manifest["panels_file"] != ref["panels"]["path"] or manifest["panels_sha256"] != ref["panels"]["sha256"]):
            raise ValueError("Manifest payload references changed")
        panels = read_environment_projection(ref["panels"]["path"], ref["panels"]["sha256"])
        if sorted(panels) != manifest["source_days"]:
            raise ValueError("Environment projection has different source sessions")
        environment = _environment(panels, calendar, record["registered_at"])
        raw_path = _checked(ref["raw"])
        raw = pd.read_parquet(raw_path, columns=list(COLUMNS))
        if len(raw) != manifest["raw_rows"]:
            raise ValueError("Registered raw row count changed")
        views, members = prepare_raw(raw, manifest["source_days"], manifest["scope_members"])
        del raw
        origins, d_counts = [], Counter()
        days = [day for day in manifest["source_days"] if manifest["start"] <= day <= manifest["end"]]
        required = [day for day in calendar if manifest["start"] <= day <= manifest["end"]]
        if days != required:
            raise ValueError("Origin dates must cover the original exact fixed-calendar decision interval")
        period_dir = out / ref["period"]; period_dir.mkdir()
        stock_path = period_dir / "stock_outcomes.parquet"
        with pq.ParquetWriter(stock_path, STOCK_SCHEMA, compression="zstd") as writer:
            for index, day in enumerate(days):
                eligible, exclusions = origin_eligibility(views, manifest["source_days"].index(day))
                d_counts.update(exclusions)
                env = environment[day]
                item = {"day": day, "phase": env["phase"], "two_close_confirmation": env["two_close_confirmation"],
                    "mode": env["mode"], "reason": env["reason"],
                    "exact_previous_present": env["exact_previous_present"],
                    "previous_session_required": env["previous_session_required"],
                    "original_available_at": panels[day]["available_at"],
                    "source_environment": panels[day]["environment"],
                    "fixed_scope_count": len(members), "D_scope_status_counts": exclusions,
                    "five_session_primary_origin": index % 5 == 0, "outcomes": {}}
                for horizon in SPEC["horizons_sessions"]:
                    summary, stock = outcomes(views, source_days=manifest["source_days"], members=members,
                        calendar=calendar, day=day, eligible=eligible, horizon=horizon, period=ref["period"], primary=index % 5 == 0)
                    item["outcomes"][str(horizon)] = summary
                    if stock["symbol"]:
                        writer.write_table(pa.Table.from_pydict(stock, schema=STOCK_SCHEMA))
                origins.append(item)
        _write(period_dir / "origins.json", origins)
        summaries = []
        for horizon, nonoverlap in ((1, False), (5, True), (5, False)):
            for confirmed in (False, True):
                summaries.append(summarize_origins(origins, horizon, nonoverlap=nonoverlap, confirmed_only=confirmed))
        period = {"period": ref["period"], "manifest": ref["manifest"], "start": manifest["start"],
            "end": manifest["end"], "through": manifest["through"], "source_sessions": len(manifest["source_days"]),
            "origin_days": len(origins), "source_rows": manifest["raw_rows"], "fixed_scope_count": len(members),
            "D_scope_status_counts": dict(d_counts), "phase_counts": dict(Counter(item["phase"] for item in origins)),
            "confirmation_counts": dict(Counter(str(item["two_close_confirmation"]) for item in origins)),
            "scope_basis": ref["scope_basis"], "source_created_at": ref["source_created_at"],
            "environment_denominator_labels": sorted({item["source_environment"].get("denominator_scope", "not_declared") for item in origins}),
            "outcome_scope_differs_from_saved_environment_denominator": True,
            "summary": summaries, "origins_file": str(period_dir / "origins.json"),
            "stock_outcomes_file": str(stock_path), "completed_at": _now(), **FLAGS}
        _write(period_dir / "summary.json", period)
        result["periods"].append(period)
        _write(out / "result.json", result)
        print(json.dumps({"period": ref["period"], "stage": "completed", "origins": len(origins), "at": _now()}), flush=True)
        del views, panels, environment, origins
    result.update(status="completed_conditional_saved_environment_risk_diagnostic", completed_at=_now())
    _write(out / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    registration = commands.add_parser("register")
    registration.add_argument("--input", action="append", required=True, help="explicit label=existing manifest")
    registration.add_argument("--out", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--registration", required=True); execute.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "register":
        inputs = []
        for item in args.input:
            label, separator, path = item.partition("=")
            if not separator:
                parser.error("--input requires label=manifest")
            inputs.append((label, path))
        result = register(inputs, args.out)
        print(json.dumps({"status": "registered_before_price_outcome_read", "registered_at": result["registered_at"],
            "periods": [item["period"] for item in result["periods"]], **FLAGS}))
    else:
        result = run(args.registration, args.out)
        print(json.dumps({"status": result["status"], "completed_at": result["completed_at"], **FLAGS}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
