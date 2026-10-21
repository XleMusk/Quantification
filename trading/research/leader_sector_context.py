"""Read-only, lagged broad-industry context for an observed daily cross-section.

The mapping is a provider snapshot, not historical concept/theme membership.
This module neither selects securities nor interprets turnover as capital flow.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone, timedelta
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .style_exposure import load_industries


def _aware(value, label):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"Timezone-aware {label} required")
    return result


def sector_context(current_features, snapshot_path, *, decision_day, observed_at,
                   current_scope="caller_supplied_daily_cross_section",
                   current_source_complete=False):
    """Aggregate one day's observed rows using an explicitly supplied old mapping.

    The caller supplies the cross-section and its completeness. Missing mappings
    stay in the amount denominator and are reported separately. A mapping not
    collected by the decision close cannot be used as historical context.
    """
    decision = date.fromisoformat(decision_day)
    observed = _aware(observed_at, "observation time")
    close = datetime.combine(decision, time(15), timezone(timedelta(hours=8)))
    if observed < close:
        raise ValueError("Daily industry context requires an observed decision close")
    path = Path(snapshot_path).resolve()
    receipt = json.loads((path.parent / "result.json").read_text(encoding="utf-8-sig"))
    entries = [entry for entry in receipt["entries"]
               if (path.parent / entry["file"]).resolve() == path]
    if len(entries) != 1:
        raise ValueError("One exact industry snapshot entry required")
    entry = entries[0]
    snapshot_day = date.fromisoformat(entry["date"])
    if snapshot_day > decision:
        raise ValueError("Future industry snapshot cannot describe this decision")
    known_at = _aware(entry["retrieved_at"], "industry retrieval time")
    if known_at > close or known_at > observed:
        raise ValueError("Industry snapshot collected after the decision close")
    industry, _ = load_industries(path.parent, [entry["date"]])
    required = {"date", "symbol", "amount", "pct_chg"}
    if not required <= set(current_features):
        raise ValueError("Daily symbol/amount/return columns required")
    frame = current_features.copy(deep=True)
    if frame.empty or frame.symbol.duplicated().any() or not frame.date.astype(str).eq(decision_day).all():
        raise ValueError("One exact nonempty unique-symbol daily cross-section required")
    input_rows = len(frame)
    if "active_non_st" in frame:
        if not frame.active_non_st.isin([True, False]).all():
            raise ValueError("Explicit active/non-ST status required when supplied")
        frame = frame.loc[frame.active_non_st.eq(True)].copy()
    frame["amount"] = pd.to_numeric(frame.amount, errors="coerce")
    frame["pct_chg"] = pd.to_numeric(frame.pct_chg, errors="coerce")
    valid = np.isfinite(frame[["amount", "pct_chg"]].to_numpy()).all(axis=1) & frame.amount.ge(0)
    invalid_rows = int((~valid).sum())
    frame = frame.loc[valid].copy()
    mapping = industry[["symbol", "industry", "industryClassification"]].copy()
    frame = frame.merge(mapping, on="symbol", how="left", validate="one_to_one")
    known = frame.industry.fillna("").astype(str).str.strip().ne("") & frame.industryClassification.fillna("").astype(str).str.strip().ne("")
    amount = float(frame.amount.sum())
    known_amount = float(frame.loc[known, "amount"].sum())
    sectors = []
    for (classification, label), group in frame.loc[known].groupby(["industryClassification", "industry"], sort=True):
        count, advances, declines = len(group), int(group.pct_chg.gt(0).sum()), int(group.pct_chg.lt(0).sum())
        value = float(group.amount.sum())
        sectors.append({"industry": str(label), "classification": str(classification),
                        "observed_names": count, "advance_count": advances, "decline_count": declines,
                        "flat_count": int(group.pct_chg.eq(0).sum()),
                        "breadth": (advances - declines) / count,
                        "median_return_pct": float(group.pct_chg.median()), "amount_cny": value,
                        "amount_share_of_observed_cross_section": value / amount if amount else None})
    sectors.sort(key=lambda row: (-row["amount_cny"], row["classification"], row["industry"]))
    return {"schema": "leader_lagged_broad_industry_context_v1", "status": "observed_lagged_context_only",
            "decision_day": decision_day, "observed_at": observed.isoformat(), "current_scope": current_scope,
            "current_source_complete": bool(current_source_complete), "input_rows": input_rows,
            "valid_active_observed_rows": len(frame), "invalid_active_rows": invalid_rows,
            "snapshot_path": str(path), "snapshot_date": entry["date"], "known_at": known_at.isoformat(),
            "known_at_basis": "source receipt entry retrieved_at, not snapshot filename or updateDate",
            "source_update_dates": sorted(industry.updateDate.astype(str).unique().tolist()),
            "snapshot_staleness_calendar_days": (decision - snapshot_day).days,
            "snapshot_rows": len(industry), "snapshot_industry_count": int(industry.industry.nunique()),
            "mapped_observed_names": int(known.sum()), "unmapped_observed_names": int((~known).sum()),
            "observed_mapping_name_coverage": float(known.mean()) if len(frame) else None,
            "observed_mapping_amount_coverage": known_amount / amount if amount else None,
            "unmapped_amount_cny": amount - known_amount, "observed_amount_cny": amount,
            "sectors": sectors, "revision_safe_pit_verified": False, "concept_membership_verified": False,
            "net_inflow_identified": False, "current_intraday_state": False,
            "selection_enabled": False, "execution_admitted": False,
            "limitations": ["Broad industry labels are not concept or theme membership",
                            "Old classifications may have changed; historical revision safety is unverified",
                            "Metrics describe the caller's observed cross-section, not missing members or all-market completeness",
                            "Turnover shares and return breadth do not identify net capital flow or participant intent"]}
