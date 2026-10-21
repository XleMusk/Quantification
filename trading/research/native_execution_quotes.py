"""Read a separately pinned native whole-row execution-price stream.

This is not a StockDB field patch: its prices, reference, amount, volume and
statuses all come from each requested session's one explicit native owner.
Missing stock rows stay missing; source presence does not establish PIT or
corporate-action completeness.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path

import pandas as pd

from .native_history_source import FLAGS, NativeHistorySource, _day, _symbols


METHOD_ID = "native_whole_row_execution_quotes_explicit_sources_v1"
NUMERIC_FIELDS = ("open", "high", "low", "close", "preclose", "volume", "amount")


def load_native_execution_quotes(views, symbols, days):
    """Return native observations and dated provenance, without account work."""
    if not isinstance(views, (list, tuple)) or not views:
        raise ValueError("Explicit native source views are required")
    paths = [Path(view).resolve() for view in views]
    if len(paths) != len(set(paths)):
        raise ValueError("Native execution source paths must be unique")
    requested_symbols = _symbols(symbols, required=True)
    if not isinstance(days, (list, tuple)) or not days:
        raise ValueError("Explicit exact native execution sessions are required")
    requested_days = [_day(day) for day in days]
    if requested_days != sorted(set(requested_days)):
        raise ValueError("Native execution sessions must be unique and chronological")

    with ExitStack() as stack:
        sources = [stack.enter_context(NativeHistorySource(path)) for path in paths]
        owners = {}
        wanted = set(requested_days)
        for index, source in enumerate(sources):
            for day in wanted.intersection(source.calendar):
                if day in owners:
                    raise ValueError(f"Overlapping native execution source owners: {day}")
                owners[day] = index
        missing_days = [day for day in requested_days if day not in owners]
        if missing_days:
            raise ValueError(f"Native execution source sessions missing: {missing_days}")

        frames, dated, missing_keys = [], [], 0
        for day in requested_days:
            index = owners[day]
            source = sources[index]
            frame = source.frame(day, requested_symbols)
            proof = deepcopy(frame.attrs["provenance"])
            if not frame.adjustflag.eq("3").all():
                raise ValueError(f"Original native execution quotes must use adjustflag 3: {day}")
            frame.attrs.clear()
            for field in NUMERIC_FIELDS:
                # Only the original empty token becomes missing; malformed
                # nonempty values cannot be silently turned into quotes.
                frame[field] = pd.to_numeric(frame[field].replace("", None), errors="raise")
            for field in ("tradestatus", "isST"):
                frame[field + "_raw"] = frame[field]
                frame[field] = pd.array([int(value) if value in {"0", "1"} else pd.NA
                    for value in frame[field]], dtype="Int8")
            missing_count = len(requested_symbols) - len(frame)
            missing_keys += missing_count
            dated.append({"day": day, "source_index": index,
                "source_view": str(paths[index]), "row_group": proof["row_group"],
                "source_task": deepcopy(source._tasks[day]), "provenance": proof,
                "requested_symbols": len(requested_symbols), "observed_rows": len(frame),
                "missing_symbol_rows": missing_count})
            frames.append(frame)
        raw = pd.concat(frames, ignore_index=True)
        if raw.duplicated(["date", "symbol"]).any():
            raise ValueError("Native execution stream contains duplicate exact stock/session observations")
        provenance = {"schema": "native_execution_quote_stream_v1", "method_id": METHOD_ID,
            "requested_days": requested_days, "requested_symbols": requested_symbols,
            "source_views": [source.provenance for source in sources], "day_sources": dated,
            "observed_rows": len(raw), "missing_stock_day_rows": missing_keys,
            "source_fields_policy": "independent_native_whole_row_stream_not_stockdb_field_replacement",
            "numeric_fields": list(NUMERIC_FIELDS), "status_policy": "explicit_0_or_1_only_unknown_remains_nullable",
            "no_stockdb_fields_replaced": True, "stockdb_rows_mixed": False,
            "future_rows_used_for_stock_scope": False, "missing_rows_imputed": False,
            "native_source_integrity_hashes_verified_by_existing_reader": True, **FLAGS}
        return raw, provenance
