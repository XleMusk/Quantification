"""Build a research-only Qlib provider from a pinned StockDB snapshot.

StockDB ``pct_chg`` is used to construct the existing cumulative-return
research proxy. The result is a feature-input artifact, never an execution
price feed or account writer.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

from ..settings import DATA_ROOT, ROOT
from .factors import catalog
from .full_market import verify_chunks
from .full_market_alpha158 import provider_rows
from .full_market_mix import load_rows


def build(snapshot: Path, output: Path | None = None, *, start: int | None = None,
          end: int | None = None) -> tuple[Path, dict]:
    snapshot = Path(snapshot).resolve()
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_chunks(snapshot, manifest)
    if manifest.get("status") != "complete_inventory_only" or manifest.get("execution_admitted") is not False:
        raise ValueError("A complete, unadmitted StockDB snapshot is required")
    frame = load_rows(snapshot, manifest, {"0", "3", "6"})
    if start is not None:
        frame = frame.loc[frame["date"] >= int(start)].copy()
    if end is not None:
        frame = frame.loc[frame["date"] <= int(end)].copy()
    if frame.empty:
        raise ValueError("StockDB snapshot contains no requested SH/SZ rows")

    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    output = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_qlib_provider" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_qlib_provider" / identifier
    output.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    provider, calendar, source = provider_rows(frame, artifacts)
    definitions = catalog()
    definitions.to_csv(artifacts / "alpha158_catalog.csv", index=False)
    provider_result = {
        "schema": "a_share_stockdb_research_provider_v1",
        "status": "research_provider_completed",
        "snapshot": str(snapshot),
        "snapshot_manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
        "provider_folder": str(provider.resolve()),
        "price_view": str(snapshot),
        "calendar": calendar,
        "dates": len(calendar),
        "symbols": int(source["codes"]),
        "rows": int(source["codes"] * len(calendar)),
        "source_quality": {
            "source_rows": int(source["source_rows"]),
            "source_live_rows": int(source["source_live_rows"]),
            "source_rows_excluded_from_provider": int(source["source_rows_excluded_from_provider"]),
            "invalid_ohlc_rows": int(source["invalid_ohlc_rows"]),
            "live_invalid_ohlc_rows": int(source["live_invalid_ohlc_rows"]),
            "nonpositive_volume_rows": int(source["nonpositive_volume_rows"]),
            "vwap_nonfinite_live_rows": int(source["vwap_nonfinite_live_rows"]),
            "vwap_outside_1pct_ohlc_rows": int(source["vwap_outside_1pct_ohlc_rows"]),
            "quality_gate": "diagnostic_only",
        },
        "fields": ["open", "high", "low", "close", "volume", "vwap", "factor"],
        "source": "StockDB physical pct_chg cumulative-return research proxy",
        "model_trained": False,
        "execution_admitted": False,
        "limitations": [
            "Adjusted provider values are a research proxy, not raw RMB order prices.",
            "StockDB membership, corporate actions, adjustment semantics and PIT are not independently verified.",
            "No account, Ledger, broker or THS state was read or mutated.",
        ],
    }
    (artifacts / "result.json").write_text(json.dumps(provider_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = {**provider_result, "artifacts": str(artifacts), "provider_folder": str(provider)}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "REPORT.md").write_text(
        "\n".join([
            "# StockDB Qlib research provider",
            "",
            f"- Snapshot: `{snapshot}`",
            f"- Calendar: `{calendar[0]}` to `{calendar[-1]}` ({len(calendar)} sessions)",
            f"- Symbols: `{source['codes']}`; source rows: `{source['source_rows']:,}`; provider live rows: `{source['source_live_rows']:,}`",
            f"- Input quality diagnostics: invalid OHLC `{source['invalid_ohlc_rows']:,}`; live invalid OHLC `{source['live_invalid_ohlc_rows']:,}`; nonfinite live VWAP `{source['vwap_nonfinite_live_rows']:,}`; VWAP outside 1% OHLC `{source['vwap_outside_1pct_ohlc_rows']:,}`.",
            "- Input quality is recorded as a diagnostic-only receipt; no source rows are repaired or promoted to execution data.",
            "- This provider is for Alpha158/native-feature research only; it is not an execution price feed.",
            "- No account, Ledger, broker or Tonghuashun state was accessed.",
            "",
            f"Artifact: `{artifacts}`",
        ]) + "\n", encoding="utf-8"
    )
    return output / "result.json", result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--start", type=int)
    parser.add_argument("--end", type=int)
    args = parser.parse_args(argv)
    path, result = build(args.snapshot, args.out, start=args.start, end=args.end)
    print(json.dumps({"result": str(path), "status": result["status"],
                      "dates": result["dates"], "symbols": result["symbols"],
                      "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

