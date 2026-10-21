"""New-view source precedence and exact-date lineage; no provider or replay."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import leader_environment_run, leader_recent_source_preview as recent
from trading.research.baostock_daily_slice import digest
from trading.research.tests.test_leader_history_repair_preview import close
from trading.research.tests.test_leader_listing_features import (
    CALENDAR, NEW, metadata, new_run_frame, proof as listing_proof,
)


DAYS = ["2026-10-12", "2026-10-13", "2026-10-14"]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}), encoding="utf-8")
    history = tmp_path / "history"
    history.mkdir()
    (history / "manifest.json").write_text("{}", encoding="utf-8")
    base = pd.concat([
        recent.canonical(close("2026-10-09", [
            ("sz.000001", "10", "10", "0", "1", "10000000", "100000000"),
            ("sz.000002", "10", "10", "0", "1", "10000000", "100000000")])),
        # A deliberately conflicting original 13th must never enter this view.
        recent.canonical(close("2026-10-13", [
            ("sz.000001", "999", "999", "0", "1", "10000000", "9990000000")])),
    ], ignore_index=True)
    source_rows = [
        [("sz.000001", "11", "10", "10", "1", "10000000", "110000000"),
         ("sz.000002", "10", "10", "", "0", "", ""),
         ("sh.600099", "10", "10", "0", "1", "10000000", "100000000")],
        [("sz.000001", "12.1", "11", "10", "1", "10000000", "121000000"),
         ("sz.000002", "11", "10", "10", "1", "10000000", "110000000")],
        [("sz.000001", "13.31", "12.1", "10", "1", "10000000", "133100000"),
         ("sz.000002", "12.1", "11", "10", "1", "10000000", "121000000")],
    ]
    packages = {}
    for day, rows in zip(DAYS, source_rows):
        folder = tmp_path / day.replace("-", "")
        folder.mkdir()
        raw = close(day, rows)
        raw.to_csv(folder / "raw_daily.csv", index=False)
        source = {"source_folder": str(folder.resolve()), "read_only_verified": True,
            "plan": {"day": day, "calendar_sha256": digest(calendar), "roster_raw_sha256": "fixture_roster"},
            "receipt": {"day": day, "status": "complete_full_dated_close_inventory_not_admitted",
                "full_dated_roster_inventory_complete": True, "raw_daily_sha256": digest(folder / "raw_daily.csv")},
            "ordinary_roster": [{"code": code, "symbol": code[3:] + (".SSE" if code.startswith("sh.") else ".SZSE"),
                                  "code_name": "fixture"} for code in raw.code]}
        (folder / "receipt.json").write_text(json.dumps(source["receipt"]), encoding="utf-8")
        packages[str(folder.resolve())] = (raw, source)
    calls = []
    def load_base(path, *, end):
        calls.append(end)
        return base.copy(deep=True), {"snapshot": str(history), "manifest_end": "2026-10-13",
            "prefixes": ["0", "3", "6"], "partitions": []}
    monkeypatch.setattr(recent, "load_complete", lambda path: deepcopy(packages[str(path)]))
    monkeypatch.setattr(recent, "read_physical_snapshot", load_base)
    return tmp_path, calendar, history, base, packages, calls


def run(setup):
    root, calendar, history, _, packages, _ = setup
    return recent.derive_previews(list(reversed(packages)), history, calendar, root / "out")


def test_single_default_producer_pass_keeps_day_rosters_raw_and_exact_prefix_lineage(setup, monkeypatch):
    root, calendar, _, base, packages, base_calls = setup
    untouched = base.copy(deep=True)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    build = recent.build_leader_daily_batches
    build_calls = []
    def once(*args, **kwargs):
        build_calls.append(kwargs.copy())
        return build(*args, **kwargs)
    monkeypatch.setattr(recent, "build_leader_daily_batches", once)
    result = run(setup)
    assert result["status"] == "completed_recent_source_feature_view"
    assert base_calls == ["2026-10-09"]
    assert len(build_calls) == 1 and build_calls[0]["start"] == DAYS[0] and build_calls[0]["end"] == DAYS[-1]
    assert build_calls[0]["retain_window_features"] is True and "ipo_qualification" not in build_calls[0]
    assert result["days"] == DAYS and result["base_history_end"] == "2026-10-09"
    assert result["recent_source_lineage"]["original_reader_rows_after_cutoff_ignored"] == 1
    monkeypatch.setattr(leader_environment_run, "load_complete", lambda path: deepcopy(packages[str(path)]))
    for index, day in enumerate(DAYS):
        folder = root / "out" / day.replace("-", "")
        envelope, features, proof = leader_environment_run.read_preview(folder)
        lineage = envelope["recent_source_lineage"]
        assert lineage["day"] == lineage["latest_source_day_used"] == day
        assert lineage["source_days_used"] == DAYS[:index + 1]
        assert [item["day"] for item in lineage["source_proofs"]] == DAYS[:index + 1]
        assert all(item["calendar_sha256"] == digest(calendar) and item["source_inventory_complete"] is True
                   for item in lineage["source_proofs"])
        assert lineage["derived_at"] == envelope["created_at"] == result["created_at"]
        assert lineage["shared_view_id"] == result["shared_view_id"]
        assert lineage["base_history_max_observed_day"] == "2026-10-09"
        assert lineage["original_rows_after_cutoff_loaded_into_view"] == 0
        assert lineage["future_bars_used"] is lineage["actual_signal_publication_verified"] is False
        assert proof["features_match_current_active_raw"] is True
        assert envelope["source"]["plan"]["day"] == day
        assert features.set_index("symbol").loc["000001.SZSE", "stage"] == index + 1
        if index:
            # Exact 12th source halt cannot become a synthetic active bar.
            assert pd.isna(features.set_index("symbol").loc["000002.SZSE", "stage"])
        else:
            assert "600099.SSE" in set(features.symbol)
            assert "000002.SZSE" not in set(features.symbol)
        assert envelope["source_inventory_complete"] is True
        assert all(envelope[key] is value for key, value in recent.BOUNDARY.items())
    pd.testing.assert_frame_equal(base, untouched)
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize("status,expected", [("running", "waiting_source_incomplete"),
                                               ("failed_stopped_no_retry", "blocked_failed_source")])
def test_partial_or_failed_source_leaves_no_final_directory(setup, monkeypatch, status, expected):
    root, _, _, _, packages, _ = setup
    folder = next(iter(packages))
    (root / "20261012/receipt.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    monkeypatch.setattr(recent, "load_complete", lambda _: pytest.fail("Partial sources cannot reach strict raw loading"))
    result = run(setup)
    assert result["status"] == expected and result["output_directory_created"] is False
    assert not (root / "out").exists()


@pytest.mark.parametrize("change", ["calendar", "nonadjacent", "duplicate_day", "bad_current_field", "missing_base_predecessor"])
def test_identity_calendar_and_cutoff_failures_precede_output_creation(setup, monkeypatch, change):
    root, _, _, base, packages, _ = setup
    if change == "calendar":
        next(iter(packages.values()))[1]["plan"]["calendar_sha256"] = "changed"
    elif change in {"nonadjacent", "duplicate_day"}:
        value = list(packages.values())[1]
        day = "2026-10-15" if change == "nonadjacent" else DAYS[0]
        value[0]["date"] = day
        value[1]["plan"]["day"] = day
        value[1]["receipt"]["day"] = day
    elif change == "bad_current_field":
        original_build = recent.build_leader_daily_batches
        def corrupt(*args, **kwargs):
            bundle = original_build(*args, **kwargs)
            bundle["window_features"].loc[lambda frame: frame.date.eq(DAYS[-1]), "close"] = 999
            return bundle
        monkeypatch.setattr(recent, "build_leader_daily_batches", corrupt)
    else:
        no_ninth = base.loc[base.date.ne("2026-10-09")]
        monkeypatch.setattr(recent, "read_physical_snapshot", lambda *a, **k: (no_ninth, {}))
    with pytest.raises(ValueError):
        run(setup)
    assert not (root / "out").exists()


def test_existing_output_and_non_distinct_or_short_source_list_are_rejected(setup):
    root, calendar, history, _, packages, _ = setup
    paths = list(packages)
    for invalid in (paths[:2], [paths[0]] * 3):
        with pytest.raises(ValueError, match="three distinct"):
            recent.derive_previews(invalid, history, calendar, root / "out")
    (root / "out").mkdir()
    with pytest.raises(FileExistsError):
        run(setup)


def test_future_source_close_is_rejected_before_history_work(setup, monkeypatch):
    root, _, _, _, _, _ = setup
    class BeforeClose(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-10-14T14:59:59+08:00")
    monkeypatch.setattr(recent, "datetime", BeforeClose)
    monkeypatch.setattr(recent, "read_physical_snapshot", lambda *a, **k: pytest.fail("Future source cannot load history"))
    with pytest.raises(ValueError, match="not-yet-completed"):
        run(setup)
    assert not (root / "out").exists()


def test_environment_integration_accepts_exact_lineage_rejects_future_used_and_mixed_views(setup, monkeypatch):
    root, calendar, _, _, packages, _ = setup
    view = run(setup)
    folders = [root / "out" / day.replace("-", "") for day in DAYS]
    monkeypatch.setattr(leader_environment_run, "load_complete", lambda path: deepcopy(packages[str(path)]))
    envelope, _, provenance = leader_environment_run.read_preview(folders[0])
    assert provenance["recent_source_view"]["shared_view_id"] == view["shared_view_id"]
    assert provenance["recent_source_view"]["source_days_used"] == [DAYS[0]]
    assert provenance["recent_source_view"]["historical_publication_verified"] is False
    first_path = folders[0] / "result.json"
    original = first_path.read_bytes()
    envelope["recent_source_lineage"]["source_days_used"].append(DAYS[1])
    first_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError, match="Recent-source view lineage"):
        leader_environment_run.read_preview(folders[0])
    first_path.write_bytes(original)
    last_path = folders[-1] / "result.json"
    changed = json.loads(last_path.read_text(encoding="utf-8"))
    changed["recent_source_lineage"]["shared_view_id"] = "a_different_valid_view"
    last_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="coherent shared feature view"):
        leader_environment_run.analyze(folders, calendar, root / "analysis")
    assert not (root / "analysis").exists()


@pytest.fixture
def qualified_assets(tmp_path, monkeypatch):
    """Use the existing mechanical day6/day7 fixture as complete daily inputs."""
    days = ["2026-10-19", "2026-10-20", "2026-10-21"]
    frame = new_run_frame()
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(CALENDAR), encoding="utf-8")
    snapshot = tmp_path / "history"
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text("{}", encoding="utf-8")
    packages = {}
    for day in days:
        folder = tmp_path / day.replace("-", "")
        folder.mkdir()
        raw = frame.loc[frame.date.eq(day)].copy().rename(columns={"pct_chg": "pctChg", "pre_close": "preclose", "is_st": "isST"})
        raw["preclose"] = raw.close / (1 + raw.pctChg / 100)
        raw["code"] = raw.code.map(lambda code: ("sh." if code.startswith("6") else "sz.") + code)
        raw["isST"], raw["tradestatus"], raw["adjustflag"] = "0", "1", "3"
        raw = raw.astype(str).reset_index(drop=True)
        raw.to_csv(folder / "raw_daily.csv", index=False)
        source = {"source_folder": str(folder.resolve()), "read_only_verified": True,
            "plan": {"day": day, "calendar_sha256": digest(calendar), "roster_raw_sha256": "synthetic"},
            "receipt": {"day": day, "status": "complete_full_dated_close_inventory_not_admitted",
                "full_dated_roster_inventory_complete": True, "raw_daily_sha256": digest(folder / "raw_daily.csv")},
            "ordinary_roster": [{"code": code, "symbol": code[3:] + (".SSE" if code.startswith("sh.") else ".SZSE"),
                "code_name": "synthetic"} for code in raw.code]}
        (folder / "receipt.json").write_text(json.dumps(source["receipt"]), encoding="utf-8")
        packages[str(folder.resolve())] = raw, source
    base = frame.loc[frame.date.le("2026-10-16")].copy()
    monkeypatch.setattr(recent, "load_complete", lambda path: deepcopy(packages[str(Path(path).resolve())]))
    monkeypatch.setattr(leader_environment_run, "load_complete", lambda path: deepcopy(packages[str(Path(path).resolve())]))
    monkeypatch.setattr(recent, "read_physical_snapshot", lambda path, *, end: (base.copy(), metadata(base)))
    class AfterFixtureClose(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-10-21T17:00:00+08:00")
    monkeypatch.setattr(recent, "datetime", AfterFixtureClose)
    monkeypatch.setattr(leader_environment_run, "datetime", AfterFixtureClose)
    return tmp_path, calendar, snapshot, packages, days


def qualified_view(assets, qualification, *, name="qualified"):
    root, calendar, snapshot, packages, days = assets
    result = recent.derive_previews(list(packages), snapshot, calendar, root / name,
                                    ipo_qualification=qualification)
    return result, [root / name / day.replace("-", "") for day in days]


def test_explicit_qualified_preview_uses_shared_day6_reset_and_retains_original_audit(qualified_assets, monkeypatch):
    from trading.research import ipo_qualification
    from trading.research.leader_listing_features import METHOD_ID
    from trading.research.leader_recent_source_history import read_recent_source_view
    root, calendar, snapshot, packages, days = qualified_assets
    q = listing_proof()
    default = recent.derive_previews(list(packages), snapshot, calendar, root / "default")
    result, folders = qualified_view(qualified_assets, q)
    assert "feature_qualification" not in default["recent_source_lineage"]
    assert result["recent_source_lineage"]["producer_method"] == recent.QUALIFIED_PRODUCER_METHOD
    with pytest.raises(ValueError, match="original-method"):
        read_recent_source_view(root / "qualified" / "recent_source_lineage.json")
    for folder, day in zip(folders, days):
        env, features, provenance = leader_environment_run.read_preview(folder, ipo_qualification=q)
        saved = env["feature_qualification"]
        assert set(saved["decisions"]) == {day}
        assert saved["method_id"] == provenance["feature_qualification"]["method_id"] == METHOD_ID
        assert features.feature_decision_as_of.eq(day + "T15:05:00+08:00").all()
        if day == "2026-10-19":
            assert env["tape"]["limit_up_proxy_count"] == 1
            assert env["tape"]["listing_qualified_limit_up_proxy_count"] == 0
        if day == "2026-10-20":
            row = features.set_index("symbol").loc[NEW]
            assert row.stage == 1 and row.unqualified_stage == 2
    monkeypatch.setattr(ipo_qualification, "load_ipo_qualification", lambda *a, **k: q)
    first_day = leader_environment_run.analyze(folders[:1], calendar, root / "day5_analysis", ipo_evidence=root / "evidence.json")
    ladder = first_day["analysis"]["market_environment"]["limit_and_ladder"]
    assert ladder["raw_limit_up_proxy_count"] == 1 and ladder["IPO_supported_limit_up_proxy_count"] == 0
    analysis = leader_environment_run.analyze(folders, calendar, root / "analysis", ipo_evidence=root / "evidence.json")
    relay = next(item for item in analysis["analysis"]["archetypes"] if item["id"] == "relay_continuation_leader")
    assert [item["stage"] for item in relay["candidates"] if item["symbol"] == NEW] == [2]
    assert all(item["feature_qualification"]["membership_unknown_is_cash"] is False for item in analysis["input_previews"])
    assert analysis["execution_admitted"] is False


def test_late_metadata_and_future_source_do_not_revise_earlier_child_membership(qualified_assets, monkeypatch):
    from trading.research import ipo_qualification
    root, calendar, _, _, days = qualified_assets
    missing = listing_proof(include_new=False)
    late = listing_proof(new_available="2026-10-20T23:59:59+08:00")
    _, old_folders = qualified_view(qualified_assets, missing, name="missing")
    _, folders = qualified_view(qualified_assets, late, name="late")
    for index in (0, 1):
        old, old_features, _ = leader_environment_run.read_preview(old_folders[index], ipo_qualification=missing)
        env, features, _ = leader_environment_run.read_preview(folders[index], ipo_qualification=late)
        pd.testing.assert_frame_equal(old_features, features)
        assert old["feature_qualification"]["decisions"] == env["feature_qualification"]["decisions"]
        for family in env["families"][:3]:
            assert family["producer_batch_status"] == "incomplete"
            assert family["feature_qualification"]["unknown_symbols"] == [NEW]
            assert family["feature_qualification"]["qualification_complete"] is False
    monkeypatch.setattr(ipo_qualification, "load_ipo_qualification", lambda *a, **k: late)
    analysis = leader_environment_run.analyze(folders, calendar, root / "late_analysis", ipo_evidence=root / "evidence.json")
    relay = next(item for item in analysis["analysis"]["archetypes"] if item["id"] == "relay_continuation_leader")
    assert [item["stage"] for item in relay["candidates"] if item["symbol"] == NEW] == [2]
    early = leader_environment_run.analyze(folders[:2], calendar, root / "early_analysis", ipo_evidence=root / "evidence.json")
    assert early["analysis"]["market_environment"]["limit_and_ladder"]["IPO_unsupported_feature_rows"] >= 1


@pytest.mark.parametrize("change", ["missing_proof", "different_proof", "method", "cutoff", "known_empty"])
def test_qualified_preview_rejects_mixed_proof_method_cutoff_and_unknown_as_cash(qualified_assets, change):
    from trading.research.ipo_qualification import IpoQualification
    _, folders = qualified_view(qualified_assets, listing_proof(include_new=False))
    folder, q = folders[1], listing_proof(include_new=False)
    if change == "missing_proof":
        q = None
    elif change == "different_proof":
        q = IpoQualification(q.records, q.calendar, {"different": True})
    else:
        path = folder / "result.json"
        env = json.loads(path.read_text(encoding="utf-8"))
        if change == "method":
            env["recent_source_lineage"]["producer_method"] = recent.DEFAULT_PRODUCER_METHOD
        elif change == "cutoff":
            features_path = folder / "current_features.csv"
            features = pd.read_csv(features_path)
            features["feature_decision_as_of"] = "2026-10-21T15:05:00+08:00"
            features.to_csv(features_path, index=False)
        else:
            env["families"][0]["producer_batch_status"] = "complete"
        path.write_text(json.dumps(env), encoding="utf-8")
    with pytest.raises(ValueError, match="Qualified"):
        leader_environment_run.read_preview(folder, ipo_qualification=q)


def test_qualified_rebound_D_minus2_first_five_is_excluded_in_environment_too(qualified_assets, monkeypatch):
    from trading.research import ipo_qualification
    root, calendar, _, packages, days = qualified_assets
    # Reuse the shared fixture's original case, changing only the last two
    # dated raw observations before the single synthetic producer pass.
    for folder, (raw, source) in packages.items():
        day = source["plan"]["day"]
        if day not in days[1:]:
            continue
        mask = raw.code.eq("sz.301660")
        values = ({"open": "12.0", "close": "11.4", "high": "12.01", "low": "11.39", "pctChg": "-5.0", "preclose": "12.0"}
            if day == days[1] else {"open": "11.8", "close": "12.54", "high": "12.55", "low": "11.79", "pctChg": "10.0", "preclose": "11.4"})
        for field, value in values.items():
            raw.loc[mask, field] = value
        raw.to_csv(Path(folder) / "raw_daily.csv", index=False)
        source["receipt"]["raw_daily_sha256"] = digest(Path(folder) / "raw_daily.csv")
    q = listing_proof()
    _, folders = qualified_view(qualified_assets, q)
    env, features, _ = leader_environment_run.read_preview(folders[-1], ipo_qualification=q)
    row = features.set_index("symbol").loc[NEW]
    assert not row.daily_rebound and row.unqualified_daily_rebound
    monkeypatch.setattr(ipo_qualification, "load_ipo_qualification", lambda *a, **k: q)
    analysis = leader_environment_run.analyze(folders, calendar, root / "rebound_analysis", ipo_evidence=root / "evidence.json")
    rebound = next(item for item in analysis["analysis"]["archetypes"] if item["id"] == "rebound_repair_candidate")
    assert NEW not in {item["symbol"] for item in rebound["candidates"]}
    assert rebound["rebound_path_status_counts"]["not_matched"] >= 1


def test_current_rebound_can_use_now_visible_D_minus2_qualification_without_rewriting_D_minus2(qualified_assets, monkeypatch):
    from trading.research import ipo_qualification
    from trading.research.ipo_qualification import IpoQualification
    root, calendar, _, packages, days = qualified_assets
    for folder, (raw, source) in packages.items():
        index = days.index(source["plan"]["day"])
        opening, close_value, previous, pct = [("11", "11", "10", "10"),
            ("11", "10.45", "11", "-5"), ("10.8", "11.495", "10.45", "10")][index]
        mask = raw.code.eq("sz.000001")
        for field, value in {"open": opening, "close": close_value,
                "high": str(max(float(opening), float(close_value)) + .01),
                "low": str(min(float(opening), float(close_value)) - .01),
                "pctChg": pct, "preclose": previous}.items():
            raw.loc[mask, field] = value
        raw.to_csv(Path(folder) / "raw_daily.csv", index=False)
        source["receipt"]["raw_daily_sha256"] = digest(Path(folder) / "raw_daily.csv")
    initial = listing_proof()
    records = deepcopy(initial.records)
    records["000001.SZSE"]["available_at"] = "2026-10-20T23:59:59+08:00"
    q = IpoQualification(records, initial.calendar, initial.verification)
    _, folders = qualified_view(qualified_assets, q)
    _, early, _ = leader_environment_run.read_preview(folders[0], ipo_qualification=q)
    assert pd.isna(early.set_index("symbol").loc["000001.SZSE", "limit_up"])
    monkeypatch.setattr(ipo_qualification, "load_ipo_qualification", lambda *a, **k: q)
    analysis = leader_environment_run.analyze(folders, calendar, root / "late_rebound_analysis", ipo_evidence=root / "evidence.json")
    rebound = next(item for item in analysis["analysis"]["archetypes"] if item["id"] == "rebound_repair_candidate")
    assert "000001.SZSE" in {item["symbol"] for item in rebound["candidates"]}
