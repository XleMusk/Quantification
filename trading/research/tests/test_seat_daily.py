from copy import deepcopy
from datetime import datetime
import csv
import json
from pathlib import Path

import pytest

from trading.research import seat_daily as daily


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "_now", lambda: datetime.fromisoformat("2026-10-21T19:00:00+08:00"))
    fixed = tmp_path / "fixed"
    calendar = save(fixed / "calendar.json", {"schema": "cn_a_share_exchange_calendar_v1",
        "year": 2026, "closed_ranges": [], "open_sessions": []})
    common = {"calendar": daily._ref(calendar), "arm_ids": ["legacy_repeated_sum_lag1", "legacy_repeated_sum_lag2",
        "stockday_unique_lag1", "stockday_unique_lag2"], "config": {"initial_cash": 100000},
        "registered_at": "2026-10-16T22:00:00+08:00", "execution_admitted": False,
        "selection_enabled": False, "strategy_validated": False}
    original = save(fixed / "original.json", {})
    method = save(fixed / "method.json", {})
    def close(day):
        root = tmp_path / ("close_" + day)
        save(root / "plan.json", {"day": day, "calendar_sha256": daily._ref(calendar)["sha256"]})
        save(root / "receipt.json", {"status": "complete_full_dated_close_inventory_not_admitted"})
        return root
    anchor = close("2026-10-16")
    bundle = save(tmp_path / "pub16/bundle.json", {"decision_day": "2026-10-16",
        "next_execution_day": "2026-10-19", "D_primary": {"source_folder": str(anchor)}})
    pub16 = save(tmp_path / "pub16/publication.json", {"decision_day": "2026-10-16",
        "execution_day": "2026-10-19", "published_at": "2026-10-16T23:00:00+08:00",
        "same_D_actual_publication": True, "bundle": daily._ref(bundle)})
    batches = []
    for day in ("2026-10-15", "2026-10-16"):
        batch = save(tmp_path / "catalog_sources" / day / "batch.json", {
            "schema": "public_seat_disclosure_batch_v1", "batch_id": day, "event_day": day,
            "status": "completed_nonempty", "created_at": "2026-10-16T21:00:00+08:00",
            "source_available_at": "2026-10-16T20:00:00+08:00"})
        batches.append({"batch_id": day, "event_day": day, "reference": daily._ref(batch)})
    catalog = save(tmp_path / "catalog/catalog16.json", {"schema": "public_seat_disclosure_catalog_v1",
        "created_at": "2026-10-16T21:30:00+08:00", "parent_ref": None, "batches": batches})
    oldreg = save(fixed / "old.json", {**common,
        "schema": "public_seat_single_snapshot_forward_method_registration_v1",
        "first_decision_day": "2026-10-16", "original_registration": daily._ref(original)})
    appreg = save(fixed / "append.json", {**common,
        "schema": "public_seat_append_forward_method_registration_v1", "first_decision_day": "2026-10-19",
        "dated_input_method_registration": daily._ref(method), "initial_catalog": daily._ref(catalog)})
    profile = daily.bootstrap_profile(old_registration=oldreg, append_registration=appreg,
        old_publication=pub16, old_close=anchor, old_run=tmp_path / "old_books", append_run=tmp_path / "new_books")
    return profile, close, catalog, tmp_path


def test_plan19_reuses_real16_prefix_and_never_requires_sameD_pub_for_consumption(setup):
    profile, close, catalog, root = setup
    package = close("2026-10-19")
    plan = daily.next_plan(profile, "2026-10-19", package, root / "operations/19")
    steps = {s["id"]: s for s in plan["steps"]}
    assert set(steps) == {"append_seat_audit", "append_seat_publication", "old_seat_audit_projection",
        "old_seat_publication", "old_seat_forward"}
    assert steps["old_seat_forward"]["requires"] == []
    command = steps["old_seat_forward"]["command"]
    assert [command[i + 1] for i, t in enumerate(command) if t == "--publication"] == profile["old_seat"]["publications"]
    assert "--execute" in steps["append_seat_audit"]["command"]
    assert plan["catalog"] == str(catalog)
    assert daily.validate_plan(plan, profile, "2026-10-19", package, root / "operations/19", catalog=plan["catalog"]) == plan
    assert not Path(profile["old_seat"]["run_directory"]).exists()
    assert not Path(profile["append_seat"]["run_directory"]).exists()
    with pytest.raises(ValueError, match="exact next"):
        daily.next_plan(profile, "2026-10-20", close("2026-10-20"), root / "skip")


def runner(profile, day, *, fail_publication=False):
    calls = []
    def run(step):
        calls.append(step["id"])
        if fail_publication and step["id"].endswith("publication"):
            return {"status": "failed", "error": "synthetic publisher failure"}
        for file in step["expected_files"]:
            value = {"synthetic": True}
            if Path(file).name == "outcome.json":
                value = {"network_accessed": True}
            elif Path(file).name == "publication.json":
                value = {"decision_day": day, "execution_day": "2026-10-20" if day == "2026-10-19" else "2026-10-21"}
            elif step["id"].endswith("forward"):
                value = {"schema": ("public_seat_four_arm_dated_forward_result_v1" if step["id"].startswith("old_")
                    else "public_seat_append_four_arm_forward_result_v1"),
                    "account_count": 4, "status": "advanced_four_isolated_dated_research_books",
                    "cases": [{"arm_id": a, "last_processed_execution_day": day} for a in profile["arm_ids"]]}
            save(file, value)
        return {"status": "completed"}
    return calls, run


def test_missing_new19_disclosure_blocks20_publication_but_not_saved19_execution(setup):
    profile, close, _, root = setup
    _, run19 = runner(profile, "2026-10-19")
    one = daily.run_profile(profile, "2026-10-19", close("2026-10-19"), root / "operations/19", run_step=run19)
    assert one["result"]["daily_complete"] is True
    assert one["result"]["network_accessed"] is True
    calls, run20 = runner(profile, "2026-10-20")
    two = daily.run_profile(one["next_profile"], "2026-10-20", close("2026-10-20"), root / "operations/20", run_step=run20)
    assert calls == ["old_seat_forward", "append_seat_forward"]
    assert two["result"]["daily_complete"] is False
    assert two["result"]["branches"]["append_seat"]["consumer_complete"] is True
    assert two["result"]["branches"]["append_seat"]["blocking_evidence"]["missing_event_days"] == ["2026-10-19"]
    assert two["next_profile"]["append_seat"]["last_publication_day"] == "2026-10-19"
    assert two["next_profile"]["old_seat"]["status"] == "static_source_exhausted_holdings_right_censored"
    assert two["result"]["network_accessed"] is False
    plan21 = daily.next_plan(two["next_profile"], "2026-10-21", close("2026-10-21"), root / "operations/21")
    assert not any(s["id"].startswith("old_seat_") for s in plan21["steps"])
    assert plan21["blocked"]["append_seat"]["status"] == "missed_actual_publication_prefix"


def test_current_publisher_failure_keeps_independent_old_consumption_and_no_fake_publication(setup):
    profile, close, _, root = setup
    calls, run = runner(profile, "2026-10-19", fail_publication=True)
    result = daily.run_profile(profile, "2026-10-19", close("2026-10-19"), root / "operations/fail", run_step=run)
    assert "old_seat_forward" in calls
    assert result["result"]["branches"]["old_seat"]["consumer_complete"] is True
    assert result["next_profile"]["append_seat"]["publications"] == []
    assert result["next_profile"]["old_seat"]["publications"] == profile["old_seat"]["publications"]
    assert result["result"]["daily_complete"] is False
    changed = deepcopy(result["next_profile"])
    changed["old_seat"]["publications"] = []
    with pytest.raises(ValueError, match="before/after"):
        daily.validate_plan(daily._read(root / "operations/fail/plan.json"), changed,
            "2026-10-19", close("2026-10-19"), root / "operations/fail")


def test_projection_preserves_exact_strings_source_response_and_missing_scope(tmp_path, monkeypatch):
    from trading.research import seat_hypothesis_dated_inputs as inputs
    from trading.research import seat_hypothesis_dated_forward as legacy
    now = datetime.fromisoformat("2026-10-19T19:00:00+08:00")
    monkeypatch.setattr(daily, "_now", lambda: now)
    monkeypatch.setattr(inputs, "_now", lambda: now)
    symbols = ["000001.SZSE", "000003.SZSE", "600001.SSE"]
    close = tmp_path / "close"
    close.mkdir()
    raw = close / "raw_daily.csv"
    raw.write_text("synthetic original primary bytes", encoding="utf-8")
    original_rows = {s: {"open": "10.0000", "high": "10.9000", "low": "9.0000", "close": "10.5000",
        "preclose": "10.0000", "amount": "123456.7000"} for s in symbols}
    monkeypatch.setattr(inputs, "_complete", lambda *a: ({"raw": daily._ref(raw)}, original_rows, []))
    comp = tmp_path / "shared/comparison_source/comparison.csv"
    comp.parent.mkdir(parents=True)
    fields = ["date", "symbol", *inputs.FIELDS]
    with comp.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows({"date": "2026-10-19", "symbol": s,
            **{k: original_rows[s]["preclose" if k == "pre_close" else k] for k in inputs.FIELDS}} for s in symbols)
    response = tmp_path / "shared/comparison_source/response.raw"
    response.write_bytes(b"original synthetic provider response")
    meta = save(comp.parent / "metadata.json", {"schema": inputs.COMPARISON_SCHEMA, "source_family": "Sina",
        "source_id": "synthetic_separate_endpoint", "basis": "raw_unadjusted", "price_unit": "CNY_per_share",
        "amount_unit": "CNY", "day": "2026-10-19", "source_available_at": "2026-10-19T18:00:00+08:00",
        "independence_declared": True, "raw_response": daily._ref(response)})
    audit = tmp_path / "shared/audit.json"
    inputs.build_exact_source_audit(close, comp, meta, audit, decision_day="2026-10-19", symbols=symbols)
    registration = save(tmp_path / "fixed/registration.json", {})
    wanted = [symbols[0], symbols[2]]
    monkeypatch.setattr(legacy, "_registration", lambda *a: {"recipes": {"legacy": {"supported": [{"symbol": s} for s in wanted]}}})
    original = comp.read_bytes(), meta.read_bytes(), response.read_bytes()
    result = daily.project_static_audit(registration, audit, close, "2026-10-19", tmp_path / "projection")
    assert result["status"] == "completed_exact_static_scope_projection"
    assert result["network_accessed"] is False and result["new_quote_request_count"] == 0
    projected = Path(result["comparison"]["file"])
    with projected.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [r["symbol"] for r in rows] == wanted and rows[0]["open"] == "10.0000"
    assert result["original_response"] == daily._ref(response)
    assert result["original_source_available_at"] == "2026-10-19T18:00:00+08:00"
    assert original == (comp.read_bytes(), meta.read_bytes(), response.read_bytes())
    wanted.append("000004.SZSE")
    missing = daily.project_static_audit(registration, audit, close, "2026-10-19", tmp_path / "missing")
    assert missing["missing_symbols"] == ["000004.SZSE"]
    assert missing["status"].startswith("blocked_") and not (tmp_path / "missing/audit.json").exists()
