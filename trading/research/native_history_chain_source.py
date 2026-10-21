"""An explicit read-only native-view prefix followed by exact dated closes.

The existing merged view is not repaired or rewritten. Each component keeps
its own physical storage, original fields, dated inventory and knowledge clock.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from .baostock_daily_slice import FIELDS, digest
from .baostock_full_close import load_complete_close
from .leader_daily_signals import _calendar_view
from .native_history_source import NativeHistorySource, FLAGS as NATIVE_FLAGS, _day, _stamp, _symbols
from .provider_close_batches import _acquisition_times

METHOD_ID = "native_history_explicit_component_chain_v1"
FLAGS = {**NATIVE_FLAGS, "source_rosters_retrofiltered": False,
    "missing_extras_filled": False, "source_components_rewritten": False}


def _stat(path):
    value = Path(path).stat()
    return value.st_size, value.st_mtime_ns


class NativeHistoryChainSource:
    """Reuse plain native readers and the existing strict full-close loader."""
    def __init__(self, base_view, extension_views, close_folders, *, calendar):
        self._native, self._closed_frames, self._owners = [], {}, {}
        self._closed, self._pins = False, {}
        self._components, self._day_sources, self._days = [], [], []
        try:
            calendar = Path(calendar).resolve()
            calendar_hash = digest(calendar)
            payload = json.loads(calendar.read_text(encoding="utf-8"))
            year = payload.get("year")
            if year != 2026:
                raise ValueError("The explicit append chain currently uses the pinned 2026 exchange calendar")
            sessions, previous, status = _calendar_view(payload, "2026-01-01", "2026-12-31", [])
            if status != "pinned_static_exchange_calendar":
                raise ValueError("A complete pinned exchange session calendar is required")
            self._pins[calendar] = _stat(calendar)
            actual = datetime.now().astimezone()
            paths = [Path(base_view).resolve()] + [Path(path).resolve() for path in extension_views]
            if len(paths) != len(set(paths)):
                raise ValueError("Repeated native components cannot supply overlapping history")
            for index, path in enumerate(paths):
                source = NativeHistorySource(path)
                self._native.append(source)
                days, proof = source.calendar, source.provenance
                if index == 0:
                    if days[-1] not in sessions:
                        raise ValueError("The original base must end inside the pinned append calendar")
                    same_year = [day for day in days if day.startswith("2026-")]
                    if same_year != [day for day in sessions if same_year[0] <= day <= days[-1]]:
                        raise ValueError("The base's 2026 source sessions disagree with the pinned append calendar")
                else:
                    self._append_days(days, sessions, previous)
                component_id = f"native_{index}"
                self._components.append({"component_id": component_id, "component_kind": "plain_native_view",
                    "source_days": days, "source_proof": proof})
                for row_group, day in enumerate(days):
                    self._add(day, ("native", index), {"day": day, "component_id": component_id,
                        "component_kind": "plain_native_view", "row_group": row_group,
                        "close_file": None, "source_view": proof["source_view"],
                        "raw_file": proof["raw_file"], "raw_sha256": proof["raw_sha256"],
                        "result_file": proof["result_file"], "result_sha256": proof["result_sha256"],
                        "plan_file": proof["plan_file"], "plan_sha256": proof["plan_sha256"],
                        "receipt_file": proof["receipt_file"], "receipt_sha256": proof["receipt_sha256"],
                        "source_available_at": proof["source_available_at"],
                        "source_clock_basis": proof["source_clock_basis"],
                        "original_raw_fields": list(source._parquet.schema_arrow.names)})
            close_paths = [Path(path).resolve() for path in close_folders]
            if len(close_paths) != len(set(close_paths)):
                raise ValueError("Repeated full-close components cannot be appended twice")
            for index, path in enumerate(close_paths):
                metadata, raw, ordinary = load_complete_close(path)
                day = metadata["plan"]["day"]
                self._append_days([day], sessions, previous)
                if (metadata.get("read_only_verified") is not True
                        or metadata["plan"].get("calendar_sha256") != calendar_hash
                        or list(raw.columns) != FIELDS.split(",") or not raw.date.eq(day).all()
                        or raw.code.tolist() != ordinary.code.tolist()):
                    raise ValueError("Strict full closes must keep their own exact roster, fields and pinned calendar")
                clocks = _acquisition_times(metadata, path)
                terminal = clocks["terminal_completion_observed_at"]
                roster_finished = clocks["roster_receipt_completed_at"]
                if terminal is None or roster_finished is None:
                    raise ValueError("Both terminal assembly and dated-roster completion clocks are required")
                completion = max(_stamp(terminal), _stamp(roster_finished))
                source_times = [*clocks["acquisition_timestamps"].values(),
                    *clocks["roster_acquisition_timestamps"].values()]
                if (completion < _stamp(day + "T15:00:00+08:00")
                        or any(_stamp(clock) > actual or _stamp(clock) > completion for clock in source_times)):
                    raise ValueError("Full-close source completion cannot predate its close or contain future acquisition clocks")
                component_id = f"close_{index}"
                proof = {"source_folder": str(path), "source_schema": metadata["plan"]["schema"],
                    "day": day, "raw_file": str(path / "raw_daily.csv"),
                    "raw_sha256": metadata["receipt"]["raw_daily_sha256"],
                    "plan_file": str(path / "plan.json"), "plan_sha256": digest(path / "plan.json"),
                    "receipt_file": str(path / "receipt.json"), "receipt_sha256": digest(path / "receipt.json"),
                    "roster_raw_sha256": metadata["plan"]["roster_raw_sha256"],
                    "calendar_sha256": calendar_hash, "ordinary_inventory_rows": len(ordinary),
                    "receipt_status": metadata["receipt"]["status"], "read_only_verified": True,
                    "full_dated_roster_inventory_complete": True, "original_raw_fields": list(raw.columns),
                    "source_available_at": completion.isoformat(), **clocks}
                self._components.append({"component_id": component_id, "component_kind": "strict_dated_close_csv",
                    "source_days": [day], "source_proof": proof})
                self._closed_frames[day] = raw.copy(deep=True)
                self._add(day, ("close", day), {**deepcopy(proof), "component_id": component_id,
                    "component_kind": "strict_dated_close_csv", "row_group": None,
                    "close_file": proof["raw_file"]})
                for file in (path / "plan.json", path / "receipt.json", path / "raw_daily.csv", Path(clocks["roster_receipt"])):
                    self._pins[file] = _stat(file)
            self._calendar, self._calendar_hash = calendar, calendar_hash
            source_available = max(_stamp(item["source_proof"]["source_available_at"]) for item in self._components)
            self._provenance = {"method_id": METHOD_ID, "source_kind": "explicit_native_and_exact_dated_close_chain",
                "source_available_at": source_available.isoformat(),
                "source_clock_basis": "maximum_actual_component_completion_not_historical_publication",
                "opened_at": actual.isoformat(), "calendar": str(calendar), "calendar_sha256": calendar_hash,
                "component_sources": deepcopy(self._components), "day_sources": deepcopy(self._day_sources),
                "source_sessions": len(self._days), "source_first_day": self._days[0], "source_last_day": self._days[-1],
                "source_failure_history": [deepcopy(item["source_proof"].get("source_failure_history", []))
                    for item in self._components if item["component_kind"] == "plain_native_view"],
                "coverage_scope": "each_component_own_observed_rows_not_exchange_PIT_or_action_continuity",
                "signal_price_policy": "not_selected_by_this_native_raw_reader", **FLAGS}
            self._unchanged()
        except Exception:
            self.close()
            raise

    def _append_days(self, days, sessions, previous):
        if (not days or days != sorted(set(days)) or any(day not in sessions for day in days)
                or self._days[-1] not in sessions or previous[days[0]] != self._days[-1]
                or any(previous[day] != before for before, day in zip(days, days[1:]))):
            raise ValueError("Components must append exact consecutive sessions, without overlap, gap or reordering")

    def _add(self, day, owner, proof):
        if day in self._owners:
            raise ValueError("Duplicate source days cannot be replaced or reconciled silently")
        self._owners[day] = owner
        self._days.append(day)
        self._day_sources.append({**proof, "logical_session_index": len(self._days) - 1})

    @property
    def calendar(self):
        return list(self._days)

    @property
    def provenance(self):
        return deepcopy(self._provenance)

    def _unchanged(self):
        if self._closed:
            raise ValueError("Native history chain is closed")
        for source in self._native:
            source._unchanged()
        if any(_stat(file) != signature for file, signature in self._pins.items()):
            raise ValueError("A pinned chain calendar or full-close source changed after initialization")

    def frame(self, day, symbols=None):
        _day(day)
        if day not in self._owners:
            raise ValueError("An exact own-source session is required, without next-available fallback")
        requested = _symbols(symbols)
        self._unchanged()
        kind, owner = self._owners[day]
        if kind == "native":
            frame = self._native[owner].frame(day, requested)
        else:
            frame = self._closed_frames[owner].copy(deep=True)
            frame["symbol"] = frame.code.str[3:] + frame.code.str[:2].map({"sh": ".SSE", "sz": ".SZSE"})
            for field, value in (("tradestatus", "tradestatus_value"), ("isST", "isST_value")):
                frame[value] = pd.array([int(token) if token in {"0", "1"} else pd.NA for token in frame[field]], dtype="Int8")
            if requested is not None:
                frame = frame.loc[frame.symbol.isin(requested)].copy()
        proof = deepcopy(self._day_sources[self._days.index(day)])
        frame.attrs["provenance"] = {**proof, "read_at": datetime.now().astimezone().isoformat(),
            "symbols": requested, "method_id": METHOD_ID, **FLAGS}
        self._unchanged()
        return frame

    def close(self):
        if not self._closed:
            for source in self._native:
                source.close()
            self._closed_frames.clear()
            self._closed = True

    def __enter__(self):
        self._unchanged()
        return self

    def __exit__(self, *args):
        self.close()
