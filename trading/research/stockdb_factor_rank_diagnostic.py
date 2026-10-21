"""Build a research-only current rank after explicit StockDB gates."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
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


def run(score_run: Path, eligibility_receipt: Path, tradability_receipt: Path,
        *, top: int = 30, output: Path | None = None) -> tuple[Path, dict]:
    score_run = Path(score_run).resolve()
    score = _json(score_run)
    if score.get("status") != "completed_stockdb_factor_score_diagnostic":
        raise ValueError("Completed StockDB score diagnostic required")
    if score.get("execution_admitted") is not False or score.get("cross_lineage_diagnostic") is not True:
        raise ValueError("Score diagnostic must remain cross-lineage and unadmitted")
    elig_path = Path(eligibility_receipt).resolve()
    elig = _json(elig_path)
    if elig.get("status") != "completed_research_only_eligibility_compatibility" or elig.get("execution_admitted") is not False:
        raise ValueError("Completed research-only eligibility receipt required")
    trad_path = Path(tradability_receipt).resolve()
    trad = _json(trad_path)
    if (trad.get("status") != "completed_research_only_tradability_compare"
            or trad.get("execution_admitted") is not False
            or trad.get("tradestatus_disagreement_rows") != 0
            or trad.get("is_st_disagreement_rows") != 0):
        raise ValueError("Current independent tradability comparison did not pass")
    score_frame = pd.read_parquet(score["result"]["score_path"])
    if score_frame.index.names != ["date", "symbol"] or score_frame.columns.tolist() != ["score"]:
        raise ValueError("Diagnostic score identity changed")
    date = score_frame.index.get_level_values("date").max().strftime("%Y-%m-%d")
    if date != elig.get("latest_session") or date != trad.get("date"):
        raise ValueError("Score, eligibility and tradability dates do not match")
    observation_path = Path(elig["artifacts"]) / "latest_eligibility_observation.csv"
    observations = pd.read_csv(observation_path, dtype={"symbol": str})
    compatibility_field = "observed_compatibility_known" if "observed_compatibility_known" in observations.columns else "compatibility_known"
    eligibility_field = "observed_eligible_proxy" if "observed_eligible_proxy" in observations.columns else "eligible_proxy"
    eligible = observations.loc[
        observations[compatibility_field].astype(bool) & observations[eligibility_field].astype(bool),
        ["symbol", "median_amount_20", "liquidity_ok", "is_st"],
    ].drop_duplicates("symbol")
    eligible["symbol"] = eligible["symbol"].map(lambda value: normalize_symbol(value).vt_symbol)
    score_day = score_frame.xs(pd.Timestamp(date), level="date").join(eligible.set_index("symbol"), how="inner")
    score_day = score_day.dropna(subset=["score"]).sort_values(["score", "symbol"], ascending=[False, True], kind="stable")
    if len(score_day) < top:
        raise ValueError(f"Only {len(score_day)} eligible diagnostic scores; need {top}")
    ranking = score_day.head(top).reset_index()
    ranking.insert(0, "rank", range(1, len(ranking) + 1))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_factor_rank_diagnostics" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_factor_rank_diagnostics" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    ranking_path = artifacts / "diagnostic_ranking.csv"
    ranking.to_csv(ranking_path, index=False)
    record = {
        "status": "completed_stockdb_factor_rank_diagnostic",
        "score_run": str(score_run),
        "eligibility_receipt": str(elig_path),
        "tradability_receipt": str(trad_path),
        "date": date,
        "eligibility_proxy_count": int(len(eligible)),
        "compatibility_field": compatibility_field,
        "eligibility_field": eligibility_field,
        "ranked_count": int(len(ranking)),
        "ranking_path": str(ranking_path),
        "top_symbols": ranking.symbol.tolist(),
        "execution_admitted": False,
        "diagnostic_ranking_generated": True,
        "ranking_generated": False,
        "target_holdings_generated": False,
        "paper_orders_generated": False,
        "ths_payload_generated": False,
        "artifacts": str(artifacts),
        "limitations": [
            "Ranking is cross-lineage research output, not a current account signal.",
            "Eligibility uses a conservative observed-history listing-age proxy and positive-activity proxy; PIT membership and company actions remain unverified.",
            "No target quantities, Portfolio/Ledger state, paper orders, broker quotes or THS payload were generated.",
        ],
    }
    (out / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# StockDB fixed-factor current rank diagnostic", "",
        "Research-only top-rank output; no target holdings or orders.",
        f"- Date: `{date}`; eligibility proxy pool: `{len(eligible)}`; ranked: `{len(ranking)}`.",
        f"- Ranking artifact: `{ranking_path}`.",
        "- Execution admitted: `false`; THS payload generated: `false`.",
        "", "PIT listing, company actions and account constraints remain outside this diagnostic.",
    ]) + "\n", encoding="utf-8")
    return out / "result.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score-run", required=True, type=Path)
    parser.add_argument("--eligibility-receipt", required=True, type=Path)
    parser.add_argument("--tradability-receipt", required=True, type=Path)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = run(args.score_run, args.eligibility_receipt, args.tradability_receipt,
                       top=args.top, output=args.out)
    print(json.dumps({"run": str(path), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
