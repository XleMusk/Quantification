"""Build the fixed 190-factor feature panel from a pinned StockDB provider.

This module is deliberately a feature-only boundary.  It adapts the binary
StockDB/Qlib provider to the small feed interface used by
``factor_candidate_replay.build_monthly_features`` and writes a raw factor
panel.  It does not rank stocks, fit a model, create target holdings, call the
Ledger, or prepare a Tonghuashun order package.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import uuid

import pandas as pd

from ..settings import DATA_ROOT, ROOT
from ..symbols import normalize_symbol
from .factor_candidate_replay import build_monthly_features


EXPECTED_FACTOR_COUNT = 190
EXPECTED_LIBRARY_COUNTS = {"Alpha158": 67, "Alpha360": 56, "Templates": 67}
PROVIDER_FIELDS = ["open", "high", "low", "close", "volume", "vwap", "factor"]


class StockDBQlibFeed:
    """Minimal read-only feed consumed by the existing feature builder.

    ``build_monthly_features`` only needs ``folder``, ``calendar`` and
    ``symbols``.  ``folder`` intentionally points to the pinned StockDB
    snapshot because ``native_binding`` checks that the provider was derived
    from the same price view; it never points at an execution or account feed.
    """

    def __init__(self, provider_view: Path, provider_result: dict):
        self.provider_view = provider_view.resolve()
        self.provider_result = provider_result
        self.provider_folder = Path(provider_result["provider_folder"]).resolve()
        self.folder = Path(provider_result["price_view"]).resolve()
        self.calendar = self._read_calendar()
        self.symbols = self._read_symbols()

    def _read_calendar(self) -> list[str]:
        path = self.provider_folder / "calendars" / "day.txt"
        if not path.is_file():
            raise ValueError("StockDB Qlib provider calendar is missing")
        calendar = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not calendar or calendar != sorted(set(calendar)):
            raise ValueError("StockDB Qlib provider calendar must be nonempty and ordered")
        try:
            parsed = pd.to_datetime(calendar, format="%Y-%m-%d", errors="raise")
        except Exception as error:
            raise ValueError("StockDB Qlib provider calendar has invalid dates") from error
        declared = [int(day.strftime("%Y%m%d")) for day in parsed]
        if declared != self.provider_result.get("calendar"):
            raise ValueError("StockDB Qlib provider calendar differs from its receipt")
        if len(calendar) != int(self.provider_result.get("dates", -1)):
            raise ValueError("StockDB Qlib provider date count differs from its receipt")
        return calendar

    def _read_symbols(self) -> list[str]:
        path = self.provider_folder / "instruments" / "all.txt"
        if not path.is_file():
            raise ValueError("StockDB Qlib provider instrument file is missing")
        rows = [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows or any(len(row) != 3 for row in rows):
            raise ValueError("StockDB Qlib provider instrument rows must have three fields")
        if any(row[1:] != [self.calendar[0], self.calendar[-1]] for row in rows):
            raise ValueError("StockDB Qlib provider instrument date range changed")
        symbols = [normalize_symbol(row[0]).vt_symbol for row in rows]
        if symbols != sorted(set(symbols)):
            raise ValueError("StockDB Qlib provider instruments must be sorted and unique")
        if len(symbols) != int(self.provider_result.get("symbols", -1)):
            raise ValueError("StockDB Qlib provider symbol count differs from its receipt")
        return symbols


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def load_stockdb_feed(provider_view: Path) -> StockDBQlibFeed:
    """Validate and adapt one completed StockDB provider receipt."""

    provider_view = Path(provider_view).resolve()
    result = _read_json(provider_view / "result.json")
    if (result.get("status") != "research_provider_completed"
            or result.get("execution_admitted") is not False
            or result.get("model_trained") is not False):
        raise ValueError("Completed untrained, unadmitted StockDB provider required")
    if result.get("fields") != PROVIDER_FIELDS:
        raise ValueError("StockDB provider must expose the shared seven-field schema")
    provider_folder = Path(result.get("provider_folder", "")).resolve()
    try:
        provider_folder.relative_to(provider_view)
    except ValueError as error:
        raise ValueError("StockDB provider folder must remain inside provider view") from error
    price_view = Path(result.get("price_view", "")).resolve()
    if not price_view.is_dir():
        raise ValueError("StockDB provider price view is missing")
    return StockDBQlibFeed(provider_view, result)


def validate_native_view(native_view: Path, provider_view: Path, feed: StockDBQlibFeed) -> dict:
    """Validate native Alpha158 receipt identity before feature construction."""

    native_view = Path(native_view).resolve()
    result = _read_json(native_view / "result.json")
    if (result.get("status") != "full_native_features_completed"
            or result.get("execution_admitted") is not False
            or result.get("strategy_validated") is not False
            or result.get("model_trained") is not False
            or result.get("factor_selection_performed") is not False):
        raise ValueError("Completed untrained, unadmitted native Alpha158 features required")
    if Path(result.get("provider_view", "")).resolve() != Path(provider_view).resolve():
        raise ValueError("Native Alpha158 features were built from a different provider view")
    if result.get("symbols") != len(feed.symbols) or result.get("rows") != len(feed.symbols) * len(feed.calendar):
        raise ValueError("Native Alpha158 feature grid does not match the provider grid")
    partitions = result.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise ValueError("Native Alpha158 partition receipt is missing")
    symbols = []
    for part in partitions:
        if not isinstance(part, dict) or not isinstance(part.get("symbols"), list):
            raise ValueError("Native Alpha158 partition identity is malformed")
        partition_path = (native_view / str(part.get("file", ""))).resolve()
        try:
            partition_path.relative_to(native_view)
        except ValueError as error:
            raise ValueError("Native Alpha158 partition escapes its view") from error
        if not partition_path.is_file():
            raise ValueError("Native Alpha158 partition file is missing")
        if int(part.get("rows", -1)) != len(feed.calendar) * len(part["symbols"]):
            raise ValueError("Native Alpha158 partition row count differs from the provider grid")
        symbols.extend(part["symbols"])
    if symbols != feed.symbols or len(symbols) != len(set(symbols)):
        raise ValueError("Native Alpha158 partitions do not cover provider symbols exactly once")
    return result


def resolve_candidates(candidate_file: Path) -> pd.DataFrame:
    """Load and freeze the selected 190-factor candidate membership."""

    path = Path(candidate_file)
    if path.is_dir():
        path = path / "candidate_factors.csv"
    if not path.is_file():
        candidate = ROOT / "outputs" / "factor_candidate_packs" / str(candidate_file) / "candidate_factors.csv"
        path = candidate if candidate.is_file() else path
    candidates = pd.read_csv(path)
    required = {"factor", "library", "original_name", "family", "formula", "train_direction", "candidate_decision"}
    if not required <= set(candidates.columns):
        raise ValueError(f"Candidate pack is missing columns: {sorted(required - set(candidates.columns))}")
    if len(candidates) != EXPECTED_FACTOR_COUNT or candidates.factor.duplicated().any():
        raise ValueError("The fixed candidate pack must contain 190 distinct factors")
    counts = candidates.groupby("library", sort=False).size().to_dict()
    if counts != EXPECTED_LIBRARY_COUNTS:
        raise ValueError(f"Unexpected candidate library counts: {counts}")
    if (candidates.factor != candidates.library + "/" + candidates.original_name).any():
        raise ValueError("Candidate factor identities must be library/original_name")
    if not candidates.candidate_decision.eq("selected_cluster_representative").all():
        raise ValueError("Candidate pack contains factors outside the frozen selected membership")
    if not candidates.train_direction.isin(["high", "low"]).all():
        raise ValueError("Candidate pack contains an invalid training direction")
    if candidates.original_name.isna().any() or candidates.formula.isna().any():
        raise ValueError("Candidate factor definitions must include names and formulas")
    return candidates


def _requested_dates(feed: StockDBQlibFeed, dates=None) -> list[pd.Timestamp]:
    calendar = pd.DatetimeIndex(pd.to_datetime(feed.calendar))
    requested = [calendar[-1]] if dates is None else list(pd.to_datetime(list(dates)))
    if not requested or len(set(requested)) != len(requested) or requested != sorted(requested):
        raise ValueError("Feature dates must be nonempty, distinct and ordered")
    if any(day not in calendar for day in requested):
        raise ValueError("Feature date is outside the pinned provider calendar")
    return requested


def build_current_features(provider_view: Path, native_view: Path, candidate_file: Path,
                           *, dates=None, output: Path | None = None) -> tuple[Path, dict]:
    """Build and save raw 190-factor features for selected provider dates."""

    provider_view = Path(provider_view).resolve()
    native_view = Path(native_view).resolve()
    feed = load_stockdb_feed(provider_view)
    native_result = validate_native_view(native_view, provider_view, feed)
    candidates = resolve_candidates(candidate_file)
    requested = _requested_dates(feed, dates)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_factor_features" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_factor_features" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    record = {
        "id": identifier,
        "status": "building_stockdb_factor_features",
        "provider_view": str(provider_view),
        "native_view": str(native_view),
        "candidate_file": str(Path(candidate_file).resolve()),
        "candidate_count": len(candidates),
        "dates": [day.strftime("%Y-%m-%d") for day in requested],
        "artifacts": str(artifacts),
        "execution_admitted": False,
        "strategy_validated": False,
        "model_trained": False,
        "ranking_generated": False,
        "paper_orders_generated": False,
        "target_holdings_generated": False,
        "result": None,
    }
    (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        source = {"bulk_inputs": {"native_view": str(native_view)}}
        features, _ = build_monthly_features(feed, source, candidates, requested)
        expected_index = pd.MultiIndex.from_product([requested, feed.symbols], names=["date", "symbol"])
        if not features.index.equals(expected_index) or features.columns.tolist() != candidates.factor.tolist():
            raise ValueError("StockDB 190-factor feature grid or factor identity changed")
        if features.index.has_duplicates or features.columns.duplicated().any():
            raise ValueError("StockDB 190-factor features must be distinct")
        feature_path = artifacts / "current_features.parquet"
        features.to_parquet(feature_path)
        result = {
            "status": "completed_stockdb_factor_features",
            "feature_path": str(feature_path),
            "rows": len(features),
            "symbols": len(feed.symbols),
            "dates": len(requested),
            "factors": len(features.columns),
            "finite_cells": int(features.notna().sum().sum()),
            "provider_cutoff": feed.calendar[-1],
            "native_status": native_result["status"],
        }
        record.update(status=result["status"], result=result)
        (out / "REPORT.md").write_text(
            "\n".join([
                "# StockDB 190-factor feature panel",
                "",
                "Research-only raw features; no ranking, target holdings, paper orders or Tonghuashun package.",
                f"- Provider cutoff: `{feed.calendar[-1]}`; requested dates: `{record['dates']}`",
                f"- Grid: `{result['rows']}` rows / `{result['symbols']}` symbols / `{result['factors']}` factors",
                f"- Feature artifact: `{feature_path}`",
                "- Execution admitted: `false`; model trained: `false`.",
            ]) + "\n", encoding="utf-8")
    except Exception as error:
        record.update(status="failed_stockdb_factor_features",
                      error=f"{type(error).__name__}: {error}")
        raise
    finally:
        (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out / "run.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-view", required=True, type=Path)
    parser.add_argument("--native-view", required=True, type=Path)
    parser.add_argument("--candidate-file", required=True, type=Path)
    parser.add_argument("--cutoff", help="One pinned session; defaults to provider's latest session")
    parser.add_argument("--dates", help="Comma-separated pinned sessions; mutually exclusive with --cutoff")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.cutoff and args.dates:
        parser.error("--cutoff and --dates are mutually exclusive")
    dates = [args.cutoff] if args.cutoff else ([item.strip() for item in args.dates.split(",") if item.strip()]
                                                if args.dates else None)
    path, result = build_current_features(args.provider_view, args.native_view, args.candidate_file,
                                          dates=dates, output=args.out)
    print(json.dumps({"run": str(path), **result}, ensure_ascii=False))
    return 0 if result["status"] == "completed_stockdb_factor_features" else 1


if __name__ == "__main__":
    raise SystemExit(main())
