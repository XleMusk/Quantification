"""Explicit ordinary-distribution components for unbound REPLAY accounting.

This partial positive-event view is neither a no-action inventory nor a price
reference exception. It never repairs raw bars, historical signals or accounts.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from ..contracts import RunContext, TradingMode
from ..corporate_action_binding import (
    ACTION_VIEW_BINDING_ID,
    STATE_KEY,
    validate_research_action_binding,
)
from .baostock_daily_slice import digest

SCHEMA = "research_corporate_action_component_view_v1"
BINDING_SCHEMA = "research_corporate_action_component_binding_v1"
METHOD_ID = "official_ordinary_distribution_components_engine_v1"
KNOWLEDGE_MODES = ("dated_asof", "posthoc_component_diagnostic")
AMOUNT_FIELDS = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "partial_ordinary_component_accounting_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "pit_verified": False,
    "corporate_actions_verified": False, "reference_formula_admitted": False,
    "feature_chain_admitted": False, "concurrent_rights_coverage_verified": False,
    "returns_reportable": False, "provider_contacted": False,
    "account_accessed": False, "raw_prices_modified": False}
_TOKEN = object()


def _time(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise ValueError("Corporate-action evidence clocks require an explicit timezone")
    return stamp


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Corporate-action dates must be explicit canonical dates")
    return value


def _amount(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _quantity(state, symbol):
    return sum(lot["qty"] for lot in state.get("positions", {}).get(symbol, []))


@dataclass(frozen=True, init=False)
class ResearchCorporateActionView:
    """Immutable loaded evidence with a pure guard; the Engine owns all writes."""
    binding_contract = ACTION_VIEW_BINDING_ID
    _binding: str
    _events: str
    _actions: str
    context: RunContext
    knowledge_mode: str
    available_at: str

    def __init__(self, binding, events, actions, context, *, _token=None):
        if _token is not _TOKEN:
            raise ValueError("Load a corporate-action view from its pinned review result")
        object.__setattr__(self, "_binding", json.dumps(binding, sort_keys=True, ensure_ascii=False, allow_nan=False))
        object.__setattr__(self, "_events", json.dumps(events, ensure_ascii=False, allow_nan=False))
        object.__setattr__(self, "_actions", json.dumps(actions, ensure_ascii=False, allow_nan=False))
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "knowledge_mode", binding["knowledge_mode"])
        object.__setattr__(self, "available_at", binding["available_at"])

    @property
    def binding(self):
        return json.loads(self._binding)

    @property
    def events(self):
        return json.loads(self._events)

    @property
    def actions(self):
        return json.loads(self._actions)

    def validate_preopen_targets(self, day, targets):
        """Opening targets cannot borrow the later record-close knowledge clock."""
        _day(day)
        opening = _time(day + "T09:30:00+08:00")
        symbols = {symbol for symbol, weight in targets.items() if weight > 0}
        for event in self.events:
            if (event["symbol"] in symbols and event["dividRegistDate"] == day
                    and _time(event["available_at"]) > opening):
                raise ValueError(f"Corporate-action evidence was unavailable before preopen execution: {event['symbol']} {day}")

    def validate_session(self, state, day):
        """Reject missing relevant components/entitlements before any mutation."""
        _day(day)
        saved_binding = state.get(STATE_KEY)
        start = _day(saved_binding["replay_start_day"]) if saved_binding is not None else day
        held = {symbol for symbol in state.get("positions", {}) if _quantity(state, symbol) > 0}
        pending = {symbol for symbol, weight in (state.get("pending_targets") or {}).items() if weight > 0}
        registered, receivables = state.get("entitlements", {}), state.get("receivables", {})
        reserve = Decimal(self.binding["dividend_tax_reserve"])
        for event in self.events:
            symbol, record, ex = event["symbol"], event["dividRegistDate"], event["dividOperateDate"]
            identifier = f"{symbol}:{ex}"
            quantity = registered.get(identifier)
            if identifier in registered and (type(quantity) is not int or quantity < 0):
                raise ValueError(f"Invalid recorded corporate-action entitlement: {identifier}")
            if record < start:
                # A cash-only context created after this record date has no
                # entitlement to chase, including shares bought after its start.
                if quantity or identifier in receivables:
                    raise ValueError(f"Pre-context corporate-action entitlement cannot be introduced: {identifier}")
                continue
            if (self.knowledge_mode == "dated_asof" and day == record
                    and _time(event["available_at"]) > _time(day + "T15:00:00+08:00")):
                # Even a zero must not be written under a future-known event
                # identity while claiming the dated information frontier.
                raise ValueError(f"Corporate-action evidence was unavailable at record close: {identifier}")
            if quantity == 0:
                if identifier in receivables:
                    raise ValueError(f"A zero corporate-action entitlement cannot have a receivable: {identifier}")
                # This is a real record-close observation, not a missing key
                # defaulted to zero. Later purchases do not create entitlement.
                continue
            relevant = symbol in held | pending or (quantity is not None and quantity > 0) or identifier in receivables
            if day < record or not relevant:
                continue
            if day > record and identifier not in registered:
                raise ValueError(f"Missing registered corporate-action entitlement: {identifier}; record session was not observed")
            if self.knowledge_mode == "dated_asof":
                cutoff = day + ("T15:00:00+08:00" if day == record else "T09:30:00+08:00")
                if _time(event["available_at"]) > _time(cutoff):
                    raise ValueError(f"Corporate-action evidence was unavailable at the dated frontier: {identifier} {day}")
            if event["component_status"] != "complete_ordinary_components":
                raise ValueError(f"Unresolved held corporate-action components: {identifier}; missing={event['missing_components']}")
            if quantity and (state.get("day") is None or state["day"] < ex) and day > ex:
                raise ValueError(f"Skipped corporate-action ex session: {identifier}")
            cash, bonus, transfer = (_amount(event[field]) for field in AMOUNT_FIELDS)
            basis = _amount(event.get("bonus_taxable_cash_ps")) if bonus else Decimal(0)
            if cash - (cash + basis) * reserve < 0:
                raise ValueError(f"Bonus tax reserve exceeds cash dividend: {identifier}")
            if day != ex or not quantity:
                continue
            allocation = quantity * (bonus + transfer)
            if allocation != allocation.to_integral_value():
                raise ValueError(f"Fractional ordinary shares require an account-specific allocation receipt: {identifier}")

    def record_entitlements(self, book, day):
        """Record actual close quantities for complete AND partial events."""
        _day(day)
        binding = book.state.get(STATE_KEY)
        if (book.context != self.context or not isinstance(binding, dict)
                or {key: value for key, value in binding.items() if key != "replay_start_day"} != self.binding):
            raise ValueError("Record-close corporate actions require their exact Engine binding")
        for event in self.events:
            if event["dividRegistDate"] != day:
                continue
            quantity = book.quantity(event["symbol"])
            if type(quantity) is not int or quantity < 0:
                raise ValueError("Record-close corporate-action quantity must be an observed nonnegative integer")
            if quantity > 0 and event["component_status"] != "complete_ordinary_components":
                raise ValueError(f"Unresolved positive record-close corporate action: {event['symbol']} {day}")
            identifier = f"{event['symbol']}:{event['dividOperateDate']}"
            if identifier in book.state["entitlements"] and book.state["entitlements"][identifier] != quantity:
                raise ValueError("The original record-close entitlement cannot be replaced")
            book.state["entitlements"][identifier] = quantity


def load_research_corporate_action_view(review_result, *, knowledge_mode, context, config, now=None):
    """Load the existing bounded official review without reparsing its PDFs."""
    if knowledge_mode not in KNOWLEDGE_MODES or not isinstance(context, RunContext) or context.mode != TradingMode.REPLAY:
        raise ValueError("An explicit research knowledge mode and REPLAY context are required")
    reserve = _amount(config.dividend_tax_reserve)
    if reserve is None or reserve > 1:
        raise ValueError("The unchanged config must declare a valid dividend tax reserve")
    actual = _time(now or datetime.now().astimezone())
    path = Path(review_result).resolve()
    review_hash = digest(path)
    review = json.loads(path.read_text(encoding="utf-8"))
    receipt_path = path.parent / "retrieval_receipt.json"
    receipt_hash = digest(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (review.get("schema") != "bounded_official_action_reference_review_v1"
            or review.get("status") != "three_official_cash_dividend_events_confirmed_partial_share_and_PIT_boundaries_retained"
            or review.get("scope", {}).get("all_market_coverage_claimed") is not False
            or any(review.get(key) is not False for key in ("execution_admitted", "selection_enabled", "ledger_mutated", "raw_prices_modified"))
            or any(receipt.get(key) is not False for key in ("execution_admitted", "selection_enabled", "ledger_mutated"))):
        raise ValueError("Expected the saved partial, unadmitted official action review and acquisition receipt")
    reviewed = _time(review["reviewed_at"])
    if reviewed > actual:
        raise ValueError("The action review has not actually been recorded yet")
    inputs = []

    def source(file, expected_hash, url, *, pdf):
        located = Path(file).resolve()
        located.relative_to(path.parent)
        rows = [item for item in receipt["sources"] if (path.parent / item["file"]).resolve() == located]
        if len(rows) != 1:
            raise ValueError("Each official action source needs its unique actual acquisition receipt")
        row = rows[0]
        raw = located.read_bytes()
        if (not url.startswith("https://") or row.get("url") != url
                or row.get("status") != "downloaded" or row.get("status_code") != 200
                or row.get("sha256") != expected_hash or digest(located) != expected_hash
                or row.get("bytes") != len(raw) or (pdf and not raw.startswith(b"%PDF-"))):
            raise ValueError("Official action source bytes, media, URL or receipt changed")
        retrieved = _time(row["retrieved_at"])
        if not _time(row["started_at"]) <= retrieved <= reviewed <= actual:
            raise ValueError("Official action source knowledge clocks are inconsistent or future")
        proof = {"file": str(located), "sha256": expected_hash, "source_url": url,
            "retrieved_at": row["retrieved_at"], "byte_count": len(raw), "kind": "primary_pdf" if pdf else "official_metadata"}
        inputs.append(proof)
        return raw, proof

    events, actions = [], []
    keys = set()
    for original in review["events"]:
        item = deepcopy(original)
        symbol = item["symbol"]
        if not re.fullmatch(r"(?:[0-9]{6}\.SSE|[0-9]{6}\.SZSE)", symbol):
            raise ValueError("Only explicit ordinary SH/SZ action identities are supported")
        record, ex = (_day(item[field]) for field in ("dividRegistDate", "dividOperateDate"))
        if record >= ex or (symbol, ex) in keys or ex != review["scope"]["exact_event_day"]:
            raise ValueError("Action record/ex chronology or unique bounded identity changed")
        keys.add((symbol, ex))
        if (any(item.get(key) is not False for key in ("pit_verified", "corporate_actions_verified", "reference_formula_admitted", "feature_chain_admitted", "selection_enabled", "execution_admitted"))
                or item.get("share_terms_not_inferred_from_blank_provider_fields") is not True):
            raise ValueError("Component evidence cannot upgrade action/reference/feature admission")
        sources, implementations = {}, []
        clocks = [reviewed]
        for evidence in item["official_sources"]:
            if (evidence.get("official_primary_source") is not True
                    or evidence.get("historical_publication_archive_verified") is not False
                    or not evidence["source_url"].startswith("https://static.cninfo.com.cn/finalpage/")):
                raise ValueError("Explicit saved CNinfo primary documents and historical-clock limitations required")
            _, proof = source(evidence["source_file"], evidence["source_sha256"], evidence["source_url"], pdf=True)
            if evidence["retrieved_at"] != proof["retrieved_at"]:
                raise ValueError("Primary document knowledge clock differs from its receipt")
            clocks.append(_time(proof["retrieved_at"]))
            sources[evidence["source_url"]] = evidence
            if evidence["role"] == "implementation_notice":
                implementations.append(evidence)
        if len(implementations) != 1:
            raise ValueError("Each distribution needs one exact implementation notice")
        metadata = item["official_announcement_metadata"]
        raw_metadata, metadata_proof = source(metadata["file"], metadata["sha256"],
            "https://www.cninfo.com.cn/new/hisAnnouncement/query", pdf=False)
        page = json.loads(raw_metadata)
        matches = [row for row in page.get("announcements", [])
            if row.get("secCode") == symbol[:6] and row.get("announcementId") == metadata["announcement_id"]]
        if (page.get("hasMore") is not False or len(matches) != 1
                or "https://static.cninfo.com.cn/" + matches[0]["adjunctUrl"].lstrip("/") != implementations[0]["source_url"]):
            raise ValueError("Official metadata does not bind the exact issuer implementation PDF")
        clocks.append(_time(metadata_proof["retrieved_at"]))
        if _time(item["local_primary_evidence_available_at"]) != max(_time(value["retrieved_at"]) for value in sources.values()):
            raise ValueError("Local primary availability must retain the actual acquired-source maximum")
        fields, missing = {}, []
        for field in AMOUNT_FIELDS:
            value = _amount(item.get(field))
            if value is None:
                missing.append(field)
                fields[field] = {"status": "unknown", "value": item.get(field), "evidence_level": "unknown_not_zero"}
                continue
            if field == "dividCashPsBeforeTax":
                level, url = "implementation_notice", implementations[0]["source_url"]
            else:
                url = item.get("share_term_sources", {}).get(field)
                if url not in sources:
                    raise ValueError("A known share component requires its own preserved primary-source linkage")
                level = sources[url]["role"]
            fields[field] = {"status": "known", "value": item[field], "evidence_level": level, "source_url": url}
        cash, bonus, transfer = (_amount(item.get(field)) for field in AMOUNT_FIELDS)
        pay, listing = item.get("dividPayDate"), item.get("dividStockMarketDate")
        if cash is not None and cash > 0:
            if not pay:
                missing.append("dividPayDate")
            elif _day(pay) < ex:
                raise ValueError("Cash payment date cannot precede its ex date")
        if bonus is not None and transfer is not None and bonus + transfer > 0:
            if not listing:
                missing.append("dividStockMarketDate")
            elif _day(listing) < ex:
                raise ValueError("Share availability cannot precede its ex date")
        if bonus is not None and bonus > 0 and not (_amount(item.get("bonus_taxable_cash_ps")) or Decimal(0)) > 0:
            missing.append("bonus_taxable_cash_ps")
        item.update(available_at=max(clocks).isoformat(), component_fields=fields,
            component_status="incomplete_ordinary_components" if missing else "complete_ordinary_components",
            missing_components=missing, knowledge_mode=knowledge_mode, **FLAGS)
        events.append(item)
        if not missing:
            action = {"symbol": symbol, "dividRegistDate": record, "dividOperateDate": ex,
                **{field: item[field] for field in AMOUNT_FIELDS},
                "dividPayDate": pay or "", "dividStockMarketDate": listing or "",
                "source_review": str(path), "source_review_sha256": review_hash,
                "component_evidence_levels": {field: detail["evidence_level"] for field, detail in fields.items()},
                "available_at": item["available_at"], "knowledge_mode": knowledge_mode, **FLAGS}
            if bonus > 0:
                action["bonus_taxable_cash_ps"] = item["bonus_taxable_cash_ps"]
            actions.append(action)
    if (sorted(review["scope"]["symbols"]) != sorted(item["symbol"] for item in events)
            or not events or digest(path) != review_hash or digest(receipt_path) != receipt_hash):
        raise ValueError("The bounded review population or pinned result/receipt changed")
    binding = {"schema": BINDING_SCHEMA, "view_schema": SCHEMA, "method_id": METHOD_ID,
        "review_result": str(path), "review_result_sha256": review_hash,
        "retrieval_receipt": str(receipt_path), "retrieval_receipt_sha256": receipt_hash,
        "reviewed_at": review["reviewed_at"], "available_at": max(_time(item["available_at"]) for item in events).isoformat(),
        "source_proofs": inputs, "scope": deepcopy(review["scope"]),
        "context": context.to_dict(), "knowledge_mode": knowledge_mode,
        "dividend_tax_reserve": str(Decimal(str(config.dividend_tax_reserve))),
        "historical_knowledge_backfilled": False,
        "ordinary_component_ready_count": len(actions), "partial_event_count": len(events) - len(actions), **FLAGS}
    return ResearchCorporateActionView(binding, events, actions, context, _token=_TOKEN)
