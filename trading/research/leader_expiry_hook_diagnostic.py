"""Add the existing independent expiry hook to pinned original common20 cases.

The saved candidate batches, feed, target policy, costs and exact old calendar
remain unchanged. Only five declared common20 cases are run in new research
accounts; the original 20-case results and failed/unavailable scopes remain.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from .baostock_daily_slice import digest
from .leader_case_study import _write
from .leader_daily_replay import (CommonCohortRawFeed, END, EXIT_POLICIES, FAMILIES,
                                  POOLS, SOURCE_CUTOFF, START, _case, dated_coverage)
from .leader_daily_signals import RAW_COLUMNS, _calendar_view, _physical_frame
from .leader_tail_overlay import _rows_digest, load_tail_source
from .shortline_decision_v3 import FiveSessionExpiryPolicy, POLICY_ID
from .shortline_panel_replay import _config

SCHEMA = "leader_original_common20_independent_expiry_hook_diagnostic_v1"
METHOD_ID = "same_saved_common20_batches_add_independent_expiry_hook_v1"
FLAGS = {"research_only": True, "candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "persistent_ledger_mutated": False,
    "broker_interacted": False, "provider_contacted": False, "model_refit": False,
    "future_labels_used_for_score": False, "parameters_tuned": False,
    "signals_recomputed": False, "original_artifacts_mutated": False,
    "pit_verified": False, "corporate_actions_verified": False,
    "future_independent_window_run": False}


def _read(path, expected=None):
    path = Path(path).resolve()
    proof = {"file": str(path), "sha256": digest(path)}
    if expected is not None and proof["sha256"] != expected:
        raise ValueError(f"Pinned original evidence changed: {path.name}")
    return json.loads(path.read_text(encoding="utf-8")), proof


def _return_label(summary):
    if not summary.get("fills"):
        return "未成交，不评收益"
    return f"{summary['metrics']['net_return']:.4%}" if summary.get("metrics") else "不可用"


def read_frozen_member_raw(snapshot, manifest, symbols, end):
    """Filter frozen codes, but keep their full past history and every chunk."""
    codes = [symbol.split(".")[0] for symbol in symbols]
    prefixes = {code[0] for code in codes}
    pieces, proofs = [], []
    for item in manifest["chunks"]:
        if str(item["prefix"]) not in prefixes or int(item["lower_inclusive"]) > int(end.replace("-", "")):
            continue
        path = Path(snapshot) / item["parquet_file"]
        if digest(path) != item["parquet_sha256"] or pq.ParquetFile(path).metadata.num_rows != item["rows"]:
            raise ValueError("Original physical partition hash or row count changed")
        piece = pd.read_parquet(path, columns=RAW_COLUMNS, filters=[("code", "in", codes)])
        piece["date"] = pd.to_datetime(piece["date"].astype(str), format="%Y%m%d", errors="raise").dt.strftime("%Y-%m-%d")
        piece = piece.loc[piece.date.le(end)].copy()
        pieces.append(piece)
        proofs.append({"file": str(path), "sha256": item["parquet_sha256"],
                       "original_partition_rows": item["rows"], "frozen_member_rows_read": len(piece)})
    if not pieces:
        raise ValueError("Original frozen-member history absent")
    return _physical_frame(pd.concat(pieces, ignore_index=True), end), proofs


def _tail(raw, metadata, symbols):
    """Reconstruct only the originally added Oct12 rows, without new retrieval."""
    quotes, proof = load_tail_source(metadata["source"]["package"], symbols)
    for name in ("plan_sha256", "receipt_sha256", "raw_daily_sha256"):
        if proof[name] != metadata["source"][name]:
            raise ValueError("Original tail package changed")
    physical = quotes.rename(columns={"pctChg": "pct_chg", "isST": "is_st", "preclose": "pre_close"}).copy()
    physical["code"] = physical.code.str[3:]
    physical["pre_close"] = pd.to_numeric(physical["pre_close"], errors="coerce")
    canonical = _physical_frame(physical.loc[:, RAW_COLUMNS], SOURCE_CUTOFF)
    added = canonical.loc[canonical.date.eq(metadata["added_day"])
        & canonical.symbol.isin(metadata["added_symbols"]) & canonical.source_row_valid, raw.columns].copy()
    if len(added) != metadata["added_rows"] or _rows_digest(added) != metadata["added_rows_sha256"]:
        raise ValueError("Original raw tail rows changed")
    if raw.loc[raw.date.eq(metadata["added_day"]) & raw.symbol.isin(metadata["added_symbols"])].shape[0]:
        raise ValueError("Tail cannot replace an original native observation")
    for field in added:
        cast = added[field].astype(raw[field].dtype)
        if not (cast.eq(added[field]) | (cast.isna() & added[field].isna())).all():
            raise ValueError("Original tail field precision cannot be retained")
        added[field] = cast
    return pd.concat([raw, added], ignore_index=True), proof


def load_inputs(annotations_index):
    index, index_proof = _read(annotations_index)
    if (index.get("schema") != "leader_existing_replay_historical_phase_annotations_v1"
            or index["counts"]["original_cases"] != 20):
        raise ValueError("Original twenty-case annotation index required")
    source = index["source"]
    old, old_proof = _read(source["replay_result"]["file"], source["replay_result"]["sha256"])
    protocol, protocol_proof = _read(old["protocol"])
    if (old["start"], old["end"], old["snapshot_end"]) != (START, END, SOURCE_CUTOFF):
        raise ValueError("Only the fixed already-inspected 183-session original window is supported")
    if (protocol["config"] != _config(100000, 5).to_dict() or protocol["families"] != list(FAMILIES)
            or protocol.get("max_holding_sessions") != 5):
        raise ValueError("Original cost, cash, holdings or family contract changed")
    if protocol["optional_research_tail_overlay"] != old["optional_research_tail_overlay"]:
        raise ValueError("Original tail provenance disagrees")
    snapshot = Path(old["snapshot"])
    manifest, manifest_proof = _read(snapshot / "manifest.json", source["matching_original_snapshot_manifest"]["sha256"])
    if manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False:
        raise ValueError("Original unadmitted provider inventory required")
    cohort, cohort_proof = _read(protocol["cohort"]["artifact"])
    symbols = cohort["pools"][POOLS[0]]
    if len(symbols) != 20 or symbols != sorted(set(symbols)) or symbols != old["optional_research_tail_overlay"]["symbols"]:
        raise ValueError("Exact original frozen common20 cohort required")
    calendar, calendar_proof = _read(source["calendar"]["file"], source["calendar"]["sha256"])
    sessions, _, status = _calendar_view(calendar, START, SOURCE_CUTOFF, [])
    if status != "pinned_static_exchange_calendar" or len(sessions) != 185:
        raise ValueError("Original 183 decisions plus two exact tail sessions required")
    if [day for day in sessions if day > END] != protocol["execution_tail_sessions"]:
        raise ValueError("Original exact execution tail changed")
    raw, partitions = read_frozen_member_raw(snapshot, manifest, symbols, SOURCE_CUTOFF)
    raw, tail_proof = _tail(raw, old["optional_research_tail_overlay"], symbols)
    if raw.duplicated(["date", "symbol"]).any() or set(raw.symbol) != set(symbols):
        raise ValueError("Original frozen raw scope duplicated or incomplete")
    original_coverage, coverage_proof = _read(Path(old["artifacts"]) / f"{POOLS[0]}_coverage.json")
    if list(dated_coverage(raw, sessions, symbols).values()) != original_coverage:
        raise ValueError("Original frozen raw coverage changed; do not repair gaps")
    originals, retained, all_proofs = [], [], [index_proof, old_proof, protocol_proof, manifest_proof, cohort_proof, calendar_proof, coverage_proof]
    for family in FAMILIES:
        selected = [case for case in old["cases"] if case["family"] == family
            and case["pool"] == POOLS[0] and case["exit_policy"] == EXIT_POLICIES[0]]
        if len(selected) != 1 or selected[0]["status"] != "completed_already_inspected_engine_diagnostic":
            raise ValueError("Each of the five declared original common20 families must remain present")
        original = selected[0]
        bound = next(item for item in index["cases"] if item["case_key"] == Path(original["artifacts"]).name)
        sidecar, sidecar_proof = _read(bound["sidecar_file"], bound["sidecar_sha256"])
        original_account_proof = next(p for p in sidecar["source_files"] if Path(p["file"]).name == "account.json")
        original_account, account_proof = _read(original_account_proof["file"], original_account_proof["sha256"])
        batch_path = Path(original["artifacts"]) / "decision_batches.json"
        batches, batch_proof = _read(batch_path)
        if set(batches) != set(sessions):
            raise ValueError("All original dated decision batches, including incomplete tail, required")
        originals.append({"summary": original, "batches": batches, "batch_proof": batch_proof,
                          "original_account": original_account})
        all_proofs += [sidecar_proof, account_proof, batch_proof]
    new_case_keys = {Path(item["summary"]["artifacts"]).name for item in originals}
    retained = [case for case in old["cases"] if Path(case["artifacts"]).name not in new_case_keys]
    if len(old["cases"]) != 20 or len(retained) != 15:
        raise ValueError("All fifteen unavailable original scope/defense cases must be retained")
    return {"raw": raw, "sessions": sessions, "symbols": symbols, "originals": originals,
        "retained": retained, "original_protocol": protocol, "proofs": all_proofs,
        "partitions": partitions, "tail_source": tail_proof}


def run(*, annotations_index, out, data_out):
    out, data_out = Path(out).resolve(), Path(data_out).resolve()
    if out.exists() or data_out.exists():
        raise FileExistsError("New diagnostic requires fresh report and research-account directories")
    inputs = load_inputs(annotations_index)
    protocol = {"schema": SCHEMA, "method_id": METHOD_ID, "recorded_at": datetime.now().astimezone().isoformat(),
        "status": "late_recorded_fault_reproduction_on_already_inspected_window",
        "start": START, "end": END, "execution_tail_sessions": inputs["original_protocol"]["execution_tail_sessions"],
        "families": list(FAMILIES), "pool": POOLS[0], "frozen_symbols": inputs["symbols"],
        "config": inputs["original_protocol"]["config"], "changed_variable": "add_existing_independent_replay_expiry_hook_only",
        "risk_policy_id": POLICY_ID, "holding_sessions": 5, "source_files": inputs["proofs"],
        "raw_partitions": inputs["partitions"], "tail_source": inputs["tail_source"],
        "unchanged": ["saved decision batches and ranking", "CommonCohortDecisionFeed",
            "original fixed_exit_policy", "183 decision and two tail sessions", "cash, costs and portfolio config",
            "full past-history first-observed listing proxy", "provider status/adjustment assumptions remain unverified"],
        "five_new_replays_fifteen_retained_original_cases": True, **FLAGS}
    out.mkdir(parents=True, exist_ok=False)
    data_out.mkdir(parents=True, exist_ok=False)
    _write(out / "protocol.json", protocol)
    base = CommonCohortRawFeed(inputs["raw"], inputs["sessions"], inputs["symbols"])
    comparisons = []
    for item in inputs["originals"]:
        old = item["summary"]
        summary, evidence = _case(base, item["batches"], old["family"], POOLS[0], EXIT_POLICIES[0], START, END,
                                  replay_risk_exit_policy=FiveSessionExpiryPolicy())
        summary.update(diagnostic_method=METHOD_ID, risk_policy_id=POLICY_ID, **FLAGS)
        case_out = data_out / old["family"]
        case_out.mkdir()
        _write(case_out / "summary.json", summary)
        _write(case_out / "account.json", evidence["state"])
        pd.DataFrame([{k:v for k,v in row.items() if k != "events"} for row in evidence["daily"]]).to_csv(case_out / "daily.csv", index=False)
        pd.DataFrame(evidence["decisions"]).to_csv(case_out / "decisions.csv", index=False)
        if digest(item["batch_proof"]["file"]) != item["batch_proof"]["sha256"]:
            raise ValueError("Original decision batch changed during diagnostic")
        comparisons.append({"family": old["family"], "original": old, "independent_hook": summary,
            "new_research_artifacts": str(case_out), "original_batches": item["batch_proof"],
            "same_trajectory_not_assumed": True})
        print(old["family"] + ": " + summary["status"], flush=True)
    result = {"schema": SCHEMA, "method_id": METHOD_ID, "created_at": datetime.now().astimezone().isoformat(),
        "status": "completed_fixed_exit_hook_diagnostic" if all(c["independent_hook"]["error"] is None for c in comparisons) else "diagnostic_with_preserved_engine_failure",
        "protocol": str(out / "protocol.json"), "new_engine_cases": len(comparisons),
        "original_unavailable_cases_retained_without_new_replay": inputs["retained"],
        "comparisons": comparisons, "artifacts": str(data_out),
        "old_results_preserved": True, "returns_are_diagnostic_not_independent_validation": True,
        "source_status_and_listing_fields_are_original_feed_proxies_not_verified_facts": True, **FLAGS}
    _write(out / "result.json", result)
    lines = ["# 原共同20票：独立退出路径历史诊断", "",
        "同原信号批次、股票池、费用、183个决策日及2个尾部日，只显式增加已有独立五日退出hook。已看窗口的故障复现，不是新独立策略检验；只新跑5族，另外15个原不可用案例保留引用，未解锁或当零收益。", "",
        "|原策略|旧/新状态|旧/新诊断净收益|旧/新超五日持仓天数|旧/新受阻卖出|旧/新残仓证券数|",
        "|---|---|---:|---:|---:|---:|"]
    for row in comparisons:
        old, new = row["original"], row["independent_hook"]
        lines.append(f"|{row['family']}|{old['status']} / {new['status']}|{_return_label(old)} / {_return_label(new)}|"
            f"{old['decision_coverage']['over_five_session_held_days']} / {new['decision_coverage']['over_five_session_held_days']}|"
            f"{old['blocked_exit_count']} / {new['blocked_exit_count']}|{len(old['open_positions'])} / {len(new['open_positions'])}|")
    lines += ["", "新退出会改变后续现金、数量与组合轨迹，不能把新销售嫁接到旧买批上。主窗和含尾部指标分别保存在result.json；尾日没有准确后继不延长日历。", "",
        "缺持仓当前raw/status时在当日变更前停止；缺准确后继时保留延期及残仓。未补价格、未删除失败、未重算候选或调阈值。", "",
        "原feed仍用首观察日作上市代理、量与开盘推导状态及研究adjustflag；这些假设与旧诊断相同，但并非官方资格/复权/PIT证明。结果不代表真实可成交、游资交割或当前交易信号。", "",
        "execution_admitted=false；selection_enabled=false；strategy_validated=false。", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("annotations-index", "out", "data-out"):
        parser.add_argument("--" + name, required=True, type=Path)
    result = run(**vars(parser.parse_args(argv)))
    print(json.dumps({"status": result["status"], "new_engine_cases": result["new_engine_cases"],
                      "created_at": result["created_at"], **FLAGS}, ensure_ascii=False))
    return 0 if result["status"] == "completed_fixed_exit_hook_diagnostic" else 2


if __name__ == "__main__":
    raise SystemExit(main())
