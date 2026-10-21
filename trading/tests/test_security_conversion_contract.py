from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

from trading.config import StrategyConfig
from trading.contracts import MARKET_TZ, RunContext, TradingMode
from trading.ledger import new_state
from trading.security_conversion import (ConversionCredit, SecurityConversionReceipt,
    SecurityConversionView, validate_view)
from trading import security_conversion as conversion


CONTEXT = RunContext("conversion-contract-test", TradingMode.REPLAY)
CONFIG = StrategyConfig()
CALENDAR = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]


@pytest.fixture(autouse=True)
def actual_clock(monkeypatch):
    monkeypatch.setattr(conversion, "_now", lambda: datetime(2024, 1, 5, 18, tzinfo=MARKET_TZ))


def receipt(tmp_path, **changes):
    source = tmp_path / "explicit_share_credit.txt"
    source.write_text("TEST ONLY: source lot 100 shares -> 125 credited shares; explicit cash 0", encoding="utf8")
    value = SecurityConversionReceipt(CONTEXT, "account-credit-1", "2024-01-03", "600000.SSE",
        "600001.SSE", 100, (ConversionCredit("fill-lot-1", 100, 125, "2024-01-04"),), 0,
        str(source), hashlib.sha256(source.read_bytes()).hexdigest(),
        "2024-01-02T18:00:00+08:00", "2024-01-02T18:01:00+08:00")
    return replace(value, **changes)


def view(*receipts, **changes):
    return replace(SecurityConversionView("fixture-receipts", CONTEXT, "dated_asof", tuple(receipts)), **changes)


def bind_empty(state, source_view):
    binding, catalog, due = validate_view(state, source_view, CALENDAR[0], CONFIG, CONTEXT, CALENDAR)
    assert due == ()
    state.update(security_conversion_binding=binding, security_conversion_catalog=catalog, day=CALENDAR[0])


def test_typed_receipt_roundtrip_preserves_explicit_zero_credits_cash_and_lot_dates(tmp_path):
    first = receipt(tmp_path, registered_quantity=101,
        credits=(ConversionCredit("fill-lot-1", 100, 125, "2024-01-04"),
                 ConversionCredit("fractional-lot-2", 1, 0, "2024-01-04")))
    raw = json.loads(json.dumps(first.to_dict()))
    assert SecurityConversionReceipt.from_dict(raw) == first
    assert SecurityConversionView.from_dict(view(first).to_dict()) == view(first)
    assert first.credited_quantity == 125 and raw["cash_cents"] == 0
    assert raw["credits"][1]["credited_quantity"] == 0
    first.validate_source()
    for changes in ({"cash_cents": None}, {"cash_cents": True}, {"registered_quantity": 102},
                    {"credits": (ConversionCredit("a", 101, 0, "2024-01-04"),)},
                    {"destination_symbol": first.source_symbol}, {"source_sha256": "short"},
                    {"recorded_at": "2024-01-02T18:01:00"}):
        with pytest.raises(ValueError):
            replace(first, **changes)


def test_first_empty_view_can_append_future_receipt_without_mutating_or_rewriting_prefix(tmp_path):
    state = new_state(100000)
    initial = deepcopy(state)
    bind_empty(state, view())
    assert initial["positions"] == state["positions"] and state["cash"] == initial["cash"]
    proof = receipt(tmp_path)
    before = deepcopy(state)
    binding, catalog, due = validate_view(state, view(proof), CALENDAR[1], CONFIG, CONTEXT, CALENDAR)
    assert state == before and due == (proof,)
    assert binding == state["security_conversion_binding"] and catalog == [proof.to_dict()]
    state.update(security_conversion_catalog=catalog, day=CALENDAR[1],
                 security_conversion_applied={proof.event_id: proof.to_dict()})
    restored = json.loads(json.dumps(state))
    assert validate_view(restored, view(proof), CALENDAR[2], CONFIG, CONTEXT, CALENDAR)[2] == ()
    assert restored == state
    for changed in (view(), view(replace(proof, cash_cents=1))):
        with pytest.raises(ValueError, match="prefix"):
            validate_view(state, changed, CALENDAR[2], CONFIG, CONTEXT, CALENDAR)


def test_binding_cannot_be_introduced_late_disabled_or_changed(tmp_path):
    state = new_state(100000)
    state["day"] = CALENDAR[0]
    with pytest.raises(ValueError, match="first session"):
        validate_view(state, view(), CALENDAR[1], CONFIG, CONTEXT, CALENDAR)
    state = new_state(100000)
    state["positions"] = {"600000.SSE": [{"qty": 100, "available": CALENDAR[0]}]}
    with pytest.raises(ValueError, match="new empty"):
        validate_view(state, view(), CALENDAR[0], CONFIG, CONTEXT, CALENDAR)
    state = new_state(100000)
    bind_empty(state, view())
    with pytest.raises(ValueError, match="disabled"):
        validate_view(state, None, CALENDAR[1], CONFIG, CONTEXT, CALENDAR)
    for changed in (view(registry_id="other"), view(knowledge_mode="posthoc_component_diagnostic")):
        with pytest.raises(ValueError, match="binding"):
            validate_view(state, changed, CALENDAR[1], CONFIG, CONTEXT, CALENDAR)
    with pytest.raises(ValueError, match="binding"):
        validate_view(state, view(), CALENDAR[1], replace(CONFIG, commission=.0005), CONTEXT, CALENDAR)
    with pytest.raises(ValueError, match="binding"):
        validate_view(state, view(), CALENDAR[1], CONFIG, CONTEXT, ["2023-12-29", *CALENDAR])
    assert validate_view(new_state(100000), None, CALENDAR[0], CONFIG, CONTEXT, CALENDAR) == (None, None, ())


def test_dated_receipt_clock_and_posthoc_scope_are_explicit(tmp_path, monkeypatch):
    proof = receipt(tmp_path, source_available_at="2024-01-03T10:00:00+08:00",
                    recorded_at="2024-01-03T10:01:00+08:00")
    state = new_state(100000)
    with pytest.raises(ValueError, match="opening"):
        validate_view(state, view(proof), CALENDAR[0], CONFIG, CONTEXT, CALENDAR)
    posthoc = view(proof, knowledge_mode="posthoc_component_diagnostic")
    binding, _, due = validate_view(state, posthoc, CALENDAR[0], CONFIG, CONTEXT, CALENDAR)
    assert due == ()
    assert binding["pit_verified"] is binding["returns_reportable"] is binding["execution_admitted"] is False
    monkeypatch.setattr(conversion, "_now", lambda: datetime(2024, 1, 3, 10, tzinfo=MARKET_TZ))
    with pytest.raises(ValueError, match="future"):
        validate_view(state, posthoc, CALENDAR[0], CONFIG, CONTEXT, CALENDAR)
    with pytest.raises(ValueError, match="source availability"):
        replace(proof, recorded_at="2024-01-03T09:59:00+08:00")


def test_changed_source_or_bad_applied_receipt_never_passes_silently(tmp_path):
    proof = receipt(tmp_path)
    state = new_state(100000)
    bind_empty(state, view(proof))
    before = deepcopy(state)
    Path(proof.source_file).write_text("changed", encoding="utf8")
    with pytest.raises(ValueError, match="SHA256"):
        validate_view(state, view(proof), CALENDAR[1], CONFIG, CONTEXT, CALENDAR)
    assert state == before
    proof = receipt(tmp_path)
    state.update(day=CALENDAR[1], security_conversion_applied={proof.event_id: {**proof.to_dict(), "cash_cents": 1}})
    with pytest.raises(ValueError, match="Applied conversion"):
        validate_view(state, view(proof), CALENDAR[2], CONFIG, CONTEXT, CALENDAR)


def test_late_append_skipped_event_calendar_and_overlap_are_not_repaired(tmp_path):
    proof = receipt(tmp_path)
    state = new_state(100000)
    bind_empty(state, view())
    state["day"] = CALENDAR[1]
    with pytest.raises(ValueError, match="already settled"):
        validate_view(state, view(proof), CALENDAR[2], CONFIG, CONTEXT, CALENDAR)
    state = new_state(100000)
    with pytest.raises(ValueError, match="skipped its exact"):
        validate_view(state, view(proof), CALENDAR[2], CONFIG, CONTEXT, CALENDAR)
    bind_empty(state, view(proof))
    state["day"] = CALENDAR[1]
    with pytest.raises(ValueError, match="skipped its exact"):
        validate_view(state, view(proof), CALENDAR[1], CONFIG, CONTEXT, CALENDAR)
    state = new_state(100000)
    off_calendar = replace(proof, credits=(ConversionCredit("fill-lot-1", 100, 125, "2024-01-06"),))
    with pytest.raises(ValueError, match="exact calendar"):
        validate_view(state, view(off_calendar), CALENDAR[0], CONFIG, CONTEXT, CALENDAR)
    with pytest.raises(ValueError, match="Duplicate conversion"):
        view(proof, proof)
    second = replace(proof, event_id="account-credit-2", source_symbol=proof.destination_symbol,
                     destination_symbol="600002.SSE")
    with pytest.raises(ValueError, match="Overlapping"):
        view(proof, second)
