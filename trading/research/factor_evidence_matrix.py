"""Join completed full-market factor diagnostics without selecting a strategy."""

import argparse
import json
from pathlib import Path

import pandas as pd

from .experiment import write_json


SOURCES = (
    ("qlib_alpha158", "sh_sz_all_equities", "alpha158_sh", "factor_summary.csv", "completed_definitions", 158),
    ("qlib_alpha158", "bse_equities", "alpha158_bse", "factor_summary.csv", "completed_definitions", 158),
    ("qlib_alpha360", "sh_sz_all_equities", "alpha360_sh", "factor_summary.csv", "completed_definitions", 360),
    ("qlib_alpha360", "bse_equities", "alpha360_bse", "factor_summary.csv", "completed_definitions", 360),
    ("local_template", "sh_sz_all_equities", "template_sh", "template_summary.csv", "completed_templates", 200),
    ("local_template", "bse_equities", "template_bse", "template_summary.csv", "completed_templates", 200),
)


def build_matrix(inventory_path, runs):
    inventory = pd.read_csv(inventory_path)
    if len(inventory) != 718 or inventory.duplicated(["library", "name"]).any():
        raise ValueError("Expected the audited 718-definition inventory")
    metadata = inventory[["library", "name", "category", "requires_volume",
                          "requires_vwap", "structural_note"]]
    pieces = []
    hashes = set()
    for library, group, key, filename, completion, expected in SOURCES:
        folder = Path(runs[key])
        record = json.loads((folder / "protocol.json").read_text(encoding="utf-8"))
        if (record.get("status") != "completed_research_only" or record.get("group") != group
                or record.get(completion) != expected):
            raise ValueError(f"Incomplete or mismatched source: {folder}")
        hashes.add(record["snapshot_manifest_sha256"])
        summary = pd.read_csv(folder / filename)
        factors = set(inventory.loc[inventory.library.eq(library), "name"])
        if (set(summary.factor) != factors or len(summary) != expected * 4
                or summary.duplicated(["factor", "direction", "segment"]).any()
                or set(summary.direction) != {"high", "low"}
                or set(summary.segment) != {"selection", "later_check"}):
            raise ValueError(f"Incomplete factor/direction/segment grid: {folder}")
        summary.insert(0, "group", group)
        summary.insert(0, "library", library)
        pieces.append(summary)
    if len(hashes) != 1:
        raise ValueError("Factor runs do not share one frozen data snapshot")
    result = pd.concat(pieces, ignore_index=True).merge(
        metadata, left_on=["library", "factor"], right_on=["library", "name"],
        how="left", validate="many_to_one")
    if len(result) != 718 * 2 * 2 * 2 or result.category.isna().any():
        raise ValueError("Evidence matrix did not cover every definition")
    return result.drop(columns="name"), hashes.pop()


def family_counts(matrix):
    rows = []
    for (library, group, category), part in matrix.groupby(["library", "group", "category"]):
        good = part.loc[part.mean_rank_ic.gt(0) & part.mean_top_minus_universe.gt(0)]
        dual = good.groupby(["factor", "direction"]).segment.nunique().eq(2)
        rows.append({"library": library, "group": group, "category": category,
                     "definitions": part.factor.nunique(),
                     "evaluable_definitions": part.loc[part.evaluated_dates.gt(0), "factor"].nunique(),
                     "dual_positive_directions": int(dual.sum()),
                     "possible_directions": part.factor.nunique() * 2})
    return pd.DataFrame(rows).sort_values(["library", "group", "category"])


def cross_market_sign_flags(matrix):
    good = matrix.loc[matrix.mean_rank_ic.gt(0) & matrix.mean_top_minus_universe.gt(0)]
    by_group = good.groupby(["library", "factor", "direction", "group"]).segment.nunique().eq(2)
    by_both = by_group.loc[by_group].reset_index().groupby(
        ["library", "factor", "direction"]).group.nunique().eq(2)
    flags = by_both.loc[by_both].reset_index()[["library", "factor", "direction"]]
    categories = matrix[["library", "factor", "category"]].drop_duplicates()
    return flags.merge(categories, on=["library", "factor"], validate="many_to_one")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    for _, _, key, _, _, _ in SOURCES:
        parser.add_argument("--" + key.replace("_", "-"), type=Path, required=True)
    args = parser.parse_args(argv)
    runs = {key: getattr(args, key) for _, _, key, _, _, _ in SOURCES}
    matrix, manifest_hash = build_matrix(args.inventory, runs)
    args.out.mkdir(parents=True, exist_ok=True)
    matrix.to_csv(args.out / "factor_signal_matrix.csv", index=False)
    counts = family_counts(matrix)
    counts.to_csv(args.out / "family_signal_counts.csv", index=False)
    flags = cross_market_sign_flags(matrix)
    flags.to_csv(args.out / "cross_market_sign_flags.csv", index=False)
    write_json(args.out / "matrix_protocol.json", {
        "status": "completed_research_only", "snapshot_manifest_sha256": manifest_hash,
        "formula_count": 718, "matrix_rows": len(matrix),
        "cross_market_sign_flags": len(flags),
        "sources": {key: str(path) for key, path in runs.items()},
        "signal_diagnostic_only": True, "account_backtest": False,
        "trading_authorized": False, "fresh_holdout_claimed": False})
    print(f"{len(matrix)} factor/group/direction/segment rows")
    print(counts.groupby(["library", "group"])[["definitions", "evaluable_definitions",
                                                   "dual_positive_directions"]].sum().to_string())


if __name__ == "__main__":
    main()
