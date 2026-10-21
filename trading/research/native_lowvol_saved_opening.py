"""Read one native low-volatility book and its explicit next publication.

A first publication is a research plan, not an account.  This adapter never
creates a Ledger, advances the Engine, ranks securities or obtains quotes.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import io
import json
import os
from pathlib import Path

import pandas as pd

from ..config import StrategyConfig
from ..contracts import MARKET_TZ, PreopenPortfolioIntent, RunContext, TradingMode
from ..target_exit_lifecycle import PREOPEN_FULL_EXIT_LIFECYCLE, validate_binding
from .native_lowvol_forward import SCHEMA as HEAD_SCHEMA, JOURNAL_SCHEMA, _context
from .native_lowvol_publication import METHOD_ID, VARIANT, load_publication
from .saved_account_intent import FLAGS as INTENT_FLAGS
from .saved_leader_account_intent import _stamp


SOURCE_SCHEMA = "native_lowvol_saved_opening_source_v1"
SCHEMA = "saved_native_lowvol_preopen_intent_v1"
OPENING_SCHEMA = "native_lowvol_saved_opening_v1"
FAMILY = "native_lowvol"
CASE_ID = VARIANT
FLAGS = {**INTENT_FLAGS, "engine_advanced": False, "ledger_mutated": False,
    "account_created": False, "broker_interacted": False, "ranks_recomputed": False,
    "returns_reportable": False, "old_engineering_account_migrated": False}


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _ref(value):
    return {"path": str(Path(value.get("path") or value["file"]).resolve()),
            "sha256": value["sha256"]}


def _read(path, expected=None):
    path = Path(path).resolve()
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Saved low-volatility metadata exceeds its bounded reader")
    data = path.read_bytes()
    reference = {"path": str(path), "sha256": sha256(data).hexdigest()}
    if expected is not None and reference != _ref(expected):
        raise ValueError("Saved low-volatility source bytes changed")
    return json.loads(data.decode("utf-8-sig")), reference


def _source_path(value):
    path = Path(value).resolve()
    return path / "head.json" if path.is_dir() else path


def _boundary(value):
    if (value.get("candidate_panel_only") is not True
            or any(value.get(k) is not False for k in
                   ("execution_admitted", "selection_enabled", "strategy_validated"))):
        raise ValueError("Native low-volatility research boundary changed")


def _publication_refs(publication):
    """Keep immutable publication inputs, excluding its historical mutable head."""
    refs, seen, record = [], set(), publication
    while record is not None:
        own = _ref(record["publication_ref"])
        if own["path"] in seen:
            raise ValueError("A publication cannot repeat in its own parent chain")
        seen.add(own["path"])
        refs += [own, _ref(record["payload_ref"]), _ref(record["calendar_ref"])]
        refs += [_ref(r) for r in record["source_refs"]]
        if record["account_ref"] is not None:
            refs.append(_ref(record["account_ref"]))
        parent = record["previous_publication_ref"]
        if parent is None:
            break
        record, reference = _read(parent["file"], parent)
        record["publication_ref"] = {"file": reference["path"], "sha256": reference["sha256"]}
    unique = {}
    for reference in refs:
        if reference["path"] in unique and unique[reference["path"]] != reference:
            raise ValueError("An immutable publication input has conflicting references")
        unique[reference["path"]] = reference
    return list(unique.values())


def _facts(run_dir, publication_path, current):
    directory = Path(run_dir).resolve()
    publication = load_publication(publication_path, now=current)
    config = StrategyConfig(**publication["config"]).validate()
    context = _context(directory).to_dict()
    dependencies = _publication_refs(publication)
    refs = {"publication": _ref(publication["publication_ref"]),
            "calendar": _ref(publication["calendar_ref"])}
    head_path = directory / "head.json"
    head_dependency = {"path": str(head_path), "exists": head_path.exists()}
    if not head_path.exists():
        if publication["account_ref"] is not None or publication["previous_publication_ref"] is not None:
            raise ValueError("A continuing publication cannot stand in for a missing account head")
        return {"publication": publication, "head": None, "refs": refs,
            "dependencies": dependencies, "account_head_dependency": head_dependency,
            "context": context, "source_known_at": publication["published_at"]}
    head, head_ref = _read(head_path)
    _boundary(head)
    binding = head["binding"]
    d = publication["source_day"]
    if (head.get("schema") != HEAD_SCHEMA or head.get("method_id") != METHOD_ID
            or binding.get("method_id") != METHOD_ID or binding.get("run_directory") != str(directory)
            or head.get("config") != config.to_dict() or binding.get("config") != config.to_dict()
            or head.get("context") != context or binding.get("context") != context
            or binding.get("mode") != TradingMode.REPLAY.value
            or binding.get("full_exit_lifecycle") != PREOPEN_FULL_EXIT_LIFECYCLE
            or binding.get("calendar_ref") != publication["calendar_ref"]
            or binding.get("inception") != publication["inception"]
            or head["state"].get("broker_binding") or head["state"].get("day") != d):
        raise ValueError("An exact same-method native source-D account is required; no account migration")
    validate_binding(head["state"], PREOPEN_FULL_EXIT_LIFECYCLE, RunContext.from_dict(context))
    consumed, journals, daily = head["consumed_publications"], head["journal_files"], head["daily"]
    if (not consumed or len(consumed) != len(journals) or len(daily) != len(consumed)
            or daily[-1]["date"] != d or consumed[-1] != publication["previous_publication_ref"]
            or publication.get("account_snapshot") != {"method_id": METHOD_ID,
                "config": config.to_dict(), "context": context, "state": head["state"]}):
        raise ValueError("Next publication does not follow the exact saved native account snapshot")
    journal_path = Path(journals[-1]["file"]).resolve()
    if journal_path != directory / "journal" / (d + ".json"):
        raise ValueError("Native account session seal escaped its own run directory")
    journal, journal_ref = _read(journal_path, journals[-1])
    _boundary(journal)
    if (journal.get("schema") != JOURNAL_SCHEMA or journal["binding_sha256"] != _hash(binding)
            or journal["publication_ref"] != consumed[-1] or journal["execution_day"] != d
            or journal["previous_journal_sha256"] != (journals[-2]["sha256"] if len(journals) > 1 else None)
            or journal["post_state"] != head["state"] or journal["post_state_sha256"] != _hash(head["state"])
            or journal["post_daily"] != daily or journal["post_daily_sha256"] != _hash(daily)
            or any(journal[k] != head[k] for k in
                   ("raw_sources", "action_prefix", "action_scope", "action_evidence_refs"))):
        raise ValueError("Native account differs from its latest immutable session seal")
    committed = _stamp(journal["committed_at"])
    if not _stamp(d + "T15:00:00+08:00") <= committed <= _stamp(publication["published_at"]) <= current:
        raise ValueError("Source-D account was not sealed before its next publication")
    source_proofs = [p for p in head["raw_sources"] if p["day"] == d]
    if source_proofs != [publication["source_proof"]]:
        raise ValueError("Next publication and saved account do not share exact source-D raw quotes")
    dependencies.append(journal_ref)
    dependencies += [_ref(r) for r in journal["action_evidence_refs"]]
    refs.update(native_head=head_ref, latest_journal=journal_ref)
    head_dependency["sha256"] = head_ref["sha256"]
    return {"publication": publication, "head": head, "refs": refs,
        "dependencies": list({r["path"]: r for r in dependencies}.values()),
        "account_head_dependency": head_dependency, "context": context,
        "source_known_at": max(_stamp(publication["published_at"]), committed).isoformat()}


def source_dependencies_current(source_index):
    """Check explicit immutable dependencies and the separately mutable head."""
    try:
        source, _ = _read(_source_path(source_index))
        if source.get("schema") != SOURCE_SCHEMA:
            return False
        for reference in source["dependencies"]:
            path = Path(reference["path"])
            if sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
                return False
        head = source["account_head_dependency"]
        path = Path(head["path"])
        if path.exists() != head["exists"]:
            return False
        return not head["exists"] or sha256(path.read_bytes()).hexdigest() == head["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return False


def build_source(run_dir, publication, out, *, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    directory, original = Path(out).resolve(), Path(run_dir).resolve()
    facts = _facts(original, publication, current)
    p, head = facts["publication"], facts["head"]
    protected = [original, Path(facts["refs"]["publication"]["path"]).parent]
    if any(directory == parent or directory in parent.parents or parent in directory.parents for parent in protected):
        raise ValueError("Composite source must remain separate from native account and publication directories")
    source = {"schema": SOURCE_SCHEMA, "source_family": FAMILY, "method_id": METHOD_ID,
        "status": "saved_native_account_and_next_publication" if head else "published_plan_before_account_creation",
        "case_ids": [CASE_ID], "account_count": 1 if head else 0,
        "native_run_directory": str(original), "run_directory": str(directory),
        "source_day": p["source_day"], "execution_day": p["execution_day"],
        "published_at": p["published_at"], "source_known_at": facts["source_known_at"],
        "kind": p["kind"], "weights": deepcopy(p["weights"]), "rank_day": p["rank_day"],
        "inception": p["inception"], "execution_config": deepcopy(p["config"]),
        "context": facts["context"], "cash_fen": head["state"]["cash"] if head else None,
        "positions": {s: sum(l["qty"] for l in lots) for s, lots in head["state"]["positions"].items()
            if sum(l["qty"] for l in lots)} if head else None,
        "equity_rmb": head["daily"][-1]["equity"] if head else None, "metrics": None,
        "refs": facts["refs"], "dependencies": facts["dependencies"],
        "account_head_dependency": facts["account_head_dependency"],
        "created_at": current.isoformat(), **FLAGS}
    source["input_id"] = _hash({k: v for k, v in source.items() if k != "created_at"})
    target = directory / "head.json"
    if target.exists():
        old, _ = _read(target)
        if old.get("input_id") == source["input_id"]:
            return old
        if (old.get("schema") != SOURCE_SCHEMA or old.get("native_run_directory") != str(original)
                or old.get("method_id") != METHOD_ID or old.get("inception") != source["inception"]):
            raise ValueError("A composite source cannot change its original native account or inception policy")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "head.tmp.json"
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(source, handle, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(temporary, target)
    return source


def _load(source_index, current):
    path = _source_path(source_index)
    source, source_ref = _read(path)
    if (source.get("schema") != SOURCE_SCHEMA or source.get("source_family") != FAMILY
            or source.get("case_ids") != [CASE_ID] or source.get("run_directory") != str(path.parent)
            or any(source.get(k) is not v for k, v in FLAGS.items())
            or not source_dependencies_current(path)):
        raise ValueError("Native low-volatility composite source is stale or corrupt")
    facts = _facts(source["native_run_directory"], source["refs"]["publication"]["path"], current)
    if (source["refs"] != facts["refs"] or source["dependencies"] != facts["dependencies"]
            or source["account_head_dependency"] != facts["account_head_dependency"]
            or source["source_known_at"] != facts["source_known_at"]
            or source["context"] != facts["context"]
            or source["account_count"] != (1 if facts["head"] else 0)
            or any(source[k] != facts["publication"][k] for k in
                   ("source_day", "execution_day", "published_at", "kind", "weights", "rank_day", "inception"))
            or source["execution_config"] != facts["publication"]["config"]
            or not _stamp(source["source_known_at"]) <= _stamp(source["created_at"]) <= current
            or source["input_id"] != _hash({k: v for k, v in source.items() if k not in {"created_at", "input_id"}})):
        raise ValueError("Composite source differs from its exact saved native originals")
    return source, source_ref, facts


def export_requests(source_index, execution_day=None, *, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    source, reference, facts = _load(source_index, current)
    if execution_day is not None and execution_day != source["execution_day"]:
        raise ValueError("An opening request cannot move the publication's exact execution date")
    head, publication = facts["head"], facts["publication"]
    lifecycle = head["state"]["full_exit_lifecycle"] if head else None
    remaining = sorted((lifecycle or {}).get("remaining_full_exits", {}))
    if publication["kind"] == "FULL_TARGET":
        remaining = [s for s in remaining if not publication["weights"].get(s, 0)]
    return {CASE_ID: {"schema": SCHEMA, "source_family": FAMILY, "case_id": CASE_ID,
        "strategy_id": FAMILY + ":" + CASE_ID, "run_directory": source["run_directory"],
        "native_run_directory": source["native_run_directory"], "source_day": source["source_day"],
        "execution_day": source["execution_day"], "kind": source["kind"], "weights": deepcopy(source["weights"]),
        "original_weights": deepcopy(source["weights"]), "remaining_exit_symbols": remaining,
        "target_version": lifecycle["target_version"] if lifecycle else None,
        "source_known_at": source["source_known_at"], "created_at": current.isoformat(),
        "execution_config": deepcopy(source["execution_config"]), "context": deepcopy(source["context"]),
        "account_count": source["account_count"], "account_scope": "whole_saved_research_account" if head else "not_created",
        "account_created_at_export": False, "source_basis": "saved_native_book_and_actual_next_publication" if head else
            "published_research_plan_before_account_creation_no_synthetic_ledger",
        "rank_day": source["rank_day"], "publication": deepcopy(publication["publication_ref"]),
        "refs": {"head": reference, **deepcopy(source["refs"])}, **FLAGS}}


def read_request(payload, *, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA or payload.get("case_id") != CASE_ID:
        raise ValueError("An exact saved native low-volatility request is required")
    expected = export_requests(payload["refs"]["head"]["path"], payload["execution_day"], current=current)[CASE_ID]
    if not _stamp(expected["source_known_at"]) <= _stamp(payload["created_at"]) <= current:
        raise ValueError("Saved opening request has an invalid creation clock")
    expected["created_at"] = payload["created_at"]
    if payload != expected:
        raise ValueError("Saved native low-volatility request differs from its original plan/account")
    return expected


def load_saved_case(source_index, case_id=CASE_ID, *, current=None):
    current = _stamp(current or datetime.now(MARKET_TZ))
    source, reference, facts = _load(source_index, current)
    if case_id != CASE_ID:
        raise ValueError("Unknown native low-volatility research case")
    head = facts["head"]
    executed, reason = None, "research_plan_account_not_created"
    if head:
        reason = "latest_state_has_no_exact_consumed_preopen_receipt"
        receipt = head["state"].get("last_preopen_intent")
        if isinstance(receipt, dict) and isinstance(receipt.get("envelope"), dict):
            envelope = PreopenPortfolioIntent.from_dict(receipt["envelope"])
            consumed = head["consumed_publications"][-1]
            original, original_ref = _read(consumed["file"], consumed)
            if (receipt.get("source_day") == envelope.source_day == original["source_day"]
                    and receipt.get("execution_day") == envelope.execution_day == original["execution_day"] == source["source_day"]
                    and receipt.get("published_at") == envelope.published_at == original["published_at"]
                    and original["method_id"] == METHOD_ID and original["config"] == source["execution_config"]
                    and envelope.intent.context.to_dict() == source["context"]
                    and envelope.intent.kind.value == original["kind"]
                    and envelope.intent.to_legacy_pending() == original["weights"]
                    and envelope.intent.source == f"research:{METHOD_ID}:{original_ref['sha256']}"):
                executed = {"source_day": envelope.source_day, "execution_day": envelope.execution_day,
                    "kind": envelope.intent.kind.value, "weights": deepcopy(original["weights"]),
                    "published_at": envelope.published_at, "ref": original_ref,
                    "context": deepcopy(source["context"]), "consumed_receipt": deepcopy(receipt)}
                reason = None
    return {"state": deepcopy(head["state"]) if head else None,
        "daily": deepcopy(head["daily"][-1]) if head else None,
        "context": deepcopy(source["context"]), "config": deepcopy(source["execution_config"]),
        "ledger_day": head["state"]["day"] if head else None, "account_count": source["account_count"],
        "status": "saved_account" if head else "research_plan_account_not_created",
        "executed_decision": executed, "executed_decision_reason": reason,
        "basis": "exact_native_lowvol_head_next_publication_and_independent_session_seal",
        "refs": {"head": reference, **deepcopy(source["refs"])}, **FLAGS}


def _quotes(publication, wanted):
    from .leader_eight_type_opening import _quote_rows
    proof = publication["source_proof"]
    path = Path(proof["source_folder"]).resolve() / "raw_daily.csv"
    if proof.get("raw_file") is not None and Path(proof["raw_file"]).resolve() != path:
        raise ValueError("Published source-D price file escaped its own exact close package")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Exact source-D raw close exceeds its bounded reader")
    data = path.read_bytes()
    if sha256(data).hexdigest() != proof["raw_sha256"]:
        raise ValueError("Published exact source-D raw price bytes changed")
    rows = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False)
    if (not {"date", "code", "close", "tradestatus", "isST", "adjustflag"} <= set(rows)
            or not rows.date.eq(publication["source_day"]).all() or not rows.adjustflag.eq("3").all()):
        raise ValueError("Exact source-D quotes require whole native raw rows, dates and adjustflag=3")
    return _quote_rows(rows.to_dict("records"), publication["source_day"], wanted)


def build_opening(source_index, out, *, current=None):
    from .leader_eight_type_opening import _case_preview, _write
    from . import opening_pack
    current = _stamp(current or datetime.now(MARKET_TZ))
    source, reference, facts = _load(source_index, current)
    intent = export_requests(source_index, current=current)[CASE_ID]
    directory = Path(out).resolve()
    protected = (Path(source["run_directory"]), Path(source["native_run_directory"]),
                 Path(source["refs"]["publication"]["path"]).parent)
    if any(directory == p or directory in p.parents or p in directory.parents for p in protected):
        raise ValueError("Opening output must remain separate from composite, account and publication inputs")
    if directory.exists():
        raise FileExistsError("Use a new native low-volatility opening output directory")
    head = facts["head"]
    quotes = {}
    if head and (intent["kind"] == "FULL_TARGET" or intent["remaining_exit_symbols"]):
        wanted = set(intent["weights"] or {}) | set(intent["remaining_exit_symbols"])
        wanted.update(s for s, lots in head["state"]["positions"].items() if sum(l["qty"] for l in lots))
        quotes = _quotes(facts["publication"], wanted)
    directory.mkdir(parents=True, exist_ok=False)
    _write(directory / "intent.json", intent)
    if head:
        row = _case_preview(directory / CASE_ID, intent, {"state": head["state"], "daily": head["daily"]},
                            quotes, current=current)
        row["strategy_id"] = intent["strategy_id"]
    else:
        row = {"case_id": CASE_ID, "strategy_id": intent["strategy_id"], "status": "not_ready",
            "reason": "published_research_plan_account_not_created", "kind": intent["kind"],
            "weights": deepcopy(intent["weights"]), "rank_day": intent["rank_day"],
            "account_count": 0, "cash_rmb": None, "equity_rmb": None, "positions": None,
            "candidate_order_count": None, "intent": str(directory / "intent.json"), **FLAGS}
    if not source_dependencies_current(source_index) or _read(_source_path(source_index))[1] != reference:
        raise ValueError("Saved native source changed during read-only opening preparation")
    previews = [row["preview"]] if row["status"] == "paper_preview_only" else []
    pack_dir, pack = opening_pack.build(previews, execution_day=source["execution_day"]) if previews else (None, None)
    result = {"schema": OPENING_SCHEMA, "source_family": FAMILY,
        "status": "paper_candidates_only" if previews else "not_ready", "generated_at": current.isoformat(),
        "source_run_directory": source["run_directory"], "source_head_sha256": reference["sha256"],
        "source_cutoff": source["source_day"], "execution_day": source["execution_day"],
        "account_count": source["account_count"], "case_count": 1, "preview_count": len(previews),
        "not_ready_count": 1-len(previews), "cases": [row],
        "opening_pack": str(pack_dir / "opening_pack.json") if pack_dir else None,
        "candidate_order_count": pack["candidate_order_count"] if pack else None,
        "cash_fen": source["cash_fen"], "equity_rmb": source["equity_rmb"], "positions": source["positions"],
        "metrics": None, "automatic_strategy_selection": False, "candidate_orders_are_fills": False, **FLAGS}
    _write(directory / "result.json", result)
    return directory, result


# Explicit producer names for coordinator integrations; both operate on saved files.
prepare = build_source
preview = build_opening
