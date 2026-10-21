"""Build an independent 2023-to-2024+ splice inventory/provider manifest.

This consumer stages the canonicalized 2023 BaoStock panel beside metadata for
the existing 2024+ StockDB/Qlib provider.  It does not append to, overwrite,
or promote the existing provider.  A splice is admitted only when date,
identity, field semantics, and coverage gates all pass; the current receipt is
expected to remain diagnostic because the raw and derived provider schemas are
different and the semantic policy rejects legacy ``pre_close`` substitution.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import uuid

import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .stockdb_semantic_policy import _normalise, _resolve_panel


SCHEMA = "stockdb_baostock_splice_inventory_v1"


def _hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code(value: object) -> str:
    match = re.search(r"(\d{6})", str(value))
    return match.group(1) if match else ""


def _provider_result(path: Path) -> tuple[Path, dict]:
    path = Path(path).resolve()
    result_path = path / "result.json" if path.is_dir() else path
    record = json.loads(result_path.read_text(encoding="utf-8"))
    if record.get("status") != "research_provider_completed" or record.get("execution_admitted") is not False:
        raise ValueError("A completed, unadmitted 2024+ research provider is required")
    provider = Path(record["provider_folder"]).resolve()
    provider.relative_to(result_path.parent.resolve())
    return result_path, record


def _provider_symbols(provider: Path) -> set[str]:
    instrument = provider / "instruments" / "all.txt"
    if not instrument.is_file():
        raise FileNotFoundError(f"provider instrument inventory missing: {instrument}")
    return {code for line in instrument.read_text(encoding="utf-8").splitlines()
            for code in [_code(line.split("\t", 1)[0])] if code}


def _date_strings(values) -> list[str]:
    result = []
    for value in values:
        text = str(value).strip()
        if re.fullmatch(r"\d{8}", text):
            result.append(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
        else:
            result.append(pd.Timestamp(value).date().isoformat())
    return sorted(set(result))


def build(extension: Path, provider: Path, *, semantic_policy: Path | None = None,
          out: Path | None = None, artifact: Path | None = None) -> tuple[Path, dict]:
    extension = Path(extension).resolve()
    extension_panel = _resolve_panel(extension)
    ext = _normalise(extension)
    # _normalise applies the dated 302132 -> 300114 projection and canonical
    # row precedence used by the semantic policy.  Preserve this as a new
    # artifact rather than mutating the extension source.
    ext["code"] = ext.pop("canonical_code")
    ext["date"] = pd.to_datetime(ext["date"]).dt.strftime("%Y-%m-%d")
    ext = ext.sort_values(["date", "code"], kind="mergesort").reset_index(drop=True)
    provider_result_path, provider_result = _provider_result(provider)
    provider_folder = Path(provider_result["provider_folder"]).resolve()
    provider_codes = _provider_symbols(provider_folder)
    provider_dates = _date_strings(provider_result["calendar"])
    extension_dates = _date_strings(ext["date"])
    extension_codes = set(ext["code"].astype(str))
    common_codes = extension_codes & provider_codes
    combined_dates = sorted(set(extension_dates) | set(provider_dates))
    ext_2024_dates = [day for day in provider_dates if day.startswith("2024-")]
    extension_last = extension_dates[-1]
    provider_first = provider_dates[0]
    date_gap_days = [day for day in combined_dates if extension_last < day < provider_first]
    semantic_record = None
    if semantic_policy:
        semantic_path = Path(semantic_policy).resolve()
        semantic_record = json.loads((semantic_path / "result.json" if semantic_path.is_dir() else semantic_path).read_text(encoding="utf-8"))
    duplicate_keys = int(ext.duplicated(["code", "date"]).sum())
    ext_cells = int(len(ext))
    expected_ext_cells = int(len(extension_dates) * len(extension_codes))
    inventory = pd.DataFrame([
        {"segment": "baostock_extension_2023", "first_date": extension_dates[0], "last_date": extension_dates[-1],
         "sessions": len(extension_dates), "symbols": len(extension_codes), "rows": len(ext),
         "expected_cells": expected_ext_cells, "observed_cells": ext_cells,
         "coverage_ratio": ext_cells / expected_ext_cells if expected_ext_cells else 0.0,
         "schema": "raw_canonical_ohlcv", "source": str(extension_panel)},
        {"segment": "stockdb_qlib_provider_2024_plus", "first_date": provider_dates[0], "last_date": provider_dates[-1],
         "sessions": len(provider_dates), "sessions_in_2024": len(ext_2024_dates), "symbols": len(provider_codes),
         "rows": int(provider_result["rows"]), "expected_cells": int(provider_result["rows"]),
         "observed_cells": int(provider_result["rows"]), "coverage_ratio": 1.0,
         "schema": "qlib_derived_open_high_low_close_volume_vwap_factor", "source": str(provider_folder)},
    ])
    splice_reasons = []
    if date_gap_days:
        splice_reasons.append("calendar_gap_between_extension_and_provider")
    if extension_last >= provider_first:
        splice_reasons.append("date_ranges_overlap_or_reverse")
    if duplicate_keys:
        splice_reasons.append("duplicate_canonical_extension_keys")
    if not common_codes:
        splice_reasons.append("no_common_symbols")
    if semantic_record and not semantic_record.get("canonical_policy", {}).get("promotion", False):
        splice_reasons.append("semantic_policy_promotion_false")
    splice_reasons.extend([
        "raw_extension_schema_differs_from_qlib_derived_provider",
        "legacy_pre_close_substitution_not_accepted",
        "historical_PIT_membership_and_corporate_actions_not_resolved",
    ])
    record = {
        "schema": SCHEMA, "status": "diagnostic_complete", "created_at": datetime.now().astimezone().isoformat(),
        "extension": {"path": str(extension_panel), "sha256": _hash(extension_panel), "rows": len(ext),
                      "symbols": len(extension_codes), "sessions": len(extension_dates), "duplicate_keys": duplicate_keys},
        "provider": {"result": str(provider_result_path), "result_sha256": _hash(provider_result_path),
                     "provider_folder": str(provider_folder), "symbols": len(provider_codes),
                     "sessions": len(provider_dates), "first_date": provider_first, "last_date": provider_dates[-1],
                     "sessions_in_2024": len(ext_2024_dates), "fields": provider_result.get("fields", [])},
        "splice_boundary": {"extension_last_date": extension_last, "provider_first_date": provider_first,
                             "calendar_gap_dates": date_gap_days, "calendar_gap_count": len(date_gap_days),
                             "date_overlap_count": len(set(extension_dates) & set(provider_dates)),
                             "combined_sessions": len(combined_dates)},
        "symbol_overlap": {"extension_symbols": len(extension_codes), "provider_symbols": len(provider_codes),
                           "common_symbols": len(common_codes), "extension_only_symbols": len(extension_codes - provider_codes),
                           "provider_only_symbols": len(provider_codes - extension_codes)},
        "field_policy": {
            "extension_fields": ["code", "date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "is_st", "pre_close"],
            "provider_fields": provider_result.get("fields", []),
            "raw_price_basis": "retain extension raw OHLCV/amount/pct_chg/is_st/pre_close for 2023 diagnostics",
            "derived_provider_basis": "retain existing 2024+ provider values as derived research features",
            "pre_close": "use extension raw pre_close paired with extension pct_chg; never substitute legacy pre_close",
            "amount": "retain extension RMB amount; provider does not expose raw amount",
            "splice_rule": "do not concatenate rows until raw/derived schema and PIT/action coverage have separate admission evidence",
        },
        "inventory": inventory.to_dict(orient="records"),
        "semantic_policy": semantic_record.get("canonical_policy") if semantic_record else None,
        "splice_admissible": False, "splice_reasons": splice_reasons,
        "promotion": False, "research_only": True, "execution_admitted": False,
        "future_labels_used": False, "model_refit": False, "ledger_mutated": False, "ths_accessed": False,
        "stockdb_snapshot_modified": False, "existing_provider_modified": False,
        "next_action": "resolve PIT membership, action dates, and raw-to-derived field contract; then run a separately versioned provider build",
    }
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    output = Path(out).resolve() if out else ROOT / "outputs" / "stockdb_baostock_splice_inventory" / identifier
    artifacts = Path(artifact).resolve() if artifact else DATA_ROOT / "research" / "stockdb_baostock_splice_inventory" / identifier
    output.mkdir(parents=True, exist_ok=False); artifacts.mkdir(parents=True, exist_ok=False)
    ext_path = artifacts / "raw_2023_canonical.parquet"
    ext.to_parquet(ext_path, index=False)
    inventory.to_csv(artifacts / "diagnostic_inventory.csv", index=False)
    provider_manifest = {"schema": "diagnostic_provider_manifest_v1", "status": "diagnostic_only",
                         "raw_2023_panel": str(ext_path), "existing_2024_plus_provider": str(provider_folder),
                         "splice_admissible": False, "promotion": False,
                         "field_policy": record["field_policy"], "splice_reasons": splice_reasons}
    (artifacts / "provider_manifest.json").write_text(json.dumps(provider_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (artifacts / "manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    for name in ("result.json", "audit.json"):
        (output / name).write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    report = ["# BaoStock 2023 + StockDB/Qlib 2024+ diagnostic inventory", "",
              "- Splice admissible: `False`; promotion: `False`; existing provider and StockDB were not modified.",
              f"- 2023 extension: `{len(extension_dates)}` sessions, `{len(extension_codes)}` symbols, `{len(ext):,}` canonical rows after dated alias projection.",
              f"- Existing provider: `{len(provider_dates)}` sessions (`{len(ext_2024_dates)}` in 2024), `{len(provider_codes)}` symbols, `{provider_result['rows']:,}` calendar-grid rows.",
              f"- Boundary: extension `{extension_last}` -> provider `{provider_first}`; date overlap `{record['splice_boundary']['date_overlap_count']}`, gap `{record['splice_boundary']['calendar_gap_count']}`.",
              f"- Symbol overlap: common `{len(common_codes)}`, extension-only `{len(extension_codes - provider_codes)}`, provider-only `{len(provider_codes - extension_codes)}`.",
              "- Field policy keeps the 2023 raw basis and the 2024+ derived provider separate; legacy pre_close is never substituted.",
              f"- Stop reasons: `{'; '.join(splice_reasons)}`.", "",
              f"- Provider manifest: `{artifacts / 'provider_manifest.json'}`",
              f"- Diagnostic inventory: `{artifacts / 'diagnostic_inventory.csv'}`",
              f"- Canonical 2023 panel: `{ext_path}`", f"- Result: `{output / 'result.json'}`"]
    (output / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return output / "result.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension", type=Path, required=True)
    parser.add_argument("--provider", type=Path, required=True)
    parser.add_argument("--semantic-policy", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args(argv)
    path, result = build(args.extension, args.provider, semantic_policy=args.semantic_policy,
                         out=args.out, artifact=args.artifact)
    print(json.dumps({"result": str(path), "status": result["status"],
                      "splice_admissible": result["splice_admissible"],
                      "promotion": result["promotion"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
