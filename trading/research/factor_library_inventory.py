"""Export the locally available formula catalog without evaluating market data."""

import argparse
from importlib.metadata import version
import json
from pathlib import Path

import pandas as pd

from .factors import catalog


COLUMNS = [
    "library", "name", "category", "formula", "requires_volume",
    "requires_vwap", "market_diagnostic", "structural_note", "source",
]


def template_notes(templates):
    relative = {
        (int(row.window_1), int(row.window_2)): row.name
        for row in templates.itertuples(index=False)
        if row.family == "relative_trend"
    }
    notes = {}
    for row in templates.itertuples(index=False):
        if row.family != "relative_trend":
            continue
        first, second = int(row.window_1), int(row.window_2)
        if first == second:
            notes[row.name] = "constant_zero_self_difference"
        else:
            mirror = relative.get((second, first))
            if mirror is None:
                raise ValueError(f"Missing sign mirror for {row.name}")
            notes[row.name] = f"sign_mirror_of_{mirror}"
    return notes


def completed_alpha360_status(sh_run=None, bse_run=None):
    if sh_run is None and bse_run is None:
        return "not_computed_on_pinned_market_snapshot"
    if sh_run is None or bse_run is None:
        raise ValueError("Both Alpha360 market run paths are required")
    hashes = set()
    for path, group in ((sh_run, "sh_sz_all_equities"), (bse_run, "bse_equities")):
        path = Path(path)
        record = json.loads((path / "protocol.json").read_text(encoding="utf-8"))
        if (record.get("status") != "completed_research_only"
                or record.get("group") != group
                or record.get("completed_definitions") != 360
                or not (path / "factor_summary.csv").is_file()):
            raise ValueError(f"Incomplete Alpha360 diagnostic: {path}")
        hashes.add(record["snapshot_manifest_sha256"])
    if len(hashes) != 1:
        raise ValueError("Alpha360 diagnostics used different source snapshots")
    return "2026_sh_sz_and_bse_signal_diagnostic"


def build_inventory(template_catalog, alpha360_status="not_computed_on_pinned_market_snapshot"):
    from qlib.contrib.data.loader import Alpha360DL

    qlib_source = f"pyqlib_{version('pyqlib')}"
    alpha158 = catalog()
    formulas360, names360 = Alpha360DL.get_feature_config()
    templates = pd.read_csv(template_catalog)
    if len(alpha158) != 158 or len(names360) != 360 or len(templates) != 200:
        raise ValueError("The installed/saved formula catalog does not match the audited scope")
    if templates.name.nunique() != 200 or len(set(names360)) != 360:
        raise ValueError("Duplicate formula names in a library")
    notes = template_notes(templates)
    alpha158_by_formula = dict(zip(alpha158.formula, alpha158.name))
    rows = []
    for item in alpha158.itertuples(index=False):
        formula = item.formula.lower()
        rows.append(("qlib_alpha158", item.name, item.category, item.formula,
                     "$volume" in formula, "$vwap" in formula,
                     "2026_sh_sz_and_bse_signal_diagnostic", "", qlib_source))
    for name, formula in zip(names360, formulas360):
        source = formula.lower()
        category = "lagged_volume" if "$volume" in source else (
            "lagged_vwap" if "$vwap" in source else "lagged_price")
        duplicate = alpha158_by_formula.get(formula)
        constant = name in {"CLOSE0", "VOLUME0"}
        note = ("constant_one_on_observed_rows" if constant else
                f"exact_formula_duplicate_of_qlib_alpha158:{duplicate}" if duplicate else "")
        rows.append(("qlib_alpha360", name, category, formula,
                     "$volume" in source, "$vwap" in source,
                     "computed_but_no_valid_ic_constant_one" if constant and alpha360_status.startswith("2026_")
                     else alpha360_status, note,
                     qlib_source))
    for item in templates.itertuples(index=False):
        uses_volume = item.family in {
            "trend_volume", "position_volume", "trend_correlation", "quiet_reversal"
        }
        rows.append(("local_template", item.name, item.family, item.formula,
                     uses_volume, False,
                     "computed_but_no_valid_ic_constant_zero" if notes.get(item.name) == "constant_zero_self_difference"
                     else "2026_sh_sz_and_bse_signal_diagnostic",
                     notes.get(item.name, ""), str(template_catalog)))
    result = pd.DataFrame(rows, columns=COLUMNS)
    if len(result) != 718 or result.duplicated(["library", "name"]).any():
        raise ValueError("Incomplete or duplicate formula inventory")
    return result


def indicator_functions():
    import talib

    rows = [
        {"group": group, "function": name, "source": f"TA-Lib_{version('TA-Lib')}"}
        for group, names in talib.get_function_groups().items()
        for name in names
    ]
    result = pd.DataFrame(rows).sort_values(["group", "function"]).reset_index(drop=True)
    if len(result) != 158 or result.function.duplicated().any():
        raise ValueError("Unexpected TA-Lib function catalog")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-catalog", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--alpha360-sh-run", type=Path)
    parser.add_argument("--alpha360-bse-run", type=Path)
    args = parser.parse_args()
    alpha360_status = completed_alpha360_status(args.alpha360_sh_run, args.alpha360_bse_run)
    inventory = build_inventory(args.template_catalog, alpha360_status)
    args.out.mkdir(parents=True, exist_ok=True)
    destination = args.out / "formula_inventory.csv"
    inventory.to_csv(destination, index=False)
    indicator_functions().to_csv(args.out / "operator_inventory.csv", index=False)
    print(f"{destination}: {len(inventory)} definitions")
    print(inventory.groupby("library").size().to_string())
    print(inventory.structural_note.str.startswith("constant_zero").sum(), "constant-zero templates")
    print(inventory.structural_note.str.startswith("sign_mirror").sum() // 2, "sign-mirror pairs")
    print("operator_inventory.csv: 158 TA-Lib functions")


if __name__ == "__main__":
    main()
