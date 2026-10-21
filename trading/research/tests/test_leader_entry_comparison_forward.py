"""Synthetic actual-publication/sidecar/shared-Ledger integration, no providers."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from trading import engine
from trading.research import leader_entry_comparison_forward as m
from trading.research import leader_entry_comparison_publication as publication
from trading.research import leader_entry_confirmation_inputs as confirmations
from trading.research import leader_published_inputs as inputs
from trading.research.baostock_bulk_extras import FLAGS as EXTRA_FLAGS, SCHEMA as EXTRA_SCHEMA
from trading.research.baostock_daily_slice import digest
from trading.research.tests.test_leader_entry_comparison_publication import evidence, read
from trading.research.tests.test_leader_forward_run import A


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


@pytest.fixture
def real_publications(evidence, monkeypatch):
    """Only time is injected; source/publication validators and Engine stay real."""
    e = evidence
    monkeypatch.setattr(confirmations, "_now", lambda: e["clock"]["value"])
    monkeypatch.setattr(engine, "_published_now", lambda: e["clock"]["value"])
    reg = publication.load_registration(e["registration"])
    calendar = Path(reg["binding"]["calendar"])
    units = e["tmp"] / "real_units.json"
    save(units, {"status": "official_provider_document_read", "turn_unit": "percent",
                 "retrieved_at": "2026-10-14T12:00:00+08:00"})
    price_fields = ["date", "code", "open", "high", "low", "close", "preclose", "volume",
                    "amount", "adjustflag", "tradestatus", "pctChg", "isST"]

    def raw(day):
        return pd.DataFrame([{"date": day, "code": "sh." + symbol[:6], "symbol": symbol,
            "open": "10.0000", "high": "10.0500", "low": "9.9500", "close": "10.0000",
            "preclose": "10.0000", "volume": "10000000", "amount": "100000000.0000",
            "adjustflag": "3", "tradestatus": "1", "pctChg": "0.0000", "isST": "0"}
            for symbol in e["symbols"]])

    def raw_bytes(day):
        return raw(day)[price_fields].to_csv(index=False, lineterminator="\n").encode("utf-8")

    extra_paths = {}
    turns = {"2026-10-15": "2", "2026-10-16": "3", "2026-10-19": "2", "2026-10-20": "1"}

    def extras(day):
        if day in extra_paths:
            return extra_paths[day]
        folder = e["tmp"] / ("real_extras_" + day)
        package = folder / "package"
        package.mkdir(parents=True)
        (package / "raw_daily.csv").write_bytes(raw_bytes(day))
        rows = raw(day).assign(turn=turns[day], turn_numeric=pd.array([float(turns[day])]*20, dtype="Float64"),
            peTTM="10", pbMRQ="1", psTTM="1", pcfNcfTTM="10")
        data = folder / "dated_extras.parquet"
        rows.to_parquet(data, index=False)
        plan = {"schema": "baostock_bulk_exact_dated_close_inventory_v1", "day": day,
            "calendar_sha256": digest(calendar), "ordinary_symbols": 20, "symbols": e["symbols"],
            "execution_admitted": False}
        save(package / "plan.json", plan)
        save(package / "receipt.json", {"day": day, "status": "complete_full_dated_close_inventory_not_admitted",
            "raw_daily_sha256": digest(package / "raw_daily.csv"), "completed_rows": 20, "execution_admitted": False})
        result = {"schema": EXTRA_SCHEMA, "status": "completed_dated_bulk_extras_research_only", **EXTRA_FLAGS,
            "day": day, "package": str(package), "package_schema": plan["schema"],
            "package_plan_sha256": digest(package / "plan.json"), "package_receipt_sha256": digest(package / "receipt.json"),
            "ordinary_roster_complete": True, "raw_strings_preserved": True, "ordinary_rows": 20,
            "calendar_sha256": digest(calendar), "turn_numeric_unit": "percent", "turn_scaled": False,
            "source_inputs_available_at": day + "T18:30:00+08:00", "actual_derived_at": day + "T18:31:00+08:00",
            "available_at": day + "T18:31:00+08:00", "data_file": str(data), "data_sha256": digest(data),
            "unit_evidence_file": str(units), "unit_evidence_sha256": digest(units), "unit_evidence": read(units)}
        path = folder / "result.json"
        save(path, result)
        extra_paths[day] = path
        return path

    saved = {}

    def make(day):
        count = {"2026-10-16": 2, "2026-10-19": 3, "2026-10-20": 4}[day]
        prior = {"2026-10-16": "2026-10-15", "2026-10-19": "2026-10-16", "2026-10-20": "2026-10-19"}[day]
        source = e["source"](count)
        for proof in source["source_packages"]:
            proof["raw_daily_sha256"] = sha256(raw_bytes(proof["day"])).hexdigest()
        e["clock"]["value"] = datetime.fromisoformat(day + "T18:35:00+08:00")
        bundle = inputs.save_bundle(source, e["tmp"] / ("actual_contract_bundle_" + day), origin="synthetic_real_contract_integration")
        e["clock"]["value"] = datetime.fromisoformat(day + "T18:40:00+08:00")
        manifest, record = confirmations.build_confirmation(extras(day), extras(prior), calendar,
            day=day, source_day_proofs=source["source_packages"], out=e["tmp"] / ("actual_confirmation_" + day))
        assert record["counts"] == {"member_count": 20, "known_true": 20 if day == "2026-10-16" else 0,
            "known_false": 0 if day == "2026-10-16" else 20, "unknown": 0, "unknown_reason_counts": {}}
        e["clock"]["value"] = datetime.fromisoformat(day + "T18:45:00+08:00")
        pub, _ = publication.publish(bundle, manifest, e["registration"], e["tmp"] / ("actual_publication_" + day))
        saved[day] = (pub, bundle)
        return pub, bundle

    return {**e, "make": make, "saved": saved}


def consume(e, days, latest, directory, through=None):
    pubs, bundles = zip(*(e["saved"][day] for day in days))
    return m.run(list(pubs), list(bundles), latest, directory, registration=e["registration"], through=through)


def qty(case):
    return sum(lot["qty"] for lot in case["state"]["positions"].get(A, []))


def unchanged_old_sources(e):
    assert all(path.read_bytes() == value for path, value in e["before"].items())


def test_real16_publication_without19_raw_creates_no_research_state(real_publications):
    e = real_publications
    pub, own16 = e["make"]("2026-10-16")
    directory = e["tmp"] / "waiting"
    result = consume(e, ["2026-10-16"], own16, directory)
    assert result["status"] == "waiting_for_exact_first_execution_source"
    assert result["required_source_day"] == "2026-10-19" and result["metrics"] is None
    assert result["research_accounts_created"] == 0 and result["orders_generated"] is False
    assert not directory.exists() and read(pub)["confirmation"]["by_symbol"][A]["status"] == "known_true"
    unchanged_old_sources(e)


def test_real16_19_20_shared_ledger_entry_only_retains_held_and_restart_is_idempotent(real_publications):
    e = real_publications
    e["make"]("2026-10-16")
    _, own19 = e["make"]("2026-10-19")
    directory = e["tmp"] / "three_arm"
    days = ["2026-10-16", "2026-10-19"]
    initial = consume(e, days, own19, directory, through="2026-10-16")
    assert initial["newly_committed_case_sessions"] == 15 and initial["research_accounts_created"] == 15
    head = directory / "head.json"
    first = read(head)
    assert len(first["cases"]) == 15
    assert all(not c["state"]["fills"] and c["state"]["pending_targets"] == {A: .16}
               for c in first["cases"].values())
    frozen = head.read_bytes()
    repeated = consume(e, days, own19, directory, through="2026-10-16")
    assert repeated["newly_committed_case_sessions"] == 0 and head.read_bytes() == frozen
    next_result = consume(e, days, own19, directory)
    assert next_result["newly_committed_case_sessions"] == 15
    second = read(head)
    for family in publication.FAMILIES:
        daily = second["cases"][family + ":daily_confirmation"]
        entry = second["cases"][family + ":entry_only_confirmation"]
        assert qty(daily) > 0 and qty(entry) > 0
        assert daily["state"]["pending_targets"] == {} and entry["state"]["pending_targets"] == {A: .16}
        for case in (daily, entry):
            assert case["state"]["pending_published_close_intent"]["execution_not_before"] == "2026-10-20"
            assert {fill["day"] for fill in case["state"]["fills"].values()} == {"2026-10-19"}
        d_obs = daily["journal"][-1]["confirmation_observation"]["evaluations"][0]
        e_obs = entry["journal"][-1]["confirmation_observation"]["evaluations"][0]
        assert d_obs["confirmation_status"] == "known_false" and d_obs["confirmation_applied"] is True
        assert e_obs["post_opening_quantity"] > 0 and e_obs["confirmation_applied"] is False
        assert e_obs["application"] == "held_exempt_from_entry_confirmation"
    _, own20 = e["make"]("2026-10-20")
    days.append("2026-10-20")
    final = consume(e, days, own20, directory)
    assert final["newly_committed_case_sessions"] == 15
    third = read(head)
    for family in publication.FAMILIES:
        daily = third["cases"][family + ":daily_confirmation"]
        entry = third["cases"][family + ":entry_only_confirmation"]
        base = third["cases"][family + ":baseline"]
        assert qty(daily) == 0 and qty(entry) > 0 and qty(base) > 0
        assert any(fill["day"] == "2026-10-20" and fill["side"] == "sell" for fill in daily["state"]["fills"].values())
        # Costs can cause ordinary lot-sized rebalancing even with unchanged .16 weights.
        # Entry-only must match baseline, not acquire daily-confirmation's full exit.
        sold = lambda case: sum(fill["qty"] for fill in case["state"]["fills"].values()
                                if fill["day"] == "2026-10-20" and fill["side"] == "sell")
        assert qty(entry) == qty(base) and sold(entry) == sold(base) < sold(daily)
    assert all(c["metrics"] is None and c["performance_available"] is False and c["error"] is None
               and c["blocking_evidence"] is None for c in final["cases"])
    frozen = head.read_bytes()
    repeat = consume(e, days, own20, directory)
    assert repeat["newly_committed_case_sessions"] == 0 and repeat["research_accounts_created"] == 0
    assert head.read_bytes() == frozen
    unchanged_old_sources(e)


def test_coordinated_head_pending_and_embedded_journal_mutation_refused(real_publications):
    e = real_publications
    e["make"]("2026-10-16")
    _, own19 = e["make"]("2026-10-19")
    directory = e["tmp"] / "tamper"
    days = ["2026-10-16", "2026-10-19"]
    result = consume(e, days, own19, directory, through="2026-10-16")
    assert result["newly_committed_case_sessions"] == 15
    path = directory / "head.json"
    head = read(path)
    key = publication.CASE_IDS[0]
    case = head["cases"][key]
    original_proof = deepcopy(case["journal_files"][-1])
    independent = Path(original_proof["file"])
    independent_bytes = independent.read_bytes()
    files_before = sorted(str(p) for p in (directory / "journal").rglob("*.json"))
    case["state"]["pending_targets"][A] = .08
    case["state"]["pending_published_close_intent"]["reduced_intent"]["weights"][A] = .08
    entry = case["journal"][-1]
    entry["published_close_reduction"]["reduced_intent"]["weights"][A] = .08
    for field in ("pending_targets", "pending_published_close_intent", "pending_risk_exits"):
        entry["post_session_" + field] = deepcopy(case["state"].get(field))
    entry["post_session_state_sha256"] = m.shared._hash(case["state"])
    save(path, head)
    tampered = path.read_bytes()
    with pytest.raises(ValueError, match="Independent published session journal changed"):
        consume(e, days, own19, directory)
    assert path.read_bytes() == tampered and independent.read_bytes() == independent_bytes
    assert digest(independent) == original_proof["sha256"]
    assert sorted(str(p) for p in (directory / "journal").rglob("*.json")) == files_before
    unchanged_old_sources(e)


def test_missing_family_batch_stays_original_no_change_across_all_three_arms(evidence, monkeypatch):
    e = evidence
    monkeypatch.setattr(engine, "_published_now", lambda: e["clock"]["value"])
    family = publication.FAMILIES[0]
    e["saved"] = {}
    for day, count in (("2026-10-16", 2), ("2026-10-19", 3)):
        source = e["source"](count)
        # The missing historical D batch is retained in the later prefix, never rebuilt.
        del source["batches"][family]["2026-10-16"]
        e["clock"]["value"] = datetime.fromisoformat(day + "T18:35:00+08:00")
        own = inputs.save_bundle(source, e["tmp"] / ("missing_batch_input_" + day),
                                 origin="synthetic_missing_original_family_batch")
        e["clock"]["value"] = datetime.fromisoformat(day + "T18:45:00+08:00")
        pub, record = publication.publish(own, None, e["registration"], e["tmp"] / ("missing_batch_pub_" + day))
        e["saved"][day] = (pub, own)
        if day == "2026-10-16":
            original = next(row for row in record["base_cases"] if row["family"] == family)
            assert original["original_day_batch"] is None and original["batch_status"] == "missing"
            assert original["portfolio_intent"]["kind"] == "NO_CHANGE"
            assert original["target_weights"] is None and original["target_cash_weight"] is None
    directory = e["tmp"] / "missing_batch_accounts"
    days = ["2026-10-16", "2026-10-19"]
    own19 = e["saved"]["2026-10-19"][1]
    initial = consume(e, days, own19, directory, through="2026-10-16")
    assert initial["newly_committed_case_sessions"] == 15
    first = read(directory / "head.json")
    for arm in ("baseline", "daily_confirmation", "entry_only_confirmation"):
        case = first["cases"][family + ":" + arm]
        assert case["daily"][0]["batch_status"] == "missing"
        assert case["daily"][0]["original_publication_kind"] == "NO_CHANGE"
        assert case["daily"][0]["effective_publication_kind"] == "NO_CHANGE"
        assert case["state"]["pending_targets"] is None
        assert case["journal"][0]["post_session_pending_targets"] is None
        assert case["journal"][0]["published_close_reduction"] is None
        assert not case["state"]["orders"] and not case["state"]["fills"]
    advanced = consume(e, days, own19, directory)
    assert advanced["newly_committed_case_sessions"] == 15
    assert all(c["processed_decision_days"] == days and c["error"] is None and c["blocking_evidence"] is None
               for c in advanced["cases"])
    second = read(directory / "head.json")
    for arm in ("baseline", "daily_confirmation", "entry_only_confirmation"):
        case = second["cases"][family + ":" + arm]
        assert not case["state"]["fills"]  # A missing16 decision cannot become a19 opening trade.
        assert case["journal"][0]["unbound_original_intent"]["kind"] == "NO_CHANGE"
        assert case["journal"][0]["post_session_pending_targets"] is None
    assert all(second["cases"][other + ":baseline"]["state"]["fills"]
               for other in publication.FAMILIES if other != family)
    unchanged_old_sources(e)
