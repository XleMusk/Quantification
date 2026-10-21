"""Pinned saved-source contracts and the existing THS handoff, synthetic only."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json

import pytest

from trading.ledger import new_state
from trading.research import saved_account_intent as source
from trading.research.stockdb_rolling_low_volatility import (
    CALENDAR_MONTH_END_METHOD, SCHEMA, portfolio)
from trading.target_exit_lifecycle import FULL_EXIT_LIFECYCLE
from trading.ths import handoff

A = "600001.SSE"
SIGNAL = "low_volatility_252_20"
D, E = "2026-10-16", "2026-10-19"
CREATED = datetime.fromisoformat("2026-10-17T00:05:00+08:00")
CURRENT = datetime.fromisoformat("2026-10-19T09:35:00+08:00")


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def bundle(tmp_path):
    config = portfolio()
    artifacts = tmp_path / "artifacts"
    account_dir = artifacts / SIGNAL
    account_dir.mkdir(parents=True)
    calendar = tmp_path / "calendar.json"
    write(calendar, {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
                     "closed_ranges": [], "open_sessions": []})
    schedule = {"method_id": CALENDAR_MONTH_END_METHOD,
        "calendar_files": [{"path": str(calendar), "year": 2026,
            "sha256": sha256(calendar.read_bytes()).hexdigest()}]}
    state = new_state(config.initial_cash)
    state.update(day=D, positions={A: [{"qty": 100, "available": D}]}, prices={A: 1000},
        full_exit_lifecycle={"schema_version": 1, "method_id": FULL_EXIT_LIFECYCLE,
            "target_version": 1, "latest_target": {"version": 1, "weights": {A: .04},
                "source": "synthetic_old_rank", "decided_on": "2026-09-30"},
            "remaining_full_exits": {}})
    protocol = {"config": config.to_dict(), "replay_end": D,
        "full_exit_lifecycle": FULL_EXIT_LIFECYCLE, "month_end_schedule": schedule}
    record = {"schema": SCHEMA, "status": "completed_research_only", "artifacts": str(artifacts),
        "replay_end": D, "sessions": 1, "research_only": True, "execution_admitted": False,
        "strategy_validated": False, "variants": [SIGNAL], "config": config.to_dict(),
        "full_exit_lifecycle": FULL_EXIT_LIFECYCLE, "month_end_schedule": schedule,
        "calendar_verification": {"fixed_open_sessions": [D, E, "2026-10-20"]},
        "completed_at": "2026-10-17T00:00:00+08:00",
        "results": [{"signal": SIGNAL, "status": "completed", "completed_sessions": 1}]}
    receipt = {"config": config.to_dict(), "full_exit_lifecycle": FULL_EXIT_LIFECYCLE,
        "status": "completed", "last_completed_session": D, "completed_sessions": 1,
        "account_basis": "last_successfully_completed_session"}
    paths = {"run": tmp_path/"run.json", "protocol": artifacts/"protocol.json",
             "account": account_dir/"account.json", "receipt": account_dir/"replay.json"}
    for key, value in {"run": record, "protocol": protocol, "account": state, "receipt": receipt}.items():
        write(paths[key], value)
    payload = source.export_request(paths["run"], SIGNAL, E, current=CREATED)
    request = {"schema": "ths_manual_prefill_handoff_v1", "task_method": "saved_fixed_account_next_session_v1",
        "execution_admitted": False, "submission": "manual_only", "strategy_id": SIGNAL,
        "source_cutoff": D, "execution_day": E,
        "target_template": [{"symbol": A, "target_qty": 100}], "rank_template": [],
        "saved_account_intent": payload}
    snapshot = {"kind": "account", "source": "ths_user_export", "account_alias": "synthetic-only",
        "observed_at": CURRENT.isoformat(), "trading_date": E,
        "cash_total": 10000000, "cash_frozen": 0, "cash_available": 10000000,
        "cash_basis": "cash_balance_includes_frozen",
        "positions": {A: {"quantity": 900, "sellable": 900}}, "marks": {A: 1000}}
    return dict(paths=paths, payload=payload, handoff=request, snapshot=snapshot,
                config=config, root=tmp_path, state=state, record=record)


def test_export_pins_actual_saved_no_change_without_research_share_targets(bundle):
    b = bundle
    before = {key: path.read_bytes() for key, path in b["paths"].items()}
    value = source.read_request(b["payload"], current=CURRENT)
    assert value["kind"] == "NO_CHANGE" and value["weights"] is None
    assert value["remaining_exit_symbols"] == [] and value["target_version"] == 1
    assert value["source_known_at"] == "2026-10-17T00:00:00+08:00"
    assert not value["prefill_allowed"] and not value["signal_publication_verified"]
    assert before == {key: path.read_bytes() for key, path in b["paths"].items()}


@pytest.mark.parametrize("part", ["payload", "account", "calendar"])
def test_changed_weights_source_or_calendar_are_rejected(bundle, part):
    b = bundle
    if part == "payload":
        b["payload"]["weights"] = {A: .2}
    elif part == "account":
        state = deepcopy(b["state"])
        state["pending_targets"] = {}
        write(b["paths"]["account"], state)
    else:
        calendar = b["root"]/"calendar.json"
        write(calendar, {"year": 2026, "changed": True})
    with pytest.raises(ValueError, match="changed|equals|calendar"):
        source.read_request(b["payload"], current=CURRENT)


def test_late_backtest_completion_cannot_be_backdated_to_the_opening(bundle):
    record = deepcopy(bundle["record"])
    record["completed_at"] = E + "T09:31:00+08:00"
    write(bundle["paths"]["run"], record)
    with pytest.raises(ValueError, match="unavailable before.*opening"):
        source.export_request(bundle["paths"]["run"], SIGNAL, E, current=CURRENT)


def test_existing_handoff_uses_observed_shares_without_research_or_rank_reselection(bundle):
    result = handoff.prepare_handoff(bundle["handoff"], bundle["snapshot"], None,
        current=CURRENT, execution_config=bundle["config"].to_dict(), sizing_mode="saved_intent")
    assert result["next_plan"] is None and result["prefill_allowed"] is False
    assert result["research_only"] is True and result["execution_admitted"] is False
    assert result["adjustments"][0]["target_qty"] == 900
    assert result["adjustments"][0]["delta_qty"] == 0
    with pytest.raises(ValueError, match="require saved_intent"):
        handoff.prepare_handoff(bundle["handoff"], bundle["snapshot"], None,
            current=CURRENT, sizing_mode="replay_shares")


@pytest.mark.parametrize("field", ["strategy_id", "source_cutoff", "execution_day"])
def test_saved_intent_cannot_attach_to_another_strategy_or_session(bundle, field):
    request = deepcopy(bundle["handoff"])
    request[field] = "different"
    with pytest.raises(ValueError, match="differs from its handoff"):
        handoff.prepare_handoff(request, bundle["snapshot"], None,
            current=CURRENT, sizing_mode="saved_intent")


def test_pack_diagnostic_does_not_become_a_saveable_current_plan(bundle):
    b = bundle
    handoff_file, preview_file = b["root"]/"handoff.json", b["root"]/"preview.json"
    write(handoff_file, b["handoff"])
    write(preview_file, {"artifacts": {"ths_manual_handoff": str(handoff_file)}})
    pack = {"schema": "a_share_opening_pack_v1", "execution_admitted": False,
        "source_cutoff": D, "execution_day": E, "strategies": [{"strategy_id": SIGNAL,
            "preview": str(preview_file), "execution_config": b["config"].to_dict()}]}
    result = handoff.prepare_opening_pack(pack, b["snapshot"], None, strategy_id=SIGNAL,
        current=CURRENT, sizing_mode="saved_intent")
    assert result["status"] == "research_diagnostic_only" and result["ready_count"] == 0
    assert result["research_diagnostic_count"] == 1
    assert result["candidates"][0]["plan"]["next_plan"] is None


def opening_pack(bundle):
    root = bundle["root"]
    write(root/"handoff.json", bundle["handoff"])
    write(root/"preview.json", {"artifacts": {"ths_manual_handoff": str(root/"handoff.json")}})
    return {"schema": "a_share_opening_pack_v1", "execution_admitted": False,
        "source_cutoff": D, "execution_day": E, "strategies": [{"strategy_id": SIGNAL,
            "preview": str(root/"preview.json"), "execution_config": bundle["config"].to_dict()}]}


@pytest.mark.parametrize("field", ["strategy_id", "source_cutoff", "execution_day"])
def test_opening_pack_cannot_relabel_saved_strategy_or_session(bundle, field):
    pack = opening_pack(bundle)
    if field == "strategy_id":
        pack["strategies"][0][field] = "different"
    else:
        pack[field] = "different"
    result = handoff.prepare_opening_pack(pack, bundle["snapshot"], None,
        current=CURRENT, sizing_mode="saved_intent")
    assert result["ready_count"] == 0 and result["status"] == "not_ready"
    assert "differs from its saved handoff" in result["candidates"][0]["error"]


@pytest.mark.parametrize("command", ["prepare-handoff", "prepare-opening-pack"])
def test_cli_direct_observation_diagnostic_never_initializes_or_saves_journal(bundle, monkeypatch, command):
    from trading.ths import cli
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CURRENT.astimezone(tz) if tz else CURRENT
    monkeypatch.setattr(handoff, "datetime", Clock)
    def forbidden(*args, **kwargs):
        raise AssertionError("Diagnostic attempted a journal write/read")
    for name in ("initialize", "save_plan", "latest"):
        monkeypatch.setattr(cli.journal, name, forbidden)
    root = bundle["root"]
    write(root/"observation.json", bundle["snapshot"])
    write(root/"intent.json", bundle["payload"])
    pack = opening_pack(bundle)
    write(root/"pack.json", pack)
    args = [command, "--account-observation", str(root/"observation.json"),
        "--saved-intent", str(root/"intent.json"), "--sizing", "saved_intent", "--out", str(root/"output.json")]
    args += (["--handoff", str(root/"handoff.json")] if command == "prepare-handoff" else
             ["--pack", str(root/"pack.json"), "--strategy", SIGNAL])
    cli.main(args)
    result = json.loads((root/"output.json").read_text(encoding="utf-8"))
    assert result["prefill_allowed"] is False and result["execution_admitted"] is False
    if command == "prepare-handoff":
        assert result["next_plan"] is None and result["adjustments"][0]["target_qty"] == 900
    else:
        assert result["ready_count"] == 0 and result["research_diagnostic_count"] == 1


@pytest.mark.parametrize("extra,expected", [
    (["--save-next"], "cannot be saved"),
    (["--account", "synthetic-only"], "require --account-observation"),
    (["--quotes-csv", "unused.csv"], "account-bound --quotes JSON"),
])
def test_cli_rejects_writable_or_unbound_routes_before_journal(bundle, monkeypatch, capsys, extra, expected):
    from trading.ths import cli
    def forbidden(*args, **kwargs):
        raise AssertionError("Diagnostic attempted journal initialization")
    monkeypatch.setattr(cli.journal, "initialize", forbidden)
    args = ["prepare-handoff", "--handoff", "unused.json", "--sizing", "saved_intent"]
    if "--account" not in extra:
        args += ["--account-observation", "unused.json"]
    assert cli.main(args + extra) == 2
    assert expected in capsys.readouterr().err
