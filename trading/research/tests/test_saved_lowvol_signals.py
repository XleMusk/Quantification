"""Complete small cache schemas, no historical feature calculation or replay."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from trading.research import saved_lowvol_signals as saved
from trading.research.baostock_daily_slice import digest
from trading.research.stockdb_rolling_low_volatility import portfolio


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def cache(tmp_path):
    snapshot, artifacts = tmp_path / "snapshot", tmp_path / "artifacts"
    snapshot.mkdir(); artifacts.mkdir()
    bars = pd.DataFrame([{"code": code, "date": day, "open": 10., "high": 11., "low": 9.,
        "close": 10., "pre_close": 9.9, "volume": 1e6, "amount": 1e8, "pct_chg": 1., "is_st": False}
        for day in [20260128, 20260129, 20260130, 20260202] for code in ["000001", "000002", "000003"]])
    path = snapshot / "bars.parquet"
    bars.to_parquet(path, index=False)
    manifest = {"status": "complete_inventory_only", "chunks_expected": 1, "execution_admitted": False,
        "chunks": [{"parquet_file": path.name, "parquet_sha256": digest(path)}]}
    _write(snapshot / "manifest.json", manifest)
    config = portfolio().to_dict()
    run = {"schema": "stockdb_rolling_low_volatility_ledger_v1", "status": "completed_research_only",
        "research_only": True, "execution_admitted": False, "artifacts": str(artifacts),
        "variants": [saved.VARIANT], "config": config, "replay_start": "2026-01-28",
        "replay_end": "2026-02-02", "symbols": 3, "sessions": 4, "supplemental_raw_view": {"path": None}}
    _write(tmp_path / "run.json", run)
    protocol = {**run, "snapshot": str(snapshot), "snapshot_manifest": manifest,
        "lookback": 252, "skip": 21, "rank_cadence": "monthly_completed_session_end"}
    _write(artifacts / "protocol.json", protocol)
    ranks = pd.DataFrame([
        {"date": "2026-01-30", "variant": saved.VARIANT, "rank": 1, "symbol": "000002.SZSE",
         "volatility": .4, "momentum": .2, "median_amount": 3e7},
        {"date": "2026-01-30", "variant": saved.VARIANT, "rank": 2, "symbol": "000001.SZSE",
         "volatility": .2, "momentum": .3, "median_amount": 4e7}])
    rank_path = artifacts / f"{saved.VARIANT}_ranks.csv"
    ranks.to_csv(rank_path, index=False)
    decisions = pd.DataFrame([{"date": day, "variant": saved.VARIANT,
        "decision": "monthly_ranking" if day == "2026-01-30" else "not_month_end",
        "eligible_count": 2 if day == "2026-01-30" else None,
        "candidate_count": 2 if day == "2026-01-30" else None}
        for day in ["2026-01-28", "2026-01-29", "2026-01-30", "2026-02-02"]])
    decision_path = artifacts / f"{saved.VARIANT}_decisions.csv"
    decisions.to_csv(decision_path, index=False)
    return {"run": tmp_path / "run.json", "ranks": rank_path, "decisions": decision_path,
        "fixed": ["2026-01-28", "2026-01-29", "2026-01-30", "2026-02-02", "2026-02-03"]}


def test_original_rank_order_full_cohort_and_no_implicit_nonmonthly_target(cache):
    result = saved.load_saved_lowvol_signals(cache["run"], "2026-01-29", "2026-01-30", cache["fixed"])
    assert result.symbols == ["000001.SZSE", "000002.SZSE", "000003.SZSE"]
    assert result.preceding_session == "2026-01-28"
    assert [row["symbol"] for row in result.signals["2026-01-30"]] == ["000002.SZSE", "000001.SZSE"]
    assert [row["score"] for row in result.signals["2026-01-30"]] == [-.4, -.2]
    assert "2026-01-29" not in result.signals
    assert result.decisions["2026-01-29"] == "not_month_end"
    assert result.provenance["initial_prior_year_signal_consumed"] is False
    assert result.provenance["ranks_recomputed"] is False


def test_missing_monthly_rank_or_duplicate_month_end_decision_rejected(cache):
    original = pd.read_csv(cache["ranks"])
    original.iloc[:1].to_csv(cache["ranks"], index=False)
    with pytest.raises(ValueError, match="rank30"):
        saved.load_saved_lowvol_signals(cache["run"], "2026-01-29", "2026-01-30", cache["fixed"])
    original.to_csv(cache["ranks"], index=False)
    decisions = pd.read_csv(cache["decisions"])
    pd.concat([decisions, decisions.loc[decisions.date.eq("2026-01-30")]]).to_csv(cache["decisions"], index=False)
    with pytest.raises(ValueError, match="uniqueness"):
        saved.load_saved_lowvol_signals(cache["run"], "2026-01-29", "2026-01-30", cache["fixed"])


def test_unknown_decision_and_missing_exact_session_cannot_become_liquidation(cache):
    original = pd.read_csv(cache["decisions"])
    changed = original.copy()
    changed.loc[changed.date.eq("2026-01-29"), "decision"] = "unknown_source_failure"
    changed.to_csv(cache["decisions"], index=False)
    with pytest.raises(ValueError, match="Unknown saved decision"):
        saved.load_saved_lowvol_signals(cache["run"], "2026-01-29", "2026-01-30", cache["fixed"])
    original.loc[~original.date.eq("2026-01-29")].to_csv(cache["decisions"], index=False)
    with pytest.raises(ValueError, match="decision coverage"):
        saved.load_saved_lowvol_signals(cache["run"], "2026-01-29", "2026-01-30", cache["fixed"])
