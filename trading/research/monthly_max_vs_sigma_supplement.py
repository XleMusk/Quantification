"""One late-primary-source recheck of saved monthly MAX/sigma formations.

No formation, scoring, collection, checkpoint continuation or original writes.
Only the two registered blank reserve terms may differ from the frozen pair.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import json

import pandas as pd

from ..market_snapshot import load_action_resolutions
from .. import market_snapshot
from . import monthly_max_vs_sigma as v1
from .adjusted_prices import checked_file
from . import action_reviewed_amendment


METHOD = "monthly_max_vs_sigma_two_primary_source_recheck_v1"
EVENTS = (("600507.SSE", "2024-05-09"), ("002293.SZSE", "2024-06-17"))
PRIMARY_SHA = {"600507.SSE": "e249ea939ecbd9f775c9dd99538fc6c3f33f15972d40c7794dba6b04370a1047",
    "002293.SZSE": "ddfbd0c9466e17cee2fa57aa38dbab0c698c61accd4622b003c9c617afd00d19"}
FORMATION_SHA = {"batches.json": "1e26d490b00da56fa8ba9497348e3ca9f7ee7178ba4bbf080a86f4b1e5768fa6",
    "scores.parquet": "0167e64dab8c3e33650627d0b117346b168b9d532072d94f4ff378f003874bc0"}
SPEC = dict(id=METHOD, original_rules=v1.SPEC, events=[list(key) for key in EVENTS],
    source_change="exact_two_blank_reserve_terms_to_reviewed_zero_only",
    saved_formation_sha256=FORMATION_SHA, primary_sha256=PRIMARY_SHA, score_or_target_recomputation=False,
    fresh_accounts_from_original_cash=True, checkpoint_continuation=False,
    maximum_source_supplement_rounds=1, further_unknowns="stop_dependent_arm_without_more_notice_chasing",
    late_evidence_not_historical_PIT=True, independent_holdout=False)


def pin(path):
    path = Path(path).resolve()
    return dict(path=str(path), sha256=v1.digest(path))


def _checked(ref):
    if pin(ref["path"]) != ref:
        raise ValueError("Registered original or source artifact changed: " + ref["path"])
    return Path(ref["path"])


def _code():
    return {**v1._implementations(), str(Path(__file__).resolve()): v1.digest(__file__),
        str(Path(market_snapshot.__file__).resolve()): v1.digest(market_snapshot.__file__),
        str(Path(action_reviewed_amendment.__file__).resolve()): v1.digest(action_reviewed_amendment.__file__)}


def _original(result):
    result = Path(result).resolve()
    value = v1.read(result)
    registration = _checked(value["registration"])
    original = v1._registration(registration)
    if (value.get("schema") != v1.METHOD + "_result" or value.get("spec") != v1.SPEC
            or value.get("config") != v1.config().to_dict()
            or value.get("status") != "completed_two_arm_attempt"
            or value.get("full_paired_account_comparison") is not False
            or [case.get("arm") for case in value.get("cases", [])] != list(v1.ARMS)
            or any(value.get(key) is not flag for key, flag in v1.FLAGS.items())):
        raise ValueError("Original frozen incomplete two-arm result required")
    for case, (symbol, day) in zip(value["cases"], EVENTS):
        expected = f"ValueError: Unresolved held corporate action: {symbol} {day}; missing=dividReserveToStockPs"
        if case.get("status") != "failed" or case.get("error") != expected or case.get("full_period_metrics_available") is not False:
            raise ValueError("Only the exact original two blank-reserve source failures qualify")
    receipt = v1.read(result.parent / "formation_receipt.json")
    batches = v1.read(result.parent / "batches.json")
    if (set(batches) != set(original["decision_days"]) or len(batches) != 32
            or not all(batch["status"] == "complete" for batch in batches.values())
            or receipt.get("common_batch_count") != 32 or receipt.get("formation_rows") != 93188
            or value.get("formation_rows") != 93188 or receipt.get("historical_publication") is not False
            or any(receipt.get(key) is not flag for key, flag in v1.FLAGS.items())):
        raise ValueError("Exact original 32 complete saved formation batches required")
    artifacts = [result, registration, result.parent / "formation_receipt.json"]
    for name, expected in FORMATION_SHA.items():
        path = result.parent / name
        if receipt["files"][name] != pin(path) or v1.digest(path) != expected:
            raise ValueError("Saved formation/scores changed; recomputation is forbidden")
        artifacts.append(path)
    for arm in v1.ARMS:
        artifacts.extend([result.parent / (arm + "_result.json"),
                          result.parent / arm / "account.json", result.parent / arm / "daily.csv"])
    return original, batches, [pin(path) for path in artifacts]


def _pair(original, amendment_receipt):
    """Check existing amendment output; this never invokes its producer."""
    output = v1.read(amendment_receipt)
    folders = [Path(output[key]).resolve() for key in ("event_view", "reference_view")]
    old = [Path(original["inputs"][key]).resolve() for key in ("event_view", "reference_view")]
    before, after = [v1.read(path / "result.json") for path in old], [v1.read(path / "result.json") for path in folders]
    receipt = output["amendment"]
    if (any(left == right for left, right in zip(old, folders))
            or Path(receipt["parent_event_view"]).resolve() != old[0]
            or Path(receipt["parent_reference_view"]).resolve() != old[1]
            or set(map(tuple, receipt["events"])) != set(EVENTS) or len(receipt["events"]) != 2
            or receipt.get("raw_prices_rescanned") is not False or receipt.get("other_events_changed") is not False
            or receipt.get("execution_admitted") is not False
            or any(value.get("reviewed_cash_amendment") != receipt for value in after)
            or Path(after[1]["event_view"]).resolve() != folders[0]):
        raise ValueError("One direct existing reviewed amendment of exactly the original two events required")
    resolutions = load_action_resolutions(receipt["reviewed_root"], {key[0] for key in EVENTS})
    if (len(resolutions) != 2 or {(r["symbol"], r["dividOperateDate"]) for r in resolutions} != set(EVENTS)
            or {r["symbol"]: r["kind"] for r in resolutions} != {"600507.SSE": "issuer_confirmed_cash_only_terms",
                "002293.SZSE": "issuer_confirmed_reserve_zero_term"}
            or {r["symbol"]: r["source_sha256"] for r in resolutions} != PRIMARY_SHA
            or any(r.get("pit_verified") is not False or r.get("execution_admitted") is not False for r in resolutions)
            or any(r.get("reserve_numeric_zero_explicit_in_notice") is not False
                or r.get("complete_cash_only_scheme_projection") is not True
                for r in resolutions if r["symbol"] == "600507.SSE")):
        raise ValueError("Exact typed full-cash interpretation and explicit reserve-zero review required")
    refs = [pin(amendment_receipt)]
    for i, (prior, current) in enumerate(zip(before, after)):
        changed = {"canonical_actions.parquet"} if i == 0 else {"raw_reference_changes.parquet", "input_events.json"}
        allowed = {"files", "reviewed_cash_amendment"} | (set() if i == 0 else
            {"event_view", "reference_formula_status_counts", "reference_formula_basis_counts"})
        if ({key: val for key, val in prior.items() if key not in allowed}
                != {key: val for key, val in current.items() if key not in allowed}
                or any(current["files"].get(name) != sha for name, sha in prior["files"].items() if name not in changed)):
            raise ValueError("Source scope, raw/canonical identity or inherited evidence changed")
        refs.append(pin(folders[i] / "result.json"))
        for name in changed | (set(current["files"]) - set(prior["files"])):
            refs.append(pin(checked_file(folders[i], name, current["files"][name])))
    def frame(i, roots, metadata, name):
        return pd.read_parquet(checked_file(roots[i], name, metadata[i]["files"][name])).fillna("")
    actions = frame(0, old, before, "canonical_actions.parquet")
    terms = frame(0, folders, after, "canonical_actions.parquet")
    expected, _ = action_reviewed_amendment.corrected_actions(actions, resolutions, old[0] / "canonical_actions.parquet")
    mask = pd.Series([(s, d) in EVENTS for s, d in zip(actions.symbol, actions.dividOperateDate)], index=actions.index)
    blank = actions.loc[mask, "dividReserveToStockPs"].eq("").all()
    expected_only = actions.copy(deep=True)
    expected_only.loc[mask, "dividReserveToStockPs"] = "0"
    if int(mask.sum()) != 2 or not blank or not expected.equals(expected_only) or not terms.equals(expected_only):
        raise ValueError("Only two blank reserve terms may become reviewed zero; all known terms/dates remain exact")
    gaps, amended = (frame(1, roots, meta, "raw_reference_changes.parquet")
                     for roots, meta in ((old, before), (folders, after)))
    keys = {(row["code"], row["dividOperateDate"]) for row in resolutions}
    mask = pd.Series([(code, day) in keys for code, day in zip(gaps.code, gaps.date)], index=gaps.index)
    unchanged = [column for column in gaps if column not in {"dividReserveToStockPs", "reference_formula_status",
        "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb"}]
    if (int(mask.sum()) != 2 or list(gaps.columns) != list(amended.columns)
            or not gaps[unchanged].equals(amended[unchanged]) or not gaps.loc[~mask].equals(amended.loc[~mask])
            or not gaps.loc[mask, "dividReserveToStockPs"].eq("").all()
            or not amended.loc[mask, "dividReserveToStockPs"].eq("0").all()
            or v1.read(folders[1] / "input_events.json") != after[0]):
        raise ValueError("Raw reference facts, known amounts or unrelated event rows changed")
    refs.extend(pin(Path(receipt["reviewed_root"]) / row["source_file"]) for row in resolutions)
    refs.append(pin(Path(receipt["reviewed_root"]) / "supplements/provider_action_resolutions.json"))
    reviews = [{key: row.get(key) for key in ("symbol", "kind", "source_url", "source_date", "source_sha256",
        "actual_primary_retrieved_at", "actual_primary_reviewed_at", "reserve_numeric_zero_explicit_in_notice",
        "complete_cash_only_scheme_projection")} for row in resolutions]
    for review in reviews:
        retrieved, reviewed = [datetime.fromisoformat(review[key]) for key in
            ("actual_primary_retrieved_at", "actual_primary_reviewed_at")]
        if retrieved.tzinfo is None or reviewed.tzinfo is None or not review["source_date"] <= retrieved.date().isoformat() or retrieved > reviewed:
            raise ValueError("Actual late primary retrieval/review clocks must remain known and ordered")
    return dict(event_view=str(folders[0]), reference_view=str(folders[1]), amendment=receipt, late_primary_reviews=reviews), refs


def register(original_result, amendment_receipt, out, run_out):
    out, run_out = Path(out).resolve(), Path(run_out).resolve()
    if out.exists() or run_out.exists():
        raise FileExistsError("New source-recheck registration and account directory required")
    original, _, artifacts = _original(original_result)
    pair, refs = _pair(original, amendment_receipt)
    v1.separate(out, [Path(original_result).resolve().parent, run_out, *original["inputs"].values(), pair["event_view"], pair["reference_view"]])
    v1.separate(run_out, [out, Path(original_result).resolve().parent, *original["inputs"].values(), pair["event_view"], pair["reference_view"]])
    record = dict(schema=METHOD + "_registration", registered_at=v1.now(), spec=SPEC,
        config=v1.config().to_dict(),
        original_result=str(Path(original_result).resolve()), original_artifacts=artifacts, source_refs=refs,
        inputs={**original["inputs"], **{key: pair[key] for key in ("event_view", "reference_view")}},
        amendment=pair["amendment"], late_primary_reviews=pair["late_primary_reviews"],
        amendment_receipt=str(Path(amendment_receipt).resolve()),
        run_output=str(run_out), implementations=_code(), source_clock=original["source_clock"],
        source_inventory=original["source_inventory"], source_limitations=original["source_limitations"], **v1.FLAGS)
    out.mkdir(parents=True, exist_ok=False)
    v1.save(out / "registration.json", record)
    return record


def _registration(path):
    saved = v1.read(path)
    if (saved.get("schema") != METHOD + "_registration" or saved.get("spec") != SPEC
            or saved.get("config") != v1.config().to_dict()
            or saved.get("implementations") != _code()
            or any(saved.get(key) is not flag for key, flag in v1.FLAGS.items())):
        raise ValueError("Fixed source-only recheck rules, flags and implementation required")
    for ref in saved["original_artifacts"] + saved["source_refs"]:
        _checked(ref)
    original, batches, artifacts = _original(saved["original_result"])
    pair, refs = _pair(original, saved["amendment_receipt"])
    inputs = {**original["inputs"], **{key: pair[key] for key in ("event_view", "reference_view")}}
    if (saved["original_artifacts"] != artifacts or saved["source_refs"] != refs or saved["inputs"] != inputs
            or saved["amendment"] != pair["amendment"] or saved["late_primary_reviews"] != pair["late_primary_reviews"]
            or any(saved[key] != original[key] for key in ("source_clock", "source_inventory", "source_limitations"))):
        raise ValueError("Registered original formations or matched amended pair changed")
    return saved, original, batches


def run(registration, out):
    out, registration = Path(out).resolve(), Path(registration).resolve()
    if out.exists():
        raise FileExistsError("Existing attempt is never retried or resumed")
    saved, original, batches = _registration(registration)
    if out != Path(saved["run_output"]).resolve():
        raise ValueError("Only the one registered fresh account output is supported")
    v1.separate(out, [registration.parent, Path(saved["original_result"]).parent, *saved["inputs"].values()])
    out.mkdir(parents=True, exist_ok=False)
    record = dict(schema=METHOD + "_result", status="running", started_at=v1.now(), spec=SPEC, config=v1.config().to_dict(),
        registration=pin(registration), original_result=pin(saved["original_result"]), cases=[],
        saved_formation_reused=True, formation_recomputed=False, scores_recomputed=False,
        original_accounts_reused=False, automatic_retry=False, **v1.FLAGS)
    v1.save(out / "started.json", record)
    base = None
    try:
        base = v1.BulkResearchFeed(**{key: saved["inputs"][key] for key in
            ("price_view", "event_view", "reference_view", "listing_master")})
        if base.calendar != original["source_calendar"]:
            raise ValueError("Original execution calendar changed")
        dates = [day for day in base.calendar if v1.START <= day <= v1.END]
        own_config = v1.config()
        for arm in v1.ARMS:
            feed = v1.MonthlyMaxSigmaFeed(base, batches, arm, own_config)
            try:
                case = v1._case_summary(v1.baseline_ledger_result(feed, own_config, dates, out / arm),
                    out / arm, own_config, dates)
            except Exception as error:
                case = dict(status="failed_command", error=f"{type(error).__name__}: {error}",
                    full_period_metrics_available=False, net_return=None, max_drawdown=None)
            case["arm"] = arm
            v1.save(out / (arm + "_result.json"), case)
            v1.save(out / (arm + "_decision_audit.json"), feed.audit)
            record["cases"].append(case)
            print(json.dumps(dict(stage="account_terminal", arm=arm, status=case["status"],
                sessions=case.get("completed_sessions"))), flush=True)
        for ref in saved["original_artifacts"]:
            _checked(ref)
        paired = all(case["full_period_metrics_available"] for case in record["cases"])
        record.update(status="completed_two_arm_source_recheck_attempt", completed_at=v1.now(),
            full_paired_account_comparison=paired, overall_outcome="complete" if paired else "partial_or_unavailable",
            all_32_formation_batches_complete=True, both_666_session_accounts_complete=paired,
            formation_rows=93188, formation_days=32, marked_returns_not_liquidated=True,
            source_limitations=base.manifest["limitations"], source_clock=saved["source_clock"],
            source_inventory=saved["source_inventory"],
            amendment=saved["amendment"], late_primary_reviews=saved["late_primary_reviews"],
            formation_receipt=pin(Path(saved["original_result"]).parent / "formation_receipt.json"),
            economic_assessment=v1._economic_assessment(record["cases"], formation_complete=True))
        v1.save(out / "result.json", record)
        return record
    except BaseException as error:
        v1.save(out / "failure.json", dict(**record, failed_at=v1.now(), error=f"{type(error).__name__}: {error}"))
        raise
    finally:
        if base is not None:
            base.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    declare = commands.add_parser("register")
    for option in ("original-result", "amendment-receipt", "out", "run-out"):
        declare.add_argument("--" + option, required=True)
    execute = commands.add_parser("run")
    for option in ("registration", "out"):
        execute.add_argument("--" + option, required=True)
    args = vars(parser.parse_args(argv))
    result = (register if args.pop("operation") == "register" else run)(**args)
    print(json.dumps({key: result[key] for key in ("schema", "status") if key in result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
