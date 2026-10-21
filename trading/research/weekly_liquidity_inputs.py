"""Load the saved weekly common pool once, with reported turnover kept separate.

This is an input adapter, not a replay, a new qualification rule or an estimate
of circulating shares.  It never reads raw prices, labels, actions or accounts.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from ..contracts import SignalUnavailable
from ..rules import stock_board
from ..symbols import normalize_symbol
from .bulk_feed import BULK_SCOPE, _load_listing_master, numeric
from .low_risk import LowRiskFeed, portfolio
from .price_boundaries import validate_rename
from . import weekly_reversal as weekly


SCHEMA = "saved_weekly_common_pool_reported_turnover_inputs_v1"
UNIT_EVIDENCE = Path(__file__).with_name("baostock_bulk_extras_units.json")
BASE_COLUMNS = ("date", "code", "close", "volume", "amount", "turn",
                "tradestatus", "isST", "adjustflag")
FLAGS = {"research_only": True, "candidate_panel_only": True,
         "selection_enabled": False, "strategy_validated": False,
         "execution_admitted": False, "engine_advanced": False,
         "account_created": False, "orders_generated": False,
         "future_labels_used_for_score": False, "network_accessed": False,
         "source_snapshots_modified": False,
         "independent_circulating_shares_verified": False,
         "historical_publication_verified": False, "pit_verified": False}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _digest(path):
    value = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": _digest(path)}


def inspect_saved_weekly_inputs(run_json):
    """Bind only saved small metadata; do not open or hash a price panel."""
    run_json = Path(run_json).resolve()
    run = _read(run_json)
    protocol = run.get("protocol", {})
    inputs = run.get("inputs", {})
    expected_signals = list(weekly.NOTICE_VARIANTS)
    if (run.get("status") != "completed_weekly_reversal_diagnostic"
            or run.get("research_family") != "weekly_reversal"
            or run.get("scope") != BULK_SCOPE
            or (run.get("start"), run.get("end")) != (weekly.START, weekly.END)
            or protocol.get("id") != "five_session_reversal_momentum_pair_entry_notices_v3"
            or protocol.get("signals") != expected_signals
            or protocol.get("decision_cadence") != "every_five_sessions_anchored_20240102"
            or protocol.get("execution_admitted") is not False
            or protocol.get("tuning") is not False
            or run.get("strategy_validated") is not False
            or run.get("live_ready") is not False
            or [(row.get("signal"), row.get("status"), row.get("completed_sessions"))
                for row in run.get("results", [])]
            != [(signal, "completed", run.get("sessions")) for signal in expected_signals]):
        raise ValueError("Require the completed fixed saved weekly notice-policy pair")
    config = portfolio(protocol.get("portfolio", {}).get("initial_cash", 0))
    if (protocol.get("portfolio") != config.to_dict()
            or protocol.get("signal_portfolio_configs")
            != {signal: config.to_dict() for signal in expected_signals}
            or protocol.get("bulk_inputs") != inputs):
        raise ValueError("The original common-pool portfolio/source contract changed")

    price = Path(inputs["price_view"]).resolve()
    raw = Path(inputs["raw_view"]).resolve()
    event = Path(inputs["event_view"]).resolve()
    listing = Path(inputs["listing_master"]).resolve()
    price_result = _read(price / "result.json")
    raw_result = _read(raw / "result.json")
    raw_plan = _read(raw / "plan.json")
    calendar_file = event / "source_input_calendar.csv"
    universe_file = event / "source_input_universe.csv"
    calendar_frame = pd.read_csv(calendar_file, dtype=str, keep_default_na=False)
    if calendar_frame.columns.tolist() != ["date"]:
        raise ValueError("The saved weekly calendar must keep its exact date-only schema")
    calendar = calendar_frame.date.tolist()
    if (not calendar or calendar != sorted(set(calendar))
            or calendar != raw_plan.get("days")
            or calendar[-1] != weekly.END or weekly.START not in calendar
            or len([d for d in calendar if weekly.START <= d <= weekly.END]) != run["sessions"]
            or raw_plan.get("kind") != "stock"
            or raw_plan.get("execution_admitted") is not False
            or raw_result.get("status") != "source_cache_complete"
            or raw_result.get("kind") != "stock" or raw_result.get("missing_days") != []
            or raw_result.get("expected_days") != len(calendar)
            or raw_result.get("acquired_days") != len(calendar)
            or raw_result.get("execution_admitted") is not False
            or raw_result.get("plan_sha256") != _digest(raw / "plan.json")
            or raw_result.get("receipts_sha256") != _digest(raw / "receipts.json")
            or price_result.get("status") != "research_reconstruction_completed"
            or price_result.get("execution_admitted") is not False
            or Path(price_result["raw_view"]).resolve() != raw
            or price_result.get("cutoff") != calendar[-1]
            or price_result.get("dates") != len(calendar)
            or price_result.get("canonical_panel_file") != "canonical_daily.parquet"
            or price_result.get("source_hashes", {}).get("raw") != raw_result.get("raw_daily_sha256")):
        raise ValueError("The saved weekly price/raw metadata and exact calendar no longer agree")
    rename = price_result.get("rename")
    if rename is not None:
        validate_rename(rename)
    universe = pd.read_csv(universe_file, dtype=str, keep_default_na=False)
    if ("code" not in universe or universe.code.duplicated().any()
            or not universe.code.str.fullmatch(r"(?:sh|sz)\.[0-9]{6}").all()
            or len(universe) != inputs.get("source_codes")):
        raise ValueError("The original full-source code inventory changed")
    codes = set(universe.code)
    if rename:
        if not {rename["old"], rename["new"]} <= codes:
            raise ValueError("The original reviewed alias lineage is not in the saved source")
        codes.remove(rename["old"])
    code_symbols = {code: normalize_symbol(code).vt_symbol for code in codes}
    codes = sorted(codes, key=code_symbols.get)
    symbols = [code_symbols[code] for code in codes]
    if (len(codes) != inputs.get("canonical_codes")
            or len(codes) != price_result.get("canonical_symbols")
            or len(codes) != run.get("symbols")):
        raise ValueError("Canonical source membership differs from the saved weekly run")
    unsupported = {}
    for symbol in symbols:
        try:
            stock_board(symbol)
        except ValueError as error:
            unsupported[symbol] = str(error)
    if unsupported != inputs.get("unsupported_instruments"):
        raise ValueError("Supported-instrument rules no longer match the saved weekly pair")
    master, master_meta = _load_listing_master(listing)
    if master.code.duplicated().any() or not set(codes) <= set(master.code):
        raise ValueError("The original listing overlay no longer covers the full source")
    units = _read(UNIT_EVIDENCE)
    if (units.get("status") != "official_provider_document_read"
            or units.get("turn_unit") != "percent"):
        raise ValueError("Require the saved official reported-turnover unit evidence")
    refs = {key: _ref(path) for key, path in {
        "saved_run": run_json, "price_result": price / "result.json",
        "raw_result": raw / "result.json", "raw_plan": raw / "plan.json",
        "raw_receipts": raw / "receipts.json", "calendar": calendar_file,
        "universe": universe_file, "listing": master_meta["path"],
        "turn_unit_evidence": UNIT_EVIDENCE}.items()}
    return {"schema": SCHEMA, "metadata_refs": refs,
            "canonical_panel": {"path": str(price / "canonical_daily.parquet"),
                "sha256": price_result["canonical_panel_sha256"]},
            "calendar": calendar, "codes": codes, "symbols": symbols,
            "unsupported_instruments": unsupported, "rename": deepcopy(rename),
            "canonical_rows": price_result["canonical_rows"],
            "config": config.to_dict(), "listing_path": str(listing),
            "scope": BULK_SCOPE, "source_price_basis": "derived_adjustflag2_research_levels_not_total_returns",
            "source_clock": {"price_started_at": price_result.get("started_at"),
                "price_finished_at": price_result.get("finished_at"),
                "saved_weekly_completed_at": run.get("completed_at"),
                "basis": "actual_research_retrieval_and_run_clocks_not_historical_publication"},
            "reported_turn_unit": "percent", "reported_turn_scaled": False,
            "raw_file_read": False, "accounting_loaded": False,
            "limitations": deepcopy(inputs.get("limitations", [])), **FLAGS}


class _DatedOwnRows:
    """Only formation-day fields used by the original LowRiskFeed.pool."""
    def __init__(self, symbols, days, present, volume, status, st):
        self.symbols, self.days = symbols, set(days)
        self.present, self.volume, self.status, self.st = present, volume, status, st

    def get(self, day, default=None):
        if day not in self.days:
            return default
        present = self.present.loc[day].to_numpy()
        return {symbol: {"volume": self.volume.at[day, symbol],
                         "tradestatus": self.status.at[day, symbol],
                         "isST": self.st.at[day, symbol]}
                for symbol, seen in zip(self.symbols, present) if seen}


def load_inputs_from_saved_weekly(run_json, *, verify_panel=True):
    """Read one projected canonical panel and reuse the original pool functions.

    ``verify_panel=False`` is intended only when the caller has just verified
    the same registered bytes; the source result always retains its declared
    SHA.  No source, feature, pool or rank files are written by this function.
    """
    source = inspect_saved_weekly_inputs(run_json)
    path = Path(source["canonical_panel"]["path"])
    signature = (path.stat().st_size, path.stat().st_mtime_ns)
    if verify_panel and _digest(path) != source["canonical_panel"]["sha256"]:
        raise ValueError("The saved canonical price/turnover panel bytes changed")
    calendar, codes, symbols = source["calendar"], source["codes"], source["symbols"]
    with pq.ParquetFile(path) as parquet:
        columns = list(BASE_COLUMNS) + (["alias_shadow"] if source["rename"] else [])
        if not set(columns) <= set(parquet.schema_arrow.names) or parquet.num_row_groups != len(calendar):
            raise ValueError("The canonical original-field schema or exact row groups changed")
        for index, day in enumerate(calendar):
            group = parquet.metadata.row_group(index)
            stat = group.column(parquet.schema_arrow.names.index("date")).statistics
            if group.num_rows and (stat is None or not stat.has_min_max
                    or stat.min != day or stat.max != day or stat.null_count != 0):
                raise ValueError("The original canonical date group no longer matches its saved calendar")
        # This is the only price-data read. All arms share the resulting inputs.
        frame = parquet.read(columns=columns).to_pandas()
    if ((path.stat().st_size, path.stat().st_mtime_ns) != signature
            or len(frame) != source["canonical_rows"]
            or frame.duplicated(["date", "code"]).any()
            or not frame.adjustflag.eq("2").all()
            or (source["rename"] and (not frame.alias_shadow.eq(False).all()
                or frame.code.eq(source["rename"]["old"]).any()))):
        raise ValueError("The canonical source changed or contains duplicate/shadow/nonadjusted rows")
    di, si = pd.Index(calendar).get_indexer(frame.date), pd.Index(codes).get_indexer(frame.code)
    if (di < 0).any() or (si < 0).any():
        raise ValueError("Observed canonical rows are outside the original calendar/member inventory")
    shape = (len(calendar), len(symbols))

    def matrix(values, *, missing=np.nan, dtype=float):
        array = np.full(shape, missing, dtype=dtype)
        array[di, si] = values
        return pd.DataFrame(array, index=calendar, columns=symbols)

    closes = matrix(numeric(frame.close).to_numpy())
    volumes = matrix(numeric(frame.volume).to_numpy())
    amounts = matrix(numeric(frame.amount).to_numpy())
    status = matrix(numeric(frame.tradestatus).to_numpy())
    st = matrix(numeric(frame.isST).to_numpy())
    if ((~status.isin([0, 1]) & status.notna()).any().any()
            or (~st.isin([0, 1]) & st.notna()).any().any()
            or volumes.lt(0).any().any() or amounts.lt(0).any().any()):
        raise ValueError("Invalid original status/volume/amount observations")
    present = matrix(np.ones(len(frame), dtype=bool), missing=False, dtype=bool)
    turnover_raw = matrix(frame.turn.to_numpy(), missing=pd.NA, dtype=object)
    values = pd.to_numeric(frame.turn, errors="coerce")
    known = np.isfinite(values) & values.ge(0)
    reported_turnover = matrix(values.where(known).to_numpy()).astype("Float64")
    observed = np.full(len(frame), "invalid_reported_turn", dtype=object)
    observed[frame.turn.eq("").to_numpy()] = "blank_unknown"
    observed[(np.isfinite(values) & values.lt(0)).to_numpy()] = "negative_unknown"
    observed[known.to_numpy()] = "known_reported_percent"
    turnover_observation = matrix(observed, missing="missing_row_unknown", dtype=object)
    turnover_active = reported_turnover.where(status.eq(1) & volumes.gt(0))
    master, _ = _load_listing_master(source["listing_path"])
    master = master.set_index("code")
    decisions = weekly.decision_days(calendar)
    base = SimpleNamespace(calendar=list(calendar), day_index={d: i for i, d in enumerate(calendar)},
        symbols=list(symbols), codes=np.asarray(codes), closes=closes, volumes=volumes, amounts=amounts,
        unsupported_instruments=deepcopy(source["unsupported_instruments"]),
        instruments={symbol: master.loc[code].to_dict() for code, symbol in zip(codes, symbols)},
        manifest={"selection_date": "2023-01-01", "scope": BULK_SCOPE,
                  "selection_is_point_in_time": False, "execution_admitted": False})
    base.rows = _DatedOwnRows(symbols, decisions, present, volumes, status, st)
    features = weekly.features(base)
    config = portfolio(source["config"]["initial_cash"])
    pool_feed = LowRiskFeed(base, features, "momentum_12_1_20", weekly.END, diagnostic_extension=True)
    pools = {day: pool_feed.pool(day, config) for day in decisions}
    return {"source": source, "base": base, "config": config, "decision_days": decisions,
            "features": features, "pools": pools, "reported_turnover": reported_turnover,
            "turnover_active": turnover_active, "turnover_raw": turnover_raw,
            "turnover_observation": turnover_observation, "trade_status": status, "isST": st,
            "row_present": present,
            "load_observation": {"canonical_data_reads": 1, "panel_sha256_verified": bool(verify_panel),
                "raw_data_reads": 0, "formation_days": len(decisions),
                "unavailable_days": {d: p.reason for d, p in pools.items() if isinstance(p, SignalUnavailable)},
                "turn_unit": "percent", "reported_turn_scaled": False, **FLAGS}}
