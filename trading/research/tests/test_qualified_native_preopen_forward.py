"""A new explicit IPO-qualified plan reaches the unchanged shared accounting path."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.contracts import RunContext
from trading.ledger import Ledger
from trading.research import native_history_chain_source as chain
from trading.research import shortline_native_event_panel as producer
from trading.research import shortline_native_preopen as policy
from trading.research import shortline_native_preopen_forward as forward
from trading.research import shortline_native_scoring as scoring
from trading.research.baostock_daily_slice import digest
from trading.research.shortline_panel_replay import _config
from trading.research.tests import test_shortline_native_preopen_forward as saved_fixture


UNKNOWN = "301660.SZSE"
D, E = saved_fixture.D, saved_fixture.E


def files_at(root):
    return {str(file.relative_to(root)): file.read_bytes() for file in root.rglob("*") if file.is_file()}


@pytest.fixture
def qualified_sources(tmp_path, monkeypatch):
    # Reuse the real strict-package/native/IPO setup. Only the frozen score math
    # is a deterministic substitute; no Engine, Ledger or source validator is mocked.
    e = saved_fixture.evidence.__wrapped__(tmp_path, monkeypatch)
    original_rows = saved_fixture.rows
    def with_known_no_limit_ipo(day):
        rows = original_rows(day)
        if day in (D, E):
            row = deepcopy(rows[0])
            close, previous = (12., 10.) if day == D else (14.4, 12.)
            row.update(code="sz.301660", open=str(close), close=str(close), high=str(close + .1),
                low=str(close - .1), preclose=str(previous), pctChg="20", amount=str(close * 1000000))
            rows.append(row)
        return rows
    monkeypatch.setattr(saved_fixture, "rows", with_known_no_limit_ipo)
    e.closes[D] = saved_fixture.strict_close(tmp_path / "qualified_D_close", D, e.calendar, e.sdk)
    e.closes[E] = saved_fixture.strict_close(tmp_path / "qualified_E_close", E, e.calendar, e.sdk)
    source_root = tmp_path / "qualified_inputs"
    source_root.mkdir()
    e.clock["value"] = datetime.fromisoformat(E + "T02:00:00+08:00")
    with chain.NativeHistoryChainSource(tmp_path / "base", [], [e.closes[D]], calendar=e.calendar) as source:
        producer.build_from_source(source, source_root / "panel", through=D)
    e.clock["value"] = datetime.fromisoformat(E + "T03:00:00+08:00")
    scoring.score_saved_generation(e.model, source_root / "panel/result.json", source_root / "scores")
    e.raw_publication = source_root / "scores/publication.json"
    e.source_root = source_root
    return e


def test_qualified_plan_executes_two_isolated_books_but_cannot_rebind_legacy_or_IPO_inputs(qualified_sources):
    e = qualified_sources
    original_rule = deepcopy(forward.RULE_SPEC)
    raw_before = files_at(e.source_root)
    source = json.loads((e.source_root / "panel/result.json").read_text(encoding="utf8"))
    assert source["latest_scope"]["unknown_stage_symbols"] == [UNKNOWN]
    assert source["latest_scope"]["stage_membership_complete"] is False

    e.clock["value"] = datetime.fromisoformat(E + "T03:30:00+08:00")
    old_dir = e.root / "same_scores_legacy_plan"
    policy.build_preopen_plan(e.raw_publication, e.calendar, old_dir)
    old = policy.load_preopen_plan(old_dir)
    assert all(item["decision"] == "NO_CHANGE" and item["weights"] is None for item in old["decisions"].values())

    e.clock["value"] = datetime.fromisoformat(E + "T03:45:00+08:00")
    qualified_dir = e.root / "new_qualified_plan"
    policy.build_preopen_plan(e.raw_publication, e.calendar, qualified_dir,
        ipo_evidence=e.ipo, research_master_run=e.master)
    qualified = policy.load_preopen_plan(qualified_dir)
    assert qualified["record"]["policy_id"] == policy.QUALIFIED_POLICY_ID != policy.POLICY_ID
    assert qualified["record"]["published_at"] == E + "T03:45:00+08:00"
    assert qualified["source_day"] == D and qualified["execution_day"] == E
    scope = json.loads((qualified_dir / "qualification_scope.json").read_text(encoding="utf8"))
    assert scope["raw_unknown_stage_symbols"] == scope["excluded_no_limit_symbols"] == [UNKNOWN]
    assert scope["remaining_unknown_stage_symbols"] == []
    assert scope["qualified_stage_membership_complete"] is True
    for kind in ("lr", "lgbm"):
        assert qualified["decisions"][kind]["decision"] == "FULL_TARGET"
        assert UNKNOWN not in qualified["decisions"][kind]["weights"]
    assert files_at(e.source_root) == raw_before
    for file in (e.source_root / "scores").glob("*_raw_scores.csv"):
        frame = pd.read_csv(file)
        assert frame.paper_candidate_only.eq(False).all()
        assert UNKNOWN not in set(frame.symbol)

    e.clock["value"] = datetime.fromisoformat(E + "T20:00:00+08:00")
    raw = [e.closes[D], e.closes[E]]
    old_book, new_book = e.root / "legacy_book", e.root / "qualified_book"
    old_result = forward.run([old_dir / "plan.json"], raw, e.ipo, e.master, old_book)
    assert old_result["new_order_count"] == 0
    new_result = forward.run([qualified_dir / "plan.json"], raw, e.ipo, e.master, new_book)
    assert new_result["newly_committed_case_sessions"] == 2
    assert new_result["new_order_count"] == 2 and new_result["metrics"] is None
    assert new_result["policy_id"] == policy.QUALIFIED_POLICY_ID
    checkpoint = json.loads((new_book / "head.json").read_text(encoding="utf8"))
    assert checkpoint["binding"]["policy_id"] == policy.QUALIFIED_POLICY_ID
    assert checkpoint["binding"]["stage_membership_qualification"]["qualification_inputs"] == qualified["registration"]["qualification_inputs"]
    for kind in ("lr", "lgbm"):
        case = checkpoint["cases"][kind]
        context = RunContext.from_dict(case["context"])
        book = Ledger(case["state"], _config(100000, 5), context)
        target = saved_fixture.TARGET[kind]
        assert book.quantity(target) == 1600 and book.available(target, E) == 0
        assert case["state"]["cash"] == 8_397_884
        assert len(case["state"]["orders"]) == 1 and len(case["state"]["fills"]) == 2
        assert [f["qty"] for f in case["state"]["fills"].values()] == [800, 800]
        assert sum(f["fees"] for f in case["state"]["fills"].values()) == 516
        assert case["state"]["last_preopen_intent"]["envelope"]["source_day"] == D
        assert case["context"] != json.loads((old_book / "head.json").read_text())["cases"][kind]["context"]

    old_head, new_head = (old_book / "head.json").read_bytes(), (new_book / "head.json").read_bytes()
    with pytest.raises(ValueError):
        forward.run([qualified_dir / "plan.json"], raw, e.ipo, e.master, old_book)
    with pytest.raises(ValueError):
        forward.run([old_dir / "plan.json"], raw, e.ipo, e.master, new_book)
    assert (old_book / "head.json").read_bytes() == old_head
    assert (new_book / "head.json").read_bytes() == new_head

    alternate_master = e.master.with_name("different_master_run.json")
    changed = json.loads(e.master.read_text(encoding="utf8"))
    changed["synthetic_revision_identity"] = "different input reference, same underlying rows"
    alternate_master.write_text(json.dumps(changed), encoding="utf8")
    rejected = e.root / "must_not_create_on_changed_qualification"
    with pytest.raises(ValueError):
        forward.run([qualified_dir / "plan.json"], raw, e.ipo, alternate_master, rejected)
    assert not rejected.exists()
    assert (new_book / "head.json").read_bytes() == new_head
    assert forward.RULE_SPEC == original_rule
    assert files_at(e.source_root) == raw_before
