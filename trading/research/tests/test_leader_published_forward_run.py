"""Synthetic saved publications; actual Engine/Ledger accounting is exercised."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading import engine
from trading.contracts import IntentKind
from trading.research import leader_close_publication as publisher
from trading.research import leader_published_forward_run as m
from trading.research import leader_published_inputs as inputs
from trading.research.baostock_daily_slice import digest
from trading.research.leader_daily_replay import freeze_cohorts
from trading.research.tests.test_leader_forward_run import A, B, DAYS, bundle, protocol


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    frozen = protocol(tmp_path)
    binding, calendar, config = m._protocol(frozen)
    clock = {"value": datetime.fromisoformat("2026-10-15T18:45:00+08:00")}
    for module in (m, publisher, inputs):
        monkeypatch.setattr(module, "_now", lambda: clock["value"])
    monkeypatch.setattr(engine, "_published_now", lambda: clock["value"])
    def source(count, *, all_candidates=False):
        value = bundle(count)
        if all_candidates:
            for batches in value["batches"].values():
                for day, row in batches.items():
                    row.update(status="complete", candidates=[{"symbol": A, "score": 1.}])
        value["source_packages"] = [{"day": day, "folder": "synthetic/" + day,
            "raw_daily_sha256": "synthetic-raw-" + day,
            "terminal_completion_observed_at": day + "T18:30:00+08:00"}
            for day in value["source_days_complete"]]
        return value
    first = source(1)
    publisher.publish_close_targets(first, tmp_path / "pub15", protocol_path=frozen)
    pub15 = tmp_path / "pub15/publication.json"
    clock["value"] = datetime.fromisoformat("2026-10-15T22:00:00+08:00")
    own15 = inputs.save_bundle(first, tmp_path / "input15", origin="synthetic_late_input_validation_not_original_attachment")
    registration = tmp_path / "registration.json"
    registration.write_text(json.dumps({"schema": "leader_published_close_forward_registration_v1",
        "registered_at": "2026-10-15T22:10:00+08:00", "first_decision_day": DAYS[0],
        "first_execution_day": DAYS[1], "first_execution_deadline": DAYS[1] + "T09:30:00+08:00",
        "original_publication": {"file": str(pub15), "sha256": digest(pub15), "recorded_at": read(pub15)["recorded_at"]},
        "initial_publication_input_bundle": inputs.input_proof(own15),
        "protocol_binding": binding, "cohort": freeze_cohorts(first["raw_bars"], DAYS[0]),
        "case_ids": list(m.CASE_IDS), "config": config.to_dict(),
        "window": {"length_open_sessions": 40, "execution_tail_sessions": 6},
        "published_target_policy_id": m.TARGET_POLICY_ID, "risk_policy_id": m.RISK_POLICY_ID,
        "risk_rule_spec": m.RISK_RULE_SPEC, "registration_is_original_publication": False,
        "synthetic_only": True, **m.FLAGS}), encoding="utf-8")
    pubs, own = [pub15], [own15]
    for count in range(2, 8):
        value, day = source(count), DAYS[count-1]
        clock["value"] = datetime.fromisoformat(day + "T18:45:00+08:00")
        output = tmp_path / ("pub" + day)
        publisher.publish_close_targets(value, output, protocol_path=frozen, cohort_registration=registration)
        pubs.append(output / "publication.json")
        own.append(output / "input_bundle/bundle.json")
    clock["value"] = datetime.fromisoformat("2026-10-30T20:00:00+08:00")
    latest15 = own15
    latest = {}
    for count in range(2, 8):
        path = inputs.save_bundle(source(count), tmp_path / ("latest" + str(count)), origin="synthetic_current_execution_source")
        latest[count] = path
    return {"tmp": tmp_path, "protocol": frozen, "registration": registration,
        "pubs": pubs, "own": own, "latest": latest, "latest15": latest15,
        "source": source, "clock": clock, "run": tmp_path / "research_books"}


def execute(e, *, count=1, raw_count=2, through=None):
    return m.run(e["pubs"][:count], e["own"][:count],
        e["latest15"] if raw_count == 1 else e["latest"][raw_count], e["run"],
        protocol_path=e["protocol"], registration=e["registration"], through=through)


def head(e):
    return read(e["run"] / "head.json")


def test_unique_at_most_five_targets_equal_for_empty_and_mixed_holdings():
    config = m._protocol.__globals__["_config"](100000, 5)
    candidates = [{"symbol": f"60000{i}.SSE", "score": 10-i} for i in range(1, 6)]
    from trading.engine import choose_targets
    assert config.holdings == 5 and config.retain_rank == 20
    assert choose_targets(candidates, [], config) == choose_targets(candidates,
        ["600009.SSE", "600004.SSE", "600002.SSE"], config)
    assert len(choose_targets(candidates, [], config)) == 5


def test_only15_waits_before_directory_state_lock_or_engine(evidence, monkeypatch):
    def forbidden(*a, **kw): pytest.fail("Initial exact E witness must precede every mutation")
    for name in ("new_state", "lock", "advance"):
        monkeypatch.setattr(m, name, forbidden)
    result = execute(evidence, raw_count=1)
    assert result["status"] == "waiting_for_exact_successor_complete_source_close"
    assert result["required_successor_day"] == "2026-10-16" and result["metrics"] is None
    assert not evidence["run"].exists()


def test_raw16_no_pub16_only_queues_original_E16_and_keeps_all20(evidence):
    result = execute(evidence, through="2026-10-19")
    assert result["newly_committed_case_sessions"] == 20
    assert result["next_real_close_publication_days_required"] == [DAYS[1]]
    saved = head(evidence)
    assert list(saved["cases"]) == list(m.CASE_IDS)
    for key, case in saved["cases"].items():
        assert [r["date"] for r in case["daily"]] == [DAYS[0]]
        original, bound = case["journal"][0]["unbound_original_intent"], case["journal"][0]["bound_original_intent"]
        assert original["weights"] == bound["weights"] and original["kind"] == bound["kind"]
        assert bound["execution_not_before"] == DAYS[1]
        assert not case["state"]["fills"]
        if key.endswith(m.EXIT_POLICIES[1]):
            assert original["kind"] == "NO_CHANGE" and original["execution_not_before"] is None
            assert case["state"].get("pending_targets") is None
    before = (evidence["run"] / "head.json").read_bytes()
    assert execute(evidence)["newly_committed_case_sessions"] == 0
    assert (evidence["run"] / "head.json").read_bytes() == before


def test_two_publications_actual_opening_fills_and_NO_CHANGE_not_cash_without19raw(evidence, monkeypatch):
    execute(evidence)
    monkeypatch.setattr(m, "new_state", lambda *a: pytest.fail("No account reset"))
    result = execute(evidence, count=2)
    assert result["newly_committed_case_sessions"] == 20
    for key, case in head(evidence)["cases"].items():
        assert [r["date"] for r in case["daily"]] == DAYS[:2]
        assert case["journal"][-1]["bound_original_intent"]["kind"] == "NO_CHANGE"
        assert case["journal"][-1]["published_close_reduction"] is None
        if key.endswith(m.EXIT_POLICIES[0]):
            assert len(case["state"]["orders"]) == 1
            assert {f["day"] for f in case["state"]["fills"].values()} == {DAYS[1]}
            assert sum(l["qty"] for l in case["state"]["positions"][A]) > 0
        else:
            assert not case["state"]["orders"]
    assert result["metrics"] is None


def test_fifth_holding_close_queues_exact_next_session_without_future_raw(evidence):
    result = execute(evidence, count=6, raw_count=6)
    assert result["newly_committed_case_sessions"] == 120
    saved = head(evidence)
    for key, case in saved["cases"].items():
        if key.endswith(m.EXIT_POLICIES[0]):
            assert case["state"]["pending_risk_exits"]["execution_on"] == DAYS[6]
            assert case["state"]["pending_risk_exits"]["symbols"] == [A]
            assert {f["side"] for f in case["state"]["fills"].values()} == {"buy"}
    result = execute(evidence, count=7, raw_count=7)
    assert result["newly_committed_case_sessions"] == 20
    for key, case in head(evidence)["cases"].items():
        if key.endswith(m.EXIT_POLICIES[0]):
            assert not any(case["state"]["positions"].values())
            assert {f["day"] for f in case["state"]["fills"].values() if f["side"] == "sell"} == {DAYS[6]}


def test_FULL_TARGET_expiry_is_post_opening_reduction_not_original_weight_rewrite(evidence):
    value, day = evidence["source"](6), DAYS[5]
    for batches in value["batches"].values():
        batches[day].update(status="complete", candidates=[{"symbol": A, "score": 1.}])
    evidence["clock"]["value"] = datetime.fromisoformat(day + "T18:45:00+08:00")
    output = evidence["tmp"] / "pub22_full"
    publisher.publish_close_targets(value, output, protocol_path=evidence["protocol"], cohort_registration=evidence["registration"])
    evidence["pubs"][5], evidence["own"][5] = output / "publication.json", output / "input_bundle/bundle.json"
    evidence["clock"]["value"] = datetime.fromisoformat("2026-10-30T20:00:00+08:00")
    evidence["latest"][6] = inputs.save_bundle(value, evidence["tmp"] / "latest22_full", origin="synthetic_current_execution_source")
    execute(evidence, count=6, raw_count=6)
    case = head(evidence)["cases"][m.CASE_IDS[0]]
    receipt = case["journal"][-1]["published_close_reduction"]
    assert receipt["original_intent"]["weights"] == {A: .16}
    assert receipt["reduced_intent"]["weights"] == {}
    assert case["state"]["pending_targets"] == {}
    assert read(evidence["pubs"][5])["cases"][0]["target_weights"] == {A: .16}
    assert case["state"]["pending_risk_exits"]["execution_on"] == DAYS[6]


def test_registered_tail_publication_is_real_NO_CHANGE_and_not_new_entry(evidence):
    binding, calendar, config = m._protocol(evidence["protocol"])
    cohort = read(evidence["registration"])["cohort"]
    window = m._window_calendar(calendar, DAYS[0])
    value = evidence["source"](7)
    for day in window[7:41]:
        last = value["source_days_complete"][-1]
        new = value["raw_bars"].loc[value["raw_bars"].date.eq(last)].copy()
        new["date"] = day
        value["raw_bars"] = pd.concat([value["raw_bars"], new], ignore_index=True)
        value["source_days_complete"].append(day)
        value["provider_inventory_by_day"][day] = {**deepcopy(value["provider_inventory_by_day"][last]), "day": day}
        for batches in value["batches"].values():
            batches[day] = {**deepcopy(batches[last]), "day": day, "status": "complete",
                "candidates": [{"symbol": A, "score": 1.}]}
        value["source_packages"].append({"day": day, "folder": "synthetic/" + day,
            "raw_daily_sha256": "synthetic-raw-" + day, "terminal_completion_observed_at": day + "T18:30:00+08:00"})
    day = window[40]
    evidence["clock"]["value"] = datetime.fromisoformat(day + "T18:45:00+08:00")
    output = evidence["tmp"] / "pub_tail"
    publisher.publish_close_targets(value, output, protocol_path=evidence["protocol"], cohort_registration=evidence["registration"])
    now = evidence["clock"]["value"]
    own = m._load(output / "input_bundle/bundle.json", binding, calendar, now)
    _, registered = m._registration(evidence["registration"], evidence["pubs"][0],
        m._load(evidence["own"][0], binding, calendar, now), binding, now)
    pub = m._publication(output / "publication.json", own, cohort=cohort, registration=registered,
        binding=binding, calendar=own["sessions"], config=config, qualification=m._ipo_proof(binding),
        window=window, now=now, first=False)
    assert pub["tail"] is True
    assert all(intent.kind == IntentKind.NO_CHANGE and intent.reason == "registered_forward_tail_no_new_decision"
        for intent in pub["intents"].values())


def test_actual_provider_three_history_packages_are_not_extra_decision_days(evidence):
    """Real producer shape: initial requested [15], all source packages [12..15]."""
    from trading.research.leader_forward_run import RECENT_SOURCE_METHOD
    binding, calendar, _ = m._protocol(evidence["protocol"])
    view_file = evidence["tmp"] / "synthetic_recent_view.json"
    view_file.write_text("{}", encoding="utf-8")
    prefix_days = ["2026-10-12", "2026-10-13", "2026-10-14"]
    def proof(day):
        return {"day": day, "source_folder": "synthetic/" + day,
            "raw_daily_sha256": "synthetic-raw-" + day, "roster_raw_sha256": "synthetic-roster-" + day,
            "read_only_verified": True, "source_inventory_complete": True,
            "full_dated_roster_inventory_complete": True,
            "receipt_status": "complete_full_dated_close_inventory_not_admitted", "calendar_sha256": "synthetic-calendar"}
    view = {"method_id": RECENT_SOURCE_METHOD, "view_file": str(view_file), "view_sha256": digest(view_file),
        "shared_view_id": "synthetic-shared", "base_history_end": "2026-10-09",
        "available_at": "2026-10-15T11:00:00+08:00", "original_history_manifest_sha256": "synthetic-native",
        "calendar_sha256": "synthetic-calendar", "view_sessions": prefix_days,
        "source_proofs": [proof(d) for d in prefix_days]}
    binding["history_source_view"] = view
    loaded = []
    for count in (1, 2):
        value = evidence["source"](count)
        value["source_days_complete"] = DAYS[:count]
        for day in prefix_days[:2]:
            rows = value["raw_bars"].loc[value["raw_bars"].date.eq("2026-10-14")].copy()
            rows["date"] = day
            value["raw_bars"] = pd.concat([value["raw_bars"], rows], ignore_index=True)
        value["source_packages"] = [{"day": d, "folder": "synthetic/" + d,
            "raw_daily_sha256": "synthetic-raw-" + d,
            "terminal_completion_observed_at": d + "T18:30:00+08:00",
            "roles": (["recent_history_prefix"] if d in prefix_days else [])
                + (["requested_current"] if d in DAYS[:count] else [])}
            for d in prefix_days + DAYS[:count]]
        value["recent_source_history"] = deepcopy(view)
        for family, batches in value["batches"].items():
            value["batches"][family] = {d: row for d, row in batches.items() if d in DAYS[:count]}
            for day, row in value["batches"][family].items():
                used = prefix_days + [d for d in DAYS[:count] if d <= day]
                row["recent_source_history"] = {k: deepcopy(view[k]) for k in
                    ("method_id", "shared_view_id", "view_file", "view_sha256", "base_history_end", "available_at", "original_history_manifest_sha256")}
                row["recent_source_history"].update(source_days_used=used, source_proofs=[proof(d) for d in used])
        path = inputs.save_bundle(value, evidence["tmp"] / ("real_shape_" + str(count)), origin="synthetic_real_provider_shape")
        loaded.append(m._load(path, binding, calendar, evidence["clock"]["value"]))
    assert loaded[0]["days"] == [DAYS[0]]
    assert [p["day"] for p in loaded[0]["bundle"]["source_packages"]] == prefix_days + [DAYS[0]]
    cohort = read(evidence["registration"])["cohort"]
    m._lineage(loaded[0], loaded[1], cohort, "2026-12-31")
    loaded[1]["bundle"]["source_packages"][0]["terminal_completion_observed_at"] = "2026-10-12T19:00:00+08:00"
    with pytest.raises(ValueError, match="prefix changed"):
        m._lineage(loaded[0], loaded[1], cohort, "2026-12-31")


def test_complete_summary_uses_existing_metrics_preserves_losses_and_residual_pending():
    window = pd.bdate_range("2026-01-05", periods=47).strftime("%Y-%m-%d").tolist()
    daily = [{"date": day, "equity": 100000 - (i+1)*100, "fees": 1., "turnover_value": 100.,
        "stale_symbols": [], "batch_status": "complete", "original_publication_kind": "FULL_TARGET" if i == 0 else "NO_CHANGE",
        "registered_tail_no_new_decision": i >= 40} for i, day in enumerate(window[:46])]
    case = {"daily": daily, "verified_breadth_seen": False,
        "state": {"day": window[45], "orders": {"one": {}}, "fills": {"one": {}},
            "positions": {A: [{"qty": 100}]}, "pending_targets": {},
            "pending_risk_exits": {"execution_on": window[46], "symbols": [A]}}}
    row = m._case_summary(m.CASE_IDS[0], case, window, 100000)
    assert row["performance_available"] and row["decision_window_metrics"]["sessions"] == 40
    assert row["including_execution_tail_metrics"]["sessions"] == 46
    assert row["decision_window_metrics"]["net_return"] == pytest.approx(-.04)
    assert row["including_execution_tail_metrics"]["net_return"] == pytest.approx(-.046)
    assert row["open_positions"] == {A: 100} and row["pending_risk_exits"] == case["state"]["pending_risk_exits"]
    for kind in ("partial", "no_fills", "no_target", "blocked", "missing_exact_day", "defense_unknown"):
        value, key = deepcopy(case), m.CASE_IDS[0]
        if kind == "partial": value["daily"] = value["daily"][:40]; value["state"]["day"] = window[39]
        elif kind == "no_fills": value["state"]["fills"] = {}
        elif kind == "no_target": value["daily"][0]["original_publication_kind"] = "NO_CHANGE"
        elif kind == "blocked": value["blocking_evidence"] = {"unresolved": True}
        elif kind == "missing_exact_day": value["daily"][1]["date"] = window[2]
        else: key = m.CASE_IDS[1]
        result = m._case_summary(key, value, window, 100000)
        assert result["metrics"] is result["decision_window_metrics"] is result["including_execution_tail_metrics"] is None
    case["verified_breadth_seen"] = True
    assert m._case_summary(m.CASE_IDS[1], case, window, 100000)["performance_available"]


@pytest.mark.parametrize("fault", ["cohort_registration", "own_bundle", "old_candidate", "late_publication", "feature_qualification"])
def test_changed_source_or_later_publication_contract_refused(evidence, fault):
    if fault in {"cohort_registration", "late_publication", "feature_qualification"}:
        path = evidence["pubs"][1]; value = read(path)
        if fault == "cohort_registration": value["frozen_cohort_registration"]["sha256"] = "changed"
        elif fault == "feature_qualification": value["feature_qualification"] = {"unexpected_top_level_proof": True}
        else: value["recorded_at"] = "2026-10-19T10:00:00+08:00"
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        path = evidence["latest"][2]; value = read(path)
        if fault == "old_candidate": value["bundle"]["batches"][m.FAMILIES[0]][DAYS[0]]["candidates"][0]["score"] = .2
        else: value["bundle"]["source_packages"][0]["raw_daily_sha256"] = "changed"
        path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError): execute(evidence, count=2)
    assert not evidence["run"].exists()


def test_pending_coedited_weights_and_receipt_rejected_by_external_journal(evidence):
    execute(evidence)
    saved = head(evidence)
    key = m.CASE_IDS[0]
    case = saved["cases"][key]
    case["state"]["pending_targets"][A] = .08
    case["state"]["pending_published_close_intent"]["reduced_intent"]["weights"][A] = .08
    (evidence["run"] / "head.json").write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="sealed journal"): execute(evidence)


def test_head_write_crash_keeps_orphans_and_recovery_has_no_double_fills(evidence, monkeypatch):
    execute(evidence)
    before = (evidence["run"] / "head.json").read_bytes()
    original_write = m.write_json
    def fail_head(path, value):
        if path == evidence["run"] / "head.json": raise OSError("synthetic atomic head replacement failure")
        return original_write(path, value)
    monkeypatch.setattr(m, "write_json", fail_head)
    with pytest.raises(OSError): execute(evidence, count=2)
    assert (evidence["run"] / "head.json").read_bytes() == before
    orphan = set((evidence["run"] / "journal").rglob("2026-10-16_*.json"))
    assert len(orphan) == 20
    monkeypatch.setattr(m, "write_json", original_write)
    evidence["clock"]["value"] = datetime.fromisoformat("2026-10-30T20:00:01+08:00")
    result = execute(evidence, count=2)
    assert result["newly_committed_case_sessions"] == 20
    assert all(p.exists() for p in orphan)
    for key, case in head(evidence)["cases"].items():
        assert len(case["state"]["orders"]) == (1 if key.endswith(m.EXIT_POLICIES[0]) else 0)
    committed = (evidence["run"] / "head.json").read_bytes()
    assert execute(evidence, count=2)["newly_committed_case_sessions"] == 0
    assert (evidence["run"] / "head.json").read_bytes() == committed


def test_bad_owned_reference_blocks_trial_without_reset_or_cash_change(evidence):
    execute(evidence, count=2)
    old = deepcopy(head(evidence)["cases"][m.CASE_IDS[0]])
    path = evidence["latest"][3]
    value = read(path)
    raw = pd.read_parquet(value["raw_bars_file"])
    raw.loc[raw.date.eq(DAYS[2]) & raw.symbol.eq(A), ["preclose", "preclose_for_limits"]] = 9.
    raw.to_parquet(value["raw_bars_file"], index=False)
    value["raw_bars_sha256"] = digest(value["raw_bars_file"])
    path.write_text(json.dumps(value), encoding="utf-8")
    # Same explicit synthetic evidence also owns the actual day19 publication;
    # only that day changes, while the consumed prefix stays byte-identical.
    own_path = evidence["own"][2]; own_value = read(own_path)
    own_value["raw_bars_sha256"] = value["raw_bars_sha256"]
    pd.read_parquet(value["raw_bars_file"]).to_parquet(own_value["raw_bars_file"], index=False)
    own_path.write_text(json.dumps(own_value), encoding="utf-8")
    pub_path = evidence["pubs"][2]; pub = read(pub_path)
    pub["publication_input_bundle"] = inputs.input_proof(own_path)
    pub_path.write_text(json.dumps(pub), encoding="utf-8")
    result = execute(evidence, count=3, raw_count=3)
    current = head(evidence)["cases"][m.CASE_IDS[0]]
    assert result["status"] == "advanced_research_only_with_blocked_cases"
    assert current["state"] == old["state"] and current["daily"] == old["daily"]
    assert current["blocking_evidence"]["session_not_committed"] is True
