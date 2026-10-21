"""Carry one saved catalog append into a new D; never execute a collector."""
from copy import deepcopy
from pathlib import Path

import pytest

from trading.research import seat_daily as seat
from trading.research import seat_disclosure_daily as disclosures
from trading.research import system_daily_refresh as system
from trading.research.tests.test_seat_daily import setup, save


def test_new_day_carries_latest_saved_catalog_but_frozen_plan_and_bad_sha_never_fall_back(setup, monkeypatch):
    profile, close, catalog16, root = setup
    day, next_day = "2026-10-19", "2026-10-20"
    prior = seat._ref(catalog16)
    batch19 = save(root / "catalog_sources/2026-10-19/batch.json", {
        "schema": "public_seat_disclosure_batch_v1", "batch_id": day, "event_day": day,
        "status": "completed_empty", "created_at": day + "T19:30:00+08:00",
        "source_available_at": day + "T19:00:00+08:00"})
    catalog19 = save(root / "catalog/catalog19.json", {
        "schema": "public_seat_disclosure_catalog_v1", "created_at": day + "T19:31:00+08:00",
        "parent_ref": prior, "batches": seat._read(catalog16)["batches"] + [
            {"batch_id": day, "event_day": day, "reference": seat._ref(batch19)}]})
    latest = seat._ref(catalog19)
    claim = {"schema": "public_seat_disclosure_daily_result_v1",
        "status": "completed_saved_disclosure_catalog_research_only", "day": day,
        "daily_complete": True, "catalog": latest}
    receipt = {"day": day, "source": {"day": day}, "seat_disclosures": claim}
    receipt_file = save(root / "main_operations/20261019/receipt.json", receipt)
    head = {"last_source_day": day, "operations": [str(receipt_file)],
        "public_seat_profile": deepcopy(profile), "registration": {"synthetic": True},
        "public_seat_attachment": {"synthetic": True}}
    original_head = deepcopy(head)
    registration = {"calendar": profile["calendar"]}
    operation = root / "main_operations/20261020"
    calls = []
    def collect(day, prior_catalog, out, **options):
        assert day == next_day and Path(out) == operation / "seat_disclosures"
        assert options["execute"] is True and callable(options["run_step"])
        calls.append(str(prior_catalog))
        return {"status": "synthetic_metadata_only", "daily_complete": False}
    monkeypatch.setattr(disclosures, "run", collect)
    def forbidden(*args, **kwargs):
        pytest.fail("Saved catalog carry cannot execute a collector command or Engine")
    monkeypatch.setattr(system, "_run_command", forbidden)
    monkeypatch.setattr("trading.engine.advance", forbidden)
    assert system._latest_saved_seat_catalog(head) == latest
    system._seat_disclosures_refresh(head, registration, operation, next_day, {"day": next_day})
    assert calls == [latest["file"]]

    # A resumed D20 collector keeps its own original prior16 even though the
    # preceding successful operation now supplies catalog19 for a new plan.
    frozen_plan = save(operation / "seat_disclosures/plan.json", {"prior_catalog": prior})
    system._seat_disclosures_refresh(head, registration, operation, next_day, {"day": next_day})
    assert calls == [latest["file"], prior["file"]]
    frozen_plan.unlink()
    consumer_catalogs = []
    def run_profile(saved_profile, requested_day, package, out, *, run_step, catalog):
        assert requested_day == next_day and Path(out) == operation / "public_seat"
        consumer_catalogs.append(catalog)
        return {"result": {"status": "synthetic_metadata_only"}, "next_profile": None}
    monkeypatch.setattr(seat, "run_profile", run_profile)
    system._public_seat_refresh(head, registration, operation, next_day, {"day": next_day}, close(next_day))
    assert consumer_catalogs == [latest["file"]]
    assert head == original_head and head["public_seat_profile"]["append_seat"]["catalog"] == prior

    bad_receipt = deepcopy(receipt)
    bad_receipt["seat_disclosures"]["catalog"]["sha256"] = "0" * 64
    save(receipt_file, bad_receipt)
    before = list(calls)
    with pytest.raises(ValueError, match="Latest completed disclosure catalog changed"):
        system._seat_disclosures_refresh(head, registration, operation, next_day, {"day": next_day})
    assert calls == before and head == original_head
