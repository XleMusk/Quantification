from hashlib import sha256
import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trading.research.bulk_adjusted_prices import build
from trading.research.tests.test_adjusted_prices import raw_frame, factor_frame, write_views


def views(tmp_path, factors=None, complete=True, raw=None):
    raw = raw_frame() if raw is None else raw
    raw_view, factor_view = write_views(tmp_path, raw=raw, factors=factors, factor_cutoff=raw.date.max())
    with pq.ParquetWriter(raw_view / "raw_daily.parquet", pa.Table.from_pandas(raw, preserve_index=False).schema) as writer:
        for _, day in raw.groupby("date"):
            writer.write_table(pa.Table.from_pandas(day, preserve_index=False))
    calendar = raw_view / "input_calendar.csv"
    dates = sorted(raw.date.unique())
    pd.DataFrame({"date": dates}).to_csv(calendar, index=False)
    plan = {"requested_start": dates[0], "requested_end": dates[-1],
            "calendar_sha256": sha256(calendar.read_bytes()).hexdigest()}
    (raw_view / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    path = raw_view / "result.json"
    result = json.loads(path.read_text())
    result.update(status="source_cache_complete" if complete else "partial_backfill", expected_days=len(dates),
                  acquired_days=len(dates) if complete else len(dates) - 1, missing_days=[] if complete else [dates[0]],
                  empty_days=[], raw_daily_sha256=sha256((raw_view / "raw_daily.parquet").read_bytes()).hexdigest(),
                  plan_sha256=sha256((raw_view / "plan.json").read_bytes()).hexdigest())
    path.write_text(json.dumps(result), encoding="utf-8")
    return raw_view, factor_view


def derived_views(tmp_path, monkeypatch, conflict=False):
    from trading.research import price_boundaries
    from trading.research.tests.test_price_boundaries import alias_day, rename
    raw = pd.concat([alias_day(), alias_day(price_boundaries.EFFECTIVE)], ignore_index=True)
    raw.loc[2, ["open", "high", "low", "close", "preclose", "volume", "amount"]] = ""
    raw.at[2, "tradestatus"] = "0"
    final = alias_day("2026-09-30").iloc[1:]
    raw = pd.concat([raw, final], ignore_index=True)
    if conflict:
        raw.at[1, "amount"] = "1"
    factors = pd.DataFrame({"code": [price_boundaries.OLD] + [price_boundaries.NEW] * 3,
                           "dividOperateDate": ["2010-08-27", "2010-08-27", price_boundaries.EFFECTIVE, "2026-06-16"],
                           "foreAdjustFactor": ["1", "0.986265", "0.986265", "1"],
                           "backAdjustFactor": ["1", "", "1", "1"], "adjustFactor": ["1", "", "1", "1"],
                           "origin": ["provider", "derived_code_change_scale", "provider", "provider"],
                           "source_code": [price_boundaries.OLD] * 2 + [price_boundaries.NEW] * 2,
                           "source_factor_date": ["2010-08-27", "2010-08-27", price_boundaries.EFFECTIVE, "2026-06-16"]})
    raw_view, factor_view = views(tmp_path, factors=factors, raw=raw)
    disclosure = factor_view / "rename_disclosure.pdf"
    disclosure.write_bytes(b"test-only reviewed disclosure fixture")
    digest = sha256(disclosure.read_bytes()).hexdigest()
    monkeypatch.setattr(price_boundaries, "RENAME_SHA256", digest)
    mapping = rename()
    mapping["disclosure_sha256"] = digest
    impl = factor_view / "implementation.py"
    impl.write_bytes(b"test-only derivation receipt")
    plan = {"requested_end": "2026-09-30", "raw_view": str(raw_view), "rename": mapping,
            "source_hashes": {"raw": sha256((raw_view / "raw_daily.parquet").read_bytes()).hexdigest()}}
    (factor_view / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    result_path = factor_view / "result.json"
    result = json.loads(result_path.read_text())
    result.update(status="derived_research_levels", rename=mapping, original_provider_rows=3,
                  derived_initial_level_count=0, derived_initial_price_rows=0, derived_code_change_level_count=1,
                  execution_admitted=False, independent_prices_verified=False, cash_share_actions_verified=False,
                  implementation_sha256=sha256(impl.read_bytes()).hexdigest(),
                  plan_sha256=sha256((factor_view / "plan.json").read_bytes()).hexdigest())
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return raw_view, factor_view


def test_every_source_date_row_code_and_unadjusted_activity_are_preserved(tmp_path):
    inputs = views(tmp_path)
    folder, result = build(*inputs, tmp_path / "output")
    assert result["status"] == "research_reconstruction_completed"
    assert (result["rows"], result["symbols"], result["dates"]) == (2, 1, 2)
    converted = pd.read_parquet(folder / "adjusted_daily.parquet")
    assert converted.open.tolist() == ["11.6816483000", "11.7500000000"]
    assert converted.adjustflag.eq("2").all()
    assert converted[["date", "code", "volume", "amount"]].equals(raw_frame()[["date", "code", "volume", "amount"]])
    assert not result["execution_admitted"] and not result["cash_share_actions_verified"]


def test_partial_raw_calendar_is_not_silently_redefined_as_complete(tmp_path):
    inputs = views(tmp_path, complete=False)
    folder, result = build(*inputs, tmp_path / "output")
    assert result["status"] == "failed_reconstruction" and "Complete nonempty" in result["error"]
    assert not (folder / "adjusted_daily.parquet").exists()


def test_missing_initial_levels_fail_before_any_price_panel(tmp_path):
    inputs = views(tmp_path, factors=factor_frame().iloc[1:])
    folder, result = build(*inputs, tmp_path / "output")
    assert result["status"] == "failed_reconstruction" and "initial/terminal" in result["error"]
    gap = json.loads((folder / "baseline_coverage/result.json").read_text())
    assert gap["missing_start_level_codes"] == ["sh.600000"]
    assert not (folder / "adjusted_daily.parquet").exists()


def test_project_command_dispatch_does_not_launch_any_other_flow(monkeypatch):
    import run_committee
    from trading.research import bulk_adjusted_prices
    calls = []
    monkeypatch.setattr(bulk_adjusted_prices, "main", lambda args: calls.append(args) or 0)
    assert run_committee.main(["bulk-adjusted-prices", "--raw-view", "local"]) == 0
    assert calls == [["--raw-view", "local"]]


def test_derived_levels_require_explicit_opt_in(tmp_path, monkeypatch):
    inputs = derived_views(tmp_path, monkeypatch)
    folder, result = build(*inputs, tmp_path / "output")
    assert result["status"] == "failed_reconstruction" and "explicit opt-in" in result["error"]
    assert not (folder / "adjusted_daily.parquet").exists()


def test_derived_bulk_consumer_keeps_all_rows_and_uses_single_reanchored_identity(tmp_path, monkeypatch):
    inputs = derived_views(tmp_path, monkeypatch)
    folder, result = build(*inputs, tmp_path / "output", allow_derived=True)
    assert result["status"] == "research_reconstruction_completed", result.get("error")
    assert (result["rows"], result["symbols"], result["dates"]) == (5, 2, 3)
    assert (result["canonical_rows"], result["canonical_symbols"], result["retained_alias_shadow_rows"]) == (3, 1, 2)
    kept = pd.read_parquet(folder / "adjusted_daily.parquet")
    canonical = pd.read_parquet(folder / "canonical_daily.parquet")
    assert kept.iloc[0].close == "15.4700" and canonical.iloc[0].close == "15.2575195500"
    assert canonical.tradable_code.tolist() == ["sz.300114", "sz.302132", "sz.302132"]
    assert canonical.factor_origin.tolist() == ["derived_code_change_scale", "provider", "provider"]
    assert kept.iloc[2].close == "" and not result["execution_admitted"]


def test_alias_conflict_preserves_failure_instead_of_publishing_cut_scope(tmp_path, monkeypatch):
    inputs = derived_views(tmp_path, monkeypatch, conflict=True)
    folder, result = build(*inputs, tmp_path / "output", allow_derived=True)
    assert result["status"] == "failed_reconstruction" and "Conflicting pre-rename" in result["error"]
    assert not (folder / "adjusted_daily.parquet").exists()
    assert not (folder / "canonical_daily.parquet").exists()
