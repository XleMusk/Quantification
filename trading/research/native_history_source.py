"""Read the existing base BaoStock raw view, without rebuilding its history.

Raw observations are not a signal-price policy, a historical dated roster or
proof of corporate-action continuity. Adjusted research streams stay separate.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import csv
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import re

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .baostock_native_history import RAW_FIELDS

FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False, "strategy_validated": False,
    "model_refit": False, "future_labels_used_for_score": False,
    "provider_contacted": False, "network_accessed": False, "orders_generated": False,
    "ledger_mutated": False, "account_accessed": False, "engine_advanced": False,
    "pit_verified": False, "corporate_actions_verified": False,
    "historical_dated_roster_verified": False, "historical_publication_verified": False,
    "source_snapshots_modified": False, "raw_prices_imputed": False,
    "status_imputed": False, "alias_projection_applied": False,
    "adjusted_prices_generated": False, "raw_only_signals_preferred": False}


def _now():
    return datetime.now().astimezone()


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("An exact canonical source session is required")
    return value


def _stamp(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise ValueError("Source knowledge clocks need an explicit timezone")
    return stamp


def _stat(path):
    value = path.stat()
    return value.st_size, value.st_mtime_ns


def _sha(path):
    before = _stat(path)
    value = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            value.update(block)
    if _stat(path) != before:
        raise ValueError(f"Source changed during its one-time hash verification: {path}")
    return value.hexdigest()


def _symbols(values, *, required=False):
    if values is None:
        if required:
            raise ValueError("History requires explicitly supplied original-code symbols")
        return None
    if not isinstance(values, (list, tuple)) or (required and not values):
        raise ValueError("Explicit symbols must be a list or tuple, never an inferred latest universe")
    if len(values) != len(set(values)) or any(not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}\.(?:SSE|SZSE)", symbol) for symbol in values):
        raise ValueError("Native symbols must be unique original SH/SZ provider codes without alias projection")
    return list(values)


class NativeHistorySource:
    """One pinned base view, a maximum two-day cache, and no data writer."""
    def __init__(self, view):
        self.folder = Path(view).resolve()
        if (self.folder / "INVALID.json").exists():
            raise ValueError("An invalidated source view cannot be consumed")
        result_path, plan_path, receipt_path = (self.folder / name for name in ("result.json", "plan.json", "receipts.json"))
        result = json.loads(result_path.read_text(encoding="utf-8"))
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if ("base_view" in plan or "extension_view" in plan or "base_result" in receipt
                or result.get("old_prefix_preserved") is True):
            raise ValueError("Only the original completed base view is supported; extended lineage must not be guessed")
        if (result.get("status") != "source_cache_complete" or result.get("kind") != "stock"
                or result.get("execution_admitted") is not False or result.get("strategy_validated") is not False
                or result.get("panel_file") != "raw_daily.parquet" or result.get("missing_days") != []
                or plan.get("kind") != "stock" or plan.get("execution_admitted") is not False
                or plan.get("historical_universe_independently_verified") is not False
                or receipt.get("status") != "source_cache_complete" or receipt.get("execution_admitted") is not False):
            raise ValueError("An unchanged unadmitted completed base raw-stock view is required")
        raw_path = self.folder / "raw_daily.parquet"
        calendar_path = Path(plan["calendar_source"]).resolve()
        paths = (result_path, plan_path, receipt_path, raw_path, calendar_path)
        signatures = {path: _stat(path) for path in paths}
        hashes = {path: _sha(path) for path in paths}
        if (hashes[plan_path] != result["plan_sha256"] or hashes[receipt_path] != result["receipts_sha256"]
                or hashes[raw_path] != result["raw_daily_sha256"] or hashes[raw_path] != result["panel_sha256"]
                or hashes[calendar_path] != plan["calendar_sha256"]):
            raise ValueError("The completed base result no longer pins the same raw/plan/receipt/calendar bytes")
        with calendar_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != ["date"]:
                raise ValueError("The source calendar must retain its date-only CSV contract")
            all_days = [_day(row["date"]) for row in reader]
        days = plan["days"]
        if (not all_days or all_days != sorted(set(all_days)) or not days or days != sorted(set(days))
                or days != [day for day in all_days if plan["requested_start"] <= day <= plan["requested_end"]]
                or len(days) != result["expected_days"] or len(days) != result["acquired_days"]
                or len(days) != receipt["expected_days"]):
            raise ValueError("The native base row-group window and pinned exact sessions disagree")
        now = _now()
        available = _stamp(receipt["updated_at"])
        if not _stamp(days[-1] + "T15:00:00+08:00") <= available <= now:
            raise ValueError("Source completion is before its final close or after the actual read clock")
        self._parquet = pq.ParquetFile(raw_path)
        try:
            if (self._parquet.schema_arrow.names != list(RAW_FIELDS)
                    or any(not pa.types.is_string(field.type) for field in self._parquet.schema_arrow)
                    or self._parquet.metadata.num_row_groups != len(days)
                    or self._parquet.metadata.num_rows != result["rows"]):
                raise ValueError("Native source must retain all original 18 string fields and one row group per exact session")
            tasks = receipt["tasks"]
            for index, day in enumerate(days):
                task = tasks.get(day)
                if (not isinstance(task, dict) or task.get("date") != day or task.get("kind") != "stock"
                        or task.get("method") != "query_daily_history_k_AStock" or task.get("error_code") != "0"
                        or task.get("file") != day + "_stock.csv" or not task.get("sha256")):
                    raise ValueError(f"Missing or failed registered base-source task: {day}")
                group = self._parquet.metadata.row_group(index)
                stats = group.column(0).statistics
                if (group.num_rows != task["quality"]["rows"]
                        or (group.num_rows and (stats is None or not stats.has_min_max
                            or stats.min != day or stats.max != day or stats.null_count != 0))):
                    raise ValueError(f"Native row-group date or original inventory differs: {day}")
        except Exception:
            self._parquet.close()
            raise
        self._calendar, self._indices = tuple(days), {day: index for index, day in enumerate(days)}
        self._tasks = deepcopy(tasks)
        self._signatures, self._cache, self._closed = signatures, OrderedDict(), False
        self._provenance = {"source_view": str(self.folder), "source_status": result["status"],
            "kind": result["kind"], "result_file": str(result_path), "result_sha256": hashes[result_path],
            "raw_file": str(raw_path), "raw_sha256": hashes[raw_path],
            "plan_file": str(plan_path), "plan_sha256": hashes[plan_path],
            "receipt_file": str(receipt_path), "receipt_sha256": hashes[receipt_path],
            "calendar": str(calendar_path), "calendar_sha256": hashes[calendar_path],
            "source_available_at": available.isoformat(),
            "source_clock_basis": "original_whole_cache_terminal_updated_at_not_historical_publication",
            "opened_at": now.isoformat(), "source_rows": result["rows"], "source_reported_symbols": result["symbols"],
            "source_sessions": len(days), "source_failure_history": deepcopy(receipt.get("failures", [])),
            "global_symbol_inventory_recomputed": False,
            "coverage_scope": "provider_observed_rows_not_exchange_dated_universe_or_action_continuity",
            "signal_price_policy": "not_selected_by_this_native_raw_reader", **FLAGS}
        self._unchanged()

    @property
    def calendar(self):
        return list(self._calendar)

    @property
    def provenance(self):
        return deepcopy(self._provenance)

    def _unchanged(self):
        if self._closed:
            raise ValueError("Native history source is closed")
        if (self.folder / "INVALID.json").exists() or any(_stat(path) != signature for path, signature in self._signatures.items()):
            raise ValueError("Pinned native source changed after initialization; do not mix generations")

    def _load(self, day):
        self._unchanged()
        if day in self._cache:
            self._cache.move_to_end(day)
            return self._cache[day]
        frame = self._parquet.read_row_group(self._indices[day], columns=list(RAW_FIELDS)).to_pandas()
        if (not frame.date.eq(day).all() or frame.code.duplicated().any()
                or not frame.code.astype("string").str.fullmatch(r"(?:sh|sz)\.[0-9]{6}").fillna(False).all()
                or not frame.adjustflag.eq("3").all()):
            raise ValueError(f"Invalid original native date/code/raw-price basis in requested group: {day}")
        frame["symbol"] = [code[3:] + (".SSE" if code.startswith("sh.") else ".SZSE") for code in frame.code]
        for raw, parsed in (("tradestatus", "tradestatus_value"), ("isST", "isST_value")):
            frame[parsed] = pd.array([int(token) if isinstance(token, str) and token in {"0", "1"} else pd.NA for token in frame[raw]], dtype="Int8")
        self._unchanged()
        self._cache[day] = frame
        while len(self._cache) > 2:
            self._cache.popitem(last=False)
        return frame

    def frame(self, day, symbols=None):
        """Return original strings plus explicit identity/nullable status columns."""
        _day(day)
        if day not in self._indices:
            raise ValueError("Requested day is not an exact session of this source; no next-available fallback")
        requested = _symbols(symbols)
        frame = self._load(day)
        selected = frame if requested is None else frame.loc[frame.symbol.isin(requested)]
        result = selected.copy(deep=True)
        result.attrs["provenance"] = {**self.provenance, "read_at": _now().isoformat(),
            "day": day, "row_group": self._indices[day], "symbols": requested,
            "symbol_scope_basis": "all_own_dated_rows" if requested is None else "explicit_original_code_request_not_latest_survivors"}
        return result

    def history(self, through, symbols, sessions):
        """Read the exact trailing session window without touching future groups."""
        _day(through)
        if through not in self._indices or type(sessions) is not int or sessions < 1:
            raise ValueError("History requires an exact cutoff session and a positive integer window")
        requested = _symbols(symbols, required=True)
        end = self._indices[through]
        days = list(self._calendar[max(0, end - sessions + 1):end + 1])
        frames, missing, sources = [], [], []
        for day in days:
            frame = self.frame(day, requested)
            present = set(frame.symbol)
            missing.extend({"day": day, "symbol": symbol} for symbol in requested if symbol not in present)
            frame.attrs.clear()
            frames.append(frame)
            sources.append({"day": day, "row_group": self._indices[day], "source_task": deepcopy(self._tasks[day])})
        raw = pd.concat(frames, ignore_index=True)
        self._unchanged()
        shortfall = sessions - len(days)
        coverage = {"requested_sessions": sessions, "observed_source_sessions": len(days),
            "unavailable_prefix_session_count": shortfall, "truncated_at_source_start": bool(shortfall),
            "requested_symbol_count": len(requested), "expected_keys_in_available_sessions": len(days) * len(requested),
            "observed_keys": len(raw), "missing_key_count": len(missing),
            "window_complete": not shortfall and not missing,
            "unknown_trade_status_rows": int(raw.tradestatus_value.isna().sum()),
            "unknown_ST_rows": int(raw.isST_value.isna().sum()),
            "scope": "requested_native_key_presence_not_eligibility_or_price_continuity",
            "missing_rows_imputed": False, "latest_universe_used": False}
        return {"raw": raw, "days": days, "missing_keys": missing, "coverage": coverage,
            "provenance": {**self.provenance, "read_at": _now().isoformat(), "through": through,
                "symbols": requested, "day_sources": sources,
                "future_row_groups_read_by_this_request": False,
                "previous_session_required": {day: self._calendar[self._indices[day] - 1] if self._indices[day] else None for day in days}}}

    def close(self):
        if not self._closed:
            self._cache.clear()
            self._parquet.close()
            self._closed = True

    def __enter__(self):
        self._unchanged()
        return self

    def __exit__(self, *args):
        self.close()
