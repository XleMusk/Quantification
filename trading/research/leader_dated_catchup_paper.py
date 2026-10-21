"""Four dated-catchup signal publications for the existing shared Engine runner.

Only signal projection and publication live here. Account creation, target/order
execution, FIFO fills and persistent recovery stay in the common runner/Engine.
"""
from copy import deepcopy
from datetime import datetime
import argparse
import json
from pathlib import Path

from ..contracts import MARKET_TZ, PortfolioIntent
from ..market_dataset import write_json
from .baostock_daily_slice import digest
from . import leader_dated_catchup_inputs as candidate
from . import leader_eight_type_paper as original
from .leader_eight_type_panel_replay import CAPS, GUARDS, _config, _rank_path, load_inputs
from .leader_daily_signals import _calendar_view
from .leader_environment import _clean
from .leader_dated_catchup_exit import (TARGET_POLICY_ID, RISK_POLICY_ID, RULE_SPEC,
    risk_policy, target_policy)
from .recent_alpha158_panel_replay import ExactSourceRawFeed

TYPE_ID = "sector_catchup_leader"
METHOD_ID = "dated_catchup_shared_engine_paper_signal_v1"
SCHEMA = "leader_dated_catchup_unbound_close_paper_plan_v1"
REGISTRATION_SCHEMA = "leader_dated_catchup_paper_method_registration_v1"
CHECKPOINT_SCHEMA = "leader_dated_catchup_shared_engine_checkpoint_v1"
RESULT_SCHEMA = "leader_dated_catchup_shared_engine_result_v1"
ACCOUNT_PREFIX = "leader-dated-catchup-forward:"
CASE_IDS = tuple(f"{TYPE_ID}__{guard}__{cap}" for guard in GUARDS for cap, _ in CAPS)
FLAGS = deepcopy(original.FLAGS)
PROTOCOL = {**deepcopy(original.PROTOCOL), "id": METHOD_ID, "types": [TYPE_ID],
    "case_count": 4, "cases": list(CASE_IDS),
    "capacity_start": "candidate_registration_first_decision_day_no_earlier_candidates",
    "candidate_method_id": candidate.METHOD_ID,
    "combined_risk_policy_id": RISK_POLICY_ID, "target_policy_id": TARGET_POLICY_ID,
    "structural_risk_rules": deepcopy(RULE_SPEC),
    "execution_lifecycle_changed": True,
    "signal_only_fair_comparison_to_old32_claimed": False,
    "shared_engine_and_ledger": True, "original_32_method_changed": False}


def _now():
    return datetime.now(MARKET_TZ)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": digest(path)}


def _calendar(payload):
    year = payload["year"]
    sessions, previous, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError("Pinned complete exchange calendar required")
    return sessions, previous


def _candidate_registration(path, now):
    """Read only the sealed BE method/base/calendar, not its historical feed."""
    path = Path(path).resolve()
    value = _read(path)
    base_ref = value["base_panel"]
    if digest(base_ref["file"]) != base_ref["sha256"]:
        raise ValueError("Candidate registration base bytes changed")
    base = _read(base_ref["file"])
    calendar = Path(base["calendar"])
    if digest(calendar) != base["calendar_sha256"]:
        raise ValueError("Candidate registration calendar changed")
    payload = _read(calendar)
    sessions, _ = _calendar(payload)
    day = value["first_decision_day"]
    if (value.get("schema") != candidate.REG_SCHEMA or value.get("method_id") != candidate.METHOD_ID
            or value.get("rules") != candidate.RULES
            or any(value.get(k) is not v for k, v in candidate.FLAGS.items())
            or value.get("candidate_only") is not True
            or any(value.get(k) is not False for k in ("target_generation_allowed",
                "paper_publication_implemented", "forward_consumer_implemented"))
            or day not in sessions[:-1]
            or value["first_execution_day"] != sessions[sessions.index(day) + 1]
            or not original._stamp(value["registered_at"]) <= now
            or original._stamp(value["registered_at"]) >= original._stamp(day + "T15:00:00+08:00")
            or value["base_binding"]["scope_members"] != base["scope_members"]
            or value["base_binding"]["calendar_sha256"] != base["calendar_sha256"]):
        raise ValueError("Frozen candidate method registration changed")
    return value, _ref(path), payload


def register(candidate_registration, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Paper method needs a new registration directory")
    start = original._stamp(_now().isoformat())
    base, reference, calendar = _candidate_registration(candidate_registration, start)
    finished = original._stamp(_now().isoformat())
    if finished < start or finished >= original._stamp(base["first_decision_day"] + "T15:00:00+08:00"):
        raise ValueError("Paper method must actually register before the original first decision close")
    result = {"schema": REGISTRATION_SCHEMA, "method_id": METHOD_ID,
        "registered_at": finished.isoformat(), "candidate_registration": reference,
        "first_decision_day": base["first_decision_day"], "first_execution_day": base["first_execution_day"],
        "calendar": calendar, "calendar_sha256": base["base_binding"]["calendar_sha256"],
        "base_binding": deepcopy(base["base_binding"]), "config": _config(100000, 5).to_dict(),
        "case_ids": list(CASE_IDS), "protocol": deepcopy(PROTOCOL),
        "target_policy_id": TARGET_POLICY_ID, "risk_policy_id": RISK_POLICY_ID,
        "risk_policy_spec": deepcopy(RULE_SPEC), "new_shared_runner_accounts_only": True, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "registration.json", result)
    return result


def _registered(path, now):
    value = _read(path)
    reference = value["candidate_registration"]
    if digest(reference["file"]) != reference["sha256"]:
        raise ValueError("Original BE registration changed")
    base, actual_ref, calendar = _candidate_registration(reference["file"], now)
    if (value.get("schema") != REGISTRATION_SCHEMA or value.get("method_id") != METHOD_ID
            or reference != actual_ref or value.get("protocol") != PROTOCOL
            or value.get("case_ids") != list(CASE_IDS) or value.get("config") != _config(100000, 5).to_dict()
            or value.get("base_binding") != base["base_binding"] or value.get("calendar") != calendar
            or value.get("calendar_sha256") != base["base_binding"]["calendar_sha256"]
            or value.get("first_decision_day") != base["first_decision_day"]
            or value.get("first_execution_day") != base["first_execution_day"]
            or value.get("target_policy_id") != TARGET_POLICY_ID or value.get("risk_policy_id") != RISK_POLICY_ID
            or value.get("risk_policy_spec") != RULE_SPEC
            or any(value.get(k) is not v for k, v in FLAGS.items())
            or not original._stamp(base["registered_at"]) <= original._stamp(value["registered_at"]) <= now
            or original._stamp(value["registered_at"]) >= original._stamp(value["first_decision_day"] + "T15:00:00+08:00")):
        raise ValueError("New four-case paper method registration changed")
    return value, _ref(path)


def registration(path, first, config, now):
    """Adapter hook used by the existing shared runner before creating books."""
    value, reference = _registered(path, now)
    if (config.to_dict() != value["config"] or first["day"] != value["first_decision_day"]
            or first["execution"] != value["first_execution_day"]
            or first["record"]["paper_registration"] != reference):
        raise ValueError("First publication/config does not bind this four-case registration")
    return {**reference, "registered_at": value["registered_at"],
        "first_decision_day": value["first_decision_day"], "first_execution_day": value["first_execution_day"],
        "candidate_registration": deepcopy(value["candidate_registration"]),
        "config": deepcopy(value["config"]), "case_ids": list(CASE_IDS),
        "protocol": deepcopy(PROTOCOL), "registration_is_original_publication": False}


def _loaded(panel, ipo, master, cache):
    key = str(Path(panel).resolve())
    if key not in cache:
        cache[key] = load_inputs(key, ipo, master)
    return cache[key]


def _input(path, paper_reg, ipo, master, now, cache):
    path = Path(path).resolve()
    value, reference = _read(path), _ref(path)
    if value.get("registration") != paper_reg["candidate_registration"]:
        raise ValueError("Candidate input changed its original method registration")
    source = value["source"]
    if digest(source["input_manifest"]) != source["input_manifest_sha256"]:
        raise ValueError("Candidate own input manifest changed")
    loaded = _loaded(source["input_manifest"], ipo, master, cache)
    started, finished = original._stamp(value["calculation_started_at"]), original._stamp(value["created_at"])
    day = value["day"]
    if (not started <= finished <= now or day < paper_reg["first_decision_day"]
            or value.get("status") != "completed_prospective_candidate_input"
            or loaded["source_days"][-1] != day or finished.date().isoformat() != day):
        raise ValueError("Only actually completed own-date prospective candidate inputs may publish")
    expected = candidate.prepare_loaded_input(loaded, value["catalog_reference"]["file"],
        paper_reg["candidate_registration"]["file"], actual=started, finished_clock=lambda: finished)
    if value != expected or digest(path) != reference["sha256"]:
        raise ValueError("Saved candidate rules/source/clock/evaluations or original facts changed")
    return {"record": value, "reference": {**reference, "day": day,
        "created_at": value["created_at"]}, "loaded": loaded}


def _project(inputs, loaded, paper_reg):
    """Project only this signal's dated facts; never mutate the old32 input."""
    sessions, _ = _calendar(loaded["calendar_payload"])
    days = [item["record"]["day"] for item in inputs]
    first, day = paper_reg["first_decision_day"], loaded["source_days"][-1]
    if days != [d for d in sessions if first <= d <= day] or not days or days[-1] != day:
        raise ValueError("Candidate input prefix must be exact firstD through current D without gaps or duplicates")
    latest = loaded["provenance"]["input_record"]
    panels = deepcopy(loaded["panel_by_day"])
    scope = list(loaded["scope_members"])
    for d, panel in panels.items():
        target = next(row for row in panel["types"] if row["id"] == TYPE_ID)
        target.update(status="unknown", candidates=[], candidate_count=0,
            unresolved_symbols=list(scope), scope_complete=False,
            missing_evidence=["new_method_has_no_candidate_input_for_this_prior_day"])
    for item in inputs:
        value, own = item["record"], item["loaded"]
        source_record = own["provenance"]["input_record"]
        for key in ("source_days", "source_proofs", "panels", "preview_sources"):
            if latest[key][:len(source_record[key])] != source_record[key]:
                raise ValueError("Cumulative candidate inputs changed an earlier own-source prefix")
        d = value["day"]
        base = ExactSourceRawFeed(own["raw"], own["source_days"], scope, context_anchor=own["context_anchor"])
        selected = deepcopy(value["candidates"])
        for row in selected:
            raw = base.rows.get(d, {}).get(row["symbol"], {})
            row.update(date=d, setup_low=original._finite(raw.get("low")),
                structure_high=original._finite(raw.get("high")), confirmed_leader=False)
        target = next(row for row in panels[d]["types"] if row["id"] == TYPE_ID)
        unresolved = list(value["unresolved_symbols"])
        target.update(status="matched" if selected else "unknown" if unresolved else "not_matched",
            candidates=selected, candidate_count=len(selected), unresolved_symbols=unresolved,
            scope_complete=not unresolved,
            missing_evidence=["lagged_broad_industry_not_verified_theme", "exchange_PIT_and_intraday_absorption_unverified"])
    return {**loaded, "panel_by_day": panels}


def _prepared(candidate_inputs, panel, ipo, master, registration_file, observed, cache):
    reg, reg_ref = _registered(registration_file, observed)
    items = [_input(path, reg, ipo, master, observed, cache) for path in candidate_inputs]
    loaded = _loaded(panel, ipo, master, cache)
    if not items or Path(items[-1]["record"]["source"]["input_manifest"]).resolve() != Path(panel).resolve():
        raise ValueError("Current publication must use its latest candidate input's own manifest")
    projected = _project(items, loaded, reg)
    day, execution, environment, prepared = original._prepare(projected, observed)
    prepared = [row for row in prepared if row["case_id"] in CASE_IDS]
    if [row["case_id"] for row in prepared] != list(CASE_IDS):
        raise ValueError("Exactly the original two guards by two capacities are required")
    return reg_ref, items, loaded, projected, day, execution, environment, prepared


def _finalize(day, execution, environment, prepared, bindings, recorded, prepared_at,
              reg_ref, inputs):
    if recorded.date().isoformat() != day or recorded < original._stamp(day + "T15:05:00+08:00"):
        raise ValueError("Publication must actually form on its own D at/after 15:05; no late binding or date roll")
    result = original._finalize(day, execution, environment, prepared, bindings, recorded)
    result.update(schema=SCHEMA, method_id=METHOD_ID, protocol=deepcopy(PROTOCOL),
        risk_policy_id=RISK_POLICY_ID, risk_policy_spec=deepcopy(RULE_SPEC),
        target_policy_id=TARGET_POLICY_ID, paper_registration=deepcopy(reg_ref),
        prepared_at=prepared_at.isoformat(), candidate_inputs=[deepcopy(x["reference"]) for x in inputs],
        limitations=["Four signal cases share the existing Engine/Ledger and original numerical portfolio/cost rules.",
            "Dated identity and sticky full-exit recovery both differ from old32; this is not a pure signal-only fair comparison.",
            "NO_CHANGE preserves the account; only the independent environment rule creates an explicit EXIT_ALL.",
            "PIT, true theme identity, queue quality, account execution and natural-person attribution remain unverified."])
    for row in result["cases"]:
        intent = dict(row["portfolio_intent"])
        intent["source"] = "research:dated_catchup_paper:" + row["case_id"]
        row["portfolio_intent"] = PortfolioIntent.from_dict(intent).to_dict()
    return result


def publish(candidate_inputs, panel, ipo_evidence, research_master_run, registration, out):
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Publication needs a new output directory")
    observed = original._stamp(_now().isoformat())
    values = _prepared(candidate_inputs, panel, ipo_evidence, research_master_run, registration, observed, {})
    reg_ref, inputs, loaded, _, day, execution, env, rows = values
    bindings = original._source_bindings(loaded, observed)
    recorded = original._stamp(_now().isoformat())
    if recorded < observed or digest(panel) != loaded["provenance"]["panel_sha256"]:
        raise ValueError("Publication clock or exact own manifest changed")
    result = _finalize(day, execution, env, rows, bindings, recorded, observed, reg_ref, inputs)
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "paper_plan.json", _clean(result))
    lines = [f"# {day} 按日期先行股补涨信号纸面方案", "",
        f"实际发表 {result['recorded_at']}；准确执行日 {execution}。只发表未绑定研究权重。", "",
        "|case|决定|理由|", "|---|---|---|"]
    lines += [f"|{r['case_id']}|{r['decision']}|{r['reason']}|" for r in result["cases"]]
    lines += ["", "共享既有Engine/Ledger；本发表不建立账户、不产生委托。旧32及BE登记保持原值。", ""]
    (out / "PLAN.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def load_publication(path, ipo, master, now, cache):
    path = Path(path).resolve()
    before, record = digest(path), _read(path)
    observed, stamp = original._stamp(record["prepared_at"]), original._stamp(record["recorded_at"])
    if not observed <= stamp <= now:
        raise ValueError("Actual publication clock is unavailable or changed")
    values = _prepared([r["file"] for r in record["candidate_inputs"]],
        record["source"]["input_manifest"], ipo, master,
        record["paper_registration"]["file"], observed, cache)
    reg_ref, inputs, loaded, projected, day, execution, env, rows = values
    expected = _finalize(day, execution, env, rows, original._source_bindings(loaded, observed),
        stamp, observed, reg_ref, inputs)
    if record != _clean(expected) or digest(path) != before:
        raise ValueError("Publication original candidate prefix, source, rules, weights or intent changed")
    paths, _ = _rank_path(projected["panel_by_day"], set(loaded["scope_members"]), loaded["source_days"][0], day)
    reference = {"day": day, "file": str(path), "sha256": before,
        "recorded_at": record["recorded_at"], "execution_day": execution,
        "input_manifest": str(Path(record["source"]["input_manifest"]).resolve()),
        "input_manifest_sha256": record["source"]["input_manifest_sha256"]}
    return {"day": day, "execution": execution, "record": record, "loaded": loaded,
        "intents": {row["case_id"]: PortfolioIntent.from_dict(row["portfolio_intent"]) for row in record["cases"]},
        "reference": reference, "paths": paths,
        "authoritative_panels": {day: deepcopy(projected["panel_by_day"][day])}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["register", "publish"])
    parser.add_argument("--candidate-registration", type=Path)
    parser.add_argument("--candidate-input", action="append", type=Path, default=[])
    for name in ("panel", "ipo-evidence", "research-master-run", "registration"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.operation == "register":
        if args.candidate_registration is None:
            parser.error("register requires --candidate-registration")
        result = register(args.candidate_registration, args.out)
    else:
        if not args.candidate_input or any(getattr(args, k) is None for k in
                ("panel", "ipo_evidence", "research_master_run", "registration")):
            parser.error("publish requires cumulative candidate inputs, panel, IPO/master and registration")
        result = publish(args.candidate_input, args.panel, args.ipo_evidence,
            args.research_master_run, args.registration, args.out)
    print(json.dumps({k: result.get(k) for k in ("schema", "registered_at", "source_close_day", "case_count")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
