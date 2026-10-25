"""Build a dated, raw-bar eligibility view for StockDB research candidates.

This view only uses facts observable in the pinned daily bar snapshot: a bar was
present on the date, OHLCV was positive, the raw ``is_st`` flag was false, and
the raw traded amount met a fixed research liquidity floor.  It does not infer
publication-time PIT membership, corporate actions, or broker eligibility.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .shortline_panel_replay import load_raw_bars


DEFAULT_MIN_AMOUNT_CNY = 10_000_000.0
REQUIRED_SCORE_FLAGS = (
    "execution_admitted", "strategy_validated", "ranking_generated",
    "target_holdings_generated", "paper_orders_generated",
)


def _read_json(path: Path) -> dict:
    path = Path(path).resolve()
    if path.is_dir():
        path = path / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _resolve_score_run(value: Path) -> tuple[Path, dict, Path]:
    path = Path(value).resolve()
    if path.is_dir():
        path = path / "run.json"
    record = _read_json(path)
    if record.get("status") != "completed_stockdb_factor_monthly_ridge_diagnostic":
        raise ValueError("Completed monthly Ridge diagnostic required")
    unsafe = [flag for flag in REQUIRED_SCORE_FLAGS if record.get(flag) is not False]
    if unsafe:
        raise ValueError(f"Ridge diagnostic has non-research flags: {unsafe}")
    score_path = Path((record.get("result") or {}).get("score_path", "")).resolve()
    if not score_path.is_file():
        raise ValueError(f"Ridge score artifact missing: {score_path}")
    return path, record, score_path


def load_score_rows(ridge_run: Path) -> pd.DataFrame:
    """Load every finite score row without ranking or future-label access."""

    _, _, score_path = _resolve_score_run(ridge_run)
    scores = pd.read_parquet(score_path)
    if scores.index.names != ["date", "symbol"] or scores.index.has_duplicates:
        raise ValueError("Ridge scores need a unique date/symbol index")
    if scores.columns.tolist() != ["score"]:
        raise ValueError("Ridge score artifact must contain exactly one score column")
    rows = scores.reset_index()
    rows["date"] = pd.to_datetime(rows["date"], errors="raise").dt.strftime("%Y-%m-%d")
    rows["symbol"] = rows["symbol"].astype(str).str.upper()
    rows["score"] = pd.to_numeric(rows["score"], errors="coerce")
    rows = rows.loc[rows["symbol"].str.endswith((".SSE", ".SZSE")) & rows["score"].notna()].copy()
    if rows.empty:
        raise ValueError("No finite StockDB Ridge scores")
    return rows[["date", "symbol", "score"]].sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)


def apply_dated_eligibility(score_rows: pd.DataFrame, bars: pd.DataFrame,
                            *, min_amount_cny: float = DEFAULT_MIN_AMOUNT_CNY) -> pd.DataFrame:
    """Join dated raw-bar facts and return one auditable eligibility row per score."""

    if min_amount_cny < 0:
        raise ValueError("min_amount_cny must be non-negative")
    required = {"date", "symbol", "score"}
    if not required <= set(score_rows.columns):
        raise ValueError("score_rows missing date/symbol/score")
    if bars.duplicated(["date", "symbol"]).any():
        raise ValueError("Raw bars contain duplicate date/symbol rows")
    cols = ["date", "symbol", "open", "close", "volume", "amount", "is_st"]
    missing = sorted(set(cols) - set(bars.columns))
    if missing:
        raise ValueError(f"Raw bars missing eligibility fields: {missing}")
    raw = bars[cols].copy()
    raw["date"] = pd.to_datetime(raw["date"], errors="raise").dt.strftime("%Y-%m-%d")
    raw["symbol"] = raw["symbol"].astype(str).str.upper()
    for col in ["open", "close", "volume", "amount"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw["observed_bar"] = True
    raw["tradable_bar"] = raw[["open", "close", "volume"]].gt(0).all(axis=1)
    raw["not_st"] = ~raw["is_st"].map(lambda value: str(value).strip().lower() in {"1", "true", "yes"})
    raw["liquid"] = raw["amount"].ge(float(min_amount_cny))
    joined = score_rows[["date", "symbol", "score"]].merge(
        raw[["date", "symbol", "observed_bar", "tradable_bar", "not_st", "liquid"]],
        on=["date", "symbol"], how="left", validate="one_to_one",
    )
    for col in ["observed_bar", "tradable_bar", "not_st", "liquid"]:
        joined[col] = joined[col].astype("boolean").fillna(False).astype(bool)
    joined["listed_on_date"] = joined["observed_bar"]
    joined["eligible_for_research"] = joined[["observed_bar", "listed_on_date", "tradable_bar", "not_st", "liquid"]].all(axis=1)
    joined["min_amount_cny"] = float(min_amount_cny)
    return joined


def load_eligibility_view(path: Path) -> pd.DataFrame:
    """Validate and load a completed view for a downstream research replay."""

    path = Path(path).resolve()
    record = _read_json(path)
    if record.get("status") != "completed_stockdb_dated_eligibility_view":
        raise ValueError("Completed StockDB dated eligibility view required")
    if record.get("execution_admitted") is not False or record.get("pit_verified") is not False:
        raise ValueError("Eligibility view must remain research-only and PIT-unverified")
    artifact = Path((record.get("result") or {}).get("eligibility_path", "")).resolve()
    if not artifact.is_file():
        raise ValueError(f"Eligibility artifact missing: {artifact}")
    rows = pd.read_parquet(artifact)
    required = {"date", "symbol", "score", "eligible_for_research"}
    if not required <= set(rows.columns) or rows.duplicated(["date", "symbol"]).any():
        raise ValueError("Eligibility artifact has invalid identity or fields")
    rows["date"] = pd.to_datetime(rows["date"], errors="raise").dt.strftime("%Y-%m-%d")
    rows["symbol"] = rows["symbol"].astype(str).str.upper()
    rows["eligible_for_research"] = rows["eligible_for_research"].astype(bool)
    return rows


def build_view(ridge_run: Path, snapshot: Path, *, min_amount_cny: float = DEFAULT_MIN_AMOUNT_CNY,
               output: Path | None = None) -> tuple[Path, dict]:
    score_run, score_record, _ = _resolve_score_run(ridge_run)
    score_rows = load_score_rows(ridge_run)
    bars = load_raw_bars(Path(snapshot), set(score_rows["symbol"]),
                         start=str(score_rows["date"].min()), end=str(score_rows["date"].max()))
    bars = bars.loc[bars["date"].isin(set(score_rows["date"]))].copy()
    view = apply_dated_eligibility(score_rows, bars, min_amount_cny=min_amount_cny)
    identifier = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y%m%d_%H%M%S")
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_eligibility_views" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_eligibility_views" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    eligibility_path = artifacts / "dated_eligibility.parquet"
    view.to_parquet(eligibility_path, index=False)
    result = {
        "status": "completed_stockdb_dated_eligibility_view",
        "eligibility_path": str(eligibility_path),
        "rows": int(len(view)),
        "dates": int(view["date"].nunique()),
        "symbols": int(view["symbol"].nunique()),
        "eligible_rows": int(view["eligible_for_research"].sum()),
        "observed_rows": int(view["observed_bar"].sum()),
        "not_st_rows": int(view["not_st"].sum()),
        "liquid_rows": int(view["liquid"].sum()),
        "min_amount_cny": float(min_amount_cny),
    }
    record = {
        "schema": "a_share_stockdb_dated_eligibility_view_v1",
        "status": result["status"],
        "ridge_run": str(score_run),
        "score_source": str((score_record.get("result") or {}).get("score_path")),
        "raw_snapshot": str(Path(snapshot).resolve()),
        "result": result,
        "eligibility_view_generated": True,
        "eligibility_applied": False,
        "pit_verified": False,
        "company_actions_applied": False,
        "execution_admitted": False,
        "strategy_validated": False,
        "limitations": [
            "Eligibility is based on dated raw-bar observation, raw is_st and a fixed amount floor.",
            "This view does not establish PIT publication timing, revision safety, corporate actions or broker eligibility.",
            "The view is a research input and does not create ranking, target, paper or broker orders.",
        ],
    }
    (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# StockDB dated eligibility view", "",
        f"Rows: `{result['rows']}`; eligible: `{result['eligible_rows']}`; dates: `{result['dates']}`; symbols: `{result['symbols']}`.",
        f"Eligibility floor: amount >= `{min_amount_cny:,.0f}` CNY; PIT verified: `false`.", "",
        *["- " + item for item in record["limitations"]], "",
    ]) + "\n", encoding="utf-8")
    receipt = {
        "schema": "stockdb_dated_eligibility_view_receipt_v1",
        "status": "COMPLETED_DATED_ELIGIBILITY_RESEARCH_ONLY",
        "run": str(out / "run.json"),
        "eligibility_path": str(eligibility_path),
        "metrics": result,
        "eligibility_applied": False,
        "pit_verified": False,
        "company_actions_applied": False,
        "execution_admitted": False,
    }
    receipt_path = ROOT / "outputs" / "pipeline_receipts" / "20261024_stockdb_dated_eligibility_view_receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out / "run.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ridge-run", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--min-amount-cny", type=float, default=DEFAULT_MIN_AMOUNT_CNY)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, record = build_view(args.ridge_run, args.snapshot, min_amount_cny=args.min_amount_cny, output=args.out)
    print(json.dumps({"run": str(path), **record}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
