"""New adapter contracts with small synthetic saved files; no Engine run."""
from copy import deepcopy
import csv
import json
from pathlib import Path

import pytest

from trading.contracts import RunContext, TradingMode
from trading.ledger import new_state
from trading.research import native_lowvol_saved_opening as m
from trading.research import native_lowvol_publication as pub
from trading.research.tests.test_native_lowvol_publication import fixture as publication_fixture


NOW = "2026-10-17T10:00:00+08:00"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return pub._ref(path)


def test_bootstrap_keeps_plan_weights_but_no_account_cash_holdings_or_order_pack(tmp_path, monkeypatch):
    calendar, cache, close, symbols, _ = publication_fixture(tmp_path, monkeypatch)
    run = tmp_path / "uncreated-native-run"
    publication = pub.publish(cache("2026-10-16"), calendar, close("2026-10-16"), tmp_path / "pub",
        inception=pub.INCEPTIONS[1], now="2026-10-17T01:00:00+08:00")
    monkeypatch.setattr("trading.engine.advance", lambda *a, **k: pytest.fail("No Engine in read-only adapter"))
    source_dir = tmp_path / "source"
    source = m.build_source(run, publication["publication_ref"]["file"], source_dir, current=NOW)
    assert source["account_count"] == 0 and source["case_ids"] == [pub.VARIANT]
    assert source["cash_fen"] is None and source["equity_rmb"] is None and source["positions"] is None
    assert source["kind"] == "FULL_TARGET" and source["weights"] == {s: .04 for s in symbols[:20]}
    assert source["rank_day"] == "2026-09-30" and source["metrics"] is None
    assert not run.exists() and m.source_dependencies_current(source_dir)
    before = (source_dir / "head.json").read_bytes()
    assert m.build_source(run, publication["publication_ref"]["file"], source_dir, current=NOW) == source
    assert (source_dir / "head.json").read_bytes() == before
    request = m.export_requests(source_dir, current=NOW)[pub.VARIANT]
    assert m.read_request(request, current=NOW) == request
    assert m.load_saved_case(source_dir, current=NOW)["state"] is None
    out, result = m.build_opening(source_dir, tmp_path / "opening", current=NOW)
    assert result["status"] == "not_ready" and result["account_count"] == 0
    assert result["opening_pack"] is None and result["candidate_order_count"] is None
    assert result["cases"][0]["weights"] == source["weights"] and result["cases"][0]["positions"] is None
    assert not list(out.rglob("account.json")) and not list(out.rglob("order_plan.csv")) and not run.exists()
    write(run / "head.json", {"some_new_head": True})
    assert not m.source_dependencies_current(source_dir)
    with pytest.raises(ValueError, match="stale"):
        m.export_requests(source_dir, current=NOW)


def saved_continuation(tmp_path, monkeypatch):
    calendar, cache, close, symbols, config = publication_fixture(tmp_path, monkeypatch)
    from trading.research import opening_pack
    monkeypatch.setattr(opening_pack, "ROOT", tmp_path / "pack-root")
    monkeypatch.setattr(opening_pack, "DATA_ROOT", tmp_path / "pack-data")
    first = pub.publish(cache("2026-10-16"), calendar, close("2026-10-16"), tmp_path / "first",
        inception=pub.INCEPTIONS[1], now="2026-10-17T01:00:00+08:00")
    run = (tmp_path / "native-run").resolve()
    context = m._context(run).to_dict()
    envelope = pub.bind_preopen(first, RunContext.from_dict(context)).to_dict()
    state = new_state(config["initial_cash"])
    state.update(day="2026-10-19", positions={symbols[0]: [{"qty": 100, "available": "2026-10-20"}]},
        last_preopen_intent={"envelope": envelope},
        full_exit_lifecycle={"schema_version": 1, "method_id": m.PREOPEN_FULL_EXIT_LIFECYCLE,
            "target_version": 1, "remaining_full_exits": {},
            "latest_target": {"version": 1, "weights": first["weights"],
                "source": envelope["intent"]["source"], "decided_on": "2026-10-17",
                "source_day": "2026-10-16", "published_at": first["published_at"],
                "decided_at": first["published_at"], "execution_not_before": "2026-10-19",
                "preopen_envelope": envelope}})
    binding = {"method_id": pub.METHOD_ID, "run_directory": str(run), "context": context,
        "config": config, "mode": TradingMode.REPLAY.value, "calendar_ref": first["calendar_ref"],
        "inception": first["inception"], "full_exit_lifecycle": m.PREOPEN_FULL_EXIT_LIFECYCLE}
    close19 = close("2026-10-19")
    proof19 = pub._source(close19, first["calendar_ref"], pub._calendar(calendar)[1], pub._now("2026-10-19T19:00:00+08:00"))["proof"]
    daily = [{"date": "2026-10-19", "equity": 140000.0}]
    action_ref = write(tmp_path / "actions" / "evidence.json", {"saved": "synthetic action proof"})
    refs = [{"path": action_ref["file"], "sha256": action_ref["sha256"]}]
    journal = {"schema": m.JOURNAL_SCHEMA, "binding_sha256": m._hash(binding),
        "publication_ref": first["publication_ref"], "previous_journal_sha256": None,
        "execution_day": "2026-10-19", "source_day": "2026-10-16", "raw_sources": [first["source_proof"], proof19],
        "post_state": state, "post_state_sha256": m._hash(state), "post_daily": daily,
        "post_daily_sha256": m._hash(daily), "action_prefix": [], "action_scope": symbols,
        "action_evidence_refs": refs, "committed_at": "2026-10-19T18:30:00+08:00", **m.FLAGS}
    journal_ref = write(run / "journal" / "2026-10-19.json", journal)
    head = {"schema": m.HEAD_SCHEMA, "method_id": pub.METHOD_ID, "config": config, "context": context,
        "binding": binding, "state": state, "daily": daily, "consumed_publications": [first["publication_ref"]],
        "journal_files": [journal_ref], "raw_sources": journal["raw_sources"], "action_prefix": [],
        "action_scope": symbols, "action_evidence_refs": refs, **m.FLAGS}
    write(run / "head.json", head)
    second = pub.publish(cache("2026-10-19"), calendar, close19, tmp_path / "second",
        account_path=run / "head.json", previous_publication=first["publication_ref"]["file"],
        inception=first["inception"], now="2026-10-19T19:00:00+08:00")
    return run, second, head, symbols[0]


def test_saved_account_no_change_preview_keeps_exact_shares_and_mutable_head_needs_refresh(tmp_path, monkeypatch):
    run, publication, head, held = saved_continuation(tmp_path, monkeypatch)
    current = "2026-10-19T20:00:00+08:00"
    original = (run / "head.json").read_bytes()
    source_dir = tmp_path / "source"
    source = m.build_source(run, publication["publication_ref"]["file"], source_dir, current=current)
    assert source["account_count"] == 1 and source["weights"] is None and source["kind"] == "NO_CHANGE"
    assert str(run / "head.json") not in [r["path"] for r in source["dependencies"]]
    assert source["account_head_dependency"]["exists"] is True
    out, result = m.build_opening(source_dir, tmp_path / "opening", current=current)
    assert result["preview_count"] == 1 and result["candidate_order_count"] == 0
    with (Path(result["cases"][0]["preview"]).parent / "target_holdings.csv").open(newline="", encoding="utf-8-sig") as stream:
        row = next(csv.DictReader(stream))
    assert row["symbol"] == held and row["current_qty"] == row["target_qty"] == "100" and row["delta_qty"] == "0"
    assert m.load_saved_case(source_dir, current=current)["state"] == head["state"]
    assert (run / "head.json").read_bytes() == original
    changed = deepcopy(head); changed["state"]["cash"] -= 1
    write(run / "head.json", changed)
    assert not m.source_dependencies_current(source_dir)
    # The actual publication remains valid because its immutable account copy,
    # rather than the mutable historical source_head_ref, is the published input.
    assert pub.load_publication(publication["publication_ref"]["file"], now=current)["account_snapshot"]["state"] == head["state"]
    with pytest.raises(ValueError, match="stale"):
        m.build_opening(source_dir, tmp_path / "stale-opening", current=current)
    assert not (tmp_path / "stale-opening").exists()


def test_different_method_or_unsealed_account_is_rejected_before_source_write(tmp_path, monkeypatch):
    run, publication, head, _ = saved_continuation(tmp_path, monkeypatch)
    bad = deepcopy(head); bad["method_id"] = "old_fixed_engineering_method"
    write(run / "head.json", bad)
    with pytest.raises(ValueError, match="no account migration"):
        m.build_source(run, publication["publication_ref"]["file"], tmp_path / "bad-source", current="2026-10-19T20:00:00+08:00")
    assert not (tmp_path / "bad-source").exists()
    bad = deepcopy(head); bad["state"]["cash"] -= 1
    write(run / "head.json", bad)
    with pytest.raises(ValueError, match="snapshot"):
        m.build_source(run, publication["publication_ref"]["file"], tmp_path / "unsealed-source", current="2026-10-19T20:00:00+08:00")
    assert not (tmp_path / "unsealed-source").exists()
