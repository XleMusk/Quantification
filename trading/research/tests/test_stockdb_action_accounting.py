"""Synthetic pinned evidence and tiny shared-Engine accounts, not market results."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.config import StrategyConfig
from trading.contracts import RunContext, SignalUnavailable, TradingMode
from trading.engine import advance
from trading.ledger import Ledger, new_state
from trading.research.stockdb_action_accounting import load_action_accounting


SYMBOL = "600000.SSE"
D0, D1, D2, D3 = "2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25"
DAYS = [D0, D1, D2, D3]


def fixture_pair(tmp_path, *, reserve="0.45", record=D1, cash="0.20"):
    event, reference, source = [tmp_path / name for name in ("event", "reference", "source")]
    for folder in (event, reference, source):
        folder.mkdir(parents=True)
    def write(path, value):
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    source_result = {"status": "source_actions_complete", "raw_view": str(tmp_path / "raw"),
                     "raw_daily_sha256": "a" * 64}
    write(source / "result.json", source_result)
    write(event / "source_result.json", source_result)
    write(event / "price_identity.json", {"status": "derived_research_levels", "raw_view": source_result["raw_view"], "rename": None})
    pd.DataFrame([{"code": "sh.600000", "symbol": SYMBOL}]).to_csv(event / "source_input_universe.csv", index=False)
    pd.DataFrame({"date": DAYS}).to_csv(event / "source_input_calendar.csv", index=False)
    pd.DataFrame([{"code": "sh.600000", "symbol": SYMBOL, "year": "2026", "queried": True,
        "rows": "1", "empty_response": False, "source_file": "source.csv", "source_sha256": "b" * 64}]).to_csv(event / "input_coverage.csv", index=False)
    action = {"code": "sh.600000", "symbol": SYMBOL, "dividRegistDate": record,
              "dividOperateDate": D2, "dividPayDate": D2, "dividStockMarketDate": D3,
              "dividCashPsBeforeTax": cash, "dividStocksPs": "0", "dividReserveToStockPs": reserve}
    pd.DataFrame([action]).to_parquet(event / "canonical_actions.parquet", index=False)
    events = {"status": "research_action_events_projected", "source_status": source_result["status"],
        "execution_admitted": False, "source_view": str(source), "raw_view": source_result["raw_view"],
        "start": D0, "end": D3, "expected_codes": 1, "queried": 1, "unqueried": 0, "canonical_events": 1,
        "files": {p.name: sha256(p.read_bytes()).hexdigest() for p in event.iterdir()}}
    write(event / "result.json", events)
    (reference / "input_events.json").write_bytes((event / "result.json").read_bytes())
    write(reference / "input_prices.json", {"status": "research_reconstruction_completed", "raw_view": source_result["raw_view"],
        "source_hashes": {"raw": "a" * 64}, "canonical_panel_sha256": "c" * 64})
    for name in ("reviewed_special_references.json", "cash_reference_evidence.json", "distribution_reference_evidence.json"):
        write(reference / name, [])
    refs = {"status": "research_reference_presence_diagnostic", "execution_admitted": False,
        "event_view": str(event), "raw_daily_sha256": "a" * 64, "canonical_panel_sha256": "c" * 64,
        "files": {p.name: sha256(p.read_bytes()).hexdigest() for p in reference.iterdir()}}
    write(reference / "result.json", refs)
    return event, reference


def load(pair):
    return load_action_accounting(*pair, symbols=[SYMBOL], raw_days=DAYS, replay_days=[D1, D2])


def state():
    saved = new_state(1000)
    saved["positions"] = {SYMBOL: [{"qty": 300, "available": D0}]}
    saved["prices"] = {SYMBOL: 1000}
    return saved


def quotes(preclose=6.758620689655173):
    return {D0: {SYMBOL: {"close": 10.0}}, D1: {SYMBOL: {
        "open": 10., "high": 10., "low": 10., "close": 10., "preclose": 10., "tradestatus": 1, "isST": 0}},
        D2: {SYMBOL: {"open": 0., "high": 0., "low": 0., "close": 6.76, "preclose": preclose,
                         "tradestatus": 0, "isST": 0}}}


def feed(overlay, rows):
    class SyntheticFeed(SimpleNamespace):
        def signal_frame(self, *args):
            return SignalUnavailable("synthetic_no_new_targets")
    return SyntheticFeed(actions=overlay.actions, manifest={"replay_only": True, "start": D1, "end": D2,
        "historical_price_adjustments": overlay.special_references}, rows=rows,
        validate_session=lambda saved, day: overlay.validate(saved, day, rows),
        calendar=DAYS, instruments={SYMBOL: {"ipoDate": "2000-01-01", "outDate": ""}},
        instrument_profiles={})


def test_pinned_cash_reserve_and_halt_use_real_engine_300_to_435(tmp_path):
    overlay = load(fixture_pair(tmp_path))
    saved, config = state(), StrategyConfig()
    context = RunContext("synthetic-action-components", TradingMode.REPLAY)
    source = feed(overlay, quotes())
    advance(saved, source, D1, config, 0, context=context)
    assert saved["entitlements"] == {SYMBOL + ":" + D2: 300}
    advance(saved, source, D2, config, 1, context=context)
    ledger = Ledger(saved, config, context)
    assert ledger.quantity(SYMBOL) == 435 and ledger.available(SYMBOL, D2) == 300
    assert saved["cash"] == 104800 and saved["receivables"] == {}
    assert saved["fills"] == {} and saved["prices"][SYMBOL] == 676
    assert overlay.checks[-1]["reference_formula_status"] == "reference_match"
    assert overlay.checks[-1]["old_native_reference_label_used"] is False
    count = len(overlay.checks)
    overlay.validate(saved, D2, quotes())
    assert len(overlay.checks) == count
    assert overlay.provenance["returns_reportable"] is False


@pytest.mark.parametrize("fault, message", [("blank", "Unresolved held corporate action"),
    ("entitlement", "Missing held action entitlement"), ("mismatch", "reference_mismatch"),
    ("previous", "Missing exact preceding"), ("record", "Unknown held action registration")])
def test_unknown_components_stop_before_state_mutation(tmp_path, fault, message):
    overlay = load(fixture_pair(tmp_path, reserve="" if fault == "blank" else "0.45", record="" if fault == "record" else D1))
    saved, rows = state(), quotes(6.8 if fault == "mismatch" else 6.758620689655173)
    saved["day"] = D1
    if fault != "entitlement":
        saved["entitlements"][SYMBOL + ":" + D2] = 300
    if fault == "previous":
        rows.pop(D1)
    before = deepcopy(saved)
    with pytest.raises(ValueError, match=message):
        advance(saved, feed(overlay, rows), D2, StrategyConfig(), 1,
                context=RunContext("synthetic-rejection", TradingMode.REPLAY))
    assert saved == before
    if fault in {"blank", "mismatch"}:
        assert overlay.checks[-1]["reference_formula_status"] in {"unresolved_blank_terms", "reference_mismatch"}


def test_explicit_zero_entitlement_and_before_inception_do_not_invent_old_rights(tmp_path):
    overlay = load(fixture_pair(tmp_path, cash="0", reserve="0"))
    saved = state()
    saved["entitlements"][SYMBOL + ":" + D2] = 0
    rows = quotes(10.)
    overlay.validate(saved, D2, rows)
    overlay = load(fixture_pair(tmp_path / "earlier", record=D0, cash="0", reserve="0"))
    saved["entitlements"] = {}
    overlay.validate(saved, D2, rows)
    assert saved["entitlements"] == {}


def test_loader_rejects_wrong_pair_and_changed_payload_without_reading_price_matrices(tmp_path):
    pair = fixture_pair(tmp_path)
    overlay = load(pair)
    assert overlay.actions[0]["dividReserveToStockPs"] == "0.45"
    assert not (tmp_path / "raw").exists()
    (pair[0] / "input_coverage.csv").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="Source hash changed: input_coverage"):
        load(pair)
    second = fixture_pair(tmp_path / "other")
    result = json.loads((second[1] / "result.json").read_text(encoding="utf-8"))
    result["event_view"] = str(pair[0])
    (second[1] / "result.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="pair required"):
        load(second)
