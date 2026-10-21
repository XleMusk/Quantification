"""Synthetic saved layouts for existing sources, without Engine or markets."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pytest

from trading.contracts import PortfolioIntent, RunContext, TradingMode
from trading.engine import PUBLISHED_CLOSE_INTENT_SCHEMA, PUBLISHED_CLOSE_REDUCED_INTENT_SCHEMA
from trading.ledger import Ledger, new_state
from trading.research import saved_published_account_intent as m
from trading.research.saved_account_intent import read_request as dispatch
from trading.research.shortline_panel_replay import _config as leader_config


D, E = "2026-10-16", "2026-10-19"
NOW = datetime.fromisoformat("2026-10-17T10:00:00+08:00")
A, B = "600001.SSE", "000002.SZSE"
CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
            "closed_ranges": ["2026-10-01/2026-10-07"], "open_sessions": []}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(data)
    return {"file": str(path.resolve()), "sha256": sha256(data).hexdigest()}


@pytest.fixture
def source_factory(tmp_path):
    def build(family):
        directory = (tmp_path / family).resolve()
        leader = family == "leader20"
        module = m.leader if leader else m.alpha
        config = leader_config(100000, 5) if leader else m.alpha._config(100000, 5)
        ids = module.CASE_IDS if leader else tuple(row["id"] for row in module.PROTOCOL["cases"])
        prefix = "leader-published-forward:" if leader else "recent-alpha158-forward:"
        contexts = {key: RunContext(prefix + m.leader._hash(str(directory))[:16] + ":" + key,
                                   TradingMode.REPLAY) for key in ids}
        originals = {}
        pub_rows = []
        for index, key in enumerate(ids):
            metadata = {"decided_at": D + "T18:45:00+08:00"}
            if leader:
                if index == 1:
                    original = PortfolioIntent.no_change("synthetic_incomplete", **metadata)
                else:
                    original = PortfolioIntent.full_target({} if index == 2 else {A: .16, B: .16},
                        execution_not_before=E, source="research:leader_close_publication:" + key.split(":")[1], **metadata)
                pub_rows.append({"case_id": key, "portfolio_intent": original.to_dict()})
            else:
                metadata.update(execution_not_before=E,
                    source="research:recent_alpha158_dated_target_publication:" + key)
                original = (PortfolioIntent.full_target({A: .16, B: .16}, **metadata) if index == 0 else
                            PortfolioIntent.no_change("synthetic_incomplete", **metadata))
                pub_rows.append({"case": key, "portfolio_intent": original.to_dict()})
            originals[key] = original
        calendar_path = tmp_path / (family + "_calendar.json")
        calendar_ref = write(calendar_path, CALENDAR)
        pub = {"schema": module.PUBLICATION_SCHEMA, "source_close_day": D, "proposed_execution_day": E,
            "recorded_at": D + "T18:45:00+08:00", "next_open_deadline": E + "T09:30:00+08:00",
            "status": "close_research_target_publication_only", "execution_binding_pending": False,
            "publication_time_backfilled": False, "execution_day_rolled_forward": False,
            "successor_raw_used": False, "cases": pub_rows}
        binding = {"run_directory": str(directory), "config": config.to_dict()}
        if leader:
            binding.update(calendar=deepcopy(CALENDAR), original_protocol={"config": config.to_dict()},
                window=[D, E, "2026-10-20"], risk_policy=deepcopy(module.RISK_RULE_SPEC),
                published_target_policy_id=module.TARGET_POLICY_ID, case_ids=list(ids))
            pub.update(case_count=20, protocol_binding=binding["original_protocol"],
                publication_at=pub["recorded_at"], timeliness_uses_actual_recording=True,
                candidate_panel_only=True, execution_admitted=False, selection_enabled=False,
                strategy_validated=False)
        else:
            binding.update(calendar=str(calendar_path.resolve()), calendar_sha256=calendar_ref["sha256"],
                protocol=deepcopy(module.PROTOCOL), holding_policy=module.HOLDING_POLICY,
                frozen_model_contract={"synthetic_frozen_identity": True})
            pub.update(protocol=deepcopy(module.PROTOCOL), config=config.to_dict(), holding_policy=module.HOLDING_POLICY,
                calendar=str(calendar_path.resolve()), calendar_sha256=calendar_ref["sha256"],
                publication_origin="local_recording_now", frozen_model_contract=binding["frozen_model_contract"],
                **module.PUBLICATION_FLAGS)
        pub_ref = {**write(tmp_path / (family + "_publication.json"), pub), "day": D,
                   "execution_day": E, "recorded_at": pub["recorded_at"]}
        cases = {}
        for index, key in enumerate(ids):
            context = contexts[key]
            original = originals[key]
            bound = (replace(original, context=context, execution_not_before=E,
                source=original.source or "research:leader_close_publication:" + key.split(":")[1])
                if leader else replace(original, context=context))
            state = new_state(config.initial_cash)
            state["day"] = D
            if index == 1 or (leader and index == 0):
                symbol = B if index == 1 else A
                state["positions"] = {symbol: [{"qty": 300, "available": E}]}
                state["prices"] = {symbol: 1000}
            pending = bound.to_legacy_pending()
            receipt = None
            if pending is not None:
                receipt = {"schema": PUBLISHED_CLOSE_INTENT_SCHEMA, "decision_day": D,
                    "decision_at": pub["recorded_at"], "execution_not_before": E,
                    "intent": bound.to_dict(), "input_kind": "explicit_published_close_PortfolioIntent_not_SignalFrame",
                    "source_availability_verified_by_engine": False, "source_verified": False,
                    "pit_verified": False, "execution_admitted": False}
                if leader:
                    reduced = replace(bound, weights=tuple({A: .08, B: .16}.items())) if index != 2 else bound
                    pending = reduced.to_legacy_pending()
                    receipt.update(schema=PUBLISHED_CLOSE_REDUCED_INTENT_SCHEMA,
                        original_intent=receipt.pop("intent"), reduced_intent=reduced.to_dict(),
                        policy_id=module.TARGET_POLICY_ID)
            state["pending_targets"] = pending
            state["pending_published_close_intent"] = receipt
            if leader:
                state["replay_risk_exit_policy"] = {"schema": "replay_risk_exit_policy_v1",
                    "policy_id": module.RISK_POLICY_ID, "context": context.to_dict()}
                state["published_close_target_policy"] = {"schema": "replay_published_close_target_policy_v1",
                    "policy_id": module.TARGET_POLICY_ID, "context": context.to_dict()}
                state["pending_risk_exits"] = {"context": context.to_dict(), "decided_on": D,
                    "execution_on": E, "symbols": [A if index == 0 else B], "reason": "synthetic_expiry",
                    "source": "synthetic_saved_rule"} if index in (0, 1) else None
            entry = {"decision_day": D, "execution_day": E, "publication": pub_ref,
                "unbound_original_intent": original.to_dict(),
                "bound_original_intent" if leader else "bound_intent": bound.to_dict(),
                "committed_at": D + "T19:00:00+08:00"}
            case = {"context": context.to_dict(), "state": state,
                "daily": [{"date": D, "equity": Ledger(state, config, context).equity() / 100}],
                "journal": [entry], "error": "synthetic original failure" if leader and index == 3 else None,
                "blocking_evidence": None}
            if leader:
                entry.update({"post_session_" + field: deepcopy(state.get(field)) for field in
                    ("pending_targets", "pending_published_close_intent", "pending_risk_exits")})
                case["journal_files"] = [write(directory / "journal" / sha256(key.encode()).hexdigest()[:20] / "synthetic.json", entry)]
            cases[key] = case
        head = {"schema": module.SCHEMA, "registered_at": D + "T18:50:00+08:00",
                "binding": binding, "publications": [pub_ref], "cases": cases, **module.FLAGS}
        write(directory / "head.json", head)
        return directory, head, ids
    return build


@pytest.mark.parametrize("family", ["leader20", "alpha158"])
def test_original_layout_roundtrip_and_risk_or_plain_full_receipt(source_factory, family):
    directory, head, ids = source_factory(family)
    before = (directory / "head.json").read_bytes()
    result = m.export_requests(directory, E, current=NOW)
    assert list(result) == list(ids)
    full, nc = result[ids[0]], result[ids[1]]
    assert full["kind"] == "FULL_TARGET" and nc["kind"] == "NO_CHANGE" and nc["weights"] is None
    assert full["source_family"] == family and full["source_known_at"] == D + "T19:00:00+08:00"
    assert dispatch(full, current=NOW) == full
    assert dispatch(nc, current=NOW) == nc
    if family == "leader20":
        assert full["weights"] == {A: 0, B: .16} and full["original_weights"] == {A: .08, B: .16}
        assert nc["remaining_exit_symbols"] == [B]
        assert result[ids[2]]["weights"] == {}
        assert result[ids[3]]["status"] == "not_ready" and "kind" not in result[ids[3]]
        assert full["source_journal_independently_sealed"] is True
        assert full["strategy_id"] == ids[0]
    else:
        assert full["weights"] == {A: .16, B: .16} and nc["remaining_exit_symbols"] == []
        journal = full["refs"]["latest_journal"]
        assert journal["path"] == full["refs"]["head"]["path"]
        assert journal["embedded_case"] == ids[0] and journal["embedded_index"] == 0
        assert journal["evidence_scope"] == m.EMBEDDED_SCOPE
        assert full["source_journal_independently_sealed"] is False
        assert full["original_head_research_only_flag_present"] is False and "research_only" not in head
        assert full["strategy_id"] == "alpha158:" + ids[0]
        assert not list(directory.glob("journal/**/*"))
    assert all(row["execution_admitted"] is False and row["prefill_allowed"] is False for row in result.values())
    assert before == (directory / "head.json").read_bytes()


@pytest.mark.parametrize("family", ["leader20", "alpha158"])
def test_source_change_and_wrong_execution_are_not_adopted(source_factory, family):
    directory, head, ids = source_factory(family)
    request = m.export_request(directory, ids[0], E, current=NOW)
    result = m.export_requests(directory, "2026-10-20", current=NOW)
    assert all(row["status"] == "not_ready" for row in result.values())
    bad = deepcopy(request)
    bad["weights"][B] = .2
    with pytest.raises(ValueError, match="original source"):
        dispatch(bad, current=NOW)
    head["extra_actual_observation"] = "changed head is not the saved source"
    write(directory / "head.json", head)
    with pytest.raises(ValueError, match="original source"):
        dispatch(request, current=NOW)


def test_alpha_embedded_full_target_or_receipt_mismatch_is_not_nochange(source_factory):
    directory, head, ids = source_factory("alpha158")
    head["cases"][ids[0]]["state"]["pending_targets"][A] = .15
    write(directory / "head.json", head)
    result = m.export_requests(directory, E, current=NOW)
    assert result[ids[0]]["status"] == "not_ready" and "kind" not in result[ids[0]]
    assert result[ids[1]]["kind"] == "NO_CHANGE"


@pytest.mark.parametrize("family", ["leader20", "alpha158"])
def test_publication_bytes_are_pinned_and_never_rerun_or_fallback(source_factory, family):
    directory, head, _ = source_factory(family)
    path = Path(head["publications"][0]["file"])
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="bytes"):
        m.export_requests(directory, E, current=NOW)
