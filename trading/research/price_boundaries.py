"""Evidence-bound initial price levels for research, not provider events or feed admission."""
import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import uuid

import pandas as pd

from ..market_dataset import write_frame, write_json
from ..settings import DATA_ROOT
from .adjusted_prices import checked_file, numeric, PRICE_FIELDS
from .daily_source_probe import ADJUSTMENT_FIELDS, WHEEL_SHA256
from a_stock_committee.market_data import BAOSTOCK_FIELDS

OLD, NEW, EFFECTIVE = "sz.300114", "sz.302132", "2025-02-17"
RENAME_DOC = "https://disc.static.szse.cn/download/disc/disk03/finalpage/2025-02-15/cedb693a-f5ee-4463-9682-ea33d406b569.PDF"
RENAME_SHA256 = "dd68049c48df826848f361fd9e7b23dd20b6805144a2e5bc36e54db638611488"


def initial_level(raw, factors, ipo, calendar):
    if raw.empty or factors.empty:
        raise ValueError("Nonempty raw and observed factor history required")
    raw = raw.sort_values("date")
    factors = factors.sort_values("dividOperateDate")
    first = factors.iloc[0]
    start, end = raw.date.iloc[0], first.dividOperateDate
    if (raw.code.nunique() != 1 or factors.code.nunique() != 1 or raw.code.iloc[0] != first.code
            or raw.date.duplicated().any() or factors.dividOperateDate.duplicated().any()
            or ipo != start or not start < end
            or numeric(first.backAdjustFactor) != 1 or numeric(first.adjustFactor) != 1):
        raise ValueError("Require a dated listing and the observed initial unit back anchor")
    prefix = raw.loc[raw.date.le(end)]
    wanted = [day for day in calendar if start <= day <= end]
    if prefix.date.tolist() != wanted or not wanted or wanted[-1] != end:
        raise ValueError("Initial price bridge has incomplete calendar observations")
    if (not prefix.adjustflag.eq("3").all() or not prefix.tradestatus.eq("1").all()
            or any(numeric(value) <= 0 for field in (*PRICE_FIELDS, "volume", "amount") for value in prefix[field])):
        raise ValueError("Initial bridge requires positive observed active raw prices/activity")
    if any(numeric(a) != numeric(b) for a, b in zip(prefix.close.iloc[:-1], prefix.preclose.iloc[1:])):
        raise ValueError("Initial price bridge crosses an unresolved close/preclose action")
    if numeric(first.foreAdjustFactor) <= 0:
        raise ValueError("Initial forward level must be observed and positive")
    return {"code": first.code, "dividOperateDate": start, "foreAdjustFactor": first.foreAdjustFactor,
            "backAdjustFactor": "", "adjustFactor": "", "origin": "derived_ipo_continuity",
            "source_code": first.code, "source_factor_date": end}, {
                "code": first.code, "first_raw_date": start, "first_factor_date": end,
                "derived_rows": len(prefix) - 1, "forward_level": first.foreAdjustFactor,
                "listing_source": "pinned_retrieval_master_not_independent_PIT_membership",
                "proof": "observed initial unit back anchor plus exact consecutive raw close/preclose continuity"}


def alias_levels(old_factors, new_factors, old_last, new_first, disclosure_hash):
    if old_factors.empty or new_factors.empty:
        raise ValueError("Nonempty observed rename factor histories required")
    old_factors = old_factors.sort_values("dividOperateDate")
    new_factors = new_factors.sort_values("dividOperateDate")
    if (disclosure_hash != RENAME_SHA256 or not old_factors.code.eq(OLD).all()
            or not new_factors.code.eq(NEW).all()
            or new_factors.dividOperateDate.iloc[0] != EFFECTIVE
            or not old_factors.dividOperateDate.lt(EFFECTIVE).all()
            or old_factors.dividOperateDate.duplicated().any()
            or new_factors.dividOperateDate.duplicated().any()
            or numeric(old_factors.foreAdjustFactor.iloc[-1]) != 1
            or numeric(new_factors.backAdjustFactor.iloc[0]) != 1
            or numeric(new_factors.adjustFactor.iloc[0]) != 1
            or new_first.date != EFFECTIVE or not old_last.date < EFFECTIVE
            or old_last.tradestatus != "1" or new_first.tradestatus != "1"
            or numeric(old_last.close) != numeric(new_first.preclose)):
        raise ValueError("Require the reviewed rename disclosure and an unchanged raw boundary reference")
    scale = numeric(new_factors.foreAdjustFactor.iloc[0])
    if (scale <= 0 or numeric(old_last.close) <= 0
            or any(numeric(value) <= 0 for frame in (old_factors, new_factors) for value in frame.foreAdjustFactor)
            or numeric(new_factors.foreAdjustFactor.iloc[-1]) != 1):
        raise ValueError("Require positive rename scale and the observed terminal forward anchor")
    return [{"code": NEW, "dividOperateDate": row.dividOperateDate,
             "foreAdjustFactor": str(numeric(row.foreAdjustFactor) * scale),
             "backAdjustFactor": "", "adjustFactor": "", "origin": "derived_code_change_scale",
             "source_code": OLD, "source_factor_date": row.dividOperateDate}
            for row in old_factors.itertuples(index=False)]


def validate_rename(rename):
    if (rename.get("old") != OLD or rename.get("new") != NEW or rename.get("effective_date") != EFFECTIVE
            or rename.get("published_date") != "2025-02-14"
            or rename.get("disclosure_sha256") != RENAME_SHA256
            or rename.get("share_count_unchanged_on_rename") is not True
            or numeric(rename["pre_rename_forward_scale"]) <= 0):
        raise ValueError("Only the reviewed dated code rename is supported")


def canonicalize(raw, adjusted, rename):
    """Retain all source identities; exclude only proven alias shadows from research."""
    validate_rename(rename)
    if (raw.empty or raw.date.nunique() != 1 or raw.code.duplicated().any()
            or not raw[["code", "date"]].equals(adjusted[["code", "date"]])):
        raise ValueError("Distinct-code aligned source day required for rename mapping")
    day = raw.date.iloc[0]
    old = raw.loc[raw.code.eq(OLD)]
    new = raw.loc[raw.code.eq(NEW)]
    if day < EFFECTIVE and (old.empty != new.empty):
        raise ValueError("Pre-rename source identity lacks its exact counterpart")
    if not old.empty:
        if day < EFFECTIVE:
            fields = [field for field in BAOSTOCK_FIELDS.split(",") if field != "code"]
            if not old[fields].reset_index(drop=True).equals(new[fields].reset_index(drop=True)):
                raise ValueError("Conflicting pre-rename raw rows cannot be silently deduplicated")
        elif (day != EFFECTIVE or new.empty or old.tradestatus.iloc[0] != "0"
              or not old[list(PRICE_FIELDS) + ["volume", "amount"]].eq("").all().all()):
            raise ValueError("Only the observed blank halt on the effective rename day may be shadowed")
    result = adjusted.copy(deep=True)
    alias = result.code.isin([OLD, NEW])
    result["security_id"] = result.code.where(~alias, OLD)
    result["tradable_code"] = result.code.where(~alias, OLD if day < EFFECTIVE else NEW)
    result["identity_origin"] = "unresolved_provider_code"
    result.loc[alias, "identity_origin"] = "reviewed_dated_code_change"
    result["alias_shadow"] = result.code.eq(OLD)
    canonical = result.loc[~result.alias_shadow].copy()
    if canonical.security_id.duplicated().any():
        raise ValueError("Canonical research day still contains duplicate identities")
    return result, canonical


def build(coverage_path, probe, check_path, master_path, disclosure, destination=None):
    coverage_path, probe, check_path, master_path, disclosure = map(
        Path, (coverage_path, probe, check_path, master_path, disclosure))
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    raw_view, factor_view = Path(coverage["raw_view"]), Path(coverage["factor_view"])
    raw_result = json.loads((raw_view / "result.json").read_text(encoding="utf-8"))
    factor_result = json.loads((factor_view / "result.json").read_text(encoding="utf-8"))
    checked_file(raw_view, "raw_daily.parquet", coverage["source_hashes"]["raw"])
    factors = pd.read_parquet(checked_file(factor_view, factor_result["panel_file"], coverage["source_hashes"]["factors"]))
    raw_plan = json.loads(checked_file(raw_view, "plan.json", raw_result["plan_sha256"]).read_text(encoding="utf-8"))
    calendar = pd.read_csv(checked_file(raw_view, "input_calendar.csv", raw_plan["calendar_sha256"]), dtype=str).date.tolist()
    receipt = json.loads((probe / "receipt.json").read_text(encoding="utf-8"))
    checked_file(probe, "collection_implementation.py", receipt["implementation_sha256"])
    checked_file(probe, "input_coverage.json", sha256(coverage_path.read_bytes()).hexdigest())
    check = json.loads(check_path.read_text(encoding="utf-8"))
    gaps = coverage["missing_start_level_codes"]
    if (raw_result["status"] != "source_cache_complete" or raw_result["missing_days"] or raw_result["empty_days"]
            or raw_result["acquired_days"] != raw_result["expected_days"]
            or receipt["coverage_sha256"] != sha256(coverage_path.read_bytes()).hexdigest()
            or gaps != sorted(set(gaps)) or NEW not in gaps
            or coverage["missing_history_codes"] or coverage["terminal_not_one_codes"]
            or receipt["status"] != "completed_probe" or receipt["sdk"]["sha256"] != WHEEL_SHA256
            or receipt["codes"] != gaps or receipt["end"] != coverage["cutoff"]
            or check["raw_daily_sha256"] != coverage["source_hashes"]["raw"]
            or Path(check["raw_view"]).resolve() != raw_view.resolve()
            or Path(check["probe"]).resolve() != probe.resolve()
            or Path(check["factor_view"]).resolve() != factor_view.resolve()
            or sorted(check["codes"]) != gaps
            or any(check[key] for key in ("post_initial_price_difference_count", "post_initial_activity_difference_count", "raw_overlap_difference_count"))
            or check["raw_overlap_rows_checked"] != check["source_rows"]):
        raise ValueError("Require the complete pinned raw scope and exact full-gap source check")
    payloads = {}
    for entry in receipt["entries"]:
        key = entry["code"], entry["kind"]
        if key in payloads or entry["error_code"] != "0":
            raise ValueError("Duplicate or failed source boundary receipt")
        frame = pd.read_parquet(checked_file(probe, entry["file"], entry["sha256"]))
        if len(frame) != entry["rows"] or frame.empty or not frame.code.eq(entry["code"]).all():
            raise ValueError("Boundary source count/code changed")
        payloads[key] = frame
    if set(payloads) != {(code, kind) for code in gaps for kind in ("raw", "adjusted", "factors")}:
        raise ValueError("All diagnosed boundary source triples required")
    master = pd.read_csv(master_path, dtype=str, keep_default_na=False).set_index("code")
    if master.index.has_duplicates:
        raise ValueError("Duplicate retrieval listing identity")
    derived, proofs = [], []
    for code in gaps:
        original = factors.loc[factors.code.eq(code)].sort_values("dividOperateDate").reset_index(drop=True)
        observed = payloads[code, "factors"].sort_values("dividOperateDate").reset_index(drop=True)
        if not original[list(ADJUSTMENT_FIELDS)].equals(observed[list(ADJUSTMENT_FIELDS)]):
            raise ValueError("Boundary factors differ from the pinned complete source union")
        if code != NEW:
            row, proof = initial_level(payloads[code, "raw"], observed, master.at[code, "ipoDate"], calendar)
            derived.append(row)
            proofs.append(proof)
    disclosure_hash = sha256(disclosure.read_bytes()).hexdigest()
    old_factors = factors.loc[factors.code.eq(OLD)]
    # The probe's backward-written raw history has been checked against all bulk rows.
    new_raw = payloads[NEW, "raw"].sort_values("date")
    old_last = new_raw.loc[new_raw.date.lt(EFFECTIVE)].iloc[-1]
    new_first = new_raw.loc[new_raw.date.eq(EFFECTIVE)].iloc[0]
    if old_last.date != max(day for day in calendar if day < EFFECTIVE):
        raise ValueError("Missing final pre-rename source session")
    derived.extend(alias_levels(old_factors, payloads[NEW, "factors"], old_last, new_first, disclosure_hash))
    original = factors.assign(origin="provider", source_code=factors.code, source_factor_date=factors.dividOperateDate)
    panel = pd.concat([original, pd.DataFrame(derived)], ignore_index=True).sort_values(["code", "dividOperateDate"])
    if panel.duplicated(["code", "dividOperateDate"]).any():
        raise ValueError("Derived levels would overwrite a source observation")
    folder = Path(destination) if destination else DATA_ROOT / "research/derived_price_levels" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    for path, name in ((Path(__file__), "implementation.py"), (coverage_path, "input_coverage.json"),
                       (check_path, "input_source_check.json"), (master_path, "input_listing_master.csv"),
                       (disclosure, "rename_disclosure.pdf")):
        shutil.copy2(path, folder / name)
    rename = {"old": OLD, "new": NEW, "effective_date": EFFECTIVE, "published_date": "2025-02-14",
              "disclosure_url": RENAME_DOC, "disclosure_sha256": disclosure_hash,
              "share_count_unchanged_on_rename": True,
              "pre_rename_forward_scale": payloads[NEW, "factors"].sort_values("dividOperateDate").foreAdjustFactor.iloc[0]}
    plan = {"kind": "adjustment", "requested_start": raw_plan["requested_start"], "requested_end": coverage["cutoff"],
            "raw_view": str(raw_view), "factor_view": str(factor_view), "source_hashes": coverage["source_hashes"],
            "probe": str(probe), "probe_receipt_sha256": sha256((probe / "receipt.json").read_bytes()).hexdigest(),
            "rename": rename, "purpose": "Derived research forward levels, never provider events or company actions"}
    write_json(folder / "plan.json", plan)
    write_json(folder / "initial_proofs.json", proofs)
    write_frame(folder / "adjustment_events.parquet", panel)
    result = {"kind": "adjustment", "status": "derived_research_levels", "raw_view": str(raw_view),
              "rows": len(panel), "symbols": int(panel.code.nunique()), "original_provider_rows": len(factors),
              "derived_initial_level_count": len(proofs), "derived_initial_price_rows": sum(p["derived_rows"] for p in proofs),
              "derived_code_change_level_count": len(old_factors), "rename": rename,
              "panel_file": "adjustment_events.parquet", "panel_sha256": sha256((folder / "adjustment_events.parquet").read_bytes()).hexdigest(),
              "plan_sha256": sha256((folder / "plan.json").read_bytes()).hexdigest(),
              "implementation_sha256": sha256((folder / "implementation.py").read_bytes()).hexdigest(),
              "execution_admitted": False, "independent_prices_verified": False, "cash_share_actions_verified": False,
              "limitations": ["Initial prices are mathematically derived from observed levels and raw continuity, not vendor qfq verification",
                              "Retrieval IPO dates are evidence for derivation, not independently verified historical membership",
                              "Derived alias levels carry only forward scales; blank back/adjust fields must not become corporate events",
                              "One reviewed code rename is not a complete permanent security master or ledger migration"]}
    write_json(folder / "result.json", result)
    return folder, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("coverage", "probe", "source-check", "master", "rename-disclosure"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, result = build(args.coverage, args.probe, args.source_check, args.master, args.rename_disclosure, args.out)
    print(json.dumps({"folder": str(folder), **result}, indent=2))
    return 0
