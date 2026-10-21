"""Register a new independent generation from one actual eight-type paper plan.

This writes only a new registration JSON. Existing publication/input readers and
the original consumer validate the fixed rules; no account or Engine is started.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path

from . import leader_eight_type_forward_run as forward


STATUS = "registered_independent_eight_type_forward_only"
GENERATION_METHOD_ID = "leader_eight_type_explicit_new_generation_per_case_start_v1"
SPLIT_GENERATION_METHOD_ID = "leader_eight_type_explicit_split_settlement_generation_v1"
SPLIT_SESSION_METHOD_ID = "published_close_settlement_then_attach_v1"
SPLIT_SESSION_OPTION = "split-settlement-v1"


def _now():
    return forward._now()


def _ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": forward.digest(path)}


def _destinations(out, run_dir, protected):
    if out.exists() or run_dir.exists():
        raise FileExistsError("New registration JSON and unused independent run directory required")
    if out == run_dir or out in run_dir.parents or run_dir in out.parents:
        raise ValueError("Registration and future account directory must be separate")
    original_registration = forward.DEFAULT_REGISTRATION.resolve()
    for target in (out, run_dir):
        book_ancestors = [parent / "head.json" for parent in target.parents]
        if (target == original_registration or original_registration.parent in target.parents
                or any(target == file or target in file.parents for file in protected)
                or any(head.is_file() and json.loads(head.read_text(encoding="utf-8-sig")).get("schema")
                    in {forward.SCHEMA, "leader_eight_type_split_forward_checkpoint_v1"} for head in book_ancestors)):
            raise ValueError("New generation cannot write original evidence or an existing account tree")


def register(publication, input_manifest, ipo_evidence, research_master_run, out, run_dir, *, session_method=None):
    if session_method not in (None, SPLIT_SESSION_OPTION):
        raise ValueError("Only the explicit split-settlement-v1 session method is supported")
    split = session_method == SPLIT_SESSION_OPTION
    method_id = SPLIT_GENERATION_METHOD_ID if split else GENERATION_METHOD_ID
    publication, input_manifest, ipo_evidence, research_master_run, out, run_dir = (
        Path(path).resolve() for path in
        (publication, input_manifest, ipo_evidence, research_master_run, out, run_dir))
    protected = [publication, input_manifest, ipo_evidence, research_master_run]
    _destinations(out, run_dir, protected)
    references = {name: _ref(path) for name, path in zip(
        ("publication", "input_manifest", "ipo_evidence", "research_master_run"), protected)}
    observed = _now()
    # Exact producer schema, own input SHA/source clocks, saved 32-case rules,
    # ranks/weights/qualification/raw references are checked by the real reader.
    first = forward._publication(publication, ipo_evidence, research_master_run, observed, {})
    if first.get("unbindable"):
        raise ValueError("A late or unbound paper plan cannot start a new generation")
    own = first["record"]["source"]
    if (Path(own["input_manifest"]).resolve() != input_manifest
            or own["input_manifest_sha256"] != references["input_manifest"]["sha256"]):
        raise ValueError("The explicit input manifest must be this publication's own unchanged input")
    actual = _now()
    stamp = forward._stamp(actual)
    deadline = first["execution"] + "T09:30:00+08:00"
    if not forward._stamp(first["reference"]["recorded_at"]) <= stamp < forward._stamp(deadline):
        raise ValueError("Actual registration must follow publication and precede the exact next open")
    if any(_ref(ref["file"]) != ref for ref in references.values()):
        raise ValueError("A publication, own input or qualification input changed during registration")
    loaded = first["loaded"]
    record = {
        "schema": "leader_eight_type_published_close_forward_registration_v1",
        "registered_at": stamp.isoformat(), "first_decision_day": first["day"],
        "first_execution_day": first["execution"], "first_execution_deadline": deadline,
        "original_publication": {key: first["reference"][key] for key in ("file", "sha256", "recorded_at")},
        "published_target_policy_id": forward.TARGET_POLICY_ID,
        "risk_policy": deepcopy(forward.FORWARD_RISK_SPEC), "case_ids": list(forward.CASE_IDS),
        "config": forward._config(100000, 5).to_dict(),
        "method": "original_published_weights_then_post_opening_reduce_only_and_current_close_risk_queue",
        "registration_is_original_publication": False, "original_publication_unchanged": True,
        "generation_method_id": method_id, "initial_quality_scope": "per_case",
        "registered_run_dir": str(run_dir),
        "generation": {
            "kind": "independent_new_research_generation", "intended_run_directory": str(run_dir),
            "original_generations_unchanged": True, "original_cash_reused": False,
            "missing_old_publications_repaired": False,
            "run_directory_enforcement": "new_consumer_generation_binding_and_wrapper"},
        "own_input_manifest": references["input_manifest"],
        "qualification_inputs": {key: references[key] for key in ("ipo_evidence", "research_master_run")},
        "source_bindings": deepcopy(own), "source_calendar": deepcopy(loaded["calendar_payload"]),
        "scope_members": list(loaded["scope_members"]),
        "limitations": [
            "This new generation does not repair old missing publications or reuse old account cash.",
            "The original consumer requires each session's own actual publication and exact raw sources.",
            "ExactSourceRawFeed has no typed corporate-action view; reference proxies do not establish action coverage.",
            "PIT and corporate actions remain unverified; no strategy selection or trading is authorized.",
            "The new consumer generation binding and wrapper both enforce per-case initial quality and the exact run directory; older methods are unchanged."],
        **forward.FLAGS,
    }
    if split:
        record["session_method_id"] = SPLIT_SESSION_METHOD_ID
        record["method"] = "settle_exact_sessions_then_attach_actual_close_publications_without_reexecuting_open"
        record["limitations"][1] = ("Exact raw sessions settle independently of their own close publication; "
            "missing publications remain explicit and are never replaced by NO_CHANGE.")
        record["limitations"].append("This explicit split method cannot migrate or reset an existing generation.")
    _destinations(out, run_dir, protected)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    # Read the saved JSON through the unchanged consumer registration contract.
    # A failed readback preserves the attempted file but cannot create an account.
    checked = forward._registration(out, first, forward._config(100000, 5), _now())
    if run_dir.exists():
        raise ValueError("Another writer created the intended account directory during registration")
    return {
        "schema": "leader_eight_type_independent_forward_registration_receipt_v1", "status": STATUS,
        "registration_file": str(out), "registration": checked,
        "registered_at": record["registered_at"], "first_decision_day": first["day"],
        "first_execution_day": first["execution"], "intended_run_directory": str(run_dir),
        "generation_method_id": method_id, "initial_quality_scope": "per_case",
        "registered_run_dir": str(run_dir),
        "case_count": 32, "initial_cash_per_case": 100000, "research_accounts_created": 0,
        "account_created": False, "engine_run": False, "orders_generated": False,
        "old_registration_modified": False, "old_account_modified": False, **forward.FLAGS,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("publication", "input-manifest", "ipo-evidence", "research-master-run", "out", "run-dir"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--session-method", choices=(SPLIT_SESSION_OPTION,))
    result = register(**vars(parser.parse_args(argv)))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
