"""Pinned action components with independent StockDB raw-reference checks.

This posthoc research overlay never reads the old adjusted price matrices and
never changes prices, canonical terms, or the supplied account state.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from ..rules import holder_distribution_action, stock_board
from ..symbols import normalize_symbol
from .action_events import reference_concordance
from .adjusted_prices import checked_file
from .bulk_feed import AMOUNT_FIELDS, BulkResearchFeed


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Canonical action-accounting date required")
    return value


def _positive(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Finite positive exact StockDB reference required") from error


def _pin(folder, result, name, refs):
    digest = result.get("files", {}).get(name)
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError(f"Missing action evidence hash: {name}")
    path = checked_file(folder, name, digest)
    refs.append({"path": str(path.resolve()), "sha256": digest})
    return path


class StockDBActionAccounting:
    def __init__(self, actions, special, evidence, coverage, universe, identity,
                 symbols, raw_days, replay_days, provenance):
        self.actions, self.special_references = deepcopy(actions), deepcopy(special)
        self.provenance = deepcopy(provenance)
        self.price_source = provenance.get("price_source", "stockdb")
        self.checks, self._check_keys = [], set()
        self.symbols, self.replay_days = set(symbols), set(replay_days)
        self.start = replay_days[0]
        self.previous = dict(zip(raw_days[1:], raw_days[:-1]))
        self.coverage = coverage
        self.evidence = evidence
        self.actions_by_ex = defaultdict(list)
        for action in self.actions:
            self.actions_by_ex[action["dividOperateDate"]].append(action)
        self.special_by_ex = defaultdict(dict)
        for item in special:
            self.special_by_ex[item["date"]][item["symbol"]] = item
        self.rename = identity.get("rename")
        self.code_symbols = dict(zip(universe.code, universe.symbol))
        self.unsupported_instruments = {}
        for symbol in symbols:
            try:
                stock_board(symbol)
            except ValueError as error:
                self.unsupported_instruments[symbol] = str(error)

    def _record(self, row):
        row = {**row, "execution_quote_source": self.price_source}
        if self.price_source != "stockdb" and "scope" in row:
            row["scope"] = "current_held_or_positive_pending_independent_native_raw"
        key = tuple(str(row.get(k)) for k in
                    ("symbol", "date", "previous_observed_close", "preclose", "reference_formula_status"))
        if key not in self._check_keys:
            self._check_keys.add(key)
            self.checks.append(row)

    def validate(self, state, day, rows):
        """Check only current exposure; all failures precede Engine mutations."""
        if day not in self.replay_days:
            raise ValueError("Action accounting day outside the registered replay")
        held = {s for s, lots in state["positions"].items() if sum(lot["qty"] for lot in lots) > 0}
        pending = {s for s, weight in (state.get("pending_targets") or {}).items() if weight > 0}
        required = held | pending
        if not required <= self.symbols:
            raise ValueError("Action accounting exposure outside its pinned symbol scope")
        unresolved = defaultdict(list)
        current = rows.get(day, {})
        previous_day = self.previous.get(day)
        previous = rows.get(previous_day, {}) if previous_day else {}
        by_symbol = {item["symbol"]: item for item in self.actions_by_ex.get(day, [])}
        entitlements = state["entitlements"]
        for symbol in sorted(required):
            code = normalize_symbol(symbol).baostock_code
            sources = [code]
            if self.rename and code == self.rename["new"]:
                sources = [self.rename["old"], self.rename["new"]]
            if not all(self.coverage.get((source, int(day[:4]))) is True for source in sources):
                raise ValueError(f"Unknown action source-year coverage: {symbol} {day}")
            if symbol not in current or symbol not in previous:
                self._record({"symbol": symbol, "date": day, "previous_day": previous_day,
                              "reference_formula_status": "missing_exact_previous_or_current_raw"})
                raise ValueError(f"Missing exact preceding StockDB raw reference: {symbol} {day} previous={previous_day}")
            prior, actual = _positive(previous[symbol].get("close")), _positive(current[symbol].get("preclose"))
            action = by_symbol.get(symbol)
            if action is not None or symbol in self.special_by_ex.get(day, {}):
                gap = {"symbol": symbol, "code": code, "date": day, "previous_day": previous_day,
                       "previous_observed_close": str(prior), "preclose": str(actual),
                       "has_reported_distribution": action is not None,
                       **{field: action.get(field, "") if action else "" for field in AMOUNT_FIELDS}}
                try:
                    checked = reference_concordance(pd.DataFrame([gap]), self.special_references, self.evidence).iloc[0].to_dict()
                except (InvalidOperation, ValueError) as error:
                    self._record({**gap, "reference_formula_status": "invalid_reference_terms", "reason": str(error)})
                    raise ValueError(f"Invalid StockDB action reference: {symbol} {day}; {error}") from error
                self._record({**checked, "scope": "current_held_or_positive_pending_stockdb_raw",
                              "old_native_reference_label_used": False})
                if checked["reference_formula_status"] != "reference_match":
                    unresolved[day].append((symbol, checked["reference_formula_status"]))
            else:
                status = "no_reported_event_reference_continuous" if abs(actual - prior) <= Decimal(".021") else "missing_source_event"
                self._record({"symbol": symbol, "code": code, "date": day, "previous_day": previous_day,
                              "previous_observed_close": str(prior), "preclose": str(actual),
                              "reference_formula_status": status,
                              "scope": "current_held_or_positive_pending_stockdb_raw",
                              "old_native_reference_label_used": False})
                if status == "missing_source_event":
                    unresolved[day].append((symbol, status))
        # Missing registration is unknown, whereas an explicitly registered zero
        # is valid. Cash-only inception does not acquire earlier entitlements.
        for action in self.actions:
            symbol, ex = action["symbol"], action["dividOperateDate"]
            if symbol not in required or day > ex:
                continue
            record = action.get("dividRegistDate", "")
            if not record:
                if day == ex:
                    raise ValueError(f"Unknown held action registration date: {symbol} {day}")
                continue
            identifier = f"{symbol}:{ex}"
            if self.start <= record < day and identifier not in entitlements:
                raise ValueError(f"Missing held action entitlement: {identifier}; record={record}")
            if identifier in entitlements and (type(entitlements[identifier]) is not int or entitlements[identifier] < 0):
                raise ValueError(f"Invalid registered action entitlement: {identifier}")
        guard = SimpleNamespace(unsupported_instruments=self.unsupported_instruments,
            rename=self.rename, code_symbols=self.code_symbols, actions_by_ex=self.actions_by_ex,
            unresolved_references=unresolved, allow_adjusted_continuity=False,
            adjusted_reference_continuity={})
        BulkResearchFeed.validate_session(guard, state, day)


def load_action_accounting(event_view, reference_view, *, symbols, raw_days, replay_days, price_source="stockdb"):
    if price_source not in {"stockdb", "independent_baostock_raw"}:
        raise ValueError("Explicit supported execution quote source required")
    event_view, reference_view = Path(event_view).resolve(), Path(reference_view).resolve()
    symbols, raw_days, replay_days = list(symbols), list(raw_days), list(replay_days)
    if (not symbols or len(symbols) != len(set(symbols))
            or any(normalize_symbol(s).vt_symbol != s for s in symbols)):
        raise ValueError("Explicit unique canonical action-accounting scope required")
    for days in (raw_days, replay_days):
        if not days or days != sorted(set(days)) or any(_day(d) != d for d in days):
            raise ValueError("Exact ordered raw and replay sessions required")
    if not set(replay_days) <= set(raw_days) or raw_days.index(replay_days[0]) == 0:
        raise ValueError("Exact preceding raw session is required before replay inception")
    events, references = _json(event_view / "result.json"), _json(reference_view / "result.json")
    if (events.get("status") != "research_action_events_projected"
            or references.get("status") != "research_reference_presence_diagnostic"
            or Path(references["event_view"]).resolve() != event_view
            or events.get("execution_admitted") is not False or references.get("execution_admitted") is not False):
        raise ValueError("Pinned unadmitted action/reference pair required")
    if not events["start"] <= replay_days[0] <= replay_days[-1] <= events["end"]:
        raise ValueError("Replay extends outside queried action dates")
    refs = [{"path": str(folder / "result.json"), "sha256": sha256((folder / "result.json").read_bytes()).hexdigest()}
            for folder in (event_view, reference_view)]
    source = _json(_pin(event_view, events, "source_result.json", refs))
    identity = _json(_pin(event_view, events, "price_identity.json", refs))
    input_events = _pin(reference_view, references, "input_events.json", refs)
    prices_path = _pin(reference_view, references, "input_prices.json", refs)
    prices = _json(prices_path)
    if (input_events.read_bytes() != (event_view / "result.json").read_bytes()
            or source.get("status") != "source_actions_complete"
            or events.get("source_status") != source["status"]
            or _json(Path(events["source_view"]) / "result.json") != source
            or identity.get("status") != "derived_research_levels"
            or prices.get("status") != "research_reconstruction_completed"
            or any(Path(item["raw_view"]).resolve() != Path(events["raw_view"]).resolve() for item in (source, identity, prices))
            or references.get("raw_daily_sha256") != prices.get("source_hashes", {}).get("raw")
            or references.get("raw_daily_sha256") != source.get("raw_daily_sha256")
            or not references.get("raw_daily_sha256")
            or references.get("canonical_panel_sha256") != prices.get("canonical_panel_sha256")):
        raise ValueError("Action/reference source origin or saved payload pair changed")
    universe = pd.read_csv(_pin(event_view, events, "source_input_universe.csv", refs), dtype=str, keep_default_na=False)
    calendar = pd.read_csv(_pin(event_view, events, "source_input_calendar.csv", refs), dtype=str, keep_default_na=False)
    coverage = pd.read_csv(_pin(event_view, events, "input_coverage.csv", refs), dtype=str, keep_default_na=False)
    needed = {"code", "symbol", "year", "queried", "rows", "empty_response", "source_file", "source_sha256"}
    if (not {"code", "symbol"} <= set(universe) or "date" not in calendar or not needed <= set(coverage)
            or universe.code.duplicated().any() or universe.symbol.duplicated().any()
            or len(universe) != events["expected_codes"]
            or not universe.symbol.eq(universe.code.map(lambda c: normalize_symbol(c).vt_symbol)).all()
            or calendar.date.tolist() != sorted(set(calendar.date))
            or not set(raw_days) <= set(calendar.date)):
        raise ValueError("Pinned action universe/calendar/coverage schema changed")
    years = range(int(events["start"][:4]), int(events["end"][:4]) + 1)
    expected = {(code, year) for code in universe.code for year in years}
    try:
        keys = [(row.code, int(row.year)) for row in coverage.itertuples(index=False)]
        queried = coverage.queried.map({"True": True, "False": False, "true": True, "false": False})
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid action coverage year") from error
    if (len(keys) != len(set(keys)) or set(keys) != expected or queried.isna().any()
            or int(queried.sum()) != events["queried"] or int((~queried).sum()) != events["unqueried"]
            or not coverage.symbol.eq(coverage.code.map(lambda c: normalize_symbol(c).vt_symbol)).all()):
        raise ValueError("Full action code/year query matrix changed")
    empty = coverage.empty_response.map({"True": True, "False": False, "true": True, "false": False})
    for index, row in coverage.iterrows():
        if queried.iloc[index]:
            try:
                count = int(row["rows"])
            except (ValueError, TypeError) as error:
                raise ValueError("Queried action rows must retain their source count") from error
            if (count < 0 or pd.isna(empty.iloc[index]) or (count == 0) != bool(empty.iloc[index])
                    or not row.source_file or len(row.source_sha256) != 64):
                raise ValueError("Queried action count/empty/source evidence is incomplete")
        elif row["rows"] != "" or row.empty_response != "":
            raise ValueError("Unqueried action rows cannot be represented as empty queries")
    availability = dict(zip(keys, queried.tolist()))
    actions = pd.read_parquet(_pin(event_view, events, "canonical_actions.parquet", refs)).fillna("")
    columns = {"symbol", "code", "dividOperateDate", "dividRegistDate", "dividPayDate", "dividStockMarketDate", *AMOUNT_FIELDS}
    if (not columns <= set(actions) or actions.duplicated(["symbol", "dividOperateDate"]).any()
            or len(actions) != events["canonical_events"]
            or not actions.symbol.eq(actions.code.map(lambda c: normalize_symbol(c).vt_symbol)).all()):
        raise ValueError("Canonical action schema/identity changed")
    for field in ("dividOperateDate", "dividRegistDate", "dividPayDate", "dividStockMarketDate"):
        for value in actions[field]:
            if value != "" or field == "dividOperateDate":
                _day(value)
    selected = actions.loc[actions.symbol.isin(symbols)].to_dict("records")
    special = _json(_pin(reference_view, references, "reviewed_special_references.json", refs))
    evidence = []
    for name in ("cash_reference_evidence.json", "distribution_reference_evidence.json"):
        evidence.extend(_json(_pin(reference_view, references, name, refs)))
    if not isinstance(special, list) or not all(isinstance(item, dict) for item in special + evidence):
        raise ValueError("Saved action reference evidence must be explicit lists")
    if len({(r["code"], r["dividOperateDate"]) for r in evidence}) != len(evidence):
        raise ValueError("Duplicate reviewed action reference")
    special = [r for r in special if r["symbol"] in symbols]
    evidence = [r for r in evidence if normalize_symbol(r["code"]).vt_symbol in symbols]
    for item in special + evidence:
        for field in ("source_file", "reference_source_file", "holder_source_file", "issuer_page_file"):
            if item.get(field):
                digest = item.get(field.replace("_file", "_sha256"))
                if references["files"].get(item[field]) != digest:
                    raise ValueError("Reviewed action evidence differs from its pinned source")
                _pin(reference_view, references, item[field], refs)
    action_keys = {(a["symbol"], a["dividOperateDate"]) for a in selected}
    for item in special:
        if item["kind"] == "restructuring_holder_distribution":
            if (item["symbol"], item["date"]) in action_keys:
                raise ValueError("Restricted distribution overlaps canonical action")
            selected.append(holder_distribution_action(item))
    provenance = {"method": "stockdb_reported_action_components_own_raw_reference_diagnostic_v1",
        "knowledge_mode": "posthoc_component_diagnostic", "event_view": str(event_view),
        "reference_view": str(reference_view), "refs": refs, "symbol_count": len(symbols),
        "raw_start": raw_days[0], "start": replay_days[0], "end": replay_days[-1],
        "price_source": price_source,
        "reference_basis": f"exact_{price_source}_D_preclose_and_exact_previous_provider_session_close",
        "old_native_reference_labels_used": False, "adjusted_continuity_override": False,
        "canonical_terms_modified": False, "raw_prices_modified": False,
        "reference_formula_tolerance_rmb": .011, "no_event_reference_change_threshold_rmb": .021,
        "source_query_complete": source["status"] == "source_actions_complete",
        "coverage_verified": False, "pit_verified": False, "returns_reportable": False,
        "execution_admitted": False, "selection_enabled": False, "strategy_validated": False}
    return StockDBActionAccounting(selected, special, evidence, availability, universe, identity,
                                   symbols, raw_days, replay_days, provenance)
