"""Extend a derived research factor view with later provider events.

The derived IPO/rename rows are retained with their provenance. Later provider
events are appended only after exact identity and numeric validation. This is a
research conversion input, never an adjusted-feed or trading admission.
"""

from __future__ import annotations

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
from .adjusted_prices import ADJUSTMENT_FIELDS, checked_file, numeric
from .factor_baselines import validate

PROVENANCE_FIELDS = ("origin", "source_code", "source_factor_date")


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def extend(base_view, extension_view, raw_view, destination=None):
    base_view, extension_view, raw_view = map(Path, (base_view, extension_view, raw_view))
    base_result = json.loads((base_view / "result.json").read_text(encoding="utf-8"))
    extension_result = json.loads((extension_view / "result.json").read_text(encoding="utf-8"))
    raw_result = json.loads((raw_view / "result.json").read_text(encoding="utf-8"))
    if base_result.get("status") != "derived_research_levels":
        raise ValueError("Base factor view must be a derived research-level view")
    if extension_result.get("kind") != "adjustment" or extension_result.get("status") not in {
            "source_cache_complete", "source_union_built"}:
        raise ValueError("Later provider factor view is incomplete")
    if raw_result.get("kind") != "stock" or raw_result.get("status") != "source_cache_complete":
        raise ValueError("Current raw view is not a complete stock source view")
    base_plan = json.loads(checked_file(base_view, "plan.json", base_result["plan_sha256"]).read_text(encoding="utf-8"))
    extension_plan = json.loads(checked_file(
        extension_view, "plan.json", extension_result["plan_sha256"]).read_text(encoding="utf-8"))
    raw_plan = json.loads(checked_file(raw_view, "plan.json", raw_result["plan_sha256"]).read_text(encoding="utf-8"))
    if extension_plan["requested_start"] <= base_plan["requested_end"]:
        raise ValueError("Provider extension must start after the derived base cutoff")
    if raw_plan["requested_end"] != extension_plan["requested_end"]:
        raise ValueError("Raw and factor extension cutoffs differ")

    base_path = checked_file(base_view, base_result["panel_file"], base_result["panel_sha256"])
    ext_path = checked_file(extension_view, extension_result["panel_file"], extension_result["panel_sha256"])
    base = pd.read_parquet(base_path)
    extension = pd.read_parquet(ext_path)
    expected = set(ADJUSTMENT_FIELDS) | set(PROVENANCE_FIELDS)
    if set(base.columns) != expected or base.duplicated(list(ADJUSTMENT_FIELDS[:2])).any():
        raise ValueError("Derived base factor schema/identity changed")
    if set(extension.columns) != set(ADJUSTMENT_FIELDS) or extension.duplicated(
            list(ADJUSTMENT_FIELDS[:2])).any():
        raise ValueError("Provider extension factor schema/identity changed")
    if any(day <= base_plan["requested_end"] for day in extension.dividOperateDate):
        raise ValueError("Provider extension contains an event at or before the base cutoff")
    for code, group in extension.groupby("code"):
        validate(group, code, extension_plan["requested_end"])

    extension = extension.copy()
    extension["origin"] = "provider"
    extension["source_code"] = extension["code"]
    extension["source_factor_date"] = extension["dividOperateDate"]
    extension = extension[base.columns]
    rows = {}
    for frame in (base, extension):
        for row in frame.to_dict("records"):
            key = row["code"], row["dividOperateDate"]
            if key in rows:
                for field in ADJUSTMENT_FIELDS[2:]:
                    left, right = rows[key][field], row[field]
                    if left == "" or right == "":
                        if left != right:
                            raise ValueError(f"Conflicting blank/provider factor: {key} {field}")
                    elif numeric(left) != numeric(right):
                        raise ValueError(f"Conflicting factor sources: {key} {field}")
            else:
                rows[key] = row
    panel = pd.DataFrame(rows.values(), columns=base.columns).sort_values(["code", "dividOperateDate"])

    folder = Path(destination) if destination else DATA_ROOT / "research/extended_derived_factor_views" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(base_view / "implementation.py", folder / "implementation.py")
    for name in ("initial_proofs.json", "input_coverage.json", "input_source_check.json",
                 "input_listing_master.csv", "rename_disclosure.pdf"):
        source = base_view / name
        if source.exists():
            shutil.copy2(source, folder / name)
    panel_path = folder / "adjustment_events.parquet"
    write_frame(panel_path, panel)
    rename = base_plan["rename"]
    plan = {"kind": "adjustment", "requested_start": raw_plan["requested_start"],
            "requested_end": raw_plan["requested_end"], "raw_view": str(raw_view),
            "factor_view": str(folder),
            "source_hashes": {"raw": raw_result["raw_daily_sha256"], "factors": _hash(panel_path)},
            "probe": base_plan.get("probe"), "probe_receipt_sha256": base_plan.get("probe_receipt_sha256"),
            "rename": rename,
            "base_view": str(base_view), "extension_view": str(extension_view),
            "purpose": "Derived research levels retained while appending later provider factor events"}
    write_json(folder / "plan.json", plan)
    result = {"kind": "adjustment", "status": "derived_research_levels", "raw_view": str(raw_view),
              "rows": len(panel), "symbols": int(panel.code.nunique()),
              "original_provider_rows": int(base_result["original_provider_rows"] + len(extension)),
              "derived_initial_level_count": base_result["derived_initial_level_count"],
              "derived_initial_price_rows": base_result["derived_initial_price_rows"],
              "derived_code_change_level_count": base_result["derived_code_change_level_count"],
              "extension_provider_rows": len(extension), "rename": rename,
              "panel_file": panel_path.name, "panel_sha256": _hash(panel_path),
              "plan_sha256": _hash(folder / "plan.json"),
              "implementation_sha256": _hash(folder / "implementation.py"),
              "execution_admitted": False, "independent_prices_verified": False,
              "cash_share_actions_verified": False,
              "limitations": list(base_result.get("limitations", [])) + [
                  "Later factor events are appended from the same provider; derived levels remain research-only",
                  "No independent qfq price comparison or cash/share entitlement verification"]}
    write_json(folder / "result.json", result)
    return folder, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-view", type=Path, required=True)
    parser.add_argument("--extension-view", type=Path, required=True)
    parser.add_argument("--raw-view", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, result = extend(args.base_view, args.extension_view, args.raw_view, args.out)
    print(json.dumps({"folder": str(folder), **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
