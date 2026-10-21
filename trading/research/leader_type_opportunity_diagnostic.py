"""Own-D known-empty routing attribution, without prices, accounts or returns.

All-D hypothetical-flat opportunities and the original saved path's observed
pre-flat subset are separate populations. Neither is a tradability check nor
a counterfactual strategy return. The original router, accounts and rules stay
unchanged; source panels are hashed once while streaming a small projection.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re

from ..contracts import MARKET_TZ
from . import leader_type_opportunity_selection as selector
from .leader_daily_signals import _calendar_view
from .leader_eight_type_panel_replay import GUARDS, CAPS, _environment, _rank_path


METHOD_ID = "saved_leader_type_known_empty_opportunity_attribution_v1"
REGISTRATION_SCHEMA = METHOD_ID + "_registration"
RESULT_SCHEMA = METHOD_ID + "_result"
CASE_IDS = tuple("single_active_type__"+g+"__"+c for g in GUARDS for c,_ in CAPS)
FLAGS = {"research_only": True, "candidate_panel_only": True,
    "strategy_validated": False, "selection_enabled": False, "execution_admitted": False,
    "returns_reportable": False, "engine_advanced": False, "account_created": False,
    "ledger_mutated": False, "prices_read": False, "future_outcomes_read": False,
    "provider_contacted": False, "model_refit": False, "independent_holdout": False,
    "historical_signal_publication_verified": False, "pit_verified": False}
SPEC = {"method_id": METHOD_ID, "selector": deepcopy(selector.SPEC),
    "sources": "all_original_2023_partial_2024_2025_router_studies_and_four_saved_decision_files_each",
    "projection": "stream_saved_pretty_JSON_day_available_at_environment_types_only_skip_risk_features",
    "candidate_projection": "symbol_and_original_score_or_amount_only_no_candidate_return_or_label_fields",
    "source_integrity": "manifest_small_metadata_pin_panel_declared_SHA_verified_during_single_stream_pass",
    "rank_and_environment": "original_rank_path_and_environment_same_fixed_members_calendar_guards_capacities",
    "cycle": "original_calendar_offset_from_period_start_mod_eight_before_candidates",
    "skip": "only_explicit_known_empty_original_capacity_path_never_unknown",
    "populations": ["all_D_assumed_flat", "original_saved_path_observed_pre_flat"],
    "actual_flat": "saved_pre_held_symbols_and_pre_positive_pending_symbols_both_explicit_empty_lists",
    "strict_actual_recovery": "pre_flat_and_original_close_rule_selected_type_unknown_or_empty_NO_CHANGE_and_reason_type_empty_or_capacity_filtered_all_no_signal_and_explicit_exit_field_present_null_and_known_nonempty_after_skip",
    "uncovered": "missing_original_decision_or_prestate_remains_unknown_not_flat_after_partial_stop",
    "entry_scope": "candidate_opportunity_only_no_raw_reference_IPO_or_E_execution_validation",
    "tail": "exclude_original_six_tail_days_from_new_entry_opportunity_denominator",
    "overlap": "same_D_and_symbols_repeat_across_cases_layers_periods_not_independent_samples",
    "fees_exits_returns": "not_computed_no_counterfactual_PnL_or_replacement_account",
    "parameters_selected_from_outcomes": False, **FLAGS}
_DAY_LINE = re.compile(rb'^  "(\d{4}-\d{2}-\d{2})": \{\s*$')


def _now():
    return datetime.now(MARKET_TZ).isoformat()


def _read_pin(path, expected=None):
    path = Path(path).resolve()
    if path.stat().st_size > 64*1024*1024:
        raise ValueError("Diagnostic small metadata reader cannot load a historical panel")
    data = path.read_bytes()
    reference = {"path": str(path), "sha256": sha256(data).hexdigest()}
    if expected is not None and reference != {"path": str(Path(expected.get("path") or expected["file"]).resolve()),
                                              "sha256": expected["sha256"]}:
        raise ValueError("Saved diagnostic metadata changed: "+str(path))
    return json.loads(data.decode("utf-8-sig")), reference


def _declared(path, digest, parent):
    return {"path": str((Path(parent)/path).resolve()), "sha256": digest}


def _write(path, value):
    with Path(path).open("x", encoding="utf8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def _code():
    paths = [Path(__file__), Path(selector.__file__), Path(__file__).with_name("leader_eight_type_panel_replay.py"),
        Path(__file__).with_name("leader_environment_policy.py"), Path(__file__).with_name("leader_daily_signals.py")]
    return [{"path": str(p.resolve()), "sha256": sha256(p.read_bytes()).hexdigest()} for p in paths]


def _outside(out, references):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Use a new diagnostic directory; no retry or overwrite")
    for reference in references:
        parent = Path(reference["path"]).resolve().parent
        if out == parent or out in parent.parents or parent in out.parents:
            raise ValueError("Diagnostic output must stay outside saved sources and original accounts")
    return out


def register(studies, router_run, out):
    mapping, mapping_ref = _read_pin(studies)
    definitions = mapping["studies"] if isinstance(mapping, dict) else mapping
    directory = Path(router_run).resolve()
    started, started_ref = _read_pin(directory/"started.json")
    original, original_ref = _read_pin(started["registration"]["path"], started["registration"])
    if (original.get("method_id") != "single_active_type_cyclic_environment_switch_shared_engine_v1"
            or not isinstance(definitions, list) or len(definitions) != 3
            or [s["label"] for s in definitions] != [s["label"] for s in original["studies"]]):
        raise ValueError("All three original ordered router studies must be retained")
    periods, deps = [], [mapping_ref, started_ref, original_ref]
    for item, prior in zip(definitions, original["studies"]):
        source = item["inputs"]
        path = source.get("path") or source.get("file") if isinstance(source, dict) else source
        path = Path(path)
        if path.is_dir(): path = path/"manifest.json"
        manifest, pin = _read_pin(path, source if isinstance(source, dict) else None)
        if pin != prior["inputs"]:
            raise ValueError("Study input must be the exact source of the original router")
        payload = prior["source_payloads"]
        if any(manifest.get(k) != payload[k] for k in ("panels_file", "panels_sha256", "calendar", "calendar_sha256")):
            raise ValueError("Original declared panel or calendar source changed")
        panels = _declared(manifest["panels_file"], manifest["panels_sha256"], path.parent)
        calendar = _declared(manifest["calendar"], manifest["calendar_sha256"], path.parent)
        _, calendar = _read_pin(calendar["path"], calendar)
        cases = []
        for case_id in CASE_IDS:
            decision_path = directory/item["label"]/case_id/"decisions.json"
            if decision_path.is_file():
                _, decision_ref = _read_pin(decision_path)
            else:
                decision_ref = None  # A failed original case is not silently a flat account.
            cases.append({"case_id": case_id, "decisions": decision_ref,
                "expected_decisions_path": str(decision_path), "saved_decisions_present": decision_ref is not None})
            if decision_ref: deps.append(decision_ref)
        periods.append({"period": item["label"], "manifest": pin, "panels": panels, "calendar": calendar,
            "start": prior["start"], "end": prior["end"], "through": prior["through"],
            "source_days": prior["source_days"], "scope_count": prior["scope_count"], "cases": cases})
        deps += [pin, calendar]
    if [p["start"][:4] for p in periods] != ["2023", "2024", "2025"]:
        raise ValueError("Retain original 2023-partial/2024/2025 order")
    out = _outside(out, deps+[p["panels"] for p in periods])
    code = _code(); out.mkdir(parents=True)
    snapshot = out/"code_snapshot"; snapshot.mkdir()
    copies = []
    for i, ref in enumerate(code):
        target = snapshot/(str(i)+"_"+Path(ref["path"]).name)
        data = Path(ref["path"]).read_bytes()
        if sha256(data).hexdigest() != ref["sha256"]: raise ValueError("Diagnostic code changed during registration")
        with target.open("xb") as stream: stream.write(data)
        copies.append({"path": str(target.resolve()), "sha256": ref["sha256"]})
    result = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID, "registered_at": _now(),
        "spec": SPEC, "original_router_registration": original_ref, "original_router_run": str(directory),
        "periods": periods, "dependencies": deps, "code": code, "code_copies": copies, **FLAGS}
    _write(out/"registration.json", result)
    return result


def read_panel_projection(path, expected_sha256):
    """Hash original bytes once; materialize only four requested own-D fields."""
    result, current, capture, lines, digest = {}, None, None, [], sha256()
    def finish_current():
        if current is not None and set(current) != {"day", "available_at", "environment", "types"}:
            raise ValueError("Incomplete original D opportunity projection")
    with Path(path).open("rb") as source:
        for line in source:
            digest.update(line)
            match = _DAY_LINE.match(line)
            if match:
                if capture: raise ValueError("Unclosed saved panel field")
                finish_current()
                day = match.group(1).decode("ascii")
                if day in result: raise ValueError("Duplicate saved panel D")
                current = {}; result[day] = current
                continue
            if current is None: continue
            if capture:
                lines.append(line)
                endings = (b"    },", b"    }") if capture == "environment" else (b"    ],", b"    ]")
                if line.rstrip() in endings:
                    value = json.loads(b"".join(lines).rstrip(b",\r\n"))
                    if capture == "types":
                        value = [{"id": item["id"], "status": item.get("status"),
                            "unresolved_symbols": item.get("unresolved_symbols", []),
                            "candidates": [{k: row[k] for k in ("symbol", "score", "amount_cny") if k in row}
                                           for row in item["candidates"]]} for item in value]
                    current[capture] = value; capture, lines = None, []
                continue
            if line.startswith(b'    "day":'):
                current["day"] = json.loads(line.split(b":",1)[1].strip().rstrip(b","))
            elif line.startswith(b'    "available_at":'):
                current["available_at"] = json.loads(line.split(b":",1)[1].strip().rstrip(b","))
            elif line.startswith(b'    "environment": {'):
                capture, lines = "environment", [b"{\n"]
            elif line.startswith(b'    "types": ['):
                capture, lines = "types", [b"[\n"]
    finish_current()
    if (digest.hexdigest() != expected_sha256 or capture or not result
            or any(day != row["day"] for day,row in result.items())):
        raise ValueError("Pinned panel bytes or pretty-JSON projection contract changed")
    return result


def _selection_fields(value):
    considered = value["considered_types"]
    initial = next((r for r in considered if r.get("environment_mode") == "ALLOW_FIXED_CANDIDATES"), None)
    selected = value["status"] == "selected_known_nonempty"
    return {"initial_selected": initial["type_id"] if initial else None,
        "initial_known_empty": bool(initial and initial.get("skipped_known_empty")),
        "new_selected": value["selected_type"] if selected else None,
        "unknown_barrier": value["status"] == "blocked_unknown",
        "unknown_barrier_type": value["selected_type"] if value["status"] == "blocked_unknown" else None,
        "skipped": [r["type_id"] for r in considered if r.get("skipped_known_empty")],
        "selected_symbols": [r["symbol"] for r in value["selected_candidates"]] if selected else None,
        "opportunity_status": value["status"], "reason": value["reason"],
        "considered_types": considered, "candidate_only_not_execution_checked": True,
        "returns": None}


def project_period(period, panels, members, sessions, decisions):
    """Pure selector/old-prestate projection; never simulate a different path."""
    if list(sorted(panels)) != period["source_days"]:
        raise ValueError("Original complete source-D panel prefix changed")
    if len(members) != period["scope_count"] or len(set(members)) != len(members):
        raise ValueError("Original fixed scope changed")
    paths, _ = _rank_path(panels, set(members), period["start"], period["end"])
    as_of = max(datetime.fromisoformat(p["available_at"]) for p in panels.values()).isoformat()
    environments = _environment(panels, sessions, as_of)
    original = {}
    for key, records in decisions.items():
        mapped = {r["day"]: r for r in records} if records is not None else {}
        if records is not None and len(mapped) != len(records): raise ValueError("Duplicate original case-D decision")
        original[key] = mapped
    all_rows, actual_rows = [], []
    for day in period["source_days"]:
        if not period["start"] <= day <= period["end"]: continue
        offset = sessions.index(day)-sessions.index(period["start"])
        cycle = [selector.TYPE_IDS[(offset+i) % len(selector.TYPE_IDS)] for i in range(len(selector.TYPE_IDS))]
        for key in CASE_IDS:
            _,guard,capacity = key.split("__")
            modes = {t: "ALLOW_FIXED_CANDIDATES" if guard == "unguarded" else
                environments.get(day, {}).get("per_type", {}).get(t, {}).get("mode", "NO_CHANGE")
                for t in selector.TYPE_IDS}
            value = selector.select_opportunity(day, cycle=cycle, modes=modes, panel=panels[day],
                paths=paths, capacity=capacity, members=members)
            saved = original.get(key, {}).get(day)
            known_prestate = (saved is not None and isinstance(saved.get("pre_held_symbols"), list)
                and isinstance(saved.get("pre_positive_pending_symbols"), list))
            flat = (not saved["pre_held_symbols"] and not saved["pre_positive_pending_symbols"]) if known_prestate else None
            covered = "not_covered_no_saved_account_state" if saved is None else (
                "covered_prestate_unknown" if not known_prestate else "covered_pre_flat" if flat else "covered_nonflat")
            if saved is not None and saved.get("cycle") != cycle:
                raise ValueError("Original saved decision cycle changed from the fixed calendar offset")
            common = {"period": period["period"], "case_id": key, "day": day, "guard": guard, "capacity": capacity,
                "original_coverage": covered, "original_pre_flat": flat,
                "original_selected_type": saved.get("selected_type") if saved else None,
                "original_selected_symbols": deepcopy(saved.get("selected_symbols")) if saved else None,
                "original_reasons": deepcopy(saved.get("reasons")) if saved else None,
                "original_close_rule": saved.get("close_rule") if saved else None,
                "original_status": saved.get("status") if saved else None,
                "original_explicit_exit": saved.get("explicit_exit") if saved else None}
            opportunity = _selection_fields(value)
            recovered = (flat is True and saved.get("close_rule") == "selected_type_unknown_or_empty_NO_CHANGE"
                and "type_empty_or_capacity_filtered_all_no_signal" in (saved.get("reasons") or [])
                and "explicit_exit" in saved and saved["explicit_exit"] is None and opportunity["initial_known_empty"]
                and opportunity["opportunity_status"] == "selected_known_nonempty")
            all_rows.append({**common, "population": "all_D_assumed_flat", "evaluated": True,
                "recovered_on_original_flat_path": None, **opportunity})
            actual_rows.append({**common, "population": "original_saved_path_observed_pre_flat",
                "evaluated": flat is True, "recovered_on_original_flat_path": recovered,
                **(opportunity if flat is True else {
                    "initial_selected": None, "initial_known_empty": None, "new_selected": None,
                    "unknown_barrier": None, "skipped": None, "selected_symbols": None,
                    "opportunity_status": None, "returns": None})})
    return all_rows, actual_rows


def aggregate(rows):
    groups = {}
    for row in rows:
        key = (row["period"], row["case_id"], row["population"])
        group = groups.setdefault(key, {"period": key[0], "case_id": key[1], "population": key[2],
            "declared_D_count": 0, "evaluated_D_count": 0, "coverage": Counter(), "status": Counter(),
            "initial_known_empty_D_count": 0, "opportunity_after_known_empty_D_count": 0,
            "recovered_on_original_flat_path_D_count": 0,
            "unknown_barrier_D_count": 0, "returns": None})
        group["declared_D_count"] += 1; group["coverage"][row["original_coverage"]] += 1
        if not row["evaluated"]: continue
        group["evaluated_D_count"] += 1; group["status"][row["opportunity_status"]] += 1
        group["initial_known_empty_D_count"] += bool(row["initial_known_empty"])
        group["unknown_barrier_D_count"] += bool(row["unknown_barrier"])
        group["recovered_on_original_flat_path_D_count"] += row.get("recovered_on_original_flat_path") is True
        group["opportunity_after_known_empty_D_count"] += bool(row["initial_known_empty"]
            and row["opportunity_status"] == "selected_known_nonempty")
    return [{**v, "coverage": dict(v["coverage"]), "status": dict(v["status"])} for v in groups.values()]


def run(registration, out):
    record, reg_ref = _read_pin(registration)
    if (record.get("schema") != REGISTRATION_SCHEMA or record.get("spec") != SPEC
            or record.get("code") != _code() or any(record.get(k) is not v for k,v in FLAGS.items())):
        raise ValueError("Exact registered source/selector contract required")
    for ref in record["code_copies"]:
        data = Path(ref["path"]).read_bytes()
        if sha256(data).hexdigest() != ref["sha256"]: raise ValueError("Saved diagnostic code copy changed")
    out = _outside(out, record["dependencies"]+[reg_ref]+[p["panels"] for p in record["periods"]])
    out.mkdir(parents=True); started = _now()
    _write(out/"started.json", {"registration": reg_ref, "started_at": started, **FLAGS})
    summaries, counts = [], []
    files = {"all_D_assumed_flat": out/"all_D_flat_assumption.jsonl",
             "original_saved_path_observed_pre_flat": out/"saved_path_flat.jsonl"}
    with files["all_D_assumed_flat"].open("x", encoding="utf8") as all_stream, files[
            "original_saved_path_observed_pre_flat"].open("x", encoding="utf8") as actual_stream:
        for period in record["periods"]:
            manifest, _ = _read_pin(period["manifest"]["path"], period["manifest"])
            calendar, _ = _read_pin(period["calendar"]["path"], period["calendar"])
            year = calendar["year"]
            sessions, _, status = _calendar_view(calendar, f"{year}-01-01", f"{year}-12-31", [])
            if status != "pinned_static_exchange_calendar": raise ValueError("Original static calendar required")
            decisions = {c["case_id"]: _read_pin(c["decisions"]["path"], c["decisions"])[0]
                if c["decisions"] else None for c in period["cases"]}
            panels = read_panel_projection(period["panels"]["path"], period["panels"]["sha256"])
            all_rows, actual_rows = project_period(period, panels, manifest["scope_members"], sessions, decisions)
            for rows, stream in ((all_rows,all_stream),(actual_rows,actual_stream)):
                for row in rows: stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+"\n")
                summaries += aggregate(rows)
            counts.append({"period": period["period"], "source_panel_D_count": len(panels),
                "all_D_flat_rows": len(all_rows), "saved_path_rows": len(actual_rows),
                "saved_path_pre_flat_evaluated_rows": sum(r["evaluated"] for r in actual_rows),
                "populations_not_combined_into_sample_count": True})
            print(json.dumps({"period": period["period"], "status": "opportunity_projection_saved", **counts[-1]}), flush=True)
    result = {"schema": RESULT_SCHEMA, "method_id": METHOD_ID, "status": "completed_saved_D_opportunity_diagnostic",
        "registration": reg_ref, "started_at": started, "completed_at": _now(), "periods": counts,
        "summaries": summaries, "rows": {k: str(v) for k,v in files.items()},
        "returns": None, "population_layers_are_not_independent_samples": True, **FLAGS}
    _write(out/"result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    registration = actions.add_parser("register")
    for flag in ("studies", "router-run", "out"): registration.add_argument("--"+flag, required=True, type=Path)
    replay = actions.add_parser("run")
    for flag in ("registration", "out"): replay.add_argument("--"+flag, required=True, type=Path)
    args = parser.parse_args(argv)
    value = register(args.studies, args.router_run, args.out) if args.action == "register" else run(args.registration,args.out)
    print(json.dumps({"schema": value["schema"], "status": value.get("status", "registered_without_outcomes"), **FLAGS}))
    return 0
