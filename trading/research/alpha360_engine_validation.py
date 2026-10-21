"""Check vectorized Alpha360 diagnostics against native Qlib output."""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .experiment import write_json


KEY = ["group", "date", "segment", "factor", "direction"]
METRICS = ["eligible", "pairs", "missing_forward_labels", "rank_ic",
           "top_minus_universe"]


def validate(native_path, vectorized_path):
    native_path, vectorized_path = Path(native_path), Path(vectorized_path)
    original = json.loads((native_path / "protocol.json").read_text(encoding="utf-8"))
    candidate = json.loads((vectorized_path / "protocol.json").read_text(encoding="utf-8"))
    if (original.get("engine", "qlib") != "qlib" or candidate.get("engine") != "vectorized"
            or original["group"] != candidate["group"]
            or original["snapshot_manifest_sha256"] != candidate["snapshot_manifest_sha256"]
            or candidate["status"] != "completed_research_only"
            or candidate["completed_definitions"] != 360
            or original["completed_definitions"] < 1):
        raise ValueError("Alpha360 run metadata is incompatible")
    native_catalog = pd.read_csv(native_path / "alpha360_catalog.csv")
    fast_catalog = pd.read_csv(vectorized_path / "alpha360_catalog.csv")
    if not native_catalog.equals(fast_catalog):
        raise ValueError("Formula catalogs differ")
    reference = pd.read_csv(native_path / "daily_factor_diagnostics.csv")
    fast = pd.read_csv(vectorized_path / "daily_factor_diagnostics.csv")
    if reference.duplicated(KEY).any() or fast.duplicated(KEY).any():
        raise ValueError("Duplicate factor/date/direction records")
    joined = reference.merge(fast, on=KEY, suffixes=("_qlib", "_vectorized"),
                             how="left", validate="one_to_one", indicator=True)
    if len(joined) != len(reference) or not joined._merge.eq("both").all():
        raise ValueError("Native Qlib rows are missing from the vectorized run")
    comparisons = {}
    for metric in METRICS:
        left, right = joined[metric + "_qlib"], joined[metric + "_vectorized"]
        exact = np.array_equal(left.to_numpy(), right.to_numpy(), equal_nan=True)
        comparisons[metric] = {"exact": bool(exact),
                               "max_abs_difference": float((left - right).abs().max(skipna=True))}
    if not all(item["exact"] for item in comparisons.values()):
        raise ValueError(f"Alpha360 engines disagree: {comparisons}")
    return {"status": "exact_match", "group": original["group"],
            "native_run": str(native_path), "vectorized_run": str(vectorized_path),
            "snapshot_manifest_sha256": original["snapshot_manifest_sha256"],
            "reference_definitions_completed": original["completed_definitions"],
            "vectorized_definitions_completed": candidate["completed_definitions"],
            "compared_rows": len(joined), "metrics": comparisons}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--vectorized", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = validate(args.native, args.vectorized)
    write_json(args.out, result)
    print(f"Exact match: {result['compared_rows']} rows, {result['group']}")


if __name__ == "__main__":
    main()
