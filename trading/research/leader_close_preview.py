"""Read-only leader and market-tape preview from one complete dated close.

Turnover direction is a price/turnover association, never identified net flows
or a claim about participants' intentions. This module creates no orders.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from ..market_dataset import write_frame, write_json
from .baostock_daily_slice import digest
from .leader_daily_signals import (
    FAMILIES, RAW_COLUMNS, _physical_frame, build_leader_daily_batches,
    read_physical_snapshot,
)

SCHEMA = "leader_complete_close_preview_v1"


def market_tape(raw):
    """Describe all active non-ST provider rows without hiding missing fields."""
    if raw.empty or raw.date.nunique() != 1 or raw.code.duplicated().any():
        raise ValueError("Tape requires one exact dated, unique-symbol close")
    day = str(raw.date.iloc[0])
    physical = canonical(raw)
    frame = _physical_frame(physical, day)
    active = frame.loc[frame.active_non_st].copy()
    # The loader validates source status; canonical keeps null suspension
    # activity, so suspended rows cannot silently enter active_non_st.
    status = pd.Series(raw.tradestatus.to_numpy(), index=raw.code.str[3:])
    active = active.loc[active.code.map(status).eq("1")].copy()
    if active.empty:
        raise ValueError("No observed active non-ST close to describe")
    advance, decline = active.pct_chg.gt(0), active.pct_chg.lt(0)
    amount = float(active.amount.sum())
    up_amount = float(active.loc[advance, "amount"].sum())
    down_amount = float(active.loc[decline, "amount"].sum())
    spread = active.high - active.low
    location = ((active.close - active.low) / spread.where(spread.gt(0))).dropna()
    result = {
        "day": day, "scope": "complete_dated_provider_ordinary_SH_SZ_active_non_ST",
        "ordinary_inventory_rows": len(raw), "active_non_st_rows": len(active),
        "suspended_inventory_rows": int(raw.tradestatus.eq("0").sum()),
        "st_inventory_rows": int(raw.isST.eq("1").sum()),
        "advance_count": int(advance.sum()), "decline_count": int(decline.sum()),
        "flat_count": int(active.pct_chg.eq(0).sum()),
        "breadth": float((advance.sum() - decline.sum()) / len(active)),
        "median_return_pct": float(active.pct_chg.median()),
        "amount_cny": amount,
        "amount_weighted_return_pct": float((active.pct_chg * active.amount).sum() / amount) if amount else None,
        "up_stock_turnover_cny": up_amount, "down_stock_turnover_cny": down_amount,
        "signed_turnover_association": (up_amount - down_amount) / amount if amount else None,
        "top20_turnover_share": float(active.amount.nlargest(20).sum() / amount) if amount else None,
        "median_close_range_location": float(location.median()) if len(location) else None,
        "zero_range_rows": int(spread.eq(0).sum()),
        "interpretation": "Observed breadth and price-associated turnover; not net capital inflow, participant identity or intent",
        "sector_diffusion_available": False, "intraday_order_flow_available": False,
        "exchange_universe_verified": False, "pit_verified": False,
    }
    return result


def canonical(raw):
    frame = raw.rename(columns={"pctChg": "pct_chg", "isST": "is_st", "preclose": "pre_close"}).copy()
    frame["code"] = frame.code.str[3:]
    return frame[RAW_COLUMNS]


def preview(package, history_snapshot, calendar, out):
    # This loader refuses budget checkpoints, failed batches and changed bytes.
    from .baostock_full_close import load_complete
    raw, source = load_complete(package)
    day = raw.date.iloc[0]
    base, metadata = read_physical_snapshot(Path(history_snapshot), end=day)
    if base.date.max() >= day:
        raise ValueError("Close preview only appends a later day; no historical source replacement")
    physical = canonical(raw)
    combined = pd.concat([base, physical], ignore_index=True)
    calendar_payload = json.loads(Path(calendar).read_text(encoding="utf-8"))
    # Keep the original producer's source/qualification boundaries. The latest
    # provider inventory is separately complete; it is not an admitted snapshot.
    bundle = build_leader_daily_batches(combined, metadata, start=day, end=day,
                                       calendar_payload=calendar_payload, retain_window_features=True)
    candidates = bundle["candidate_table"]
    tape = market_tape(raw)
    current = bundle["window_features"]
    tape["limit_up_proxy_count"] = int(current.limit_up.sum())
    tape["limit_up_stage_unknown_count"] = int((current.limit_up & current.stage.isna()).sum())
    tape["stages"] = {str(i): int(current.stage.eq(i).sum()) for i in range(1, 6)}
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    write_frame(out / "candidates.csv", candidates)
    write_frame(out / "current_features.csv", current)
    families = []
    for family in FAMILIES:
        rows = candidates.loc[candidates.family.eq(family)]
        families.append({"family": family, "candidate_count": len(rows),
                         "display_top5": json.loads(rows.head(5).to_json(orient="records")),
                         "producer_batch_status": bundle["batches"][family][day]["status"]})
    result = {
        "schema": SCHEMA, "created_at": datetime.now().astimezone().isoformat(), "day": day,
        "status": "complete_source_close_research_preview_only", "source": source,
        "history_snapshot": str(Path(history_snapshot).resolve()),
        "calendar": str(Path(calendar).resolve()), "calendar_sha256": digest(calendar),
        "source_inventory_complete": True, "tape": tape, "families": families,
        "producer_coverage": bundle["coverage_facts"],
        "candidate_count": len(candidates),
        "history_semantics": bundle["protocol"],
        "successor_raw_bar_present": False, "orders_generated": False,
        "ths_payload_generated": False, "model_refit": False,
        "future_labels_used_for_score": False, "candidate_panel_only": True,
        "strategy_validated": False, "selection_enabled": False,
        "signal_admitted": False, "execution_admitted": False, "ledger_mutated": False,
        "limitations": [
            "One complete provider close is not independent exchange/PIT eligibility or company-action clearance",
            "Previous StockDB observations retain historical gaps; observed-row 20/60 returns are not guaranteed calendar-session returns",
            "Limit-up streaks remain unknown after missing/reference-inconsistent bars; no gap is filled with a synthetic price",
            "Displayed top5 only limits report length; it is not a new selected portfolio or a profitable capacity choice",
            "No next-session execution prices, fresh account observations or sector/intraday flow evidence supplied",
        ],
    }
    write_json(out / "result.json", result)
    lines = [f"# {day} 全名册收盘研究预览", "",
             f"来源名册 {len(raw)} 只；活跃非 ST {tape['active_non_st_rows']} 只。",
             f"上涨 {tape['advance_count']}，下跌 {tape['decline_count']}；广度 {tape['breadth']:.4f}。",
             f"上涨股/下跌股成交额关联值 {tape['signed_turnover_association']:.4f}，不能解释为主力净流入或意图。", "",
             "| 固定规则族 | 候选数 |", "|---|---:|"]
    lines.extend(f"| {item['family']} | {item['candidate_count']} |" for item in families)
    lines += ["", "仅为已收盘数据的研究预览。历史缺口、PIT/公司行动、后继原始报价和账户门禁仍保留；不生成订单。"]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--history-snapshot", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = preview(args.package, args.history_snapshot, args.calendar, args.out)
    print(json.dumps({"status": result["status"], "day": result["day"], "candidate_count": result["candidate_count"], "execution_admitted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
