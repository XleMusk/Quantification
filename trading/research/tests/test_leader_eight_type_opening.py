"""Synthetic bridge evidence, using existing opening and THS order arithmetic."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trading.config import StrategyConfig
from trading.ledger import new_state
from trading.research import leader_eight_type_opening as bridge
from trading.research import opening_pack
from trading.ths import handoff, saved_intent as consumer

DAY, EXECUTION = "2026-10-16", "2026-10-19"
NOW = datetime.fromisoformat("2026-10-17T02:00:00+08:00")
LATER = datetime.fromisoformat("2026-10-19T09:35:00+08:00")
A, B = "space_height_leader__unguarded__causal_first_seen_cap20", "space_height_leader__two_close_envguard__causal_first_seen_cap20"
SYMBOL = "002343.SZSE"


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def saved(tmp_path, monkeypatch):
    from trading.research import saved_leader_account_intent as source
    root = tmp_path/"source"
    root.mkdir()
    config = StrategyConfig()
    state = new_state(100000)
    state.update(day=DAY, cash=9_900_000, prices={SYMBOL: 1000},
        positions={SYMBOL: [{"qty": 100, "available": EXECUTION}]})
    cases = {k: {"state": deepcopy(state), "daily": [{"date": DAY, "equity": 100000}]} for k in (A, B)}
    raw = root/"raw.parquet"
    pq.write_table(pa.Table.from_pylist([{"date": DAY, "code": "sz.002343", "close": "10.00",
        "tradestatus": "1", "isST": "0"}]), raw)
    manifest = root/"manifest.json"
    write(manifest, {"raw_file": str(raw), "raw_sha256": sha256(raw.read_bytes()).hexdigest()})
    pub = {"day": DAY, "input_manifest": str(manifest),
           "input_manifest_sha256": sha256(manifest.read_bytes()).hexdigest()}
    hp = root/"head.json"
    write(hp, {"cases": cases, "publications": [pub]})
    intents = {k: {"schema": "saved_leader_eight_type_account_intent_v1",
        "strategy_id": k, "case_id": k, "run_directory": str(root),
        "source_day": DAY, "execution_day": EXECUTION,
        "kind": "NO_CHANGE", "weights": None, "remaining_exit_symbols": [SYMBOL] if k == A else [],
        "execution_config": config.to_dict(), "refs": {"head": {"path": str(hp),
            "sha256": sha256(hp.read_bytes()).hexdigest()}}, **bridge.FLAGS} for k in (A, B)}
    monkeypatch.setattr(source, "export_requests", lambda *args, **kwargs: deepcopy(intents))
    monkeypatch.setattr(consumer, "read_request", lambda value, **kwargs: deepcopy(value))
    monkeypatch.setattr(opening_pack, "ROOT", tmp_path/"repo")
    monkeypatch.setattr(opening_pack, "DATA_ROOT", tmp_path/"data")
    return root, hp, intents, tmp_path


def test_saved_exit_reaches_existing_pack_and_observed_account_without_advancing_books(saved):
    root, hp, intents, tmp_path = saved
    before = hp.read_bytes()
    directory, result = bridge.build(root, EXECUTION, tmp_path/"out", current=NOW)
    assert hp.read_bytes() == before and result["preview_count"] == 2
    pack = json.loads(open(result["opening_pack"], encoding="utf-8").read())
    assert pack["candidate_order_count"] == 1 and pack["strategy_count"] == 2
    assert pack["automatic_strategy_selection"] is False
    assert pack["ledger_preflight_by_strategy"][A]["submitted"] == 1
    assert pack["ledger_preflight_by_strategy"][B]["submitted"] == 0
    state = json.loads((directory/A/"account.json").read_text(encoding="utf-8"))
    assert state["orders"] == {} and state["cash"] == 9_900_000
    snapshot = {"kind": "account", "source": "ths_user_export", "account_alias": "synthetic-only",
        "trading_date": EXECUTION, "observed_at": LATER.isoformat(), "cash_available": 10000000,
        "cash_total": 10000000, "cash_frozen": 0, "cash_basis": "cash_balance_includes_frozen",
        "positions": {SYMBOL: {"quantity": 900, "sellable": 900}}, "marks": {SYMBOL: 1000}}
    quote = {"schema": "ths_raw_quote_observation_v1", "source": "ths_controls",
        "account_alias": "synthetic-only", "trading_date": EXECUTION,
        "observed_at": LATER.isoformat(), "price_basis": "raw_rmb",
        "prices": {SYMBOL: {"price_rmb": "10.00", "preclose_rmb": "10.00", "is_st": False, "tradable": True}}}
    value = handoff.prepare_opening_pack(pack, snapshot, quote, strategy_id=A,
        current=LATER, sizing_mode="saved_intent")
    plan = value["candidates"][0]["plan"]
    assert value["ready_count"] == 0 and value["status"] == "research_diagnostic_only"
    assert plan["next_plan"] is None and plan["prefill_allowed"] is False
    assert plan["adjustments"][0]["delta_qty"] == -900
    assert plan["adjustments"][0]["target_qty"] == 0
    assert hp.read_bytes() == before


def test_blocked_case_is_retained_without_fabricating_nochange(saved, monkeypatch):
    from trading.research import saved_leader_account_intent as source
    root, hp, intents, tmp_path = saved
    intents[B] = {"schema": "saved_leader_eight_type_account_intent_not_ready_v1",
        "status": "not_ready", "case_id": B, "reason": "saved_case_blocked", **bridge.FLAGS}
    monkeypatch.setattr(source, "export_requests", lambda *args, **kwargs: deepcopy(intents))
    _, result = bridge.build(root, EXECUTION, tmp_path/"out", current=NOW)
    assert result["case_count"] == 2 and result["preview_count"] == result["not_ready_count"] == 1
    assert result["cases"][1]["reason"] == "saved_case_blocked"
    assert "trade_status" not in result["cases"][1]


def test_source_drift_is_rejected_before_outputs(saved, monkeypatch):
    from trading.research import saved_leader_account_intent as source
    root, hp, intents, tmp_path = saved
    intents[A]["refs"]["head"]["sha256"] = "changed"
    monkeypatch.setattr(source, "export_requests", lambda *args, **kwargs: deepcopy(intents))
    with pytest.raises(ValueError, match="changed after intent"):
        bridge.build(root, EXECUTION, tmp_path/"out", current=NOW)
    assert not (tmp_path/"out").exists()


def test_missing_head_retains_unavailable_cases_without_creating_accounts(saved, monkeypatch):
    from trading.research import saved_leader_account_intent as source
    root, hp, intents, tmp_path = saved
    hp.unlink()
    unavailable = {key: {"case_id": key, "status": "not_ready", "reason": "no_saved_eight_type_research_head"}
                   for key in intents}
    monkeypatch.setattr(source, "export_requests", lambda *args, **kwargs: unavailable)
    directory, result = bridge.build(root, EXECUTION, tmp_path/"out", current=NOW)
    assert result["status"] == "not_ready" and result["not_ready_count"] == 2
    assert result["preview_count"] == 0 and result["opening_pack"] is None
    assert not (directory/A).exists() and not hp.exists()


@pytest.mark.parametrize("family", ["leader20", "alpha158"])
def test_published_family_preserves_strategy_identity_with_windows_safe_case_directories(saved, monkeypatch, family):
    from trading.research import saved_published_account_intent as source
    root, hp, intents, tmp_path = saved
    head = json.loads(hp.read_text(encoding="utf-8"))
    names = {A: "pool:stage1:exit", B: "pool:stage2:exit"}
    head["cases"] = {names[k]: v for k, v in head["cases"].items()}
    write(hp, head)
    exports = {}
    for key, intent in intents.items():
        intent.update(case_id=names[key], strategy_id=("alpha158:" if family == "alpha158" else "")+names[key],
                      source_family=family)
        intent["refs"]["head"]["sha256"] = sha256(hp.read_bytes()).hexdigest()
        exports[names[key]] = intent
    monkeypatch.setattr(source, "export_requests", lambda *a, **kw: deepcopy(exports))
    monkeypatch.setattr(bridge, "_source_quotes", lambda pub, day, wanted, source_family: bridge._quotes(pub, day, wanted))
    directory, result = bridge.build(root, EXECUTION, tmp_path/"out", current=NOW, source_family=family)
    assert result["source_family"] == family and result["preview_count"] == 2
    first = result["cases"][0]
    assert first["case_id"] == names[A] and ":" not in Path(first["preview"]).parent.name
    preview = json.loads(open(first["preview"], encoding="utf-8").read())
    expected = exports[names[A]]["strategy_id"]
    assert preview["strategy_id"] == expected
    pack = json.loads(open(result["opening_pack"], encoding="utf-8").read())
    assert pack["strategies"][0]["strategy_id"] == expected
    assert pack["ledger_preflight_by_strategy"][expected]["submitted"] == 1


def test_leader20_quotes_use_exact_own_bundle_and_decoded_dated_rows(saved):
    from trading.research.leader_published_inputs import SCHEMA as INPUT_SCHEMA
    root, _, _, _ = saved
    bundle = root/"bundle.json"
    raw = root/"twenty.parquet"
    pq.write_table(pa.Table.from_pylist([{"date": DAY, "code": "002343", "symbol": SYMBOL,
        "close": json.dumps("10.00"), "tradestatus": "1", "isST": "0"},
        {"date": "2026-10-15", "code": "002343", "symbol": SYMBOL,
         "close": json.dumps(99.0), "tradestatus": "1", "isST": "0"}]), raw)
    write(bundle, {"schema": INPUT_SCHEMA, "source_days_complete": ["2026-10-15", DAY],
        "recorded_at": DAY+"T18:00:00+08:00", "origin": "synthetic_own_input",
        "raw_bars_file": str(raw), "raw_bars_sha256": sha256(raw.read_bytes()).hexdigest(),
        "json_encoded_object_columns": ["close"]})
    proof = {"file": str(bundle), "sha256": sha256(bundle.read_bytes()).hexdigest(),
             "recorded_at": DAY+"T18:00:00+08:00", "origin": "synthetic_own_input"}
    quotes = bridge._source_quotes({"publication_input_bundle": proof}, DAY, {SYMBOL}, "leader20")
    assert quotes[SYMBOL]["close_cents"] == 1000
    bundle.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="input bundle changed"):
        bridge._source_quotes({"publication_input_bundle": proof}, DAY, {SYMBOL}, "leader20")


def test_alpha_quotes_pin_provider_raw_and_do_not_borrow_a_previous_close(saved):
    root, _, _, _ = saved
    provider = root/"provider"
    provider.mkdir()
    raw = provider/"raw.parquet"
    pq.write_table(pa.Table.from_pylist([{"date": DAY, "code": "sz.002343", "close": 10.0, "tradestatus": 1, "isST": 0},
        {"date": "2026-10-15", "code": "sz.000002", "close": 20.0, "tradestatus": 1, "isST": 0}]), raw)
    rawhash = sha256(raw.read_bytes()).hexdigest()
    p = provider/"result.json"
    write(p, {"recent_raw_observations_file": str(raw), "recent_raw_observations_sha256": rawhash})
    pub = root/"alpha.json"
    write(pub, {"source": {"provider_view": str(provider), "provider_result_sha256": sha256(p.read_bytes()).hexdigest(),
                           "raw_observations_sha256": rawhash}})
    reference = {"file": str(pub), "sha256": sha256(pub.read_bytes()).hexdigest()}
    quotes = bridge._source_quotes(reference, DAY, {SYMBOL, "000002.SZSE"}, "alpha158")
    assert list(quotes) == [SYMBOL] and quotes[SYMBOL]["close_cents"] == 1000
    raw.write_bytes(raw.read_bytes()+b" ")
    with pytest.raises(ValueError, match="raw price reference changed"):
        bridge._source_quotes(reference, DAY, {SYMBOL}, "alpha158")
    pub.write_bytes(pub.read_bytes()+b" ")
    with pytest.raises(ValueError, match="target bytes changed"):
        bridge._source_quotes(reference, DAY, {SYMBOL}, "alpha158")


def test_new_pack_seals_optional_same_export_holding_review_without_intent_changes(saved, monkeypatch):
    from trading.research import saved_leader_account_intent as source
    root, hp, intents, tmp_path = saved
    original = deepcopy(intents)
    def export(*args, holding_reviews, **kwargs):
        for key, intent in intents.items():
            holding_reviews[key] = {"schema": "saved_leader_holding_review_v1", "strategy_id": key,
                "source_family": "eight_type", "source_day": DAY, "execution_day": EXECUTION,
                "refs": deepcopy(intent["refs"]), "saved_position_review": {"case_id": key,
                    "positions": [{"symbol": SYMBOL, "quantity_shares": 100,
                        "anchor": {"status": "unknown"}, "assessment": None,
                        "queued_risk_exit": {"execution_on": EXECUTION} if key == A else None}],
                    "latest_order_blocks": []}, **bridge.FLAGS}
        return deepcopy(intents)
    monkeypatch.setattr(source, "export_requests", export)
    before = hp.read_bytes()
    directory, result = bridge.build(root, EXECUTION, tmp_path/"out", current=NOW)
    pack_path = Path(result["opening_pack"])
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    reference = pack["holding_review_ref"]
    path = Path(reference["path"])
    assert path.name == "holding_review.json" and path.parent.parent.name == "opening_pack"
    assert reference["sha256"] == sha256(path.read_bytes()).hexdigest()
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    assert [r["strategy_id"] for r in sidecar["case_reviews"]] == [A, B]
    assert (path.parent/"opening_pack.json").read_bytes() == pack_path.read_bytes()
    assert intents == original and hp.read_bytes() == before
    for key, intent in original.items():
        assert json.loads((directory/key/"intent.json").read_text(encoding="utf-8")) == intent
