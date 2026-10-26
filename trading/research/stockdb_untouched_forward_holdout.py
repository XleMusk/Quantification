"""Evaluate a frozen replay portfolio on an untouched later StockDB segment.

The input account is the terminal state of a completed historical replay.  No
new score, ranking, target or order is read or generated; the shared Engine /
Ledger only marks the frozen holdings over later raw sessions.  This is a
forward diagnostic, not a strategy-adoption or live-execution gate.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

import pandas as pd

from ..contracts import RunContext, TradingMode
from ..engine import advance
from ..report import metrics
from ..settings import ROOT
from ..symbols import normalize_symbol
from .shortline_panel_replay import _code_symbol
from .stockdb_alpha158_lgbm_panel_replay import _config
from .stockdb_factor_monthly_ridge_replay import MatureFactorFeed


REQUIRED_REPLAY_FLAGS = ("execution_admitted", "strategy_validated", "ledger_mutated")


def _read_json(path: Path) -> dict:
    path = Path(path).resolve()
    if path.is_dir():
        path = path / "result.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _sha256(path: Path) -> str:
    """Return a content hash for a small snapshot identity file."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _snapshot_identity(raw_view: Path, snapshot: dict) -> dict:
    """Bind a native snapshot receipt to the exact files consumed by holdout.

    A forward result without a snapshot identity cannot be reproduced after a
    later capture replaces the same directory.  Keep this identity local and
    research-only; it does not infer PIT or source quality from the hash.
    """

    raw_view = Path(raw_view).resolve()
    snapshot_path = raw_view / "snapshot.json"
    files = {"snapshot": {"path": str(snapshot_path), "sha256": _sha256(snapshot_path)}}
    for name in ("daily.csv", "raw_daily.parquet", "availability.csv"):
        path = raw_view / name
        if path.is_file():
            files[name] = {"path": str(path), "sha256": _sha256(path)}
    rows = snapshot.get("rows")
    return {
        "schema": snapshot.get("schema"),
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": files["snapshot"]["sha256"],
        "captured_at": snapshot.get("captured_at"),
        "effective_session_dates": list(snapshot.get("effective_session_dates") or []),
        "rows": rows,
        "files": files,
    }


def _resolve_replay(value: Path) -> tuple[Path, dict, Path]:
    path = Path(value).resolve()
    if path.is_dir():
        path = path / "result.json"
    result = _read_json(path)
    status = result.get("status")
    if status in {"completed_research_only_mature_factor_replay",
                  "completed_research_only_candidate_panel_replay"}:
        unsafe = [flag for flag in REQUIRED_REPLAY_FLAGS if result.get(flag) is not False]
        if unsafe:
            raise ValueError(f"Replay has non-research flags: {unsafe}")
    elif status == "completed":
        # The fixed StockDB replay predates the result.json contract and keeps
        # its research-only flags in the run manifest one directory above the
        # variant.  Require that manifest explicitly; missing flags are never
        # treated as permission to continue.
        run_path = path.parent.parent / "run.json"
        if not run_path.is_file():
            raise ValueError("Legacy replay requires a completed research-only run manifest")
        run = _read_json(run_path)
        if run.get("status") != "completed_research_only":
            raise ValueError("Legacy replay run manifest is not research-only")
        unsafe = [flag for flag in ("execution_admitted", "strategy_validated", "fresh_holdout_claimed")
                  if run.get(flag) is not False]
        if unsafe:
            raise ValueError(f"Legacy replay run has non-research flags: {unsafe}")
        result = {**result, "status": "completed_research_only_candidate_panel_replay",
                  "end_date": result.get("last_completed_session") or result.get("attempted_session"),
                  "legacy_run_manifest": str(run_path)}
    else:
        raise ValueError("Completed research-only replay result required")
    account = path.parent / "account.json"
    if not account.is_file():
        raise ValueError(f"Replay account state missing: {account}")
    return path, result, account


def _receipt_for_raw(raw_view: Path) -> dict:
    raw_view = Path(raw_view).resolve()
    receipt_path = raw_view / "receipt.json"
    if receipt_path.is_file():
        receipt = _read_json(receipt_path)
        if receipt.get("status") != "COMPLETE_LOCAL_STOCKDB_EXTENDED_VIEW" or receipt.get("execution_admitted") is not False:
            raise ValueError("A completed, unadmitted local StockDB extended view is required")
        return receipt
    # A native StockDB freeze is a valid raw capture for a bounded holdout.
    # Normalize only the receipt boundary; it remains research-only and does
    # not get promoted to the extended-view schema.
    snapshot_path = raw_view / "snapshot.json"
    if not snapshot_path.is_file():
        raise ValueError(f"Extended StockDB receipt or native snapshot missing: {receipt_path}")
    snapshot = _read_json(snapshot_path)
    if (snapshot.get("schema") != "stockdb_research_snapshot_v1"
            or snapshot.get("capture_complete") is not True
            or snapshot.get("execution_admitted") is not False):
        raise ValueError("A completed, unadmitted native StockDB snapshot is required")
    identity = _snapshot_identity(raw_view, snapshot)
    return {
        "status": "COMPLETED_NATIVE_STOCKDB_SNAPSHOT",
        "execution_admitted": False,
        "snapshot_schema": snapshot.get("schema"),
        "rows": snapshot.get("rows"),
        "snapshot_identity": identity,
    }


def _load_forward_bars(raw_view: Path, symbols: set[str], *, start: str, end: str) -> pd.DataFrame:
    raw_view = Path(raw_view).resolve()
    path = raw_view / "raw_daily.parquet"
    if path.is_file():
        columns = ["date", "code", "open", "high", "low", "close", "preclose", "volume",
                   "amount", "tradestatus", "pctChg", "isST"]
        raw = pd.read_parquet(path, columns=columns, filters=[("date", ">=", str(start)), ("date", "<=", str(end))])
    else:
        path = raw_view / "daily.csv"
        if not path.is_file():
            raise ValueError(f"Extended StockDB raw_daily missing: {raw_view}; native daily.csv missing")
        raw = pd.read_csv(path, encoding="utf-8-sig")
        if "preclose" not in raw.columns and "pre_close" in raw.columns:
            raw = raw.rename(columns={"pre_close": "preclose"})
        raw["date"] = pd.to_datetime(raw["date"], errors="raise").dt.strftime("%Y-%m-%d")
        raw = raw.loc[(raw["date"] >= str(start)) & (raw["date"] <= str(end))].copy()
        for col, default in (("tradestatus", 1), ("pctChg", 0.0), ("isST", False)):
            if col not in raw.columns:
                raw[col] = default
        required = ["date", "code", "symbol", "open", "high", "low", "close", "preclose", "volume", "amount",
                    "tradestatus", "pctChg", "isST"]
        missing = [col for col in required if col not in raw.columns]
        if missing:
            raise ValueError(f"Native StockDB daily.csv missing columns: {missing}")
    if raw.empty:
        raise ValueError("No forward raw rows in requested date range")
    def symbol_of(value: object) -> str | None:
        text = str(value).strip().upper()
        if "." in text:
            market, code = text.split(".", 1)
            market = "SH" if market in {"SH", "SSE"} else "SZ" if market in {"SZ", "SZSE"} else ""
            if market:
                try:
                    return normalize_symbol(f"{market}.{code.zfill(6)}").vt_symbol
                except ValueError:
                    return None
        return _code_symbol(text)
    raw["symbol"] = raw["code"].map(symbol_of)
    raw["date"] = pd.to_datetime(raw["date"], errors="raise").dt.strftime("%Y-%m-%d")
    raw = raw.loc[raw["symbol"].isin(symbols)].copy()
    if raw.empty:
        raise ValueError("No held symbols in forward raw view")
    if raw.duplicated(["date", "symbol"]).any():
        raise ValueError("Forward raw view contains duplicate date/symbol rows")
    for col in ["open", "high", "low", "close", "preclose", "volume", "amount", "pctChg"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw = raw.loc[raw[["open", "high", "low", "close", "preclose"]].gt(0).all(axis=1)].copy()
    raw["preclose_for_limits"] = raw["preclose"]
    raw["is_st"] = raw["isST"].fillna(False)
    raw["tradestatus"] = raw["tradestatus"].fillna((raw["volume"] > 0) & (raw["open"] > 0)).astype(int)
    raw = raw.sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
    if raw.empty:
        raise ValueError("Forward raw view has no positive OHLC rows")
    return raw


def _load_forward_dates(raw_view: Path, *, start: str, end: str) -> list[str]:
    """Read the full captured market calendar before filtering to holdings."""

    path = Path(raw_view).resolve() / "raw_daily.parquet"
    if path.is_file():
        dates = pd.read_parquet(path, columns=["date"],
                                filters=[("date", ">=", str(start)), ("date", "<=", str(end))])["date"]
    else:
        csv_path = Path(raw_view).resolve() / "daily.csv"
        if not csv_path.is_file():
            raise ValueError(f"No raw calendar file in {raw_view}")
        dates = pd.read_csv(csv_path, usecols=["date"], encoding="utf-8-sig")["date"]
        dates = pd.to_datetime(dates, errors="raise")
        dates = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    result = sorted(pd.to_datetime(dates, errors="raise").dt.strftime("%Y-%m-%d").unique().tolist())
    if not result:
        raise ValueError("No captured market sessions in requested forward range")
    return result


def _held_symbols(state: dict) -> set[str]:
    held = {symbol for symbol, lots in state.get("positions", {}).items()
            if sum(int(lot.get("qty", 0)) for lot in lots) > 0}
    if not held:
        raise ValueError("Replay terminal state has no holdings for a holdout")
    return held


def evaluate(replay: Path, raw_view: Path, *, start: str, end: str,
             output: Path | None = None) -> tuple[Path, dict]:
    replay_path, result, account_path = _resolve_replay(replay)
    raw_receipt = _receipt_for_raw(raw_view)
    state = json.loads(account_path.read_text(encoding="utf-8"))
    cutoff = str(result.get("end_date"))
    if str(start) <= cutoff:
        raise ValueError(f"Untouched forward start must be after replay end {cutoff}")
    held = _held_symbols(state)
    dates = _load_forward_dates(raw_view, start=start, end=end)
    bars = _load_forward_bars(raw_view, held, start=start, end=end)
    coverage = bars.groupby("symbol")["date"].nunique()
    missing = sorted(symbol for symbol in held if int(coverage.get(symbol, 0)) != len(dates))
    if missing:
        raise ValueError(f"Forward segment is incomplete for held symbols: {missing[:10]}")
    candidates = pd.DataFrame({"date": ["1900-01-01"] * len(held),
                               "symbol": sorted(held), "score": [0.0] * len(held)})
    feed = MatureFactorFeed(bars, candidates)
    config = _config(100000, max(1, min(5, len(held))))
    context = RunContext("stockdb-untouched-forward-holdout", TradingMode.REPLAY)
    initial_state = copy.deepcopy(state)
    initial_cash = float(initial_state["cash"])
    for symbol, lots in initial_state["positions"].items():
        price = int(initial_state["prices"].get(symbol, 0))
        initial_cash += sum(int(lot.get("qty", 0)) for lot in lots) * price
    daily = [advance(state, feed, day, config, index, context=context)
             for index, day in enumerate(feed.calendar)]
    if len(state["orders"]) != len(initial_state["orders"]) or len(state["fills"]) != len(initial_state["fills"]):
        raise ValueError("Untouched holdout generated orders or fills")
    metric = metrics(daily, initial_cash / 100.0)
    identifier = f"{pd.Timestamp.now(tz='Asia/Shanghai').strftime('%Y%m%d_%H%M%S')}_untouched"
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_untouched_forward_holdouts" / identifier
    out.mkdir(parents=True, exist_ok=False)
    holdout = {
        "schema": "a_share_stockdb_untouched_forward_holdout_v1",
        "status": "completed_research_only_untouched_forward_holdout",
        "replay": str(replay_path), "raw_view": str(Path(raw_view).resolve()),
        "raw_receipt_status": raw_receipt["status"],
        "raw_snapshot_identity": raw_receipt.get("snapshot_identity"),
        "historical_cutoff": cutoff,
        "forward_start": dates[0], "forward_end": dates[-1],
        "forward_sessions": len(dates), "held_symbols": sorted(held),
        "daily": daily, "metrics": metric,
        "future_scores_used": False, "model_refit": False, "ranking_generated": False,
        "target_holdings_generated": False, "orders_generated": False, "fills_generated": False,
        "holdout_untouched": True, "execution_admitted": False, "strategy_validated": False,
        "pit_verified": False, "company_actions_applied": False,
        "limitations": [
            "This segment only marks the terminal historical holdings; it does not test a refreshed signal.",
            "The extended raw view is a local StockDB capture and does not establish PIT publication or corporate actions.",
            "No account, broker or Tonghuashun state was read or changed.",
        ],
    }
    (out / "result.json").write_text(json.dumps(holdout, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    pd.DataFrame(daily).to_csv(out / "daily.csv", index=False)
    (out / "account.json").write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    audit = {
        "schema": "a_share_stockdb_untouched_forward_holdout_audit_v1",
        "status": "passed_research_only_untouched_holdout_gates",
        "result": str(out / "result.json"), "holdout_untouched": True,
        "raw_snapshot_identity": raw_receipt.get("snapshot_identity"),
        "future_scores_used": False, "orders_generated": False, "fills_generated": False,
        "execution_admitted": False, "strategy_validated": False,
        "pit_verified": False, "company_actions_applied": False,
    }
    (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = {
        "schema": "stockdb_untouched_forward_holdout_receipt_v1",
        "status": "COMPLETED_UNTOUCHED_FORWARD_RESEARCH_ONLY",
        "run": str(out / "result.json"),
        "audit": str(out / "audit.json"),
        "replay": str(replay_path),
        "raw_view": str(Path(raw_view).resolve()),
        "raw_receipt_status": raw_receipt["status"],
        "raw_snapshot_identity": raw_receipt.get("snapshot_identity"),
        "historical_cutoff": cutoff,
        "forward_start": dates[0], "forward_end": dates[-1],
        "forward_sessions": len(dates), "metrics": metric,
        "holdout_untouched": True, "future_scores_used": False,
        "orders_generated": False, "fills_generated": False,
        "execution_admitted": False, "strategy_validated": False,
        "pit_verified": False, "company_actions_applied": False,
    }
    receipt_path = ROOT / "outputs" / "pipeline_receipts" / f"20261024_untouched_forward_holdout_{out.name}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out / "result.json", holdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", required=True, type=Path)
    parser.add_argument("--raw-view", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, result = evaluate(args.replay, args.raw_view, start=args.start, end=args.end, output=args.out)
    print(json.dumps({"result": str(path), **{k: result[k] for k in ("status", "forward_sessions", "metrics")}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
