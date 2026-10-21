from contextlib import contextmanager
from hashlib import sha256
import json
import socket
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.market_dataset import collect, load_receipt, task_complete, write_frame, write_json
from trading.research.action_scope import pin, prepare
from trading.research.daily_source_probe import WHEEL_SHA256
from trading.research.tests.test_adjusted_prices import raw_frame
from trading.research.tests.test_bulk_adjusted_prices import views


def source_views(tmp_path):
    seed = raw_frame().iloc[:1]
    raw = pd.concat([seed.assign(code="sz.300114", date="2023-01-03"),
                     seed.assign(code="sz.302132", date="2026-09-30"),
                     seed.assign(date="2023-01-03"), seed.assign(date="2026-09-30")], ignore_index=True)
    return views(tmp_path, raw=raw)[0]


def action(code="sz.300114", year=2023, cash="0.03"):
    return pd.DataFrame({"code": [code], "dividOperateDate": [f"{year}-06-06"], "dividCashPsBeforeTax": [cash]})


def legacy_cache(root):
    symbol = "300114.SZSE"
    entries = {}
    for year in (2023, 2026):
        relative = f"actions/{symbol}/{year}.csv"
        write_frame(root / relative, action(year=year).assign(symbol=symbol))
        entries[f"actions_{year}"] = {"path": relative, "retrieved_at": "2026-09-26T10:00:00+08:00", "quality": {"rows": 1}}
    write_json(root / "receipts" / (symbol + ".json"), {"symbol": symbol, "tasks": entries})


def probe_cache(root, frame):
    root.mkdir(parents=True)
    (root / "implementation.py").write_bytes(b"test-only probe implementation")
    write_frame(root / "source.csv", frame)
    write_json(root / "receipt.json", {"status": "completed_probe", "method": "query_dividend_data",
        "year_type": "operate", "sdk": {"sha256": WHEEL_SHA256}, "finished_at": "2026-10-01T10:00:00+08:00",
        "implementation_sha256": sha256((root / "implementation.py").read_bytes()).hexdigest(),
        "entries": [{"code": frame.code.iloc[0], "year": int(frame.dividOperateDate.iloc[0][:4]),
                     "file": "source.csv", "sha256": sha256((root / "source.csv").read_bytes()).hexdigest(),
                     "rows": len(frame), "error_code": "0"}]})


def test_scope_keeps_all_raw_codes_and_only_reuses_closed_year_legacy_records(tmp_path):
    raw = source_views(tmp_path / "raw")
    legacy_cache(tmp_path / "old")
    folder, result = prepare(raw, tmp_path / "scope", reuse_root=tmp_path / "old")
    assert (result["expected_symbols"], result["action_queries"], result["action_rows"]) == (3, 1, 1)
    assert not result["raw_symbols"] and not result["adjusted_symbols"] and not result["execution_admitted"]
    universe = pd.read_csv(folder / "universe.csv")
    assert set(universe.code) == {"sh.600000", "sz.300114", "sz.302132"}
    receipt = load_receipt(folder, "300114.SZSE")
    assert set(receipt["tasks"]) == {"actions_2023"}
    assert not (folder / "actions/300114.SZSE/2026.csv").exists()
    assert (tmp_path / "old/actions/300114.SZSE/2026.csv").exists()
    plan = json.loads((folder / "collection_plan.json").read_text())
    assert plan["expected_queries"] == 12 and plan["summary_stage"] == "actions"


def test_fresh_probe_seeds_current_year_and_preserves_post_cutoff_reported_dates(tmp_path):
    raw = source_views(tmp_path / "raw")
    frame = action("sz.302132", 2026)
    frame["dividOperateDate"] = "2026-10-03"
    probe_cache(tmp_path / "probe", frame)
    folder, result = prepare(raw, tmp_path / "scope", probe=tmp_path / "probe")
    assert result["action_queries"] == 1
    saved = pd.read_csv(folder / "actions/302132.SZSE/2026.csv", dtype=str)
    assert saved.dividOperateDate.tolist() == ["2026-10-03"]
    assert not result["execution_admitted"]


def test_conflicting_overlapping_seeds_are_not_silently_overwritten(tmp_path):
    raw = source_views(tmp_path / "raw")
    legacy_cache(tmp_path / "old")
    probe_cache(tmp_path / "probe", action(cash="0.99"))
    with pytest.raises(ValueError, match="Overlapping action seeds conflict"):
        prepare(raw, tmp_path / "scope", reuse_root=tmp_path / "old", probe=tmp_path / "probe")
    saved = pd.read_csv(tmp_path / "scope/actions/300114.SZSE/2023.csv", dtype=str)
    assert saved.dividCashPsBeforeTax.tolist() == ["0.03"]


def test_actions_only_root_cannot_launch_bar_or_all_stage_queries(tmp_path):
    folder, _ = prepare(source_views(tmp_path / "raw"), tmp_path / "scope")
    for stage in ("bars", "all"):
        with pytest.raises(ValueError, match="actions-only"):
            collect(folder, 1, stage)


def test_changed_cached_hash_fails_without_requery_or_overwrite(tmp_path):
    raw = source_views(tmp_path / "raw")
    legacy_cache(tmp_path / "old")
    folder, _ = prepare(raw, tmp_path / "scope", reuse_root=tmp_path / "old")
    path = folder / "actions/300114.SZSE/2023.csv"
    path.write_bytes(b"user changed source")
    with pytest.raises(ValueError, match="Cached task hash changed"):
        task_complete(folder, load_receipt(folder, "300114.SZSE"), ("actions_2023", "actions/300114.SZSE/2023.csv", 2023))
    assert path.read_bytes() == b"user changed source"


def test_partial_failure_keeps_success_and_immutable_attempt_then_resume_skips_it(tmp_path, monkeypatch):
    from trading import market_dataset
    folder, _ = prepare(source_views(tmp_path / "raw"), tmp_path / "scope")
    calls, fail = [], [True]
    def query(code, year, year_type):
        calls.append((code, year, year_type))
        if year == "2024" and fail[0]:
            raise OSError("test source timeout")
        frame = action(code, int(year))
        return SimpleNamespace(error_code="0", error_msg="success", fields=frame.columns.tolist(), get_data=lambda: frame)
    @contextmanager
    def session():
        yield SimpleNamespace(query_dividend_data=query)
    monkeypatch.setattr(market_dataset, "baostock_session", session)
    monkeypatch.setattr(market_dataset.time, "sleep", lambda _: None)
    before_timeout = socket.getdefaulttimeout()
    with pytest.raises(OSError):
        collect(folder, 1, "actions")
    assert socket.getdefaulttimeout() == before_timeout
    attempts = [json.loads(path.read_text()) for path in (folder / "collection_runs").glob("*.json")]
    assert len(attempts) == 1 and attempts[0]["status"] == "failed" and attempts[0]["completed_queries"] == 1
    assert attempts[0]["failed_query"]["year"] == 2024
    fail[0] = False
    result = collect(folder, 1, "actions")
    assert result["complete_symbols"] == 1 and result["action_queries"] == 4
    assert calls.count(("sh.600000", "2023", "operate")) == 1
    assert len(list((folder / "collection_runs").glob("*.json"))) == 2


def test_project_route_dispatches_only_action_scope(monkeypatch):
    import run_committee
    from trading.research import action_scope
    calls = []
    monkeypatch.setattr(action_scope, "main", lambda args: calls.append(args) or 0)
    assert run_committee.main(["stock-action-scope", "--raw-view", "local"]) == 0
    assert calls == [["--raw-view", "local"]]


def test_source_view_keeps_full_query_matrix_and_distinguishes_unqueried_from_empty(tmp_path):
    raw = source_views(tmp_path / "raw")
    legacy_cache(tmp_path / "old")
    root, _ = prepare(raw, tmp_path / "scope", reuse_root=tmp_path / "old")
    symbol, relative = "302132.SZSE", "actions/302132.SZSE/2026.csv"
    write_frame(root / relative, action().iloc[:0].assign(symbol=symbol))
    write_json(root / "receipts/302132.SZSE.json", {"symbol": symbol, "tasks": {"actions_2026": {
        "path": relative, "quality": {"rows": 0}, "sha256": sha256((root / relative).read_bytes()).hexdigest()}}})
    folder, result = pin(root, tmp_path / "view")
    assert result["status"] == "source_actions_partial"
    assert (result["expected_codes"], result["expected_queries"], result["queried"], result["unqueried"]) == (3, 12, 2, 10)
    assert result["rows"] == result["empty_responses"] == 1
    coverage = pd.read_csv(folder / "coverage.csv")
    assert coverage.loc[~coverage.queried, "rows"].isna().all()
    assert coverage.loc[coverage.empty_response.eq(True), "rows"].eq(0).all()
    assert not result["cash_share_actions_verified"] and not result["execution_admitted"]


def test_source_view_and_collector_reject_reduced_code_scope_before_use(tmp_path):
    raw = source_views(tmp_path / "raw")
    root, _ = prepare(raw, tmp_path / "scope")
    universe = pd.read_csv(root / "universe.csv")
    write_frame(root / "universe.csv", universe.iloc[:1])
    with pytest.raises(ValueError, match="full action-query"):
        collect(root, 1, "actions")
    with pytest.raises(ValueError, match="reduced or relabelled"):
        pin(root, tmp_path / "view")
    assert not (tmp_path / "view").exists()
