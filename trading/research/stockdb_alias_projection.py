"""Read-only historical code-alias projection and reconciliation diagnostic.

Some BaoStock legacy panels contain both ``300114`` and its later code
``302132`` for the same historical dates.  This module evaluates a dated
projection (302132 -> 300114 strictly before 2025-02-17), preferring an
existing canonical row when the two rows collide.  It never edits the source
panel or promotes the projection into StockDB or the trading path.
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


SCHEMA = "stockdb_alias_projection_diagnostic_v1"
ALIAS_MAP = {"302132": "300114"}
DEFAULT_CUTOFF = "2025-02-17"
PRICE_FIELDS = ("open", "high", "low", "close")
VALUE_FIELDS = PRICE_FIELDS + ("pre_close",)
RECON_FIELDS = VALUE_FIELDS + ("volume", "amount", "pct_chg")


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_code(value: object) -> str:
    text = str(value).strip().upper()
    match = re.search(r"(\d{6})(?:$|\.)", text)
    return match.group(1) if match else ""


def _normal_date(value: object) -> str:
    return pd.Timestamp(value).date().isoformat()


def _parse_panel_dates(values: pd.Series) -> pd.Series:
    """Parse ISO dates and StockDB-style YYYYMMDD integers without epoch drift."""
    numeric = pd.to_numeric(values, errors="coerce")
    yyyymmdd = numeric.between(19000101, 21001231)
    parsed = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
    if yyyymmdd.any():
        parsed.loc[yyyymmdd] = pd.to_datetime(
            numeric.loc[yyyymmdd].astype("Int64").astype("string"),
            format="%Y%m%d", errors="coerce",
        )
    if (~yyyymmdd).any():
        parsed.loc[~yyyymmdd] = pd.to_datetime(values.loc[~yyyymmdd], errors="coerce")
    return parsed.dt.date.astype("string")


def _panel_paths(values: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        path = Path(value).resolve()
        if path.is_file():
            paths.append(path)
            continue
        if path.is_dir():
            preferred = path / "raw_daily.parquet"
            if preferred.is_file():
                paths.append(preferred)
            else:
                paths.extend(sorted(path.glob("*.parquet")))
        if not paths:
            raise FileNotFoundError(f"No panel found under {path}")
    if not paths:
        raise ValueError("At least one source panel is required")
    return paths


def _read_panel(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path, low_memory=False)
    else:
        frame = pd.read_parquet(path)
    if "code" not in frame.columns or "date" not in frame.columns:
        raise ValueError(f"panel {path} requires code and date columns")
    rename = {"preclose": "pre_close", "pctChg": "pct_chg", "isST": "is_st"}
    frame = frame.rename(columns={key: value for key, value in rename.items() if key in frame.columns}).copy()
    frame["_source_code"] = frame["code"].astype("string").str.strip()
    frame["_code"] = frame["_source_code"].map(normalize_code)
    frame["_date"] = _parse_panel_dates(frame["date"])
    for field in RECON_FIELDS:
        if field not in frame.columns:
            frame[field] = pd.NA
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    return frame


def _equal(left: object, right: object, tolerance: float = 1e-8) -> bool:
    left_number, right_number = pd.to_numeric(pd.Series([left, right]), errors="coerce").tolist()
    if pd.isna(left_number) and pd.isna(right_number):
        return True
    if pd.isna(left_number) or pd.isna(right_number):
        return False
    return abs(float(left_number) - float(right_number)) <= tolerance * max(1.0, abs(float(left_number)), abs(float(right_number)))


def _row_comparison(left: pd.Series, right: pd.Series) -> dict:
    ohlc_equal = all(_equal(left[field], right[field]) for field in PRICE_FIELDS)
    pre_close_equal = _equal(left["pre_close"], right["pre_close"])
    mismatch_fields = [field for field in VALUE_FIELDS if not _equal(left[field], right[field])]
    return {
        "ohlc_equal": ohlc_equal, "pre_close_equal": pre_close_equal,
        "mismatch_fields": mismatch_fields,
        "classification": "exact_duplicate" if not mismatch_fields else "field_conflict",
    }


def _stats(frame: pd.DataFrame) -> dict:
    keys = frame[["_code", "_date"]].dropna().drop_duplicates()
    valid_ohlc = frame[list(PRICE_FIELDS)].notna().all(axis=1)
    return {
        "rows": int(len(frame)), "distinct_code_date_keys": int(len(keys)),
        "symbols": int(frame["_code"].nunique()),
        "sessions": int(frame["_date"].nunique()),
        "first_date": frame["_date"].min() if len(frame) else None,
        "last_date": frame["_date"].max() if len(frame) else None,
        "ohlc_complete_rows": int(valid_ohlc.sum()),
        "pre_close_complete_rows": int(frame["pre_close"].notna().sum()),
        "duplicate_key_rows": int(frame.duplicated(["_code", "_date"], keep=False).sum()),
    }


def _project(frame: pd.DataFrame, cutoff: str) -> tuple[pd.DataFrame, dict]:
    frame = frame.copy()
    historical_alias = frame["_code"].eq("302132") & frame["_date"].lt(cutoff)
    canonical = frame["_code"].eq("300114")
    affected = frame.loc[historical_alias | canonical].copy()
    # Compare historical canonical/alias rows before deduplication.  A panel
    # can carry both rows; canonical wins only when they collide after mapping.
    comparisons = []
    for day, scoped in affected.loc[affected["_date"].lt(cutoff)].groupby("_date", sort=True):
        canonical_rows = scoped.loc[scoped["_code"].eq("300114")]
        alias_rows = scoped.loc[scoped["_code"].eq("302132")]
        if canonical_rows.empty:
            for _, row in alias_rows.iterrows():
                comparisons.append({"date": day, "classification": "alias_only", "mismatch_fields": [],
                                    "ohlc_equal": None, "pre_close_equal": None})
        elif alias_rows.empty:
            for _, row in canonical_rows.iterrows():
                comparisons.append({"date": day, "classification": "canonical_only", "mismatch_fields": [],
                                    "ohlc_equal": None, "pre_close_equal": None})
        else:
            # Usually one row per side. Compare the first deterministic row;
            # additional duplicates are reported by duplicate_key_rows.
            item = _row_comparison(canonical_rows.iloc[0], alias_rows.iloc[0])
            comparisons.append({"date": day, **item})
    frame["_projected_code"] = frame["_code"]
    frame.loc[historical_alias, "_projected_code"] = "300114"
    frame["_preference"] = frame["_code"].eq("300114").map({True: 0, False: 1}).astype(int)
    frame = frame.sort_values(["_projected_code", "_date", "_preference", "_source_code"], kind="mergesort")
    deduped = frame.drop_duplicates(["_projected_code", "_date"], keep="first").copy()
    classification_counts: dict[str, int] = {}
    for item in comparisons:
        key = item["classification"]
        classification_counts[key] = classification_counts.get(key, 0) + 1
    # Keep a compact evidence table for rows affected by the historical alias.
    changed = frame.loc[historical_alias.loc[frame.index].fillna(False) if historical_alias.index.equals(frame.index) else frame["_code"].eq("302132") & frame["_date"].lt(cutoff)].copy()
    if not changed.empty:
        changed["projected_code"] = changed["_projected_code"]
        changed["projection_rule"] = "302132->300114_before_2025-02-17"
    info = {
        "cutoff_exclusive": cutoff, "alias_map": ALIAS_MAP,
        "historical_alias_rows": int(historical_alias.sum()),
        "post_cutoff_alias_rows_preserved": int((frame["_code"].eq("302132") & frame["_date"].ge(cutoff)).sum()),
        "canonical_rows_pre_cutoff": int((frame["_code"].eq("300114") & frame["_date"].lt(cutoff)).sum()),
        "duplicate_rows_removed_after_projection": int(len(frame) - len(deduped)),
        "classification_counts": classification_counts,
        "comparisons": comparisons,
        "affected_rows": changed,
    }
    return deduped, info


def _reconcile(projected: dict[str, pd.DataFrame]) -> list[dict]:
    names = sorted(projected)
    results = []
    for i, left_name in enumerate(names):
        for right_name in names[i + 1:]:
            left = projected[left_name][["_projected_code", "_date", *RECON_FIELDS]].copy()
            right = projected[right_name][["_projected_code", "_date", *RECON_FIELDS]].copy()
            left = left.rename(columns={field: f"{field}_left" for field in RECON_FIELDS})
            right = right.rename(columns={field: f"{field}_right" for field in RECON_FIELDS})
            merged = left.merge(right, on=["_projected_code", "_date"], how="inner", validate="one_to_one")
            common = len(merged)
            if common:
                ohlc_equal_mask = pd.Series(True, index=merged.index)
                for field in PRICE_FIELDS:
                    lvalue, rvalue = merged[f"{field}_left"], merged[f"{field}_right"]
                    equal = lvalue.eq(rvalue) | (lvalue.isna() & rvalue.isna())
                    numeric = pd.to_numeric(lvalue, errors="coerce").notna() & pd.to_numeric(rvalue, errors="coerce").notna()
                    equal |= numeric & (pd.to_numeric(lvalue, errors="coerce") - pd.to_numeric(rvalue, errors="coerce")).abs().le(1e-8 * pd.concat([pd.Series(1.0, index=merged.index), pd.to_numeric(lvalue, errors="coerce").abs(), pd.to_numeric(rvalue, errors="coerce").abs()], axis=1).max(axis=1))
                    ohlc_equal_mask &= equal
                lvalue, rvalue = merged["pre_close_left"], merged["pre_close_right"]
                lnum, rnum = pd.to_numeric(lvalue, errors="coerce"), pd.to_numeric(rvalue, errors="coerce")
                numeric = lnum.notna() & rnum.notna()
                same_missing = lnum.isna() & rnum.isna()
                scale = pd.concat([pd.Series(1.0, index=merged.index), lnum.abs(), rnum.abs()], axis=1).max(axis=1)
                pre_equal_mask = same_missing | (numeric & (lnum - rnum).abs().le(1e-8 * scale))
                pre_equal_tol_mask = same_missing | (numeric & (lnum - rnum).abs().le(0.02))
                conflict = ~(ohlc_equal_mask & pre_equal_mask)
                examples = [{"code": row["_projected_code"], "date": row["_date"], "ohlc_equal": bool(ohlc_equal_mask.loc[index]), "pre_close_equal_exact": bool(pre_equal_mask.loc[index]), "pre_close_equal_tolerance_0_02": bool(pre_equal_tol_mask.loc[index])} for index, row in merged.loc[conflict].head(20).iterrows()]
                ohlc_equal = int(ohlc_equal_mask.sum())
                ohlc_conflicts = int((~ohlc_equal_mask).sum())
                pre_equal = int(pre_equal_mask.sum())
                pre_conflicts = int((~pre_equal_mask).sum())
                pre_equal_tol = int(pre_equal_tol_mask.sum())
                pre_conflicts_tol = int((~pre_equal_tol_mask).sum())
                metric_masks = {}
                for field in ("volume", "amount", "pct_chg"):
                    lnum, rnum = pd.to_numeric(merged[f"{field}_left"], errors="coerce"), pd.to_numeric(merged[f"{field}_right"], errors="coerce")
                    same = lnum.isna() & rnum.isna()
                    exact = same | (lnum.notna() & rnum.notna() & (lnum - rnum).abs().le(1e-8 * pd.concat([pd.Series(1.0, index=merged.index), lnum.abs(), rnum.abs()], axis=1).max(axis=1)))
                    metric_masks[field] = {"equal_rows": int(exact.sum()), "conflict_rows": int((~exact).sum())}
            else:
                ohlc_equal = ohlc_conflicts = pre_equal = pre_conflicts = pre_equal_tol = pre_conflicts_tol = 0
                metric_masks = {field: {"equal_rows": 0, "conflict_rows": 0} for field in ("volume", "amount", "pct_chg")}
                examples = []
            results.append({
                "left": left_name, "right": right_name,
                "left_keys": int(len(left)), "right_keys": int(len(right)),
                "common_keys": int(common),
                "left_only_keys": int(len(left) - common),
                "right_only_keys": int(len(right) - common),
                "ohlc_equal_rows": ohlc_equal, "ohlc_conflict_rows": ohlc_conflicts,
                "pre_close_equal_rows": pre_equal, "pre_close_conflict_rows": pre_conflicts,
                "pre_close_exact_equal_rows": pre_equal, "pre_close_exact_conflict_rows": pre_conflicts,
                "pre_close_tolerance_0_02_equal_rows": pre_equal_tol,
                "pre_close_tolerance_0_02_conflict_rows": pre_conflicts_tol,
                "volume_reconciliation": metric_masks["volume"],
                "amount_reconciliation": metric_masks["amount"],
                "pct_chg_reconciliation": metric_masks["pct_chg"],
                "conflict_examples": examples,
            })
    return results


def diagnose(source_panels: list[Path], *, cutoff: str = DEFAULT_CUTOFF, out: Path | None = None,
             artifact: Path | None = None) -> tuple[Path, dict]:
    cutoff = _normal_date(cutoff)
    if cutoff != DEFAULT_CUTOFF:
        # The current migration evidence is tied to this dated alias; callers
        # must choose an explicit new rule rather than silently changing it.
        raise ValueError(f"302132->300114 projection is pinned to {DEFAULT_CUTOFF}")
    paths = _panel_paths(source_panels)
    projected: dict[str, pd.DataFrame] = {}
    panel_reports = []
    affected_frames = []
    for index, path in enumerate(paths, start=1):
        frame = _read_panel(path)
        before = _stats(frame)
        after, info = _project(frame, cutoff)
        after_for_stats = after.drop(columns=["_code"]).rename(columns={"_projected_code": "_code"})
        after_stats = _stats(after_for_stats)
        # Multiple legacy roots commonly contain a file named raw_daily.parquet;
        # keep a stable unique key so pairwise reconciliation is not overwritten.
        projected[f"panel_{index}:{path.name}"] = after
        affected = info.pop("affected_rows")
        if not affected.empty:
            evidence_fields = ["_source_code", "_code", "_date", "_projected_code", *VALUE_FIELDS]
            evidence = affected[[field for field in evidence_fields if field in affected.columns]].copy()
            for field in VALUE_FIELDS:
                evidence[field] = pd.to_numeric(evidence[field], errors="coerce")
            evidence["source_panel"] = str(path)
            affected_frames.append(evidence)
        panel_reports.append({
            "path": str(path), "sha256": _sha256(path), "before": before,
            "after": after_stats, "projection": info,
        })
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    output = Path(out).resolve() if out else ROOT / "outputs" / "stockdb_alias_projection" / identifier
    artifacts = Path(artifact).resolve() if artifact else DATA_ROOT / "research" / "stockdb_alias_projection" / identifier
    output.mkdir(parents=True, exist_ok=False); artifacts.mkdir(parents=True, exist_ok=False)
    affected_path = artifacts / "alias_resolved_affected_rows.parquet"
    if affected_frames:
        pd.concat(affected_frames, ignore_index=True).to_parquet(affected_path, index=False)
    else:
        pd.DataFrame(columns=["_source_code", "_code", "_date", "_projected_code", "source_panel"]).to_parquet(affected_path, index=False)
    pairwise = _reconcile(projected)
    record = {
        "schema": SCHEMA, "status": "diagnostic_complete", "created_at": datetime.now().astimezone().isoformat(),
        "source_panels": panel_reports, "pairwise_reconciliation": pairwise,
        "alias_map": ALIAS_MAP, "cutoff_exclusive": cutoff,
        "affected_evidence_path": str(affected_path), "affected_evidence_sha256": _sha256(affected_path),
        "promotion": False, "promotion_admitted": False, "research_only": True,
        "execution_admitted": False, "future_labels_used": False, "model_refit": False,
        "ledger_mutated": False, "ths_accessed": False, "stockdb_snapshot_modified": False,
        "source_panels_modified": False,
        "next_action": "review conflict classes and independent instrument-master evidence before any dated alias admission",
    }
    for name in ("result.json", "audit.json"):
        (output / name).write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    (artifacts / "manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    lines = ["# Historical code alias projection diagnostic", "",
             "- Rule: `302132 -> 300114` for dates strictly before `2025-02-17`; canonical 300114 wins on collisions.",
             "- Original legacy panels are read-only and were not modified.", "- Promotion: `false`; StockDB, Ledger and THS were not touched.", ""]
    for panel in panel_reports:
        before, after = panel["before"], panel["after"]
        proj = panel["projection"]
        lines.extend([f"## {panel['path']}", "",
                      f"- Before rows/keys: `{before['rows']:,}` / `{before['distinct_code_date_keys']:,}`; after: `{after['rows']:,}` / `{after['distinct_code_date_keys']:,}`.",
                      f"- Historical alias rows: `{proj['historical_alias_rows']:,}`; duplicate rows removed: `{proj['duplicate_rows_removed_after_projection']:,}`; post-cutoff alias rows preserved: `{proj['post_cutoff_alias_rows_preserved']:,}`.",
                      f"- Conflict classes: `{proj['classification_counts']}`.", ""])
    for comparison in pairwise:
        lines.extend([
            "## Pairwise alias-resolved reconciliation", "",
            f"- `{comparison['left']}` vs `{comparison['right']}`: common keys `{comparison['common_keys']:,}`; left-only `{comparison['left_only_keys']:,}`; right-only `{comparison['right_only_keys']:,}`.",
            f"- OHLC equal/conflict: `{comparison['ohlc_equal_rows']:,}` / `{comparison['ohlc_conflict_rows']:,}`; pre_close equal/conflict: `{comparison['pre_close_equal_rows']:,}` / `{comparison['pre_close_conflict_rows']:,}`.",
            f"- pre_close tolerance 0.02 equal/conflict: `{comparison['pre_close_tolerance_0_02_equal_rows']:,}` / `{comparison['pre_close_tolerance_0_02_conflict_rows']:,}`; volume exact equal/conflict: `{comparison['volume_reconciliation']['equal_rows']:,}` / `{comparison['volume_reconciliation']['conflict_rows']:,}`; amount exact equal/conflict: `{comparison['amount_reconciliation']['equal_rows']:,}` / `{comparison['amount_reconciliation']['conflict_rows']:,}`.",
            "- pre_close differences remain a diagnostic semantic conflict; no source precedence or repair was applied.", "",
        ])
    lines.extend([f"- Affected-row evidence: `{affected_path}`", f"- Result: `{output / 'result.json'}`", ""])
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output / "result.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-panel", type=Path, action="append", required=True,
                        help="legacy parquet/CSV panel or a folder containing raw_daily.parquet; repeatable")
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args(argv)
    path, result = diagnose(args.source_panel, cutoff=args.cutoff, out=args.out, artifact=args.artifact)
    print(json.dumps({"result": str(path), "status": result["status"],
                      "promotion": result["promotion"], "panels": len(result["source_panels"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
