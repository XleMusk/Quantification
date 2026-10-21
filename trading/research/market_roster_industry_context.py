"""Lagged industry labels over a complete caller-verified intraday roster.

Only price-change distributions are aggregated. The old mapping neither proves
today's eligibility nor identifies themes, capital flow or participant intent.
"""
from datetime import date, datetime
from hashlib import sha256
import json
import math
from pathlib import Path

from ..settings import DATA_ROOT
from .baostock_daily_slice import digest
from .leader_cohort_quote_view import summarize
from .style_exposure import load_industries


SCHEMA = "market_roster_lagged_industry_intraday_context_v1"
MAPPING_RELATIVE = Path("research/style_exposure/20261004_033506_867ed142/industry_history")
MAPPING_DAY = "2026-09-30"
FLAGS = {"research_only": True, "revision_safe_pit_verified": False,
         "current_industry_membership_verified": False, "current_theme_membership_verified": False,
         "today_eligibility_verified": False, "all_A_breadth": False,
         "amount_aggregation_performed": False, "net_inflow_identified": False,
         "participant_intent_inferred": False, "selection_enabled": False,
         "execution_admitted": False}


def _time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Industry context clocks require an explicit timezone")
    return result


def _inputs(rows, targets):
    if (not targets or len(rows) != len(targets)
            or [row["symbol"] for row in rows] != [target["symbol"] for target in targets]
            or len({target["symbol"] for target in targets}) != len(targets)):
        raise ValueError("Industry context retains every exact ordered roster target")
    for row in rows:
        if type(row["current_at_read"]) is not bool:
            raise ValueError("Caller-revalidated quote status must be explicit")
        if row["current_at_read"] and not math.isfinite(float(row["day_change_percent"])):
            raise ValueError("Known price changes must be finite")


def industry_context(*, read_rows, saved_rows, targets, source_day, source_available_at,
                     observation_began_at, read_at, current_valid, data_root=DATA_ROOT):
    """Read one saved industry snapshot; failure affects this context only.

    ``saved_rows`` must already be revalidated by the price view at this
    observation's response time. ``read_rows`` are revalidated at ``read_at``.
    Both retain the complete target order; this helper never inner-joins away
    unmapped targets or upgrades saved quote-validity flags.
    """
    result = {"schema": SCHEMA, "status": "unavailable_industry_context", "source_day": source_day,
              "source_available_at": str(source_available_at), "observation_began_at": str(observation_began_at),
              "read_at": str(read_at), "mapping": None, "member_count": len(targets),
              "mapped_target_count": None, "unknown_mapping_count": None, "mapping_coverage": None,
              "groups": [], "unknown_mapping": None, **FLAGS}
    try:
        began, now, available = map(_time, (observation_began_at, read_at, source_available_at))
        source_date = date.fromisoformat(source_day)
        if not available <= began <= now or source_date > began.date():
            raise ValueError("Roster source must be actually available before observation/read clocks")
        if type(current_valid) is not bool:
            raise ValueError("Caller current-view validity must be explicit")
        _inputs(read_rows, targets)
        _inputs(saved_rows, targets)
        if any(target["source_day"] != source_day or type(target["in_fixed_590"]) is not bool for target in targets):
            raise ValueError("Dated roster source/590 relationship changed")

        folder = (Path(data_root).resolve() / MAPPING_RELATIVE).resolve()
        receipt_path, snapshot_path = folder / "result.json", folder / (MAPPING_DAY + ".parquet")
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8-sig"))
        entries = [entry for entry in receipt["entries"] if entry["date"] == MAPPING_DAY]
        if receipt.get("status") != "completed_requested_industry_history" or len(entries) != 1:
            raise ValueError("One completed exact saved industry entry required")
        entry = entries[0]
        if (folder / entry["file"]).resolve() != snapshot_path:
            raise ValueError("Saved industry entry differs from the pinned mapping file")
        known_at, mapping_date = _time(entry["retrieved_at"]), date.fromisoformat(entry["date"])
        if mapping_date > source_date or known_at > began or known_at > now:
            raise ValueError("Future or not-yet-known industry mapping cannot be used")
        snapshot_sha = digest(snapshot_path)
        mapping, _ = load_industries(folder, [MAPPING_DAY])
        if receipt_path.read_bytes() != receipt_bytes or digest(snapshot_path) != snapshot_sha:
            raise ValueError("Industry source changed during this read")
        required = {"date", "symbol", "industryClassification", "industry", "updateDate"}
        if (not required <= set(mapping) or mapping.empty or mapping.symbol.duplicated().any()
                or not mapping.date.eq(MAPPING_DAY).all()):
            raise ValueError("Unique exact-dated industry identities required")

        labels = {}
        for row in mapping.to_dict("records"):
            classification, industry = row["industryClassification"], row["industry"]
            if isinstance(classification, str) and isinstance(industry, str) and classification.strip() and industry.strip():
                labels[row["symbol"]] = (classification, industry)
        members = {}
        unknown = []
        for index, target in enumerate(targets):
            key = labels.get(target["symbol"])
            if key is None:
                unknown.append(index)
            else:
                members.setdefault(key, []).append(index)

        def group(indices, *, classification=None, industry=None, mapped=True):
            return {"industryClassification": classification, "industry": industry,
                    "member_count": len(indices), "industry_mapping_covered_count": len(indices) if mapped else 0,
                    "fixed_590_count": sum(targets[index]["in_fixed_590"] for index in indices),
                    "current_summary": summarize([read_rows[index] for index in indices]) if current_valid else None,
                    "observation_summary": summarize([saved_rows[index] for index in indices])}

        mapped_count = len(targets) - len(unknown)
        result.update(status="observed_lagged_industry_price_context_only", mapped_target_count=mapped_count,
                      unknown_mapping_count=len(unknown), mapping_coverage=mapped_count / len(targets),
                      groups=[group(indices, classification=key[0], industry=key[1])
                              for key, indices in sorted(members.items())],
                      unknown_mapping=group(unknown, mapped=False),
                      mapping={"file": str(snapshot_path), "sha256": snapshot_sha,
                               "receipt_file": str(receipt_path), "receipt_sha256": sha256(receipt_bytes).hexdigest(),
                               "snapshot_date": MAPPING_DAY, "known_at": known_at.isoformat(),
                               "known_at_basis": "actual source entry retrieved_at, not filename or updateDate",
                               "source_update_dates": sorted(mapping.updateDate.astype(str).unique().tolist()),
                               "snapshot_rows": len(mapping), "snapshot_staleness_calendar_days_at_source": (source_date - mapping_date).days,
                               "snapshot_staleness_calendar_days_at_read": (now.date() - mapping_date).days,
                               "known_age_seconds_at_observation": (began - known_at).total_seconds()},
                      group_order="industryClassification_then_industry_lexicographic_not_return_rank",
                      limitations=["Old industry labels are frozen background, not today's eligibility or theme membership",
                                   "Unknown industry members remain in their own full-denominator bucket",
                                   "Price-change distributions do not identify net capital flow or participant intent"])
    except (OSError, ValueError, KeyError, TypeError, ImportError, ArithmeticError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    return result
