"""Intersect a current diagnostic rank with a pinned daily-action probe."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import pandas as pd

from ..settings import DATA_ROOT, ROOT
from ..symbols import normalize_symbol


def _json(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def run(ranking_run: Path, adjustment_receipt: Path, *, output: Path | None = None) -> tuple[Path, dict]:
    ranking = _json(Path(ranking_run).resolve())
    if ranking.get("status") != "completed_stockdb_factor_rank_diagnostic" or ranking.get("execution_admitted") is not False:
        raise ValueError("Completed unadmitted rank diagnostic required")
    adjustment = _json(Path(adjustment_receipt).resolve())
    if adjustment.get("status") != "completed_probe" or adjustment.get("execution_admitted") is not False:
        raise ValueError("Completed unadmitted adjustment probe required")
    entry = next((item for item in adjustment.get("entries", []) if item.get("kind") == "adjustment"), None)
    if not entry:
        raise ValueError("Adjustment probe entry is missing")
    action_path = Path(adjustment_receipt).resolve().parent / entry["file"]
    actions = pd.read_csv(action_path, dtype=str, keep_default_na=False)
    actions["symbol"] = actions["code"].map(lambda value: normalize_symbol(value).vt_symbol)
    ranking_path = Path(ranking.get("ranking_path") or ranking.get("result", {}).get("ranking_path", ""))
    ranked = pd.read_csv(ranking_path, dtype={"symbol": str})
    ranked_symbols = set(ranked.symbol)
    action_symbols = set(actions.symbol)
    ranked_action = sorted(ranked_symbols & action_symbols)
    code_part = actions.code.str.split(".").str[-1]
    etf_rows = code_part.str.startswith(("1", "5"))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_action_gate_diagnostics" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_action_gate_diagnostics" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    actions.to_csv(artifacts / "actions.csv", index=False)
    result = {
        "status": "completed_stockdb_action_gate_diagnostic",
        "ranking_run": str(Path(ranking_run).resolve()),
        "adjustment_receipt": str(Path(adjustment_receipt).resolve()),
        "date": adjustment.get("days", [None])[0],
        "action_rows": int(len(actions)),
        "equity_action_rows": int((~etf_rows).sum()),
        "etf_or_non_equity_rows": int(etf_rows.sum()),
        "ranked_count": int(len(ranked)),
        "ranked_action_intersection": ranked_action,
        "ranked_action_count": len(ranked_action),
        "execution_admitted": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "ths_payload_generated": False,
        "action_semantics_verified": False,
        "artifacts": str(artifacts),
        "limitations": [
            "BaoStock adjustment factors are not cash/share action terms and were not applied to prices or holdings.",
            "The action probe is retrieval-time and does not establish PIT entitlement or settlement semantics.",
            "No action was substituted into the model, Ledger, paper portfolio or THS route.",
        ],
    }
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# StockDB daily action gate diagnostic", "",
        f"Date: `{result['date']}`; adjustment rows: `{len(actions)}`; ranked intersection: `{len(ranked_action)}`.",
        f"Ranked action symbols: `{ranked_action}`.",
        "Adjustment semantics remain unverified; no action was applied or promoted.",
    ]) + "\n", encoding="utf-8")
    return out / "result.json", result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ranking-run", required=True, type=Path)
    parser.add_argument("--adjustment-receipt", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = run(args.ranking_run, args.adjustment_receipt, output=args.out)
    print(json.dumps({"run": str(path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
