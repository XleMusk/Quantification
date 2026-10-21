"""Synthetic saved caches and strict-close boundary; no Engine or market run."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.contracts import IntentKind, RunContext, TradingMode
from trading.ledger import new_state
from trading.research import native_lowvol_publication as pub


def fixture(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar" / "calendar.json"
    calendar.parent.mkdir()
    calendar.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": [], "open_sessions": []}), encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    manifest = {"status": "complete_inventory_only", "execution_admitted": False, "chunks": []}
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    symbols = [f"{i:06d}.SZSE" for i in range(1, 31)]
    config = pub.portfolio().to_dict()

    def cache(end):
        directory = tmp_path / ("saved_" + end)
        directory.mkdir()
        decisions = []
        rank_days = []
        for d in pd.bdate_range("2026-09-01", end).strftime("%Y-%m-%d"):
            monthly = d in {"2026-09-30", "2026-10-30"}
            decisions.append({"date": d, "variant": pub.VARIANT, "decision": "monthly_ranking" if monthly else "not_month_end",
                "eligible_count": 30 if monthly else None, "candidate_count": 30 if monthly else None})
            if monthly:
                rank_days.append(d)
        pd.DataFrame(decisions).to_csv(directory / f"{pub.VARIANT}_decisions.csv", index=False)
        pd.DataFrame([{"date": d, "variant": pub.VARIANT, "rank": i + 1, "symbol": s,
            "volatility": .1 + i * .001, "momentum": .2, "median_amount": 50_000_000}
            for d in rank_days for i, s in enumerate(symbols)]).to_csv(directory / f"{pub.VARIANT}_ranks.csv", index=False)
        run = {"schema": "stockdb_rolling_low_volatility_ledger_v1", "status": "completed_research_only",
            "research_only": True, "execution_admitted": False, "variants": [pub.VARIANT], "config": config,
            "artifacts": str(directory), "replay_start": "2026-09-01", "replay_end": end,
            "sessions": len(decisions), "symbols": 30, "supplemental_raw_view": None,
            "completed_at": end + "T18:00:00+08:00"}
        (directory / "run.json").write_text(json.dumps(run), encoding="utf-8")
        protocol = {**run, "snapshot": str(snapshot), "snapshot_manifest": manifest,
            "lookback": 252, "skip": 21, "rank_cadence": "monthly_completed_session_end"}
        (directory / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
        return directory / "run.json"

    def close(day):
        folder = tmp_path / ("close_" + day)
        folder.mkdir()
        (folder / "raw_daily.csv").write_text("synthetic native close " + day, encoding="utf-8")
        (folder / "plan.json").write_text(json.dumps({"day": day}), encoding="utf-8")
        (folder / "receipt.json").write_text(json.dumps({"day": day}), encoding="utf-8")
        return folder

    def strict_source(folder, calendar_ref, sessions, now):
        folder = Path(folder)
        day = json.loads((folder / "plan.json").read_text())["day"]
        assert day in sessions
        return {"proof": {"day": day, "source_folder": str(folder.resolve()),
            "raw_sha256": pub._ref(folder / "raw_daily.csv")["sha256"],
            "plan_sha256": pub._ref(folder / "plan.json")["sha256"],
            "receipt_sha256": pub._ref(folder / "receipt.json")["sha256"],
            "calendar_sha256": calendar_ref["sha256"], "source_available_at": day + "T18:00:00+08:00"}}
    monkeypatch.setattr(pub, "_source", strict_source)
    return calendar, cache, close, symbols, config


def test_default_actual_overnight_publication_is_no_change_and_binds_without_backdating(tmp_path, monkeypatch):
    calendar, cache, close, _, _ = fixture(tmp_path, monkeypatch)
    record = pub.publish(cache("2026-10-16"), calendar, close("2026-10-16"), tmp_path / "publication",
        now="2026-10-17T01:00:00+08:00")
    assert record["source_day"] == "2026-10-16" and record["execution_day"] == "2026-10-19"
    assert record["kind"] == "NO_CHANGE" and record["weights"] is None and record["account_ref"] is None
    assert record["published_at"] == "2026-10-17T01:00:00+08:00" and record["rank_day"] is None
    loaded = pub.load_publication(record["publication_ref"]["file"], now="2026-10-17T02:00:00+08:00")
    intent = pub.bind_preopen(loaded, RunContext("synthetic-current-lowvol", TradingMode.REPLAY))
    assert intent.intent.kind == IntentKind.NO_CHANGE and intent.source_day == "2026-10-16"
    assert intent.published_at == record["published_at"] and intent.intent.decided_at is None
    assert not (tmp_path / "snapshot" / "raw_daily.parquet").exists()


def test_bootstrap_uses_saved_month_rank_without_normalizing_or_moving_old_positions(tmp_path, monkeypatch):
    calendar, cache, close, symbols, _ = fixture(tmp_path, monkeypatch)
    run = cache("2026-10-16")
    record = pub.publish(run, calendar, close("2026-10-16"), tmp_path / "bootstrap", inception=pub.INCEPTIONS[1],
        now="2026-10-17T01:00:00+08:00")
    assert record["weights"] == {s: .04 for s in symbols[:20]}
    assert record["rank_day"] == "2026-09-30" and record["bootstrap_applied"] is True
    assert record["account_snapshot"] is None and record["old_account_migrated"] is False
    ranks = run.parent / f"{pub.VARIANT}_ranks.csv"
    ranks.write_text(ranks.read_text() + "changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        pub.load_publication(record["publication_ref"]["file"], now="2026-10-17T02:00:00+08:00")


def test_monthly_continuation_retains_rank30_holding_and_uses_immutable_account_snapshot(tmp_path, monkeypatch):
    calendar, cache, close, symbols, config = fixture(tmp_path, monkeypatch)
    first = pub.publish(cache("2026-10-29"), calendar, close("2026-10-29"), tmp_path / "first", inception=pub.INCEPTIONS[1],
        now="2026-10-29T19:00:00+08:00")
    context = RunContext("synthetic-native-monthly-book", TradingMode.REPLAY)
    saved = new_state(config["initial_cash"])
    saved.update(day="2026-10-30", positions={symbols[24]: [{"qty": 100, "available": "2026-10-30"}]})
    head = tmp_path / "account" / "head.json"
    head.parent.mkdir()
    head.write_text(json.dumps({"method_id": pub.METHOD_ID, "config": config, "context": context.to_dict(), "state": saved}), encoding="utf-8")
    second = pub.publish(cache("2026-10-30"), calendar, close("2026-10-30"), tmp_path / "second", inception=pub.INCEPTIONS[1],
        account_path=head, previous_publication=first["publication_ref"]["file"], now="2026-10-30T19:00:00+08:00")
    assert second["kind"] == "FULL_TARGET" and second["rank_day"] == "2026-10-30"
    assert second["bootstrap_applied"] is False and symbols[24] in second["weights"] and symbols[19] not in second["weights"]
    assert len(second["weights"]) == 20 and set(second["weights"].values()) == {.04}
    head.write_text("mutable forward head advanced", encoding="utf-8")
    loaded = pub.load_publication(second["publication_ref"]["file"], now="2026-10-30T20:00:00+08:00")
    assert loaded["account_snapshot"]["state"] == saved
    assert loaded["source_head_ref"]["file"] == str(head.resolve())


def test_bad_publication_clock_or_wrong_continuation_method_does_not_write_publication(tmp_path, monkeypatch):
    calendar, cache, close, _, config = fixture(tmp_path, monkeypatch)
    run, source = cache("2026-10-16"), close("2026-10-16")
    with pytest.raises(ValueError, match="execution opening"):
        pub.publish(run, calendar, source, tmp_path / "late", now="2026-10-19T09:30:00+08:00")
    assert not (tmp_path / "late").exists()
    with pytest.raises(ValueError, match="paired continuation"):
        pub.publish(run, calendar, source, tmp_path / "unpaired", account_path=tmp_path / "unknown")
    assert not (tmp_path / "unpaired").exists()
    record = pub.publish(run, calendar, source, tmp_path / "valid", now="2026-10-17T01:00:00+08:00")
    head = tmp_path / "bad_head.json"
    head.write_text(json.dumps({"method_id": "old_engineering", "config": config, "state": {"day": "2026-10-19"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="this method"):
        pub.publish(cache("2026-10-19"), calendar, close("2026-10-19"), tmp_path / "wrong_method",
            account_path=head, previous_publication=record["publication_ref"]["file"], now="2026-10-19T19:00:00+08:00")
    assert not (tmp_path / "wrong_method").exists()


def test_non_month_end_continuation_reuses_older_cache_but_month_end_requires_new_rank(tmp_path, monkeypatch):
    calendar, cache, close, symbols, config = fixture(tmp_path, monkeypatch)
    run = cache("2026-10-16")
    first = pub.publish(run, calendar, close("2026-10-16"), tmp_path / "first16",
        inception=pub.INCEPTIONS[1], now="2026-10-17T01:00:00+08:00")

    def legacy_shape(path):
        record = json.loads(path.read_text())
        payload_path = Path(record["payload_ref"]["file"])
        payload = json.loads(payload_path.read_text())
        for field in ("decision_source_basis", "decision_cache_through"):
            payload.pop(field)
            record.pop(field)
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        record["payload_ref"] = pub._ref(payload_path)
        path.write_text(json.dumps(record), encoding="utf-8")

    # An exact-date old v1 publication remains loadable without new fields.
    first_path = Path(first["publication_ref"]["file"])
    legacy_shape(first_path)
    pub.load_publication(first_path, now="2026-10-19T19:00:00+08:00")
    saved = new_state(config["initial_cash"])
    saved.update(day="2026-10-19", positions={symbols[0]: [{"qty": 100, "available": "2026-10-19"}]})
    head = tmp_path / "account" / "head.json"
    head.parent.mkdir()
    head.write_text(json.dumps({"method_id": pub.METHOD_ID, "config": config,
        "context": RunContext("synthetic-next-day", TradingMode.REPLAY).to_dict(), "state": saved}), encoding="utf-8")
    decision_file = run.parent / f"{pub.VARIANT}_decisions.csv"
    original_decisions = decision_file.read_bytes()
    result = pub.publish(run, calendar, close("2026-10-19"), tmp_path / "next19",
        inception=pub.INCEPTIONS[1], account_path=head, previous_publication=first_path,
        now="2026-10-19T19:00:00+08:00")
    assert result["kind"] == "NO_CHANGE" and result["weights"] is None and result["rank_day"] is None
    assert result["decision_source_basis"] == "fixed_calendar_non_month_end_after_saved_rank_cache"
    assert result["decision_cache_through"] == "2026-10-16" and result["bootstrap_applied"] is False
    assert result["signal_source_identity"] == first["signal_source_identity"]
    assert decision_file.read_bytes() == original_decisions
    assert "2026-10-19" not in decision_file.read_text()
    loaded = pub.load_publication(result["publication_ref"]["file"], now="2026-10-19T20:00:00+08:00")
    assert loaded["account_snapshot"]["state"] == saved
    legacy_shape(Path(result["publication_ref"]["file"]))
    with pytest.raises(ValueError, match="Legacy publications require"):
        pub.load_publication(result["publication_ref"]["file"], now="2026-10-19T20:00:00+08:00")
    with pytest.raises(ValueError, match="month-end requires"):
        pub.publish(run, calendar, close("2026-10-30"), tmp_path / "missing_month_end",
            now="2026-10-30T19:00:00+08:00")
    assert not (tmp_path / "missing_month_end").exists()
