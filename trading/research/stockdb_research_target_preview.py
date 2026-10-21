"""Convert a gated research rank into diagnostic target weights only."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import pandas as pd

from ..settings import DATA_ROOT, ROOT


def _json(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def run(rank_run: Path, action_gate: Path, *, top: int = 20,
        output: Path | None = None) -> tuple[Path, dict]:
    rank = _json(Path(rank_run).resolve())
    gate = _json(Path(action_gate).resolve())
    if rank.get("status") != "completed_stockdb_factor_rank_diagnostic" or rank.get("execution_admitted") is not False:
        raise ValueError("Completed unadmitted diagnostic rank required")
    if gate.get("status") != "completed_stockdb_action_gate_diagnostic" or gate.get("execution_admitted") is not False:
        raise ValueError("Completed unadmitted action gate required")
    if gate.get("ranked_action_count") != 0:
        raise ValueError("Research target preview requires an empty ranked action intersection")
    ranking_path = Path(rank.get("ranking_path") or rank.get("result", {}).get("ranking_path", ""))
    ranking = pd.read_csv(ranking_path, dtype={"symbol": str}).sort_values("rank")
    if len(ranking) < top:
        raise ValueError(f"Diagnostic rank has only {len(ranking)} rows; need {top}")
    selected = ranking.head(top).copy()
    selected["target_weight"] = 1.0 / top
    selected["target_weight"] = selected["target_weight"].round(10)
    selected = selected[["rank", "symbol", "score", "target_weight"]]
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_research_target_previews" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_research_target_previews" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    target_path = artifacts / "research_target_weights.csv"
    selected.to_csv(target_path, index=False)
    result = {
        "status": "completed_stockdb_research_target_preview",
        "rank_run": str(Path(rank_run).resolve()),
        "action_gate": str(Path(action_gate).resolve()),
        "date": rank.get("date"),
        "target_count": int(len(selected)),
        "weight_sum": float(selected.target_weight.sum()),
        "target_path": str(target_path),
        "research_target_generated": True,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "ths_payload_generated": False,
        "execution_admitted": False,
        "artifacts": str(artifacts),
        "limitations": [
            "Weights are diagnostic equal weights; no prices, quantities, cash or T+1 sellability were applied.",
            "This is not a Portfolio/Ledger target and cannot be sent to a broker or Tonghuashun.",
            "PIT listing, company-action settlement, account observation and quote freshness remain outside this preview.",
        ],
    }
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# StockDB research target-weight preview", "",
        f"Date: `{result['date']}`; targets: `{len(selected)}`; weight sum: `{selected.target_weight.sum():.6f}`.",
        f"Artifact: `{target_path}`.",
        "Research-only equal weights; no quantities, orders, Ledger or THS payload.",
    ]) + "\n", encoding="utf-8")
    return out / "result.json", result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rank-run", required=True, type=Path)
    parser.add_argument("--action-gate", required=True, type=Path)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = run(args.rank_run, args.action_gate, top=args.top, output=args.out)
    print(json.dumps({"run": str(path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
