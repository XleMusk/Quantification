"""Real Engine/Ledger synthetic source-to-four-book recovery, no market calls."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading import engine
from trading.research import seat_hypothesis_dated_forward as forward
from trading.research import seat_hypothesis_dated_inputs as inputs
from trading.research.tests.fixtures.ipo_qualification import write_ipo_fixture
from trading.research.tests import test_shortline_native_preopen_forward as close_fixture
from test_seat_hypothesis_dated_inputs import evidence as input_evidence, save, read, A, B, D, F


G, H = "2026-10-20", "2026-10-21"


@pytest.fixture
def evidence(input_evidence, monkeypatch):
    e = input_evidence
    # The assembler fixture forbids Engine execution. This suite explicitly
    # opts into the real shared Engine while keeping network/SQLite forbidden.
    monkeypatch.setattr(engine, "advance", forward.advance)
    monkeypatch.setattr(forward, "_now", lambda: e.clock["value"])
    monkeypatch.setattr(engine, "_published_now", lambda: e.clock["value"])
    ipo_dir = e.inputs / "ipo"
    ipo_dir.mkdir()
    ipo, master = write_ipo_fixture(ipo_dir)
    master_record = read(master)
    pd.DataFrame([{"canonical_symbol": symbol, "provider_ipo_date": "2026-01-02", "board": "main"}
        for symbol in (A, B)]).to_csv(ipo_dir / master_record["master_file"], index=False)
    master_record["master_sha256"] = inputs._ref(ipo_dir / master_record["master_file"])["sha256"]
    save(master, master_record)
    original = read(e.registration)
    method = e.inputs / "input_method.json"
    save(method, {"schema": "public_seat_single_snapshot_dated_input_method_registration_v1",
        "registered_at": D + "T18:50:00+08:00", "original_registration": inputs._ref(e.registration),
        "implementation": inputs._ref(inputs.__file__), "original_frozen_code_refs": original["code_refs"],
        "arm_ids": list(inputs.ARMS), "source_event_day": original["source_event_date"],
        "config": original["config"], "calendar": inputs._ref(e.calendar),
        "launcher": {"file": "historical_only_launcher_not_loaded", "sha256": "0" * 64}})
    registration = e.inputs / "forward_registration.json"
    save(registration, {"schema": forward.REGISTRATION_SCHEMA, "method_id": forward.METHOD_ID,
        "registered_at": D + "T19:00:00+08:00", "original_registration": inputs._ref(e.registration),
        "dated_input_method_registration": inputs._ref(method), "implementation": inputs._ref(forward.__file__),
        "calendar": inputs._ref(e.calendar), "ipo_evidence": inputs._ref(ipo),
        "research_master_run": inputs._ref(master), "first_decision_day": D,
        "arm_ids": list(inputs.ARMS), "config": original["config"], "rule_spec": forward.RULE_SPEC,
        "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False, "execution_admitted": False})
    inputs.build_dated_inputs(e.registration, e.calendar, e.closes[D], e.audit, e.out, decision_day=D)
    first = e.out / "publication.json"
    old_rows = close_fixture.rows
    def extra_rows(day):
        if day not in (G, H): return old_rows(day)
        values = old_rows(F)
        for row in values:
            row["date"] = day
            previous = 10. if row["code"] == "sh.600099" else 12.1 if day == G else 12.2
            closing = 10. if row["code"] == "sh.600099" else 12.2
            row.update(open=str(previous), close=str(closing), preclose=str(previous),
                high=str(max(previous, closing) + .1), low=str(min(previous, closing) - .1),
                amount=str((previous + closing) / 2 * 1000000), pctChg=str((closing / previous - 1) * 100))
        return values
    monkeypatch.setattr(close_fixture, "rows", extra_rows)
    sdk = {"wheel": str(e.inputs / "synthetic.whl"), "sha256": inputs._ref(e.inputs / "synthetic.whl")["sha256"],
        "distribution_version": "0.9.4"}
    closes = {**e.closes, **{day: close_fixture.strict_close(e.inputs / ("close_" + day), day, e.calendar, sdk)
        for day in (G, H)}}
    def publish(day):
        e.clock["value"] = datetime.fromisoformat(day + "T20:00:00+08:00")
        compare_dir = e.inputs / ("comparison_" + day)
        compare_dir.mkdir()
        csv = compare_dir / "prices.csv"
        raw = pd.read_csv(closes[day] / "raw_daily.csv", dtype=str, keep_default_na=False)
        raw["symbol"] = raw.code.str.split(".").str[1] + ".SSE"
        raw.rename(columns={"preclose": "pre_close"}).loc[raw.symbol.isin([A, B]),
            ["date", "symbol", *inputs.FIELDS]].to_csv(csv, index=False)
        metadata = compare_dir / "source.json"
        save(metadata, {**read(e.metadata), "day": day, "source_available_at": day + "T18:45:00+08:00"})
        audit = e.inputs / ("audit_" + day + ".json")
        inputs.build_exact_source_audit(closes[day], csv, metadata, audit, decision_day=day, symbols=[A, B])
        out = e.root / ("dated_" + day)
        inputs.build_dated_inputs(e.registration, e.calendar, closes[day], audit, out, decision_day=day)
        return out / "publication.json"
    e.clock["value"] = datetime.fromisoformat(F + "T20:00:00+08:00")
    return SimpleNamespace(base=e, registration=registration, publications=[first], closes=closes,
        run_dir=e.root / "four_books", publish=publish, clock=e.clock)


def execute(e, *, publications=None, days=(D, F), run_dir=None, through=None):
    return forward.run(e.publications if publications is None else publications,
        [e.closes[day] for day in days], e.registration, e.run_dir if run_dir is None else run_dir, through=through)


def test_actual_D_executes_E_without_E_pub_then_resume_NO_CHANGE_idempotency_and_actual_held_retention(evidence):
    e = evidence
    first = execute(e)
    assert first["research_accounts_created"] == 4 and first["newly_committed_execution_sessions"] == 1
    assert [case["fill_count"] for case in first["cases"]] == [4, 0, 4, 0]
    assert all(case["last_processed_execution_day"] == F for case in first["cases"])
    assert all(case["metrics"] is None for case in first["cases"])
    head_path = e.run_dir / "head.json"
    head_before = head_path.read_bytes()
    duplicate = execute(e)
    assert duplicate["newly_committed_execution_sessions"] == 0 and duplicate["new_order_count"] == 0
    assert head_path.read_bytes() == head_before
    state = read(head_path)["cases"]["legacy_repeated_sum_lag1"]["state"]
    config = forward.inputs.original._config(100000, 5)
    ranks = [{"symbol": f"60000{i}.SSE", "score": 1 - i / 100} for i in range(2, 7)] + [
        {"symbol": A, "score": .8}, {"symbol": B, "score": .7}]
    retained = forward._targets({"decision_kind": "RANKED_CANDIDATES", "ranked_candidates": ranks}, state, config)
    assert A in retained and B in retained and len(retained) == 5
    assert list(retained)[:2] == [A, B]
    # E's real publication is consumed only at G, without re-running E.
    e.publications.append(e.publish(F))
    e.clock["value"] = datetime.fromisoformat(G + "T20:00:00+08:00")
    second = execute(e, days=(D, F, G))
    assert second["research_accounts_created"] == 0 and second["newly_committed_execution_sessions"] == 1
    assert [case["fill_count"] for case in second["cases"]] == [4, 4, 4, 4]
    before_NC = read(head_path)
    e.publications.append(e.publish(G))
    e.clock["value"] = datetime.fromisoformat(H + "T20:00:00+08:00")
    third = execute(e, days=(D, F, G, H))
    after_NC = read(head_path)
    assert third["new_order_count"] == 0
    for arm in inputs.ARMS:
        assert after_NC["cases"][arm]["state"]["day"] == H
        for field in ("cash", "positions", "orders", "fills"):
            assert before_NC["cases"][arm]["state"][field] == after_NC["cases"][arm]["state"][field]


@pytest.mark.parametrize("missing", ["publication", "E_raw"])
def test_missing_actual_publication_or_E_raw_never_creates_directory_or_account(evidence, missing):
    e = evidence
    result = execute(e, publications=[] if missing == "publication" else None,
        days=(D,) if missing == "E_raw" else (D, F))
    assert result["status"].startswith("waiting_") and result["checkpoint"] is None
    assert result["account_count"] == 0 and result["metrics"] is None and not e.run_dir.exists()


def test_one_arm_failure_discards_all_trials_without_head_or_fake_account(evidence, monkeypatch):
    e = evidence
    actual = forward.advance
    def failing(state, feed, day, config, session_number, **kwargs):
        row = actual(state, feed, day, config, session_number, **kwargs)
        if kwargs["context"].account_id.endswith("stockday_unique_lag1"):
            raise ValueError("synthetic failure after real fills")
        return row
    monkeypatch.setattr(forward, "advance", failing)
    result = execute(e)
    assert result["status"] == "failed_four_arm_trial_no_commit"
    assert result["all_four_trials_discarded"] is True and result["account_count"] == 0
    assert not e.run_dir.exists() and result["new_order_count"] == 0


@pytest.mark.parametrize("change", ["source", "claimed_candidates", "skip_pub", "journal"])
def test_changed_source_semantics_or_persistent_prefix_is_rejected(evidence, change):
    e = evidence
    if change == "source":
        file = e.closes[D] / "raw_daily.csv"
        file.write_bytes(file.read_bytes() + b"\n")
    elif change == "claimed_candidates":
        pub = read(e.publications[0])
        bundle_file = Path(pub["bundle"]["file"])
        bundle = read(bundle_file)
        bundle["arms"][0]["ranked_candidates"] = list(reversed(bundle["arms"][0]["ranked_candidates"]))
        save(bundle_file, bundle)
        pub["bundle"] = inputs._ref(bundle_file)
        save(e.publications[0], pub)
    elif change == "skip_pub":
        e.publications = [e.publish(F)]
    else:
        execute(e)
        head = read(e.run_dir / "head.json")
        journal_file = Path(head["journal_files"][0]["file"])
        journal = read(journal_file)
        journal["publication"]["execution_day"] = G
        save(journal_file, journal)
        head["journal_files"][0] = inputs._ref(journal_file)
        save(e.run_dir / "head.json", head)
    with pytest.raises(ValueError):
        execute(e)
    if change != "journal": assert not e.run_dir.exists()


def test_exact_reference_difference_and_late_actual_IPO_master_both_stop_without_account(evidence):
    e = evidence
    sdk = {"wheel": str(e.base.inputs / "synthetic.whl"),
        "sha256": inputs._ref(e.base.inputs / "synthetic.whl")["sha256"], "distribution_version": "0.9.4"}
    other = close_fixture.strict_close(e.base.inputs / "different_reference_E", F, e.base.calendar, sdk,
        reference_override={A: 10.5})
    result = forward.run(e.publications, [e.closes[D], other], e.registration, e.run_dir)
    assert result["status"] == "blocked_exact_reference_raw_or_actual_IPO_knowledge"
    assert any("E_reference_differs_from_D_close_action_evidence_unresolved" in issue["reasons"]
        for quality in result["blocking_evidence"].values() for issue in quality["issues"])
    assert not e.run_dir.exists()
    registration = read(e.registration)
    master = Path(registration["research_master_run"]["file"])
    value = read(master)
    value["created_at"] = F + "T10:00:00+08:00"
    save(master, value)
    registration["research_master_run"] = inputs._ref(master)
    save(e.registration, registration)
    result = execute(e)
    assert result["status"] == "blocked_exact_reference_raw_or_actual_IPO_knowledge"
    assert any("fixed_IPO_or_master_source_actually_acquired_after_E_open" in issue["reasons"]
        for quality in result["blocking_evidence"].values() for issue in quality["issues"])
    assert not e.run_dir.exists()


def test_audit_acquired_after_pub_cannot_be_rebound_into_an_earlier_decision(evidence):
    e = evidence
    pub = read(e.publications[0])
    bundle_file = Path(pub["bundle"]["file"])
    bundle = read(bundle_file)
    audit_file = Path(bundle["source_audit"]["reference"]["file"])
    audit = read(audit_file)
    audit["checked_at"] = D + "T21:00:00+08:00"
    save(audit_file, audit)
    bundle["source_audit"]["reference"] = inputs._ref(audit_file)
    bundle["input_references"] = [inputs._ref(audit_file) if item["file"] == str(audit_file)
        else item for item in bundle["input_references"]]
    save(bundle_file, bundle)
    pub["bundle"] = inputs._ref(bundle_file)
    save(e.publications[0], pub)
    with pytest.raises(ValueError, match="precedes"):
        execute(e)
    assert not e.run_dir.exists()


def test_cli_wait_result_is_external_and_failed_command_retains_unknown_progress(evidence, capsys, monkeypatch):
    e = evidence
    output = e.base.root / "observations" / "waiting.json"
    args = ["--registration", str(e.registration), "--run-dir", str(e.run_dir),
        "--raw-close", str(e.closes[D]), "--result-json", str(output)]
    assert forward.main(args) == 0
    result = read(output)
    assert result["status"] == "waiting_for_first_actual_dated_publication" and result["metrics"] is None
    assert result["account_count"] == 0 and not e.run_dir.exists()
    assert json.loads(capsys.readouterr().out)["new_order_count"] == 0
    failed = e.base.root / "separate_observations" / "failure.json"
    def fail(*args, **kwargs):
        raise ValueError("synthetic command failure after validation")
    monkeypatch.setattr(forward, "run", fail)
    with pytest.raises(ValueError, match="synthetic command"):
        forward.main(args[:-1] + [str(failed)])
    record = read(failed)
    assert record["execution_progress_unknown"] is True and record["execution_admitted"] is False
    protected = e.base.inputs / "forbidden_result.json"
    with pytest.raises(ValueError, match="input package"):
        forward.main(args[:-1] + [str(protected)])
    assert not protected.exists()
    with pytest.raises(ValueError, match="input package"):
        forward.main(["--registration", str(e.registration), "--run-dir", str(e.base.inputs / "unsafe_run"),
            "--result-json", str(protected)])
    assert not protected.exists()


def test_zero_commit_head_cannot_erase_consumed_prefix_and_seed_arbitrary_positions(evidence):
    e = evidence
    execute(e)
    head_file = e.run_dir / "head.json"
    head = read(head_file)
    head["consumed_publications"] = []
    head["journal_files"] = []
    head["raw_sources"] = []
    for case in head["cases"].values():
        case["state"]["cash"] = 987654321
        case["state"]["day"] = None
        case["daily"] = []
    save(head_file, head)
    with pytest.raises(ValueError, match="at least one committed publication"):
        execute(e)


def test_direct_run_rejects_new_account_inside_indirect_comparison_source_package(evidence):
    e = evidence
    outside = e.base.root / "separate_comparison_source"
    outside.mkdir()
    comparison, metadata = outside / "prices.csv", outside / "source.json"
    comparison.write_bytes(e.base.comparison.read_bytes())
    metadata.write_bytes(e.base.metadata.read_bytes())
    audit_file = e.base.root / "separate_audit_package" / "audit.json"
    e.clock["value"] = datetime.fromisoformat(D + "T20:00:00+08:00")
    inputs.build_exact_source_audit(e.closes[D], comparison, metadata, audit_file,
        decision_day=D, symbols=[A, B])
    pub = read(e.publications[0])
    bundle_file = Path(pub["bundle"]["file"])
    bundle = read(bundle_file)
    old_audit = bundle["source_audit"]
    new_audit = inputs.validate_exact_source_audit(audit_file, e.closes[D], decision_day=D, symbols=[A, B])
    replaced = {old_audit["reference"]["file"]: new_audit["reference"],
        old_audit["verification"]["comparison"]["raw"]["file"]: inputs._ref(comparison),
        old_audit["verification"]["comparison"]["metadata"]["file"]: inputs._ref(metadata)}
    bundle["source_audit"] = new_audit
    bundle["input_references"] = [replaced.get(reference["file"], reference) for reference in bundle["input_references"]]
    save(bundle_file, bundle)
    pub["bundle"] = inputs._ref(bundle_file)
    save(e.publications[0], pub)
    run_dir = outside / "must_not_create"
    e.clock["value"] = datetime.fromisoformat(F + "T20:00:00+08:00")
    with pytest.raises(ValueError, match="indirect publication/source package"):
        execute(e, run_dir=run_dir)
    assert not run_dir.exists()
