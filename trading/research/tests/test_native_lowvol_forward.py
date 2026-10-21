"""Synthetic consumer integration: real native feed, Engine and Ledger.

Only source/publication/qualification/action-evidence loaders are fixture
substitutes. These tests neither use market evidence nor estimate returns.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading import engine
from trading.config import StrategyConfig
from trading.contracts import PortfolioIntent, PreopenPortfolioIntent, RunContext
from trading.ledger import Ledger
from trading.research import native_lowvol_forward as forward


D, E, F, G = "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"
A = "600001.SSE"


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ref(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    now = datetime.fromisoformat("2026-10-30T20:00:00+08:00")
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now
    monkeypatch.setattr(forward, "datetime", Clock)
    monkeypatch.setattr(engine, "_published_now", lambda: now)
    calendar = tmp_path / "calendar.json"
    save(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
        "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []})
    config = StrategyConfig(holdings=1, retain_rank=1).validate()
    raw = {}
    for day in (D, E, F, G):
        frame = pd.DataFrame([{"date": day, "symbol": A,
            "open": 10., "high": 10.2, "low": 9.8, "close": 10.,
            "preclose": 10., "preclose_for_limits": 10., "volume": 10000000.,
            "amount": 100000000., "pct_chg": 0., "tradestatus": 1, "isST": 0,
            "is_st": False, "is_st_unknown": False, "adjustflag": "3",
            "source_row_valid": True, "provider_raw_observation": True}])
        raw[day] = {"proof": {"day": day, "synthetic_raw_sha256": "fixed-" + day},
            "canonical": frame}
    monkeypatch.setattr(forward, "_source", lambda folder, *args: deepcopy(raw[str(folder)]))
    qualification = SimpleNamespace(verification={"synthetic_qualification": True},
        records={A: {"ipo_date": "1991-04-03", "source_basis": "synthetic_fixture"}},
        assess=lambda *args: {"engine_block_reason": None})
    monkeypatch.setattr(forward, "load_ipo_qualification", lambda *args, **kwargs: qualification)
    evidence = tmp_path / "action_evidence.json"
    save(evidence, {"synthetic_only": True, "no_market_action_claim": True})
    actions = []
    class Accounting:
        def __init__(self, replay_days):
            self.actions = deepcopy(actions)
            self.start = replay_days[0]
            self.replay_days = list(replay_days)
            self.special_references = {}
            self.provenance = {"refs": [{"path": str(evidence.resolve()),
                "sha256": sha256(evidence.read_bytes()).hexdigest()}]}
            self.checks = [{"synthetic_action_guard": True}]

        def validate(self, *args):
            return None
    monkeypatch.setattr(forward, "load_action_accounting", lambda *args, **kwargs: Accounting(kwargs["replay_days"]))

    def load_publication(path, **kwargs):
        return {**read(path), "publication_ref": ref(path)}
    monkeypatch.setattr(forward, "load_publication", load_publication)
    def bind_preopen(record, context):
        options = {"context": context, "source": "synthetic_saved_lowvol_publication",
            "reason": "synthetic_behavior_only"}
        intent = (PortfolioIntent.no_change(**options) if record["kind"] == "NO_CHANGE"
            else PortfolioIntent.full_target(record["weights"], **options))
        return PreopenPortfolioIntent(record["source_day"], record["execution_day"],
            record["published_at"], intent,
            explicit_exit_all=record["kind"] == "FULL_TARGET" and not record["weights"])
    monkeypatch.setattr(forward, "bind_preopen", bind_preopen)
    run_dir = tmp_path / "isolated_research_account"
    publications = []
    def publish(source, execution, weights=None, *, account=None):
        path = tmp_path / ("publication_" + source + ".json")
        account_ref = None
        if account is not None:
            snapshot = tmp_path / ("account_snapshot_" + source + ".json")
            save(snapshot, {"state": deepcopy(account["state"]), "context": account["context"],
                "config": account["config"], "method_id": forward.METHOD_ID})
            account_ref = ref(snapshot)
        payload = {"source_day": source, "execution_day": execution,
            "calendar_ref": ref(calendar), "config": config.to_dict(),
            "inception": "bootstrap_latest_saved_monthly_rank",
            "previous_publication_ref": ref(publications[-1]) if publications else None,
            "account_ref": account_ref, "source_proof": deepcopy(raw[source]["proof"]),
            "published_at": source + "T18:00:00+08:00", "rank_day": "2026-09-30",
            "kind": "NO_CHANGE" if weights is None else "FULL_TARGET", "weights": weights}
        save(path, payload)
        publications.append(path)
        return path
    first = publish(D, E, {A: .2})
    return SimpleNamespace(root=tmp_path, config=config, calendar=calendar, raw=raw,
        actions=actions, evidence=evidence, run_dir=run_dir, publications=publications,
        publish=publish, first=first)


def execute(e, *, days=(D, E, F), publications=None, through=None):
    return forward.run(e.publications if publications is None else publications, list(days),
        "fixture_ipo", "fixture_master", "fixture_events", "fixture_references",
        e.run_dir, through=through)


def head(e):
    return read(e.run_dir / "head.json")


def first_commit(e):
    result = execute(e, days=(D, E))
    assert result["status"] == "advanced_independent_native_lowvol_research_account", result
    assert result["newly_committed_sessions"] == 1 and result["new_order_count"] == 1
    return head(e)


def test_real_feed_engine_full_then_NO_CHANGE_resume_and_idempotent(fixture):
    e = fixture
    first = first_commit(e)
    context = RunContext.from_dict(first["context"])
    book = Ledger(first["state"], e.config, context)
    quantity, cash = book.quantity(A), first["state"]["cash"]
    assert quantity > 0 and first["state"]["fills"]
    assert book.available(A, E) == 0 and book.available(A, F) == quantity
    assert first["state"]["pending_targets"] is None
    e.publish(E, F, account=first)
    waiting_bytes = (e.run_dir / "head.json").read_bytes()
    waiting = execute(e, days=(D, E))
    assert waiting["status"] == "waiting_for_exact_complete_execution_close"
    assert waiting["missing_source_days"] == [F] and waiting["newly_committed_sessions"] == 0
    assert (e.run_dir / "head.json").read_bytes() == waiting_bytes
    result = execute(e)
    second = head(e)
    assert result["newly_committed_sessions"] == 1 and result["new_order_count"] == 0
    assert result["orders_present"] is True and result["metrics"] is None
    assert second["state"]["cash"] == cash
    assert Ledger(second["state"], e.config, context).quantity(A) == quantity
    assert second["state"]["orders"] == first["state"]["orders"]
    assert second["state"]["fills"] == first["state"]["fills"]
    assert len(second["journal_files"]) == len(second["consumed_publications"]) == 2
    before = (e.run_dir / "head.json").read_bytes()
    again = execute(e)
    assert again["status"] == "already_consumed_original_publication_prefix"
    assert again["newly_committed_sessions"] == again["new_order_count"] == 0
    assert (e.run_dir / "head.json").read_bytes() == before
    for key in ("execution_admitted", "selection_enabled", "strategy_validated", "returns_reportable"):
        assert result[key] is False


def test_immutable_journal_recovers_interrupted_head_once_without_reexecuting(fixture, monkeypatch):
    e = fixture
    first = first_commit(e)
    e.publish(E, F, account=first)
    original_head = (e.run_dir / "head.json").read_bytes()
    original_writer = forward.write_json
    def interrupted(path, value):
        if Path(path).name == "head.json":
            raise OSError("synthetic crash after durable journal before head")
        return original_writer(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(forward, "write_json", interrupted)
        with pytest.raises(OSError, match="synthetic crash"):
            execute(e)
    journal = e.run_dir / "journal" / (F + ".json")
    journal_bytes = journal.read_bytes()
    assert (e.run_dir / "head.json").read_bytes() == original_head
    with monkeypatch.context() as patch:
        patch.setattr(forward, "advance", lambda *args, **kwargs: pytest.fail("Durable journal must recover without replay"))
        recovered = execute(e)
    assert recovered["newly_committed_sessions"] == 1 and recovered["new_order_count"] == 0
    assert journal.read_bytes() == journal_bytes
    assert len(head(e)["daily"]) == 2
    before = (e.run_dir / "head.json").read_bytes()
    altered = read(journal)
    altered["post_state"]["cash"] += 1
    save(journal, altered)
    with pytest.raises(ValueError, match="sealed research source or journal changed"):
        execute(e)
    assert (e.run_dir / "head.json").read_bytes() == before


def test_consumed_publication_raw_and_account_prefix_changes_cannot_commit(fixture):
    e = fixture
    first = first_commit(e)
    second = e.publish(E, F, account=first)
    before = (e.run_dir / "head.json").read_bytes()
    old_publication = e.first.read_bytes()
    changed = read(e.first)
    changed["synthetic_replacement"] = True
    save(e.first, changed)
    with pytest.raises(ValueError):
        execute(e)
    assert (e.run_dir / "head.json").read_bytes() == before
    e.first.write_bytes(old_publication)
    old_raw = deepcopy(e.raw[E]["proof"])
    e.raw[E]["proof"]["synthetic_raw_sha256"] = "replaced-consumed-native-row"
    with pytest.raises(ValueError):
        execute(e)
    assert (e.run_dir / "head.json").read_bytes() == before
    e.raw[E]["proof"] = old_raw
    publication = read(second)
    snapshot_file = Path(publication["account_ref"]["file"])
    snapshot = read(snapshot_file)
    snapshot["state"]["cash"] += 1
    save(snapshot_file, snapshot)
    publication["account_ref"] = ref(snapshot_file)
    save(second, publication)
    with pytest.raises(ValueError, match="different D account"):
        execute(e)
    assert not (e.run_dir / "journal" / (F + ".json")).exists()
    assert (e.run_dir / "head.json").read_bytes() == before


def test_late_past_economic_action_and_changed_consumed_evidence_are_rejected(fixture):
    e = fixture
    first = first_commit(e)
    e.publish(E, F, account=first)
    before = (e.run_dir / "head.json").read_bytes()
    e.actions.append({"symbol": A, "dividRegistDate": D, "dividOperateDate": F,
        "dividPayDate": G, "dividCashPsBeforeTax": .1})
    rejected = execute(e)
    assert rejected["status"] == "failed_required_evidence_or_engine_session_no_commit"
    assert "already relevant economic action prefix" in rejected["error"]
    assert rejected["newly_committed_sessions"] == 0 and rejected["session_not_committed"] is True
    assert (e.run_dir / "head.json").read_bytes() == before
    assert not (e.run_dir / "journal" / (F + ".json")).exists()
    e.actions.clear()
    save(e.evidence, {"synthetic_only": True, "replaced_previously_consumed_evidence": True})
    with pytest.raises(ValueError, match="Previously consumed action evidence changed"):
        execute(e)
    assert (e.run_dir / "head.json").read_bytes() == before


def test_real_engine_trial_failure_discards_mutation_and_does_not_commit(fixture, monkeypatch):
    e = fixture
    first = first_commit(e)
    e.publish(E, F, account=first)
    before = (e.run_dir / "head.json").read_bytes()
    advance = forward.advance
    trial_days = []
    def fail_after_engine(*args, **kwargs):
        result = advance(*args, **kwargs)
        trial_days.append(args[0]["day"])
        raise ValueError("synthetic evidence failure after real Engine trial")
    monkeypatch.setattr(forward, "advance", fail_after_engine)
    rejected = execute(e)
    assert trial_days == [F]
    assert rejected["status"] == "failed_required_evidence_or_engine_session_no_commit"
    assert rejected["newly_committed_sessions"] == rejected["new_order_count"] == 0
    assert rejected["session_not_committed"] is True and rejected["last_execution_day"] == E
    assert (e.run_dir / "head.json").read_bytes() == before
    assert not (e.run_dir / "journal" / (F + ".json")).exists()


def test_missing_first_execution_waits_without_creating_or_advancing_account(fixture, monkeypatch):
    e = fixture
    monkeypatch.setattr(forward, "new_state", lambda *args: pytest.fail("No account before exact E source"))
    result = execute(e, days=(D,))
    assert result["status"] == "waiting_for_exact_complete_execution_close"
    assert result["account_count"] == result["newly_committed_sessions"] == 0
    assert result["cash_fen"] is result["cash_rmb"] is result["positions"] is result["metrics"] is None
    assert result["missing_source_days"] == [E] and not e.run_dir.exists()
