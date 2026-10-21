"""Fixed reporting-calendar cohorts from existing frozen provider receipts."""
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from .financials import FIELDS, cached, normalize


POLICY = {
    "selection": "Calendar only: Jan-Apr previous Q3; May-Aug current Q1; Sep-Oct current Q2; Nov-Dec current Q3",
    "availability": "Read the exact frozen quarter; actual pubDate strictly earlier than the decision session",
    "missing": "Exclude unavailable/nonfinite observations equally from all four cases; no older/newer-period fallback",
    "standardization": "Recompute original profitability ranks within this single fixed-period cohort",
    "annualization_or_ttm": False,
    "source_boundary": "Reporting-calendar research convention, not issuer compliance or revision-safe PIT verification",
    "sources": [
        {"url": "https://docs.static.szse.cn/www/lawrules/rule/allrules/bussiness/W020240430677262007864.pdf",
         "read": "2024 final main-board rules, section5.2.2, printed page57"},
        {"url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/mainipo/c/c_20260424_10816589.shtml",
         "read": "2026 main-board rules, section5.2.2"},
    ],
}


def fixed_period(day):
    current = date.fromisoformat(day)
    if current.isoformat() != day or not 2024 <= current.year <= 2026:
        raise ValueError("Fixed reporting-calendar study covers canonical 2024-2026 dates only")
    if current.month < 5:
        return f"{current.year - 1}-09-30"
    month_day = "03-31" if current.month < 9 else "06-30" if current.month < 11 else "09-30"
    return f"{current.year}-{month_day}"


def load_frozen_periods(view, days):
    view = Path(view).resolve()
    result = json.loads((view / "result.json").read_text(encoding="utf-8"))
    source_path = view / "source_receipts.json"
    if (result.get("status") != "completed_publication_lagged_provider_view"
            or result.get("execution_admitted") is not False
            or sha256(source_path.read_bytes()).hexdigest() != result["source_receipts_sha256"]):
        raise ValueError("Original frozen financial receipt required")
    state = json.loads(source_path.read_text(encoding="utf-8"))
    plan = json.loads((view / "source_plan.json").read_text(encoding="utf-8"))
    periods = sorted({fixed_period(day) for day in days})
    queries = [(int(period[:4]), int(period[5:7]) // 3) for period in periods]
    if not queries or not all([year, quarter] in plan["periods"] for year, quarter in queries):
        raise ValueError("Requested fixed quarters outside the frozen source scope")
    complete = [info for info in plan["symbols"] if all(
        f"{info['symbol']}:{year}Q{quarter}" in state["tasks"] for year, quarter in plan["periods"])]
    if len(complete) != result["symbols"]:
        raise ValueError("Frozen completed financial scope changed")
    frames = []
    for info in complete:
        for year, quarter in queries:
            receipt = state["tasks"][f"{info['symbol']}:{year}Q{quarter}"]
            if (receipt["code"], receipt["year"], receipt["quarter"]) != (info["code"], year, quarter):
                raise ValueError("Frozen quarter receipt identity conflict")
            raw = cached(view.parent.parent, receipt)
            if len(raw) != receipt["rows"]:
                raise ValueError("Frozen quarter row count changed")
            frames.append(normalize(raw, info["code"], year, quarter))
    reports = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=[*FIELDS, "symbol"])
    if reports.duplicated(["symbol", "statDate"]).any():
        raise ValueError("Unresolved frozen-quarter financial revisions")
    return reports


def period_observations(reports, day):
    selected = fixed_period(day)
    if reports.duplicated(["symbol", "statDate"]).any():
        raise ValueError("Distinct financial symbol/report periods required")
    pub = pd.to_datetime(reports.pubDate, errors="raise")
    stat = pd.to_datetime(reports.statDate, errors="raise")
    if pub.isna().any() or stat.isna().any() or not stat.le(pub).all():
        raise ValueError("Known chronological financial publication/period dates required")
    current = reports.loc[reports.statDate.eq(selected) & pub.lt(pd.Timestamp(day)),
                          ["symbol", "pubDate", "statDate", "roeAvg", "gpMargin"]].copy()
    return current.rename(columns={"pubDate": "source_pub_date", "statDate": "source_stat_date",
                                   "roeAvg": "roeAvg_provider_value", "gpMargin": "gpMargin_provider_value"})
