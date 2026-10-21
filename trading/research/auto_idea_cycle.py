"""Advance the research-only idea pipeline without return-driven retuning.

The cycle discovers the newest completed factor library and walks its fixed
candidate path through the shared Engine/Ledger consumers.  Completed stages
are reused by identity; only missing stages are run when ``--execute-missing``
is supplied.  This is an operational convenience, not a promotion gate.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from .. import settings
from .experiment import write_json


PIPELINE = (
    ("library", "factor_libraries", "completed_research_diagnostics"),
    ("pack", "factor_candidate_packs", "completed_research_candidate_pack"),
    ("replay", "factor_candidate_replays", "completed_factor_candidate_replay"),
    ("family", "factor_candidate_family_replays", "completed_factor_candidate_family_replay"),
    ("cadence", "factor_candidate_cadence_replays", "completed_factor_candidate_cadence_replay"),
    ("cost", "factor_candidate_cost_replays", "completed_factor_candidate_cost_replay"),
    ("attribution", "factor_candidate_attributions", "completed_factor_candidate_attribution"),
)


def _records(folder, status):
    root = settings.ROOT / "outputs" / folder
    if not root.exists():
        return []
    records = []
    for path in root.glob("*/run.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if record.get("status") != status:
            continue
        result = record.get("result")
        if isinstance(result, dict) and result.get("status") not in {None, "completed"}:
            continue
        records.append((path, record))
    return sorted(records, key=lambda item: item[0].stat().st_mtime, reverse=True)


def _path(value):
    return str(Path(value).resolve()) if value else None


def _same(value, path):
    return value is not None and _path(value) == _path(path)


def _latest_related(folder, status, relation_key, source_path):
    for path, record in _records(folder, status):
        if _same(record.get(relation_key), source_path):
            return path, record
    return None, None


def _latest_failed_related(folder, relation_key, source_path):
    """Find a failed attempt without treating it as a completed stage.

    Failed runs are evidence, not reusable outputs.  Keeping the newest one
    attached to the stage prevents an unchanged data defect from being rerun
    by every heartbeat while preserving the last successful diagnostic path.
    """
    root = settings.ROOT / "outputs" / folder
    if not root.exists():
        return None, None
    records = []
    for path in root.glob("*/run.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not _same(record.get(relation_key), source_path):
            continue
        status = str(record.get("status") or "")
        result_status = str((record.get("result") or {}).get("status") or "")
        if not (status.startswith("failed") or result_status == "failed"):
            continue
        records.append((path, record))
    return max(records, key=lambda item: item[0].stat().st_mtime, default=(None, None))


def _attach_failed_attempt(stage, folder, relation_key, source_path, completed_path):
    failed_path, failed = _latest_failed_related(folder, relation_key, source_path)
    if not failed_path:
        return
    if completed_path and failed_path.stat().st_mtime <= completed_path.stat().st_mtime:
        return
    stage["latest_failed_attempt"] = {
        "run": _path(failed_path),
        "status": failed.get("status"),
        "error": (failed.get("result") or {}).get("error") or failed.get("error"),
        "completed_sessions": (failed.get("result") or {}).get("completed_sessions"),
        "last_completed_session": (failed.get("result") or {}).get("last_completed_session"),
        "attempted_session": (failed.get("result") or {}).get("attempted_session"),
        "rerun_required": "new_fixed_input_or_explicit_override",
    }


def _replay_frontier(stages):
    """Read the timing receipt produced by the shared Ledger replay, if any."""
    replay = next((item for item in stages if item.get("stage") == "replay" and item.get("run")), None)
    if not replay:
        return {"status": "missing_replay_frontier", "live_feed_connected": False}
    run_path = Path(replay["run"])
    try:
        record = json.loads(run_path.read_text(encoding="utf-8"))
        result = record.get("result") or {}
        artifacts = result.get("artifacts") or record.get("artifacts")
        signal = result.get("signal")
        replay_path = Path(artifacts) / signal / "replay.json" if artifacts and signal else None
        if replay_path and replay_path.is_file():
            return json.loads(replay_path.read_text(encoding="utf-8")).get(
                "event_time_frontier", {"status": "missing_replay_frontier", "live_feed_connected": False})
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {"status": "missing_replay_frontier", "live_feed_connected": False}


def _idea_frontier(*, execute_missing=False):
    """Attach the fixed-rule frontier to the recurring research consumer.

    The factor pipeline and the compact market-replay frontier have different
    source identities, so the frontier is deliberately kept as a sibling
    result rather than being forced into the factor-stage dependency chain.
    A failed or unavailable frontier is retained as evidence and never turns
    into a promotion or a Ledger write.
    """
    try:
        from . import idea_frontier

        run_path = None
        run_record = None
        if execute_missing:
            run_path, run_record = idea_frontier.run(execute_missing=True)
        frontier = idea_frontier.discover()
        return {
            "status": "available",
            "frontier": frontier,
            "run": str(run_path.resolve()) if run_path else None,
            "run_status": (run_record or {}).get("status") if run_record else None,
        }
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {
            "status": "unavailable",
            "frontier": None,
            "run": None,
            "run_status": None,
            "error": f"{type(error).__name__}: {error}",
            "research_only": True,
            "execution_admitted": False,
        }


def discover():
    """Return the newest fixed pipeline and explicit missing-stage reasons."""
    stages = []
    library_path, library = next(iter(_records("factor_libraries", "completed_research_diagnostics")), (None, None))
    stages.append({"stage": "library", "status": "reused" if library else "missing",
                   "run": _path(library_path) if library_path else None,
                   "research_family": library.get("research_family") if library else None})
    if not library:
        return stages

    pack_path, pack = _latest_related("factor_candidate_packs", "completed_research_candidate_pack",
                                      "source_run", library_path)
    stages.append({"stage": "pack", "status": "reused" if pack else "missing",
                   "run": _path(pack_path) if pack_path else None,
                   "source_run": _path(library_path)})
    if not pack:
        return stages

    replay_path, replay = _latest_related("factor_candidate_replays", "completed_factor_candidate_replay",
                                          "candidate_run", pack_path)
    stages.append({"stage": "replay", "status": "reused" if replay else "missing",
                   "run": _path(replay_path) if replay_path else None,
                   "source_run": _path(pack_path)})
    replay_stage = stages[-1]
    _attach_failed_attempt(
        replay_stage, "factor_candidate_replays", "candidate_run", pack_path, replay_path
    )
    if not replay:
        failed_path, failed = _latest_failed_related("factor_candidate_replays", "candidate_run", pack_path)
        if failed_path:
            replay_stage.update(
                status="blocked",
                run=_path(failed_path),
                error=(failed.get("result") or {}).get("error") or failed.get("error"),
                next_action="change or repair the pinned input before an explicit replay rerun",
            )
        return stages

    family_path, family = _latest_related("factor_candidate_family_replays",
                                          "completed_factor_candidate_family_replay",
                                          "source_run", replay_path)
    stages.append({"stage": "family", "status": "reused" if family else "missing",
                   "run": _path(family_path) if family_path else None,
                   "source_run": _path(replay_path)})
    if not family:
        return stages

    # Downstream diagnostics must follow the newest completed path.  In
    # particular, cost and attribution should consume the fixed-cadence
    # account when that stage exists, rather than silently falling back to the
    # daily family replay.
    source = family_path
    for stage, folder, status in PIPELINE[4:]:
        path, record = _latest_related(folder, status, "source_run", source)
        stages.append({"stage": stage, "status": "reused" if record else "missing",
                       "run": _path(path) if path else None,
                       "source_run": _path(source)})
        if record:
            source = path
    return stages


def _execute_missing(stages):
    """Run only the first missing stage, then rediscover dependencies."""
    missing = next((item for item in stages if item["status"] == "missing"), None)
    if missing is None:
        return None
    stage = missing["stage"]
    source = missing.get("source_run")
    if stage == "pack":
        from .factor_candidate_pack import run
        _, record = run(source)
    elif stage == "replay":
        from .factor_candidate_replay import run
        _, record = run(source, rights_replay_policy="waive", merger_replay_policy="floor")
    elif stage == "family":
        from .factor_candidate_family_replay import run
        _, record = run(source)
    elif stage == "cadence":
        from .factor_candidate_cadence_replay import run
        _, record = run(source)
    elif stage == "cost":
        from .factor_candidate_cost_replay import run
        _, record = run(source)
    elif stage == "attribution":
        from .factor_candidate_attribution import run
        _, record = run(source)
    else:
        raise ValueError(f"No executable action for stage {stage}")
    return {"stage": stage, "status": record.get("status"), "run": record.get("id"),
            "source_run": source}


def run(*, execute_missing=False):
    identifier = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = settings.ROOT / "outputs" / "auto_research_cycles" / identifier
    out.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    actions = []
    stages = discover()
    if execute_missing:
        while any(item["status"] == "missing" for item in stages):
            try:
                action = _execute_missing(stages)
                if action is None:
                    break
                actions.append(action)
                stages = discover()
                # A completed stage is enough to continue.  Failed runs are
                # retained in their own receipts and stop this cycle cleanly.
                if action["status"] not in {
                    "completed_research_candidate_pack",
                    "completed_factor_candidate_replay",
                    "completed_factor_candidate_family_replay",
                    "completed_factor_candidate_cadence_replay",
                    "completed_factor_candidate_cost_replay",
                    "completed_factor_candidate_attribution",
                }:
                    break
            except Exception as error:
                actions.append({"stage": next(item["stage"] for item in stages if item["status"] == "missing"),
                                "status": "failed", "error": f"{type(error).__name__}: {error}"})
                break
    frontier = _idea_frontier(execute_missing=execute_missing)
    final = {
        "id": identifier,
        "schema": "quant_research_auto_idea_cycle_v1",
        "status": "completed_research_auto_idea_cycle",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "execute_missing": bool(execute_missing),
        "actions": actions,
        "stages": stages,
        "idea_frontier": frontier,
        "event_time_frontier": _replay_frontier(stages),
        "research_only": True,
        "execution_admitted": False,
        "fresh_holdout_claimed": False,
        "limitations": [
            "The cycle reuses fixed source snapshots and never selects parameters from observed returns",
            "A completed replay is diagnostic evidence, not strategy validation or account admission",
            "No broker submission, account mutation or Ledger promotion is performed",
            "Missing PIT-complete financial inputs and prospective paper observation remain explicit unknowns",
            "Fixed idea-frontier candidates are attached as research-only evidence; failed or missing candidates are not promoted",
        ],
    }
    write_json(out / "run.json", final)
    (out / "REPORT.md").write_text(
        "\n".join([
            "# Automatic Research Idea Cycle", "", "Research-only; `NO_TRADE`.", "",
            f"Cycle: `{identifier}`; executed missing stages: `{bool(execute_missing)}`.",
            "Completed stages are reused by source identity; no return-based retuning is performed.", "",
            "## Stage status", "",
            *[f"- {item['stage']}: {item['status']} ({item.get('run') or 'UNKNOWN'})" for item in stages],
            "", "## Fixed idea frontier", "",
            f"- status: `{frontier['status']}`",
            f"- run: `{frontier.get('run') or 'UNKNOWN'}`",
            *[
                f"- {item['candidate']}: {item['status']}"
                for item in ((frontier.get('frontier') or {}).get('candidates') or [])
            ],
            "", "## Boundaries", "", *[f"- {item}" for item in final["limitations"]], "",
        ]), encoding="utf-8")
    print(json.dumps(final, ensure_ascii=False, indent=2))
    return out, final


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-missing", action="store_true",
                        help="run only the first missing fixed research stage, then continue")
    args = parser.parse_args(argv)
    run(execute_missing=args.execute_missing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
