"""Synthetic evidence with real Engine/Ledger settlement, never market runs."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading.research import leader_eight_type_forward_registration as registrar
from trading.research import leader_eight_type_forward_run as original
from trading.research import leader_eight_type_paper as paper
from trading.research import leader_eight_type_split_forward as split
from trading.research.tests.test_leader_eight_type_forward_run import evidence, RELAY, GUARDED, SYMBOLS
from trading.research.tests.test_leader_eight_type_forward_registration import setup_registration
from trading.research.tests.test_leader_eight_type_generation_isolation import split_fixture_targets


def register(evidence, monkeypatch):
    options = setup_registration(evidence, monkeypatch)
    registrar.register(**options, session_method="split-settlement-v1")
    return options


def consume(evidence, options, *, end="2026-10-16", pubs=None):
    return split.run(evidence["pubs"][:1] if pubs is None else pubs,
        evidence["panels"][end], options["ipo_evidence"], options["research_master_run"],
        options["run_dir"], through=end, registration=options["out"])


def saved(options):
    return json.loads((options["run_dir"] / "head.json").read_text(encoding="utf-8"))


def test_seed_then_settle_without_E_publication_and_attach_without_repeated_fill(evidence, monkeypatch):
    options = register(evidence, monkeypatch)
    seed = consume(evidence, options, end="2026-10-15")
    assert seed["newly_committed_case_sessions"] == 32
    assert all(not case["state"]["orders"] for case in saved(options)["cases"].values())
    result = consume(evidence, options)
    assert result["newly_committed_case_sessions"] == 32 and result["metrics"] is None
    assert result["new_order_count"] > 0 and result["new_fill_count"] > 0 and result["orders_generated"] is True
    before = saved(options)
    relay = before["cases"][RELAY]
    assert relay["state"]["day"] == "2026-10-16" and relay["state"]["fills"]
    assert {fill["day"] for fill in relay["state"]["fills"].values()} == {"2026-10-16"}
    assert relay["missing_publication_days"] == ["2026-10-16"]
    assert relay["last_publication_day"] == "2026-10-15"
    assert relay["state"]["last_published_close_session"]["status"] == "awaiting_publication"
    assert not any(event["kind"] == "no_decision" for event in relay["daily"][-1]["events"])
    raw = (options["run_dir"] / "head.json").read_bytes()
    assert consume(evidence, options)["newly_committed_case_sessions"] == 0
    assert (options["run_dir"] / "head.json").read_bytes() == raw

    attached = consume(evidence, options, pubs=evidence["pubs"][:2])
    assert attached["newly_committed_case_sessions"] == 0
    assert attached["newly_attached_case_publications"] == 32
    assert attached["new_order_count"] == attached["new_fill_count"] == 0
    assert attached["orders_generated"] is False and attached["cumulative_orders_present"] is True
    after = saved(options)
    for key, case in after["cases"].items():
        assert case["daily"] == before["cases"][key]["daily"]
        assert case["state"]["fills"] == before["cases"][key]["state"]["fills"]
        assert case["state"]["orders"] == before["cases"][key]["state"]["orders"]
        assert case["missing_publication_days"] == []
        assert case["last_publication_day"] == "2026-10-16"
        assert len(case["attachments"]) == 1
    assert after["cases"][GUARDED]["state"]["pending_targets"] == {}
    assert after["cases"][GUARDED]["state"]["pending_published_close_intent"]["execution_not_before"] == "2026-10-19"
    raw = (options["run_dir"] / "head.json").read_bytes()
    unchanged = consume(evidence, options, pubs=evidence["pubs"][:2])
    assert unchanged["newly_attached_case_publications"] == 0
    assert unchanged["new_order_count"] == unchanged["new_fill_count"] == 0
    assert unchanged["orders_generated"] is False and unchanged["cumulative_orders_present"] is True
    assert (options["run_dir"] / "head.json").read_bytes() == raw
    assert not evidence["run"].exists()


def test_new_current_publication_can_follow_missing_day_but_no_historical_attachment(evidence, monkeypatch):
    options = register(evidence, monkeypatch)
    consume(evidence, options, end="2026-10-19")
    before = (options["run_dir"] / "head.json").read_bytes()
    with pytest.raises(ValueError, match="latest settled"):
        consume(evidence, options, end="2026-10-19", pubs=evidence["pubs"][:2])
    assert (options["run_dir"] / "head.json").read_bytes() == before
    result = consume(evidence, options, end="2026-10-19", pubs=[evidence["pubs"][0], evidence["pubs"][2]])
    assert result["newly_attached_case_publications"] == 32
    assert saved(options)["cases"][RELAY]["missing_publication_days"] == ["2026-10-16"]
    assert saved(options)["cases"][RELAY]["last_publication_day"] == "2026-10-19"


def test_missing_execution_reference_only_stops_its_cases(evidence, monkeypatch):
    split_fixture_targets(evidence)
    options = register(evidence, monkeypatch)
    result = consume(evidence, options)
    assert result["status"] == "advanced_research_only_with_blocked_cases"
    data = saved(options)
    blocked = data["cases"][RELAY]
    assert blocked["state"]["day"] == "2026-10-15"
    assert blocked["blocking_evidence"] and not blocked["state"]["fills"]
    gap = blocked["blocking_evidence"]["saved_gap_record"]
    assert json.loads(Path(gap["file"]).read_text(encoding="utf-8"))["engine_trial_started"] is False
    assert blocked["state"]["pending_published_close_intent"]["execution_not_before"] == "2026-10-16"
    healthy = data["cases"]["space_height_leader__unguarded__uncapped"]
    assert healthy["state"]["day"] == "2026-10-16"
    assert {row["symbol"] for row in healthy["state"]["fills"].values()} == {SYMBOLS[-1]}
    raw = (options["run_dir"] / "head.json").read_bytes()
    assert consume(evidence, options)["newly_committed_case_sessions"] == 0
    assert (options["run_dir"] / "head.json").read_bytes() == raw


def test_new_close_candidate_gap_does_not_prevent_known_opening_settlement(evidence, monkeypatch):
    value = evidence["loaded"][str(evidence["panels"]["2026-10-16"]) ]
    # Original D targets only stocks 1..5. The separately published E space
    # decision targets stock 6, whose malformed geometry must stop attachment,
    # while the already-known D buys still settle on the five sound E rows.
    panel = value["panel_by_day"]["2026-10-16"]
    space = next(item for item in panel["types"] if item["id"] == "space_height_leader")
    space["candidates"] = space["candidates"][-1:]
    value["raw"].loc[value["raw"].date.eq("2026-10-16") & value["raw"].code.eq("sz.000006"), "high"] = "9.8"
    day, execution, environment, prepared = paper._prepare(value, datetime.fromisoformat("2026-10-16T18:30:00+08:00"))
    record = paper._finalize(day, execution, environment, prepared, value["_bindings"], datetime.fromisoformat("2026-10-16T18:45:00+08:00"))
    evidence["pubs"][1].write_text(json.dumps(record), encoding="utf-8")
    options = register(evidence, monkeypatch)
    result = consume(evidence, options, pubs=evidence["pubs"][:2])
    case = saved(options)["cases"]["space_height_leader__unguarded__uncapped"]
    assert result["newly_committed_case_sessions"] == 64
    assert case["state"]["day"] == "2026-10-16" and len(case["state"]["orders"]) == 5
    assert {row["symbol"] for row in case["state"]["fills"].values()} == set(SYMBOLS[:5])
    assert case["blocking_evidence"]["phase"] == "attach"
    assert case["state"]["last_published_close_session"]["status"] == "awaiting_publication"
    assert case["last_publication_day"] == "2026-10-15" and case["attachments"] == []
    observed = next(c for c in result["cases"] if c["case_id"] == "space_height_leader__unguarded__uncapped")
    assert observed["missing_publication_days"] == []
    assert observed["unattached_publication_days"] == ["2026-10-16"]


def test_original_five_session_expiry_continues_without_new_publication(evidence, monkeypatch):
    options = register(evidence, monkeypatch)
    source = deepcopy(evidence["loaded"][str(evidence["panels"]["2026-10-19"])])
    extras = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    rows = source["raw"].loc[source["raw"].date.eq("2026-10-19")].copy()
    source["raw"] = pd.concat([source["raw"], *[rows.assign(date=day) for day in extras]], ignore_index=True)
    source["source_days"] += extras
    for day in extras:
        source["provenance"]["source_proofs"].append({"day": day, "raw_daily_sha256": "synthetic-raw-" + day,
            "ordinary_inventory_rows": len(SYMBOLS)})
    path = options["out"].parent / "synthetic_expiry_input.json"
    path.write_text(json.dumps({"synthetic_only": True, "source_days": source["source_days"]}), encoding="utf-8")
    evidence["panels"]["2026-10-23"] = path
    evidence["loaded"][str(path)] = source
    result = consume(evidence, options, end="2026-10-23")
    case = saved(options)["cases"][RELAY]
    assert result["newly_committed_case_sessions"] == 32 * 7
    buys = [row for row in case["state"]["fills"].values() if row["side"] == "buy"]
    sells = [row for row in case["state"]["fills"].values() if row["side"] == "sell"]
    assert buys and sells
    assert {row["day"] for row in sells} == {"2026-10-23"}
    assert not any(sum(lot["qty"] for lot in lots) for lots in case["state"]["positions"].values())
    assert case["last_publication_day"] == "2026-10-15"
    assert case["missing_publication_days"] == ["2026-10-16", "2026-10-19", *extras]
    assert result["metrics"] is None


def test_orphan_phase_is_adopted_without_repeating_engine(evidence, monkeypatch):
    options = register(evidence, monkeypatch)
    consume(evidence, options, end="2026-10-15")
    real = split.write_json
    def fail_head(path, value):
        if path.name == "head.json":
            raise OSError("synthetic stop after sealed phases before head")
        return real(path, value)
    monkeypatch.setattr(split, "write_json", fail_head)
    with pytest.raises(OSError, match="synthetic stop"):
        consume(evidence, options)
    assert saved(options)["cases"][RELAY]["state"]["day"] == "2026-10-15"
    monkeypatch.setattr(split, "write_json", real)
    monkeypatch.setattr(split, "advance", lambda *args, **kwargs: pytest.fail("Sealed orphan phase must not re-execute Engine"))
    result = consume(evidence, options)
    assert result["newly_committed_case_sessions"] == 32
    assert len(saved(options)["cases"][RELAY]["state"]["orders"]) == 5


def test_fixed_registration_cannot_be_silently_migrated(evidence, monkeypatch):
    options = setup_registration(evidence, monkeypatch)
    registrar.register(**options)
    with pytest.raises(ValueError, match="Explicit split generation"):
        consume(evidence, options, end="2026-10-15")
    assert not options["run_dir"].exists()
