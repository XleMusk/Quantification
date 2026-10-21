"""Post-account, sealed-score monthly price diagnostics; never an account repair."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from ..contracts import MARKET_TZ
from ..symbols import normalize_symbol
from .baostock_daily_slice import digest
from .factors import daily_ic
from .monthly_timing import month_ends
from .price_boundaries import canonicalize


METHOD = "sealed_market_residual_monthly_open_to_next_rebalance_price_diagnostic_v1"
ARMS = ("raw", "standardized", "residual_std")
FLAGS = dict(research_only=True, candidate_panel_only=True, execution_admitted=False,
    selection_enabled=False, strategy_validated=False, account_backtest=False,
    engine_advanced=False, ledger_mutated=False, scores_recomputed=False,
    returns_reportable=False, independent_holdout_claimed=False,
    historical_signal_publication_verified=False, network_accessed=False)
SPEC = dict(id=METHOD, min_ic_pairs=100, top_n=20,
    entry="exact_next_source_session_open_after_original_D",
    exit="exact_first_source_session_after_next_confirmed_month_end",
    cutoff="original_source_calendar_only_missing_F_right_censored_no_extension",
    price="raw_open_times_same_D_canonical_adjusted_close_divided_by_raw_close",
    active="raw_and_adjusted_status1_positive_finite_volume_and_prices",
    identity="original_reviewed_canonicalize_and_normalize_symbol",
    common_ic="same_finite_label_cross_section_for_all_three_original_finite_scores",
    basket="original_D_top20_before_labels_momentum_desc_symbol_asc_no_refill",
    summaries="date_equal_weight_all_2025_2026_partial_no_significance_test",
    registration_timing="supplement_after_account_failures_not_prospective",
    price_return_not_cash_total_return=True, fees_or_execution_evaluated=False, **FLAGS)


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _pin(path):
    path = Path(path).resolve()
    return dict(path=str(path), sha256=digest(path))


def _check(ref):
    if _pin(ref["path"]) != ref:
        raise ValueError("Registered saved input changed: " + ref["path"])


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(member) for key, member in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(member) for member in value]
    if isinstance(value, np.generic):
        return _clean(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _save(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(_clean(value), handle, ensure_ascii=False, indent=2, allow_nan=False)


def monthly_schedule(days, calendar):
    if calendar != sorted(set(calendar)) or days != sorted(set(days)) or not set(days) <= set(calendar):
        raise ValueError("Original unique chronological D/source calendar required")
    confirmed = month_ends(calendar, calendar[-1])
    result = {}
    for day in days:
        if day not in confirmed:
            raise ValueError("Only original confirmed month-end D may be evaluated")
        entry = calendar[calendar.index(day)+1]
        next_month_end = next((date for date in confirmed if date > day), None)
        exit_day = calendar[calendar.index(next_month_end)+1] if next_month_end is not None else None
        result[day] = dict(entry_day=entry, next_rebalance_day=next_month_end,
            exit_day=exit_day, right_censored=exit_day is None)
    return result


def register(source_run, out):
    directory, out = Path(source_run).resolve(), Path(out).resolve()
    result = _read(directory/"result.json")
    old_registration = Path(result["registration"]).resolve()
    original = _read(old_registration)
    if (result.get("schema") != "market_model_504d_residual_momentum_matched_three_arm_v1"
            or result.get("status") != "completed_fixed_three_arm_research"
            or digest(old_registration) != result["registration_sha256"]):
        raise ValueError("Use the exact completed three-arm command, preserving its partial accounts")
    publication = _read(directory/"score_publication.json")
    refs = {"source_result": _pin(directory/"result.json"), "original_registration": _pin(old_registration),
        "score_publication": _pin(directory/"score_publication.json")}
    for name in ("scores.parquet", "batches.json"):
        ref = publication["files"][name]
        if Path(ref["path"]).resolve() != directory/name:
            raise ValueError("Sealed score payload is outside its original run")
        _check(ref)
        refs[name] = ref
    inputs = original["inputs"]
    for kind in ("price_view", "raw_view"):
        refs[kind+"_result"] = _pin(Path(inputs[kind])/"result.json")
    calendar_file = Path(inputs["event_view"])/"source_input_calendar.csv"
    refs["source_calendar"] = _pin(calendar_file)
    calendar = pd.read_csv(calendar_file, dtype=str).date.tolist()
    price, raw = (_read(refs[kind+"_result"]["path"]) for kind in ("price_view", "raw_view"))
    if (calendar[-1] != original["end"] or calendar != sorted(set(calendar))
            or Path(price["raw_view"]).resolve() != Path(inputs["raw_view"]).resolve()):
        raise ValueError("Original raw/adjusted source calendar or pair conflicts")
    panels = dict(adjusted=dict(path=str(Path(inputs["price_view"])/"canonical_daily.parquet"),
        declared_sha256=price["canonical_panel_sha256"]),
        raw=dict(path=str(Path(inputs["raw_view"])/raw["panel_file"]), declared_sha256=raw["panel_sha256"]))
    protected = [directory, old_registration.parent, *[Path(ref["path"]).parent for ref in refs.values()],
        *[Path(ref["path"]).parent for ref in panels.values()]]
    if out.exists():
        raise FileExistsError("Use a new exclusive outcome directory")
    if any(out == path or out in path.parents or path in out.parents for path in protected):
        raise ValueError("Outcome output cannot overlap an original input or account")
    implementations = [_pin(__file__), _pin(Path(__file__).with_name("factors.py")),
        _pin(Path(__file__).with_name("price_boundaries.py")), _pin(Path(__file__).with_name("monthly_timing.py")),
        _pin(Path(__file__).parents[1]/"symbols.py")]
    record = dict(schema=METHOD+"_registration", registered_at=_now(), spec=SPEC,
        source_run=str(directory), refs=refs, price_panels=panels, implementations=implementations,
        calendar=calendar, days=original["decision_days"], rename=price.get("rename"),
        supplement_after_account_failures=True, original_score_published_at=publication["published_at"],
        original_formation_rows=result["formation_rows"], original_accounts_modified=False,
        large_price_payload_hash_recomputed=False, **FLAGS)
    monthly_schedule(record["days"], calendar)
    out.mkdir(parents=True, exist_ok=False)
    _save(out/"registration.json", record)
    return record


def _positive(value):
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) and number > 0 else None


def endpoint_opens(calendar, wanted, panels, rename):
    """Read only union(E,F) rowgroups once each, with original alias handling."""
    indices = {day: index for index, day in enumerate(calendar)}
    readers = {kind: pq.ParquetFile(ref["path"]) for kind, ref in panels.items()}
    rows, known_dates = {}, []
    try:
        if any(reader.metadata.num_row_groups != len(calendar) for reader in readers.values()):
            raise ValueError("Original price rowgroup calendar changed")
        for day, symbols in sorted(wanted.items()):
            group = indices[day]
            for reader in readers.values():
                stats = reader.metadata.row_group(group).column(reader.schema_arrow.names.index("date")).statistics
                if stats is None or stats.min != day or stats.max != day or stats.null_count:
                    raise ValueError("Endpoint rowgroup is not its exact source date")
            raw = readers["raw"].read_row_group(group, columns=BAOSTOCK_FIELDS.split(",")).to_pandas()
            adjusted = readers["adjusted"].read_row_group(group,
                columns=["date", "code", "adjustflag", "close", "volume", "tradestatus"]).to_pandas()
            if (not raw.date.eq(day).all() or raw.code.duplicated().any() or not raw.adjustflag.eq("3").all()
                    or not adjusted.date.eq(day).all() or adjusted.code.duplicated().any()
                    or not adjusted.adjustflag.eq("2").all()):
                raise ValueError("Original raw/adjusted endpoint identity or price basis conflicts")
            if rename:
                _, raw = canonicalize(raw, raw, rename)
            raw["symbol"] = raw.code.map(lambda code: normalize_symbol(code).vt_symbol)
            adjusted["symbol"] = adjusted.code.map(lambda code: normalize_symbol(code).vt_symbol)
            if raw.symbol.duplicated().any() or adjusted.symbol.duplicated().any():
                raise ValueError("Canonical endpoint has duplicate security identities")
            raw = raw.set_index("symbol").to_dict("index")
            adjusted = adjusted.set_index("symbol").to_dict("index")
            for symbol in symbols:
                original, own = raw.get(symbol), adjusted.get(symbol)
                reason, opening = None, None
                if original is None or own is None:
                    reason = "missing_exact_raw_or_adjusted_endpoint"
                elif str(original["tradestatus"]) != "1" or str(own["tradestatus"]) != "1":
                    reason = "inactive_or_unknown_endpoint"
                elif any(_positive(row.get(key)) is None for row, key in (
                        (original,"open"), (original,"close"), (original,"volume"),
                        (own,"close"), (own,"volume"))):
                    reason = "nonpositive_or_nonfinite_endpoint_price_or_volume"
                else:
                    opening = float(original["open"])*float(own["close"])/float(original["close"])
                    if _positive(opening) is None:
                        opening, reason = None, "adjusted_open_proxy_invalid"
                rows[(day, symbol)] = (opening, reason)
            known_dates.append(day)
    finally:
        for reader in readers.values():
            reader.close()
    return rows, dict(endpoint_dates_read=known_dates, endpoint_rowgroups_each=len(known_dates),
        symbol_endpoint_observations=len(rows), large_matrix_loaded=False)


def run(registration_path):
    registration_path = Path(registration_path).resolve()
    record, out = _read(registration_path), registration_path.parent
    if record.get("schema") != METHOD+"_registration" or record["spec"] != SPEC:
        raise ValueError("Use the exact registered post-account outcome question")
    for ref in [*record["refs"].values(), *record["implementations"]]:
        _check(ref)
    if (out/"result.json").exists() or (out/"event_observations.parquet").exists():
        raise FileExistsError("Do not repeat a saved outcome calculation")
    started = _now()
    scores = pd.read_parquet(record["refs"]["scores.parquet"]["path"])
    batches = _read(record["refs"]["batches.json"]["path"])
    if (len(scores) != record["original_formation_rows"] or scores.duplicated(["date", "symbol"]).any()
            or not set(scores.date) <= set(record["days"]) or not np.isfinite(scores[list(ARMS)]).all().all()):
        raise ValueError("Original common-score identities or finite score contract changed")
    schedule, selected, wanted = monthly_schedule(record["days"], record["calendar"]), [], {}
    for day in record["days"]:
        own = scores.loc[scores.date.eq(day)]
        batch = batches[day]
        if batch["status"] != "complete" or len(own) != batch["common_pool"]:
            raise ValueError("The original complete common formation pool differs")
        for arm in ARMS:
            ranked = own.sort_values([arm, "symbol"], ascending=[False, True], kind="stable")
            if ranked.symbol.head(len(batch["targets"][arm])).tolist() != [row["symbol"] for row in batch["targets"][arm]]:
                raise ValueError("Saved ranks no longer match the original sealed top30")
            selected.extend(dict(date=day, arm=arm, rank=index, symbol=row.symbol)
                for index, row in enumerate(ranked.head(20).itertuples(), 1))
        for endpoint in (schedule[day]["entry_day"], schedule[day]["exit_day"]):
            if endpoint is not None:
                wanted.setdefault(endpoint, set()).update(own.symbol)
    selected = pd.DataFrame(selected)
    selected.to_csv(out/"original_top20.csv", index=False)
    _save(out/"selection_saved.json", dict(saved_at=_now(), labels_not_yet_read=True,
        payload=_pin(out/"original_top20.csv"), original_score_ref=record["refs"]["scores.parquet"]))
    signatures = {kind: (Path(ref["path"]).stat().st_size, Path(ref["path"]).stat().st_mtime_ns)
        for kind, ref in record["price_panels"].items()}
    opens, source_read = endpoint_opens(record["calendar"], wanted, record["price_panels"], record["rename"])
    observations = []
    for row in scores.to_dict("records"):
        plan = schedule[row["date"]]
        entry, entry_reason = opens.get((plan["entry_day"],row["symbol"]), (None,"missing_entry"))
        exit_price, exit_reason = opens.get((plan["exit_day"],row["symbol"]), (None,"right_censored_source_cutoff"))
        label = exit_price/entry-1 if entry is not None and exit_price is not None else np.nan
        if not np.isfinite(label): label = np.nan
        observations.append(dict(**row, **plan, entry_adjusted_open_proxy=entry,
            exit_adjusted_open_proxy=exit_price, entry_reason=entry_reason, exit_reason=exit_reason,
            monthly_price_return=label))
    observations = pd.DataFrame(observations)
    indexed = observations.set_index(["date","symbol"])
    ic = daily_ic(indexed[list(ARMS)], indexed.monthly_price_return, min_pairs=100)
    per_day = []
    for day in record["days"]:
        own = observations.loc[observations.date.eq(day)].set_index("symbol")
        for arm in ARMS:
            top = selected.loc[selected.date.eq(day) & selected.arm.eq(arm)]
            labels = own.monthly_price_return.reindex(top.symbol)
            known = int(labels.notna().sum())
            complete = len(top) == 20 and known == 20
            per_day.append(dict(date=day, arm=arm, **schedule[day], common_pool=len(own),
                known=int(own.monthly_price_return.notna().sum()), unknown=int(own.monthly_price_return.isna().sum()),
                rank_ic=ic.at[day,arm] if day in ic.index else None, top20_members=top.symbol.tolist(),
                top20_known=known, top20_unknown=len(top)-known, top20_complete=complete,
                conditional_basket_price_return=float(labels.mean()) if known else None,
                complete_basket_price_return=float(labels.mean()) if complete else None))
    table = pd.DataFrame(per_day)
    summaries, paired = [], []
    for scope in ("all", "2025", "2026"):
        scoped = table if scope == "all" else table.loc[table.date.str.startswith(scope)]
        for arm in ARMS:
            own = scoped.loc[scoped.arm.eq(arm)]
            summaries.append(dict(scope=scope, arm=arm, registered_dates=len(own),
                valid_ic_dates=int(own.rank_ic.notna().sum()), mean_rank_ic=own.rank_ic.mean(),
                top20_complete_dates=int(own.top20_complete.sum()),
                mean_complete_basket_price_return=own.complete_basket_price_return.mean(),
                mean_conditional_basket_price_return=own.conditional_basket_price_return.mean(),
                conditional_basket_dates=int(own.conditional_basket_price_return.notna().sum()),
                known=int(own.known.sum()), unknown=int(own.unknown.sum())))
        for candidate, control in (("standardized","raw"),("residual_std","raw"),("residual_std","standardized")):
            a = scoped.loc[scoped.arm.eq(candidate)].set_index("date").rank_ic
            b = scoped.loc[scoped.arm.eq(control)].set_index("date").rank_ic
            delta = (a-b).dropna()
            paired.append(dict(scope=scope, candidate=candidate, control=control, valid_dates=len(delta), mean_delta_rank_ic=delta.mean()))
    for kind, ref in record["price_panels"].items():
        stat = Path(ref["path"]).stat()
        if signatures[kind] != (stat.st_size,stat.st_mtime_ns):
            raise ValueError("Original endpoint source changed during the bounded read")
    observations.to_parquet(out/"event_observations.parquet", index=False)
    table.to_csv(out/"per_day.csv", index=False)
    result = dict(schema=METHOD+"_result", status="completed_saved_monthly_price_diagnostic",
        started_at=started, completed_at=_now(), registration=_pin(registration_path), spec=SPEC,
        supplement_after_account_failures=True, formation_dates=len(record["days"]),
        finite_horizon_dates=sum(plan["exit_day"] is not None for plan in schedule.values()),
        common_stock_month_rows=len(observations), known_price_labels=int(observations.monthly_price_return.notna().sum()),
        unknown_price_labels=int(observations.monthly_price_return.isna().sum()),
        source_read=source_read, summaries=summaries, paired_IC_differences=paired, per_day=per_day,
        observation_units="correlated_stock_month_panels_not81829_independent_trials",
        account_returns=None, original_accounts_modified=False, **FLAGS)
    _save(out/"result.json", result)
    return _clean(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    register(args.source_run, args.out)
    result = run(Path(args.out)/"registration.json")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
