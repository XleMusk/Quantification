from contextlib import contextmanager
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import factor_baselines as source


def coverage(tmp_path):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"cutoff": "2026-09-30", "raw_codes": 3,
                               "outcomes": [{"code": code} for code in
                                            ("sh.600000", "sh.600001", "sz.000001")]}), encoding="utf-8")
    return path


def payload(code):
    return pd.DataFrame([[code, "2000-01-01", "0.9", "1", "1"],
                         [code, "2026-01-01", "1", "1.1", "1.1"]], columns=source.ADJUSTMENT_FIELDS)


def setup_source(monkeypatch, calls, fail_code=None):
    @contextmanager
    def session():
        def query(code, start_date, end_date):
            calls.append((code, start_date, end_date))
            if code == fail_code:
                raise TimeoutError("actual transport unavailable in fixture")
            return SimpleNamespace(error_code="0", error_msg="success", code=code)
        yield SimpleNamespace(query_adjust_factor=query)
    monkeypatch.setattr(source, "provider_frame", lambda result:
                        payload(result.code) if result.code != "sh.600001" else payload(result.code).iloc[:0])
    monkeypatch.setattr(source.time, "sleep", lambda duration: None)
    return session


def test_resumes_every_pinned_code_and_retains_empty_success_not_one(tmp_path, monkeypatch):
    path, calls = coverage(tmp_path), []
    session = setup_source(monkeypatch, calls)
    folder, first, result = source.backfill(path, {"sha256": "reviewed"}, tmp_path / "bulk", 1, session)
    assert result["acquired_codes"] == 1 and result["missing_query_codes"] == ["sh.600001", "sz.000001"]
    before = (first / "result.json").read_bytes()
    _, second, result = source.backfill(path, {"sha256": "reviewed"}, folder, 2, session)
    assert [call[0] for call in calls] == ["sh.600000", "sh.600001", "sz.000001"]
    assert all(call[1:] == ("1990-01-01", "2026-09-30") for call in calls)
    assert result["status"] == "source_cache_complete" and result["requested_codes"] == 3
    assert result["symbols"] == 2 and result["empty_success_codes"] == ["sh.600001"]
    assert (first / "result.json").read_bytes() == before
    assert not result["adjusted_prices_verified"] and not result["execution_admitted"]
    assert len(pd.read_parquet(second / "adjustment_events.parquet")) == 4


def test_failure_is_retained_and_retry_reuses_successful_codes(tmp_path, monkeypatch):
    path, calls = coverage(tmp_path), []
    session = setup_source(monkeypatch, calls, fail_code="sh.600001")
    folder, first, failed = source.backfill(path, {"sha256": "reviewed"}, tmp_path / "bulk", 3, session)
    assert failed["status"] == "failed_backfill" and failed["acquired_codes"] == 1
    saved = json.loads((first / "receipts.json").read_text())
    assert saved["failures"][0]["query"]["code"] == "sh.600001"
    session = setup_source(monkeypatch, calls)
    _, _, result = source.backfill(path, {"sha256": "reviewed"}, folder, 2, session)
    assert result["status"] == "source_cache_complete" and result["preserved_failure_count"] == 1
    assert [call[0] for call in calls].count("sh.600000") == 1
    assert json.loads((first / "result.json").read_text())["status"] == "failed_backfill"


def test_altered_cache_or_scope_blocks_before_new_queries(tmp_path, monkeypatch):
    path, calls = coverage(tmp_path), []
    session = setup_source(monkeypatch, calls)
    folder, view, _ = source.backfill(path, {"sha256": "reviewed"}, tmp_path / "bulk", 1, session)
    original = path.read_bytes()
    path.write_text(original.decode().replace("sz.000001", "sz.000002"))
    with pytest.raises(ValueError, match="scope changed"):
        source.backfill(path, {"sha256": "reviewed"}, folder, 1, session)
    path.write_bytes(original)
    (view / "sh_600000.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Source hash changed"):
        source.backfill(path, {"sha256": "reviewed"}, folder, 1, session)
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("code", "sz.000002"), ("dividOperateDate", "2026-10-01"),
                                         ("foreAdjustFactor", "0"), ("backAdjustFactor", "NaN")])
def test_invalid_source_fields_cannot_enter_factor_panel(field, value):
    frame = payload("sh.600000")
    frame.at[0, field] = value
    with pytest.raises(ValueError):
        source.validate(frame, "sh.600000", "2026-09-30")


def test_project_command_routes_only_the_requested_backfill(monkeypatch):
    import run_committee
    calls = []
    monkeypatch.setattr(source, "main", lambda args: calls.append(args) or 0)
    assert run_committee.main(["factor-baseline-backfill", "--max-codes", "64"]) == 0
    assert calls == [["--max-codes", "64"]]


def _factor_view(tmp_path, name, frame, start, end):
    view = tmp_path / name
    view.mkdir(parents=True)
    frame.to_parquet(view / "adjustment_events.parquet", index=False)
    plan = {"kind": "adjustment", "requested_start": start, "requested_end": end}
    (view / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    panel_hash = source.sha256((view / "adjustment_events.parquet").read_bytes()).hexdigest()
    plan_hash = source.sha256((view / "plan.json").read_bytes()).hexdigest()
    result = {"kind": "adjustment", "status": "source_cache_complete", "rows": len(frame),
              "symbols": int(frame.code.nunique()), "panel_file": "adjustment_events.parquet",
              "panel_sha256": panel_hash, "plan_sha256": plan_hash}
    (view / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return view


def test_extend_factor_history_appends_later_events_without_relabeling(tmp_path):
    base = _factor_view(tmp_path, "base", payload("sh.600000"), "1990-01-01", "2026-09-30")
    later = payload("sh.600000").iloc[:1].copy()
    later["dividOperateDate"] = "2026-10-08"
    later["foreAdjustFactor"] = "1.000000"
    extension = _factor_view(tmp_path, "extension", later, "2026-10-08", "2026-10-13")
    folder, result = source.extend(base, extension, tmp_path / "extended")
    assert result["status"] == "source_union_built"
    assert result["rows"] == 3 and result["additional_unique_extension_rows"] == 1
    assert result["symbols"] == 1 and not result["execution_admitted"]
    assert json.loads((folder / "plan.json").read_text())["requested_end"] == "2026-10-13"


def test_extend_factor_history_rejects_non_later_events(tmp_path):
    base = _factor_view(tmp_path, "base", payload("sh.600000"), "1990-01-01", "2026-09-30")
    earlier = payload("sh.600000").iloc[:1].copy()
    earlier["dividOperateDate"] = "2026-09-30"
    extension = _factor_view(tmp_path, "extension", earlier, "2026-09-30", "2026-10-13")
    with pytest.raises(ValueError, match="start after the base cutoff"):
        source.extend(base, extension, tmp_path / "rejected")


def test_project_command_routes_factor_extension(monkeypatch):
    import run_committee
    calls = []
    monkeypatch.setattr(source, "extend_main", lambda args: calls.append(args) or 0)
    assert run_committee.main(["factor-baseline-extend", "--base-view", "a"]) == 0
    assert calls == [ ["--base-view", "a"] ]


def test_overlap_merge_keeps_unqueried_daily_codes_and_adds_initial_levels(tmp_path, monkeypatch):
    from trading.research.tests.test_adjusted_prices import write_views
    daily = pd.concat([payload("sh.600000").iloc[1:], payload("sh.600001").iloc[1:]])
    _, daily_view = write_views(tmp_path / "daily", factors=daily, factor_cutoff="2026-09-30")
    path, calls = coverage(tmp_path), []
    session = setup_source(monkeypatch, calls)
    _, history_view, _ = source.backfill(path, {"sha256": "reviewed"}, tmp_path / "bulk", 1, session)
    before = (daily_view / "adjustment_events.parquet").read_bytes()
    folder, result = source.merge(daily_view, history_view, tmp_path / "merged")
    assert result["equal_overlap_rows_verified"] == 1 and result["additional_unique_history_rows"] == 1
    assert result["rows"] == 3 and result["symbols"] == 2
    panel = pd.read_parquet(folder / "adjustment_events.parquet")
    assert panel.loc[panel.code.eq("sh.600001"), "foreAdjustFactor"].tolist() == ["1"]
    assert (daily_view / "adjustment_events.parquet").read_bytes() == before
    assert not result["execution_admitted"] and not result["adjusted_prices_verified"]


def test_overlap_merge_rejects_conflict_instead_of_last_writer_wins(tmp_path, monkeypatch):
    from trading.research.tests.test_adjusted_prices import write_views
    daily = payload("sh.600000").iloc[1:].copy()
    daily["foreAdjustFactor"] = "0.99"
    _, daily_view = write_views(tmp_path / "daily", factors=daily, factor_cutoff="2026-09-30")
    path, calls = coverage(tmp_path), []
    session = setup_source(monkeypatch, calls)
    _, history_view, _ = source.backfill(path, {"sha256": "reviewed"}, tmp_path / "bulk", 1, session)
    with pytest.raises(ValueError, match="Conflicting factor sources"):
        source.merge(daily_view, history_view, tmp_path / "merged")
    assert not (tmp_path / "merged").exists()
