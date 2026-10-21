"""Explicit common20 raw tail addition for an already frozen research bundle.

Only missing 2026-10-12 observations may be added. Existing source rows and
all signal objects remain untouched; overlapping provider quotes are evidence
for agreement/precision differences, never replacements for StockDB fields.
"""

from hashlib import sha256
import json
import math
from pathlib import Path

import pandas as pd

from .baostock_daily_slice import SCHEMA as SOURCE_SCHEMA, FIELDS, cached, digest, scope
from .daily_source_probe import WHEEL_SHA256
from .leader_daily_signals import RAW_COLUMNS, _physical_frame


SCHEMA = "leader_common20_research_tail_overlay_v1"
DAYS = ["2026-10-09", "2026-10-12", "2026-10-13"]
ADD_DAY = "2026-10-12"
OHLC = ["open", "high", "low", "close"]
OHLC_TOLERANCE = .0001


def load_tail_source(package, symbols):
    """Read only a terminal, hash-consistent exact20-by3 raw source package."""
    package = Path(package).resolve()
    plan = json.loads((package / "plan.json").read_text(encoding="utf-8"))
    receipt = json.loads((package / "receipt.json").read_text(encoding="utf-8"))
    if (plan.get("schema") != SOURCE_SCHEMA or receipt.get("schema") != SOURCE_SCHEMA
            or receipt.get("status") != "complete_raw_slice_not_admitted"
            or receipt.get("failures") != [] or receipt.get("in_flight") is not None
            or receipt.get("pending_symbols") != []
            or plan.get("symbols") != list(symbols) or len(symbols) != 20 or plan.get("days") != DAYS
            or plan.get("fields") != FIELDS or plan.get("frequency") != "d" or plan.get("adjustflag") != "3"
            or plan.get("sdk_sha256") != WHEEL_SHA256 or receipt.get("sdk", {}).get("sha256") != WHEEL_SHA256
            or receipt.get("complete_rows") != 60 or set(receipt.get("completed", {})) != set(symbols)
            or any(value.get(flag) is not False for value in (plan, receipt)
                   for flag in ("execution_admitted", "stockdb_modified", "ledger_mutated"))):
        raise ValueError("Tail source package is not the completed exact common20 raw three-session slice")
    calendar = scope(list(symbols), DAYS, plan["calendar"])
    if calendar["calendar_sha256"] != plan["calendar_sha256"]:
        raise ValueError("Tail source calendar hash changed")
    source_scope = plan.get("source_scope")
    if (not isinstance(source_scope, dict) or source_scope.get("pool") != "common_pre_window_symbol20"
            or digest(source_scope["cohort"]) != source_scope["sha256"]):
        raise ValueError("Tail source needs the pinned common20 cohort identity")
    pinned_cohort = json.loads(Path(source_scope["cohort"]).read_text(encoding="utf-8"))
    if (pinned_cohort.get("pools", {}).get("common_pre_window_symbol20") != list(symbols)
            or pinned_cohort.get("anchor") != source_scope.get("anchor")):
        raise ValueError("Tail source symbols/anchor differ from their pinned cohort")
    frames = []
    for symbol in symbols:
        entry = receipt["completed"][symbol]
        if entry.get("symbol") != symbol or entry.get("provider_error_code") != "0" or entry.get("quality", {}).get("issues") != []:
            raise ValueError("Tail source per-symbol receipt is not a successful valid provider result")
        frames.append(cached(package, entry, symbol, DAYS))
    quotes = pd.concat(frames, ignore_index=True)
    aggregate = package / "raw_daily.csv"
    if digest(aggregate) != receipt.get("raw_daily_sha256"):
        raise ValueError("Tail source aggregate raw hash changed")
    raw_aggregate = pd.read_csv(aggregate, dtype=str, keep_default_na=False)
    if not quotes.equals(raw_aggregate):
        raise ValueError("Tail source aggregate differs from exact per-code source values")
    return quotes, {"package": str(package), "plan_sha256": digest(package / "plan.json"),
                    "receipt_sha256": digest(package / "receipt.json"), "raw_daily_sha256": digest(aggregate),
                    "calendar": calendar, "source_scope": source_scope,
                    "source_unit_check": "provider raw shares/RMB; amount/volume within source OHLC; not exchange-wide unit proof"}


def _rows_digest(rows):
    fields = ["date", "symbol", *OHLC, "volume", "amount", "pct_chg", "preclose_for_limits", "is_st"]
    return sha256(rows.sort_values(["date", "symbol"])[fields].to_csv(index=False, float_format="%.15g").encode()).hexdigest()


def _number(value):
    value = float(value)
    return value if math.isfinite(value) else None


def apply_tail_overlay(bundle, package):
    """Return a new bundle plus provenance; only raw_bars gains missing tail rows."""
    from .leader_daily_replay import END, START, POOLS, freeze_cohorts
    if END != DAYS[0] or ADD_DAY <= END or not set(DAYS[1:]).issubset(bundle["calendar"]):
        raise ValueError("Tail overlay requires the unchanged decision cutoff and declared following exchange sessions")
    raw = bundle["raw_bars"]
    if raw.duplicated(["date", "symbol"]).any():
        raise ValueError("Base raw source has duplicate observations")
    cohort = freeze_cohorts(raw, START)
    symbols = cohort["pools"][POOLS[0]]
    quotes, source = load_tail_source(package, symbols)
    if source["source_scope"]["anchor"] != cohort["anchor"]:
        raise ValueError("Tail cohort anchor differs from frozen base source")
    physical = quotes.rename(columns={"pctChg": "pct_chg", "isST": "is_st", "preclose": "pre_close"}).copy()
    physical["code"] = physical["code"].str[3:]
    physical["pre_close"] = pd.to_numeric(physical["pre_close"], errors="coerce")
    source_close = pd.to_numeric(physical["close"])
    source_preclose = pd.to_numeric(physical["pre_close"])
    if ((source_close / source_preclose - 1) * 100 - pd.to_numeric(physical["pct_chg"], errors="coerce")).abs().gt(.0001).any():
        raise ValueError("Tail source percentage-change units disagree with its raw close/preclose")
    canonical = _physical_frame(physical.loc[:, RAW_COLUMNS], DAYS[-1])
    if len(canonical) != 60:
        raise ValueError("Tail source does not preserve exact20-by3 inventory")
    provider_index = quotes.assign(symbol=quotes.code.str[3:] + quotes.code.str[:2].map({"sh": ".SSE", "sz": ".SZSE"})).set_index(["date", "symbol"])
    source_index = canonical.set_index(["date", "symbol"])
    base_index = raw.loc[raw.date.isin(DAYS) & raw.symbol.isin(symbols)].set_index(["date", "symbol"])
    overlap, added, unusable, absent_nullable_overlap = [], [], [], []
    for (day, symbol), other in source_index.iterrows():
        provider_row = provider_index.loc[(day, symbol)]
        missing_activity = [field for field in ("volume", "amount", "pctChg") if provider_row[field] == ""]
        if not other.source_row_valid:
            if provider_row.tradestatus != "0" or not missing_activity:
                raise ValueError(f"Tail source contains an invalid execution bar outside explicit nullable suspension: {day} {symbol}")
            unusable.append({"day": day, "symbol": symbol, "reason": "unusable_raw_activity",
                             "source_missing_fields": missing_activity, "tradestatus": provider_row.tradestatus,
                             "isST": provider_row.isST, "raw_values_preserved": True, "zero_filled": False})
        if (day, symbol) not in base_index.index:
            if day != ADD_DAY:
                if other.source_row_valid:
                    raise ValueError("Tail source lacks a base overlap witness on Oct9/13; no historical repair admitted")
                # This is a retained nullable suspension observation, not an
                # executable quote and not permission to repair history.
                absent_nullable_overlap.append({"day": day, "symbol": symbol, "base_raw_row_observed": False,
                                                "source_execution_bar_usable": False, "row_added": False})
                continue
            if other.source_row_valid:
                added.append((day, symbol))
            continue
        original = base_index.loc[(day, symbol)]
        deltas = {field: float(other[field] - original[field]) for field in OHLC}
        if any(abs(value) > OHLC_TOLERANCE + 1e-12 for value in deltas.values()):
            raise ValueError(f"Overlapping raw OHLC disagree for {day} {symbol}; replacement is forbidden")
        source_volume, base_volume = _number(other.volume), _number(original.volume)
        source_amount, base_amount = _number(other.amount), _number(original.amount)
        source_pct, base_pct = _number(other.pct_chg), _number(original.pct_chg)
        volume_delta = source_volume - base_volume if source_volume is not None and base_volume is not None else None
        amount_delta = source_amount - base_amount if source_amount is not None and base_amount is not None else None
        # A hundred-share/0.1% reporting allowance is explicit and retained.
        # Large discrepancies must not be silently normalised as lot units.
        if volume_delta is not None and abs(volume_delta) > max(100., abs(base_volume) * .001):
            raise ValueError(f"Overlapping share volume differs beyond the declared precision allowance: {day} {symbol}")
        if amount_delta is not None and abs(amount_delta) > max(1., abs(base_amount) * .001):
            raise ValueError(f"Overlapping RMB turnover differs beyond the declared precision allowance: {day} {symbol}")
        overlap.append({"day": day, "symbol": symbol, "ohlc_deltas": deltas,
                        "base_volume_shares": base_volume, "provider_volume_shares": source_volume,
                        "volume_delta_shares": volume_delta,
                        "volume_relative_delta": volume_delta / abs(base_volume) if volume_delta is not None and base_volume else None,
                        "base_amount_rmb": base_amount, "provider_amount_rmb": source_amount,
                        "amount_delta_rmb": amount_delta, "base_pct_chg": base_pct,
                        "provider_pct_chg": source_pct,
                        "pct_chg_delta_percentage_points": source_pct - base_pct if source_pct is not None and base_pct is not None else None,
                        "source_execution_bar_usable": bool(other.source_row_valid),
                        "existing_row_replaced": False})
    rows = canonical.set_index(["date", "symbol"]).loc[added].reset_index() if added else canonical.iloc[:0]
    # Canonical producer columns are the sole price surface. No signal is
    # recomputed, and all pre-existing rows retain their original values.
    if set(raw.columns) - set(rows.columns):
        raise ValueError("Tail canonical raw schema cannot preserve the existing producer surface")
    rows = rows.loc[:, raw.columns].copy()
    for field in rows:
        # Excluding a nullable row can leave an integral source volume in a
        # float column. Cast only the new rows, and reject any loss of value,
        # so concatenation cannot silently change historical column types.
        cast = rows[field].astype(raw[field].dtype)
        if not (cast.eq(rows[field]) | (cast.isna() & rows[field].isna())).all():
            raise ValueError(f"Tail source cannot retain field precision in the existing raw schema: {field}")
        rows[field] = cast
    result = dict(bundle)
    result["raw_bars"] = pd.concat([raw, rows], ignore_index=True)
    missing_full_tail = sorted(set(cohort["pools"][POOLS[1]]) - set(result["raw_bars"].loc[result["raw_bars"].date.eq(ADD_DAY), "symbol"]))
    metadata = {"schema": SCHEMA, "status": "complete_common20_tail_overlay_not_admitted",
                "price_source_mode": "mixed_source_research_tail_overlay", "base_snapshot": bundle.get("source_metadata", {}).get("snapshot"),
                "decision_end": END, "added_day": ADD_DAY, "declared_tail_sessions": DAYS[1:],
                "symbols": symbols, "source": source, "added_rows": len(rows), "added_symbols": sorted(rows.symbol.tolist()),
                "source_inventory_rows": len(canonical), "source_execution_bar_rows": int(canonical.source_row_valid.sum()),
                "unusable_source_rows": unusable, "skipped_unusable_tail_rows": [row for row in unusable if row["day"] == ADD_DAY],
                "absent_nullable_overlap_rows": absent_nullable_overlap,
                "added_rows_sha256": _rows_digest(rows), "base_rows": len(raw), "result_rows": len(result["raw_bars"]),
                "overlap_comparisons": overlap, "overlap_price_tolerance": OHLC_TOLERANCE,
                "volume_precision_allowance": "max(100 shares,0.1% original volume), retained rather than rounded",
                "amount_precision_allowance": "max(1 RMB,0.1% original turnover), retained rather than rounded",
                "existing_rows_replaced": 0, "decision_window_rows_modified": 0, "signals_recomputed": False,
                "batches_unchanged": result["batches"] is bundle["batches"],
                "candidate_table_unchanged": result["candidate_table"] is bundle["candidate_table"],
                "full_cohort_tail_missing_count": len(missing_full_tail), "full_cohort_repair_claimed": False,
                "stockdb_modified": False, "persistent_ledger_mutated": False, "pit_verified": False,
                "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
                "limitations": ["Only usable fixed common20 Oct12 missing raw rows were added to a separate research view; nullable source0 activity is never zero-filled.",
                                "Other full-cohort tail gaps and historical status/qualification evidence are not repaired.",
                                "Provider dated status/isST and overlap agreement do not prove PIT or company-action completeness."]}
    validate_tail_metadata(result, metadata)
    return result, metadata


def validate_tail_metadata(bundle, metadata):
    """Check explicit replay provenance against the added raw tail surface."""
    if (metadata.get("schema") != SCHEMA or metadata.get("status") != "complete_common20_tail_overlay_not_admitted"
            or metadata.get("added_day") != ADD_DAY or metadata.get("decision_end") != DAYS[0]
            or metadata.get("existing_rows_replaced") != 0 or metadata.get("decision_window_rows_modified") != 0
            or metadata.get("signals_recomputed") is not False or metadata.get("batches_unchanged") is not True
            or metadata.get("candidate_table_unchanged") is not True or metadata.get("execution_admitted") is not False):
        raise ValueError("Replay tail metadata does not describe the unchanged-signal research overlay")
    raw = bundle["raw_bars"]
    added = raw.loc[raw.date.eq(ADD_DAY) & raw.symbol.isin(metadata["added_symbols"])]
    if (len(raw) != metadata["result_rows"] or len(added) != metadata["added_rows"]
            or metadata["result_rows"] - metadata["base_rows"] != metadata["added_rows"]
            or _rows_digest(added) != metadata["added_rows_sha256"]):
        raise ValueError("Replay tail metadata does not match the actual added raw rows")
