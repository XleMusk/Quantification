"""Diagnose missing StockDB bars against already-cached local sources.

This is a read-only coverage diagnostic.  It never fills a bar, changes a
frozen snapshot, or promotes an alternate provider into the trading feed.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

import pandas as pd

from ..settings import DATA_ROOT, ROOT


SCHEMA = "stockdb_source_coverage_diagnostic_v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _normal_code(value: object) -> str:
    text = str(value).strip()
    digits = "".join(character for character in text if character.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _stockdb_rows(snapshot: Path, code: str, day: str) -> dict:
    manifest = _read_json(snapshot / "manifest.json")
    matches = []
    latest = None
    earliest = None
    for item in manifest.get("chunks", []):
        path = snapshot / item["parquet_file"]
        frame = pd.read_parquet(path, columns=["code", "date", "close"])
        frame["code"] = frame["code"].map(_normal_code)
        frame["date"] = frame["date"].astype(str).str.replace("-", "", regex=False)
        scoped = frame.loc[frame["code"].eq(code)]
        if not scoped.empty:
            dates = scoped["date"]
            earliest = min(earliest, dates.min()) if earliest else dates.min()
            latest = max(latest, dates.max()) if latest else dates.max()
            matches.extend(scoped.loc[scoped["date"].eq(day)].to_dict("records"))
    return {"source": "stockdb_inventory", "path": str(snapshot),
            "path_sha256": _sha256(snapshot / "manifest.json"),
            "observed": bool(matches), "rows": matches,
            "symbol_first_date": earliest, "symbol_last_date": latest,
            "execution_admitted": bool(manifest.get("execution_admitted")),
            "research_only": True}


def _raw_rows(path: Path, code: str, day: str) -> dict:
    raw_path = path / "raw_daily.parquet" if path.is_dir() else path
    frame = pd.read_parquet(raw_path, columns=["date", "code", "close"])
    frame["code"] = frame["code"].map(_normal_code)
    frame["date"] = frame["date"].astype(str).str.replace("-", "", regex=False)
    scoped = frame.loc[frame["code"].eq(code)]
    rows = scoped.loc[scoped["date"].eq(day)].to_dict("records")
    return {"source": "local_raw_daily_supplement", "path": str(raw_path),
            "path_sha256": _sha256(raw_path), "observed": bool(rows), "rows": rows,
            "symbol_first_date": scoped["date"].min() if not scoped.empty else None,
            "symbol_last_date": scoped["date"].max() if not scoped.empty else None,
            "execution_admitted": False, "research_only": True}


def _alternate_rows(root: Path, symbol: str, day: str) -> dict:
    plan = _read_json(root / "plan.json")
    result = _read_json(root / "result.json")
    entry = (result.get("entries") or {}).get(symbol) or {}
    file_path = root / str(entry.get("file")) if entry.get("file") else None
    rows = []
    first = last = None
    if file_path and file_path.is_file():
        frame = pd.read_parquet(file_path, columns=["date", "close"])
        frame["date"] = frame["date"].astype(str).str.replace("-", "", regex=False)
        first = frame["date"].min() if not frame.empty else None
        last = frame["date"].max() if not frame.empty else None
        rows = frame.loc[frame["date"].eq(day)].to_dict("records")
    return {"source": f"alternate_{plan.get('provider', 'unknown')}", "path": str(root),
            "path_sha256": _sha256(root / "plan.json"), "provider": plan.get("provider"),
            "status": result.get("status"), "observed": bool(rows), "rows": rows,
            "symbol_first_date": first, "symbol_last_date": last,
            "entry_metadata": entry, "execution_admitted": bool(result.get("execution_admitted")),
            "research_only": True}


def diagnose(snapshot: Path, symbols: list[str], days: list[str], *, supplemental_raw_view: Path | None = None,
             alternate_roots: list[Path] | None = None, out_root: Path | None = None) -> tuple[Path, dict]:
    snapshot = snapshot.resolve()
    symbols = sorted({_normal_code(symbol) for symbol in symbols})
    days = sorted({str(day).replace("-", "") for day in days})
    if not symbols or not days:
        raise ValueError("At least one symbol and date are required")
    alternate_roots = [Path(root).resolve() for root in (alternate_roots or [])]
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    artifacts = (out_root or (DATA_ROOT / "research" / "stockdb_source_coverage" / identifier)).resolve()
    output = ROOT / "outputs" / "stockdb_source_coverage" / identifier
    artifacts.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    observations = []
    for code in symbols:
        for day in days:
            sources = [_stockdb_rows(snapshot, code, day)]
            if supplemental_raw_view:
                sources.append(_raw_rows(supplemental_raw_view, code, day))
            symbol = code + (".SSE" if code.startswith("6") else ".SZSE")
            sources.extend(_alternate_rows(root, symbol, day) for root in alternate_roots)
            observations.append({"code": code, "symbol": symbol, "date": day,
                                 "observed_by_any_source": any(item["observed"] for item in sources),
                                 "sources": sources})
    missing = [item for item in observations if not item["observed_by_any_source"]]
    record = {
        "schema": SCHEMA, "status": "coverage_gap_confirmed" if missing else "source_row_observed",
        "created_at": datetime.now().astimezone().isoformat(), "snapshot": str(snapshot),
        "symbols": symbols, "dates": days, "observations": observations,
        "missing_observations": len(missing), "research_only": True,
        "execution_admitted": False, "snapshot_modified": False, "imputation_performed": False,
        "next_action": "obtain an explicit verified source row or keep the affected replay blocked",
        "artifacts": str(artifacts), "output": str(output),
    }
    for path in (artifacts / "run.json", output / "run.json"):
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    report = ["# StockDB Source Coverage Diagnostic", "", f"Status: `{record['status']}`.",
              f"Requested observations: `{len(observations)}`; missing from all checked sources: `{len(missing)}`.",
              "No bar was imputed, no frozen snapshot was changed, and no execution admission was made.", ""]
    for item in observations:
        state = "observed" if item["observed_by_any_source"] else "missing_from_all_checked_sources"
        report.append(f"- `{item['symbol']} {item['date']}`: `{state}`")
        for source in item["sources"]:
            report.append(f"  - {source['source']}: observed=`{source['observed']}`, last=`{source.get('symbol_last_date')}`")
    report.extend(["", f"Artifacts: `{artifacts}`", f"Machine-readable result: `{output / 'run.json'}`"])
    (output / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return output, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--date", action="append", required=True)
    parser.add_argument("--supplemental-raw-view", type=Path)
    parser.add_argument("--alternate-root", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    output, record = diagnose(args.snapshot, args.symbol, args.date,
                               supplemental_raw_view=args.supplemental_raw_view,
                               alternate_roots=args.alternate_root)
    print(json.dumps({"output": str(output), "status": record["status"],
                      "missing_observations": record["missing_observations"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
