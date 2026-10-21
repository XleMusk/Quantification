"""System pointers route saved status only; no producer or account execution."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading.research import shortline_native_preopen as policy
from trading.research import shortline_native_preopen_forward as forward
from trading.research import shortline_native_preopen_status as view
from trading.tests.test_native_preopen_status import (
    D, E, F, evidence as legacy_evidence, reference, snapshot, write,
)


READ_STATUS = view.native_preopen_status
NOW = datetime.fromisoformat("2026-10-20T20:00:00+08:00")
G = "2026-10-20"
SYSTEM_SCHEMA = "a_share_existing_routes_daily_refresh_v1"
SYSTEM_FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "broker_interacted": False,
    "model_refit": False, "network_accessed": False, "user_account_mutated": False}
NATIVE_FLAGS = {"research_only": True, "candidate_panel_only": True,
    "execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "model_refit": False,
    "future_labels_used_for_score": False, "network_accessed": False,
    "engine_advanced": False, "ledger_mutated": False, "account_accessed": False,
    "orders_generated": False, "old_outputs_modified": False}
POLICIES = {"v1": policy.POLICY_ID, "qualified": policy.QUALIFIED_POLICY_ID}


def _save_system(root, original_plans, latest_plans, *, source_day=E, core_day=F,
        missing_plan_days=(), include_byte_count=True, calendar_file=None):
    directory = root / "research/system_daily/20261017_v1"
    calendar_file = calendar_file or root / "calendar.json"
    if not calendar_file.exists():
        write(calendar_file, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
            "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []})
    initial_templates, templates = {}, {}
    for key, policy_id in POLICIES.items():
        run = root / "research/shortline_native_preopen_forward" / view.POLICY_SCOPES[policy_id]
        raw = str((root / "raw" / D).resolve())
        argv = ["shortline-native-preopen-forward", "--run", str(run.resolve()),
            "--plan", str(original_plans[key].resolve()), "--raw-close", raw,
            "--ipo-proof", str(root / (key + "_ipo.json")),
            "--research-master-run", str(root / "master.json"),
            "--through", E, "--result-json", str(root / (key + "_old_result.json"))]
        if key == "qualified":
            argv.append("--ipo-append-only")
        initial = {"argv": argv, "run_dir": str(run.resolve()),
            "plan_prefix": [str(original_plans[key].resolve())], "raw_close_prefix": [raw],
            "result_path_must_be_replaced_before_call": True}
        initial_templates[key] = initial
        current = deepcopy(initial)
        if latest_plans[key] != original_plans[key]:
            current["plan_prefix"].append(str(latest_plans[key].resolve()))
            current["argv"].extend(("--plan", str(latest_plans[key].resolve())))
        templates[key] = current
    initial_native = {"schema": "native_shortline_daily_profile_v1",
        "method_id": "native_incremental_frozen_score_two_fixed_preopen_policies_v1",
        "calendar": str(calendar_file.resolve()),
        "source_day": D, "latest_plans": {k: str(v.resolve()) for k, v in original_plans.items()},
        "forward_templates": initial_templates, "chain_has_missing_plan": False,
        "missing_plan_days": [], **NATIVE_FLAGS}
    native = {**deepcopy(initial_native), "source_day": source_day,
        "latest_plans": {k: str(v.resolve()) for k, v in latest_plans.items()},
        "forward_templates": templates, "missing_plan_days": list(missing_plan_days),
        "chain_has_missing_plan": bool(missing_plan_days)}
    registration = {"schema": SYSTEM_SCHEMA, "run_directory": str(directory.resolve()),
        "calendar": reference(calendar_file), "initial_native_profile": initial_native, **SYSTEM_FLAGS}
    registration_file = directory / "registration.json"
    write(registration_file, registration)
    registration_ref = reference(registration_file)
    if not include_byte_count:
        registration_ref.pop("byte_count")
    head = {"schema": SYSTEM_SCHEMA, "registration": registration_ref,
        "native_profile": native, "profile": {"last_source_day": core_day},
        "last_source_day": core_day, "operations": [], **SYSTEM_FLAGS}
    head_file = directory / "head.json"
    write(head_file, head)
    return SimpleNamespace(directory=directory, head_file=head_file,
        registration_file=registration_file, head=head, registration=registration)


@pytest.fixture
def system_evidence(tmp_path, monkeypatch):
    root, workspace = tmp_path / "data", tmp_path / "workspace"
    originals, latest = {}, {}
    for key, policy_id in POLICIES.items():
        originals[key] = root / "research/shortline_native_preopen" / view.POLICY_SCOPES[policy_id] / "plan.json"
        latest[key] = root / "research/system_daily/20261017_v1/operations/20261019/native" / ("plan_" + key) / "plan.json"
        for path, source, execution in ((originals[key], D, E), (latest[key], E, F)):
            write(path, {"policy_id": policy_id, "source_day": source, "execution_day": execution,
                "published_at": source + "T18:30:00+08:00", "calendar": {"fixture": "same_calendar"}})
    system = _save_system(root, originals, latest, source_day=F, missing_plan_days=[F])
    loaded, discoveries = [], []

    def plan(path, now, expected_policy_id=policy.POLICY_ID):
        path = Path(path).resolve()
        record = json.loads(path.read_text(encoding="utf8"))
        if record["policy_id"] != expected_policy_id:
            raise ValueError("Saved plan belongs to another policy")
        loaded.append(path)
        return {"record": record, "ref": reference(path), "registration": {},
            "calendar": {}, "models": [], "model_generation": {}, "unknown_stage_symbols": [],
            "stage_scope": {}, "validation_basis": "synthetic_saved_plan_routing_only"}

    def discover(data_root, policy_id=policy.POLICY_ID):
        discoveries.append(policy_id)
        return originals["qualified" if policy_id == policy.QUALIFIED_POLICY_ID else "v1"]

    monkeypatch.setattr(view, "_plan", plan)
    monkeypatch.setattr(view, "_discover_plan", discover)
    monkeypatch.setattr(forward, "run", lambda *a, **k: pytest.fail("Status cannot call a consumer"))
    return SimpleNamespace(root=tmp_path, data=root, workspace=workspace,
        originals=originals, latest=latest, system=system, loaded=loaded, discoveries=discoveries)


@pytest.mark.parametrize("include_byte_count", [False, True])
def test_registered_current_plan_overrides_legacy_discovery_and_exposes_separate_cursors(
        system_evidence, include_byte_count):
    e = system_evidence
    system = _save_system(e.data, e.originals, e.latest, source_day=F, core_day=F,
        missing_plan_days=[F], include_byte_count=include_byte_count)
    before = snapshot(e.root)
    result = READ_STATUS(e.data, e.workspace, now=NOW)
    assert result["status"] == "saved_preopen_plan_only", result.get("error")
    assert result["source_refs"]["plan"] == reference(e.latest["v1"])
    progress = result["system_progress"]
    assert progress["native_context_source_day"] == progress["core_signal_source_day"] == F
    assert progress["missing_plan_days"] == [F]
    assert progress["plan_source_day"] == E and progress["plan_execution_day"] == F
    assert progress["account_execution_day"] is None
    assert progress["system_head"] == str(system.head_file.resolve())
    assert progress["system_head_ref"] == reference(system.head_file)
    assert progress["registration_ref"]["file"] == str(system.registration_file.resolve())
    assert progress["registration_ref"]["sha256"] == reference(system.registration_file)["sha256"]
    assert e.discoveries == [] and e.loaded == [e.latest["v1"].resolve()]
    assert snapshot(e.root) == before


@pytest.mark.parametrize("fault", ["sha", "byte_count", "run_directory", "missing_head",
    "missing_registration", "missing_latest", "latest_pointer"])
def test_registered_invalid_or_missing_evidence_never_discovers_an_older_plan(system_evidence, fault):
    e, system = system_evidence, system_evidence.system
    if fault in {"sha", "byte_count"}:
        system.head["registration"]["sha256" if fault == "sha" else "byte_count"] = "changed" if fault == "sha" else 1
        write(system.head_file, system.head)
    elif fault == "run_directory":
        system.registration["run_directory"] = str(e.root / "another_system")
        write(system.registration_file, system.registration)
        system.head["registration"] = reference(system.registration_file)
        write(system.head_file, system.head)
    elif fault == "missing_head":
        system.head_file.unlink()
    elif fault == "missing_registration":
        system.registration_file.unlink()
    elif fault == "missing_latest":
        e.latest["v1"].unlink()
    else:
        system.head["native_profile"]["latest_plans"]["v1"] = str(e.originals["v1"])
        write(system.head_file, system.head)
    before = snapshot(e.root)
    result = READ_STATUS(e.data, e.workspace, now=NOW)
    assert result["status"] == "unknown_saved_evidence"
    assert result["accounts"] == [] and result["account_count"] is None
    assert result["fallback_to_older_success"] is False and result["error"]
    assert e.discoveries == [] and snapshot(e.root) == before


@pytest.mark.parametrize("fault", ["account", "static_command", "plan_prefix", "raw_prefix"])
def test_bad_qualified_template_does_not_hide_valid_v1(system_evidence, fault):
    e, system = system_evidence, system_evidence.system
    template = system.head["native_profile"]["forward_templates"]["qualified"]
    if fault == "account":
        template["run_dir"] = str(e.root / "other_account")
    elif fault == "static_command":
        template["argv"].remove("--ipo-append-only")
    elif fault == "plan_prefix":
        template["plan_prefix"][0] = str(e.root / "replacement_plan.json")
    else:
        template["raw_close_prefix"][0] = str(e.root / "replacement_raw")
    write(system.head_file, system.head)
    before = snapshot(e.root)
    result = view.native_preopen_comparison(e.data, e.workspace, now=NOW)
    baseline, qualified = [branch["view"] for branch in result["policies"]]
    assert baseline["status"] == "saved_preopen_plan_only", baseline.get("error")
    assert baseline["source_refs"]["plan"] == reference(e.latest["v1"])
    assert qualified["status"] == "unknown_saved_evidence" and qualified["accounts"] == []
    assert e.discoveries == [] and snapshot(e.root) == before


@pytest.mark.parametrize("override", ["plan_file", "run_dir"])
def test_explicit_paths_preserve_legacy_routing_even_when_system_registration_is_bad(system_evidence, override):
    e = system_evidence
    e.system.registration_file.write_text("not JSON", encoding="utf8")
    options = {override: e.originals["v1"] if override == "plan_file" else e.root / "explicit_account"}
    before = snapshot(e.root)
    result = READ_STATUS(e.data, e.workspace, now=NOW, **options)
    assert result["status"] == "saved_preopen_plan_only", result.get("error")
    assert result["source_refs"]["plan"] == reference(e.originals["v1"])
    assert result.get("system_progress") is None
    assert e.discoveries == ([] if override == "plan_file" else [policy.POLICY_ID])
    assert snapshot(e.root) == before


def test_no_system_registration_keeps_legacy_discovery(system_evidence):
    e = system_evidence
    e.system.head_file.unlink()
    e.system.registration_file.unlink()
    before = snapshot(e.root)
    result = READ_STATUS(e.data, e.workspace, now=NOW)
    assert result["status"] == "saved_preopen_plan_only"
    assert result["source_refs"]["plan"] == reference(e.originals["v1"])
    assert result.get("system_progress") is None and e.discoveries == [policy.POLICY_ID]
    assert snapshot(e.root) == before


def _newer_plan(e):
    """A new real four-payload publication sharing the old frozen generation."""
    old = json.loads(e.plan.read_text(encoding="utf8"))
    directory = e.data / "research/system_daily/20261017_v1/operations/20261019/native/plan_v1"
    for file in e.plan.parent.iterdir():
        if file.is_file():
            destination = directory / file.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(file.read_bytes())
    publication = directory / "source_publication.json"
    write(publication, {"schema": "native_shortline_raw_score_publication_v1",
        "published_at": F + "T18:00:00+08:00", "target_generation_allowed": False,
        "execution_admitted": False})
    registration_file, decisions_file = directory / "registration.json", directory / "decisions.json"
    registration = json.loads(registration_file.read_text(encoding="utf8"))
    registration.update(source_day=F, execution_day=G, registered_at=F + "T18:15:00+08:00",
        source_publication=reference(publication))
    write(registration_file, registration)
    decisions = json.loads(decisions_file.read_text(encoding="utf8"))
    decisions.update(source_day=F, execution_day=G)
    for model in decisions["models"]:
        model.update(source_day=F, execution_day=G)
    write(decisions_file, decisions)
    old.update(source_day=F, execution_day=G, published_at=F + "T18:30:00+08:00",
        next_open_deadline=G + "T09:30:00+08:00", source_publication=reference(publication))
    old["files"] = [{"path": item["path"], **{key: reference(directory / item["path"])[key]
        for key in ("sha256", "byte_count")}} for item in old["files"]]
    write(directory / "plan.json", old)
    return directory / "plan.json"


def test_new_unconsumed_plan_displays_old_account_only_after_its_original_journal_validation(
        legacy_evidence, monkeypatch):
    e = legacy_evidence
    e.save_head()
    newer = _newer_plan(e)
    qualified = e.data / "unused_ipoqualified/plan.json"
    write(qualified, {"not_loaded_by_v1": True})
    system = _save_system(e.data, {"v1": e.plan, "qualified": qualified},
        {"v1": newer, "qualified": qualified}, source_day=G, core_day=G, missing_plan_days=[G],
        calendar_file=e.root / "calendar.json")
    monkeypatch.setattr(view, "_discover_plan", lambda *a, **k: pytest.fail("Registered pointer owns routing"))
    before = snapshot(e.root)
    result = READ_STATUS(e.data, e.workspace, now=NOW)
    assert result["status"] == "active_research_books", result.get("error")
    assert result["source_day"] == F and result["execution_day"] == G
    assert result["source_refs"]["plan"] == reference(newer)
    assert result["account_count"] == 2
    assert {account["last_processed_day"] for account in result["accounts"]} == {E}
    assert all(account["cash_cents"] == 9_949_400 for account in result["accounts"])
    progress = result["system_progress"]
    assert progress["plan_execution_day"] == G and progress["account_execution_day"] == E
    assert progress["system_head_ref"] == reference(system.head_file)
    assert snapshot(e.root) == before

    # A current display plan must not weaken the original journal/account seal.
    head_file = e.run / "head.json"
    head = json.loads(head_file.read_text(encoding="utf8"))
    head["cases"]["lr"]["state"]["cash"] += 1
    write(head_file, head)
    before = snapshot(e.root)
    rejected = READ_STATUS(e.data, e.workspace, now=NOW)
    assert rejected["status"] == "unknown_saved_evidence" and rejected["accounts"] == []
    assert snapshot(e.root) == before
