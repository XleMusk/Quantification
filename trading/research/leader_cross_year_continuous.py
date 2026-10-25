"""One continuous 2025--2026 leader replay on the existing Engine/Ledger.

The annual 2025 and continuous 2026 inputs were already replayed separately.
This adapter combines those saved, immutable inputs into one new account path
so cross-year carry, T+1 and exit behavior are visible.  It keeps the 2025
first-day scope fixed; 2026 candidate panels are filtered to that scope and
their original dated ranks are retained.  It never contacts a broker and all
research flags remain false.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from collections.abc import Mapping

import pandas as pd

from ..market_dataset import write_json
from .baostock_daily_slice import digest
from .leader_daily_signals import _calendar_view
from .leader_eight_type_case_replay import CASE_IDS, _save_case
from .leader_eight_type_historical_native import load_inputs
from .leader_eight_type_panel_replay import (
    FLAGS, PROTOCOL, TYPE_IDS, _case, _environment, _rank_path,
)
from .leader_historical_execution_policy import HistoricalExecutionFactory
from .recent_alpha158_panel_replay import ExactSourceRawFeed
from .stockdb_action_accounting import load_action_accounting


PY25 = Path(r"D:\QuantificationData\trading\research\leader_eight_type_historical\20261018_2025_annual_classified_inputs_v1\manifest.json")
PY26 = Path(r"D:\QuantificationData\trading\research\leader_eight_type_historical\20261021_2026_continuous_classified_v1\manifest.json")
EVENTS = Path(r"D:\QuantificationData\trading\research\stock_action_events\20261018_020124_21507756")
REFERENCES = Path(r"D:\QuantificationData\trading\research\stock_action_references\20261018_020124_21507756")
OUT = Path(r"D:\QuantificationData\trading\research\leader_eight_type_case_replay\20261024_2025_2026_continuous_v1")
REPORT = Path(r"D:\Quantification\outputs\leader_cross_year\20261024_2025_2026_continuous_v1")

_REPLAY_RAW_COLUMNS = ["code", "symbol", "date", "open", "high", "low", "close", "preclose",
                       "volume", "amount", "tradestatus", "isST"]


class _CombinedPanels(Mapping):
    """Read-only view over the two non-overlapping lazy annual panel files."""
    def __init__(self, first, second):
        overlap = set(first).intersection(second)
        if overlap:
            raise ValueError(f"Annual panels overlap: {sorted(overlap)[:3]}")
        self._parts = (first, second)
        self._keys = tuple(sorted((*first, *second)))

    def __getitem__(self, day):
        for part in self._parts:
            if day in part:
                return part[day]
        raise KeyError(day)

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)


class _PanelFeatures(Mapping):
    """Lazy risk-feature view used by one case's structural exit policy."""
    def __init__(self, panels):
        self._panels = panels

    def __getitem__(self, day):
        return self._panels[day].get("risk_features", {})

    def __iter__(self):
        return iter(self._panels)

    def __len__(self):
        return len(self._panels)


class _PanelCandidates(Mapping):
    """Candidate evidence limited to each type's original dated top five."""
    def __init__(self, paths):
        self._paths = paths

    def __getitem__(self, day):
        return self._paths[day]["original_top5"]

    def __iter__(self):
        return iter(self._paths)

    def __len__(self):
        return len(self._paths)


def _now():
    return datetime.now().astimezone().isoformat()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _calendar(path):
    payload = _read(path)
    sessions, _, status = _calendar_view(payload, f"{payload['year']}-01-01", f"{payload['year']}-12-31", [])
    if status != "pinned_static_exchange_calendar":
        raise ValueError(f"Pinned exchange calendar required: {path}")
    return sessions, payload


def _case_symbol_scope(loaded25, loaded26, *, identity, members):
    """Return the fixed candidate symbol scope for one bounded case.

    The original 32-case process keeps the full 4,958-name scope.  A single
    case can be run with the union of that case's dated candidates only; this
    preserves the case's frozen ranks while avoiding a second full-market raw
    frame in memory.  The scope is recorded in the receipt and is never used
    to claim a full-universe result.
    """
    selected = set()
    allowed = set(members)
    for bundle in (loaded25, loaded26):
        for day in bundle["source_days"]:
            panel = bundle["panel_by_day"][day]
            item = next((row for row in panel.get("types", []) if row.get("id") == identity), None)
            if item is None:
                continue
            for row in item.get("candidates", []):
                symbol = row.get("symbol")
                if symbol in allowed:
                    selected.add(symbol)
    if not selected:
        raise ValueError(f"No frozen candidates for bounded case: {identity}")
    return sorted(selected)


def combine_inputs(*, only_case=None):
    """Load and combine saved inputs without changing either source tree.

    ``only_case`` enables a memory-bounded single-case route.  The default
    remains the exact full-scope 32-case preparation used by the original
    protocol.
    """
    loaded25, m25 = load_inputs(PY25, lazy_panels=True, raw_columns=_REPLAY_RAW_COLUMNS)
    loaded26, m26 = load_inputs(PY26, lazy_panels=True, raw_columns=_REPLAY_RAW_COLUMNS)
    if m25["fixed_protocol"] != m26["fixed_protocol"] or m25["fixed_protocol"] != PROTOCOL:
        raise ValueError("Annual and continuous inputs do not share the fixed 32-case protocol")
    full_members = sorted(loaded25["scope_members"])
    if len(full_members) != 4958:
        raise ValueError(f"Expected the fixed 2025 first-day scope, got {len(full_members)}")
    identity = None if only_case is None else str(only_case).split("__", 1)[0]
    members = (_case_symbol_scope(loaded25, loaded26, identity=identity, members=full_members)
               if identity else full_members)
    days25, cal25 = _calendar(m25["calendar"])
    days26, cal26 = _calendar(m26["calendar"])
    sessions = list(days25) + [day for day in days26 if day not in days25]
    source_days = list(loaded25["source_days"]) + [day for day in loaded26["source_days"] if day not in loaded25["source_days"]]
    if source_days != sorted(set(source_days)) or source_days[0] != "2025-01-02" or source_days[-1] != "2026-09-30":
        raise ValueError("Combined source days are not the declared cross-year chain")
    raw25 = loaded25["raw"].loc[loaded25["raw"].symbol.isin(members)].copy()
    raw26 = loaded26["raw"].loc[loaded26["raw"].symbol.isin(members)].copy()
    raw = pd.concat([raw25, raw26], ignore_index=True)
    if raw.duplicated(["date", "code"]).any():
        raise ValueError("Combined raw source has duplicate date/code rows")
    panels = _CombinedPanels(loaded25["panel_by_day"], loaded26["panel_by_day"])
    if sorted(panels) != source_days:
        raise ValueError("Combined panels do not cover the exact source calendar")
    # The 2025 qualification carries the fixed first-day listing witness.  Its
    # assessment calendar is widened to the declared 2025+2026 calendar so the
    # same IPO/T+1 gate is applied after the year boundary.
    qualification = replace(loaded25["qualification"], calendar=tuple(sessions), assessment_calendar=tuple(sessions))
    context = pd.read_parquet(m25["context_raw_file"])
    if sorted(context.date.unique()) != [m25["context_anchor"]["day"]]:
        raise ValueError("2025 input must provide one exact preceding context session")
    as_of = max(str(panel["available_at"]) for panel in panels.values())
    for frame in (loaded25.get("raw"), loaded26.get("raw"), raw25, raw26):
        del frame
    # The annual lazy mappings own their mmap handles for the lifetime of the
    # returned bundle; source raw frames are no longer needed after concat.
    loaded25.pop("raw", None)
    loaded26.pop("raw", None)
    return {
        "raw": raw,
        "source_days": source_days,
        "calendar": sessions,
        "panels": panels,
        "members": members,
        "qualification": qualification,
        "context": context,
        "anchor": loaded25["context_anchor"],
        "start": "2025-01-02",
        "end": "2026-09-21",
        "through": "2026-09-30",
        "as_of": as_of,
        "source_manifests": [str(PY25.resolve()), str(PY26.resolve())],
        "source_manifest_hashes": [digest(PY25), digest(PY26)],
        "calendar_manifests": [str(Path(m25["calendar"]).resolve()), str(Path(m26["calendar"]).resolve())],
        "scope_basis": ("fixed_2025_first_decision_day_native_observed_scope; 2026 candidates filtered to the same scope"
                        if identity is None else
                        f"bounded_single_case_candidate_union:{identity}; subset of fixed 2025 first-day scope"),
        "full_scope_members": len(full_members),
        "bounded_single_case": identity is not None,
        "bounded_case_identity": identity,
    }


def _write_report(result):
    report = Path(result["report"]).parent
    report.mkdir(parents=True, exist_ok=True)
    rows = result["cases"]
    lines = [
        "# 龙头风格跨年连续账户回放",
        "",
        "2025-01-02 至 2026-09-30，2025 首日固定 4,958 只观察范围，424 个 source sessions（含 2026-09-22 至 09-30 六日尾部）。",
        "2025 年度和 2026 连续输入均来自已保存原件；本次只把两段接入同一 shared Engine/Ledger 账户路径，不重写信号、费用、T+1 或风险退出规则。",
        "",
        "| 类型 | 环境 | 容量 | 状态 | 会话 | 净收益 | 最大回撤 | 订单 | 成交 | 阻断 |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        metrics = row.get("metrics") or {}
        lines.append("| {type} | {guard} | {capacity} | {status} | {sessions} | {ret} | {dd} | {orders} | {fills} | {blocked} |".format(
            type=row["type"], guard=row["guard"], capacity=row["capacity"], status=row["status"],
            sessions=row.get("engine_sessions_committed", 0),
            ret=f"{metrics['net_return']:.2%}" if "net_return" in metrics else "不可用",
            dd=f"{metrics['max_drawdown']:.2%}" if "max_drawdown" in metrics else "不可用",
            orders=len(row.get("orders", [])) if isinstance(row.get("orders"), list) else row.get("order_count", 0),
            fills=len(row.get("fills", [])) if isinstance(row.get("fills"), list) else row.get("fill_count", 0),
            blocked=row.get("blocked_order_count", 0),
        ))
    lines += [
        "", "结果仅为跨年连续账户诊断；2025 scope 与 2026 panel 供应商观察范围、资格、行动和公开时点仍非 PIT 完整证明。",
        "所有 cases 均保留 skipped/blocked/residual 证据；execution_admitted=false，selection_enabled=false，strategy_validated=false，returns_reportable=false。",
    ]
    (report / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(*, out=OUT, report=REPORT, only_case=None):
    if out.exists() or report.exists():
        raise FileExistsError("Cross-year output already exists; do not overwrite a prior attempt")
    bundle = combine_inputs(only_case=only_case)
    out.mkdir(parents=True, exist_ok=False)
    report.mkdir(parents=True, exist_ok=False)
    # Build one action-accounting factory for the whole contiguous chain.
    raw_days = sorted(set(bundle["context"].date) | set(bundle["source_days"]))
    accounting = load_action_accounting(EVENTS, REFERENCES, symbols=bundle["members"], raw_days=raw_days,
                                         replay_days=bundle["source_days"], price_source="independent_baostock_raw")
    factory = HistoricalExecutionFactory(accounting)
    paths, original_ranks = _rank_path(bundle["panels"], bundle["members"], bundle["start"], bundle["end"])
    env = _environment(bundle["panels"], bundle["calendar"], bundle["as_of"])
    complete_feature_days = tuple(day for day in bundle["source_days"]
                                  if bundle["panels"][day].get("risk_feature_scope_complete") is True)
    # Construct one immutable raw feed for the contiguous chain.  Each case
    # has an isolated Engine/Ledger state; the feed itself is read-only apart
    # from the per-case action/manifest bindings installed by the policy.
    base_raw = pd.concat([bundle["context"], bundle["raw"]], ignore_index=True)
    shared_feed = ExactSourceRawFeed(base_raw, sorted(set(bundle["context"].date) | set(bundle["source_days"])),
                                     sorted(bundle["members"]), context_anchor=bundle["anchor"], copy_raw=False,
                                     bounded_memory=True)
    del base_raw
    bundle["raw"], bundle["context"] = None, None
    registration = {
        "schema": "leader_cross_year_continuous_registration_v1", "created_at": _now(),
        "start": bundle["start"], "end": bundle["end"], "through": bundle["through"],
        "source_days": bundle["source_days"], "scope_members": bundle["members"],
        "scope_basis": bundle["scope_basis"], "source_manifests": bundle["source_manifests"],
        "source_manifest_hashes": bundle["source_manifest_hashes"], "calendar_manifests": bundle["calendar_manifests"],
        "action_accounting": accounting.provenance, "protocol": deepcopy(PROTOCOL), **FLAGS,
        "cross_year_scope_warning": "2026 panels retain their original 5181-scope producer rank; candidates are filtered to the frozen 2025 scope before replay",
    }
    write_json(out / "registration.json", registration)
    write_json(out / "preparation.json", {"environment_decisions": env, "raw_top5_paths": original_ranks, "capacity_paths": paths,
                                            "prepared_at": _now(), **FLAGS})
    case_ids = list(CASE_IDS) if only_case is None else [only_case]
    if any(case_id not in CASE_IDS for case_id in case_ids):
        raise ValueError(f"Unknown fixed case: {only_case}")
    cases, target_rows, order_rows, fill_rows, equity_rows = [], [], [], [], []
    for ordinal, case_id in enumerate(case_ids, 1):
        identity, guard, cap = case_id.split("__")
        print(f"case {ordinal}/32 {case_id}", flush=True)
        try:
            case = _case(bundle["raw"], bundle["source_days"], bundle["calendar"],
                         {day: bundle["calendar"][index - 1] if index else None for index, day in enumerate(bundle["calendar"])},
                         bundle["panels"], paths[identity][cap], env, bundle["members"], bundle["qualification"],
                         bundle["anchor"], bundle["start"], bundle["end"], identity, cap, guard,
                         f"leader-cross-year:{ordinal}:{case_id}", execution_policy_factory=factory,
                         context_raw=bundle["context"], base_feed=shared_feed,
                         risk_policy_candidates=_PanelCandidates(paths[identity]["uncapped"]),
                         risk_policy_features=_PanelFeatures(bundle["panels"]),
                         risk_policy_complete_days=complete_feature_days)
        except BaseException as error:
            case = {"case_id": case_id, "type": identity, "guard": guard, "capacity": cap,
                    "status": "adapter_case_failed", "error": f"{type(error).__name__}: {error}",
                    "engine_sessions_committed": 0, "complete_PnL_available": False, "orders": [], "fills": [],
                    **FLAGS}
            print(f"  FAILED {case['error']}", flush=True)
        case_dir = out / case_id / "attempt-0001"
        case_dir.mkdir(parents=True, exist_ok=False)
        _save_case(case, case_dir)
        # Persist the complete case before dropping its large daily/account
        # payload.  The top-level result only needs the original metadata.
        cases.append({key: value for key, value in case.items()
                      if key not in {"decisions", "daily", "orders", "fills", "account"}})
        account = case.get("account") or {}
        decisions = case.get("decisions") or []
        daily = case.get("daily") or []
        targets = account.get("targets") or {}
        target_rows.append({"case_id": case_id, "final_targets": targets, "decision_count": len(decisions),
                            "target_events": [event for day in daily for event in (day.get("events") or [])
                                              if event.get("kind") in {"target", "target_created", "order_target"}],
                            "execution_admitted": False})
        for row in case.get("orders") or []:
            order_rows.append({"case_id": case_id, **row, "execution_admitted": False})
        for row in case.get("fills") or []:
            fill_rows.append({"case_id": case_id, **row, "execution_admitted": False})
        for row in daily:
            equity_rows.append({"case_id": case_id, **{key: row.get(key) for key in ("date", "equity", "cash", "receivables", "holdings", "fees", "turnover_value")},
                                "execution_admitted": False})
    result = {"schema": "leader_cross_year_continuous_result_v1", "created_at": _now(),
              "start": bundle["start"], "end": bundle["end"], "through": bundle["through"],
              "source_days": bundle["source_days"], "scope_members": bundle["members"], "case_count": len(cases),
              "cases": cases,
              "all_cases_complete_PnL": all(case.get("complete_PnL_available") is True for case in cases),
              "target_receipts": str(out / "target_receipts.json"), "orders": str(out / "orders.csv"),
              "fills": str(out / "fills.csv"), "equity": str(out / "equity.csv"),
              "registration": str(out / "registration.json"), "report": str(report / "REPORT.md"),
              "requested_case_ids": case_ids, "fixed_case_count": len(CASE_IDS), **FLAGS,
              "returns_reportable": False}
    write_json(out / "target_receipts.json", target_rows)
    pd.DataFrame(order_rows).to_csv(out / "orders.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(fill_rows).to_csv(out / "fills.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(equity_rows).to_csv(out / "equity.csv", index=False, encoding="utf-8-sig")
    write_json(out / "result.json", result)
    _write_report(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--only-case", choices=CASE_IDS,
                        help="Run one fixed case for a bounded receipt when a full 32-case process is too large")
    args = parser.parse_args(argv)
    result = run(out=args.out, report=args.report, only_case=args.only_case)
    print(json.dumps({"result": str((args.out / 'result.json').resolve()), "report": str((args.report / 'REPORT.md').resolve()),
                      "case_count": result["case_count"], "all_cases_complete_PnL": result["all_cases_complete_PnL"], **FLAGS}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
