"""Append consumer integration: synthetic local sources, real Engine/Ledger.

The catalog loader is a tiny validated-shape adapter.  Its real loader has a
separate source suite; real catalog ancestry, dated publishing, registration,
strict raw feeds, four-account execution and journal recovery are exercised.
No market source, real account, production registration or worker is touched.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading import engine
from trading.contracts import TradingMode
from trading.research import seat_append_forward as forward
from trading.research import seat_append_dated_inputs as dated
from trading.research import seat_append_registration as registration
from trading.research import seat_disclosure_catalog as catalog
from trading.research import seat_hypothesis_dated_inputs as inputs
from trading.research.tests.fixtures.ipo_qualification import write_ipo_fixture
from trading.research.tests import test_shortline_native_preopen_forward as raw_fixture
from test_seat_hypothesis_dated_inputs import evidence as input_evidence, save, read, A, B, D, F


G, H = "2026-10-20", "2026-10-21"


@pytest.fixture
def evidence(input_evidence, monkeypatch):
    e = input_evidence
    monkeypatch.setattr(engine, "advance", forward.advance)
    monkeypatch.setattr(engine, "_published_now", lambda: e.clock["value"])
    monkeypatch.setattr(forward, "_now", lambda: e.clock["value"])
    _, recipes, refs = inputs._registered(e.registration, e.clock["value"])
    cdir = e.inputs / "catalog"
    cdir.mkdir()
    versions = {}
    for index in (1, 2):
        file = cdir / f"catalog_v{index}.json"
        save(file, {"validated_shape_synthetic_fixture": True, "version": index})
        versions[str(file.resolve())] = {"reference": inputs._ref(file),
            "record": {"batches": ["source15"] + (["empty16"] if index == 2 else [])},
            "batches": [{"batch_id": "source15", "event_day": "2026-10-15", "status": "completed_nonempty",
                "source_available_at": D + "T05:36:00+08:00", "recipes": deepcopy(recipes)}],
            "references": [inputs._ref(file), *refs], "files": [file], "packages": [cdir],
            "created_at": D + ("T06:00:00+08:00" if index == 1 else "T08:00:00+08:00")}
    c1, c2 = (cdir / f"catalog_v{i}.json" for i in (1, 2))
    first = versions[str(c1.resolve())]
    second = versions[str(c2.resolve())]
    first["lineage_refs"] = [first["reference"]]
    second["lineage_refs"] = [first["reference"], second["reference"]]
    second["references"] = [first["reference"], *second["references"]]
    second["batches"].append({"batch_id": "empty16", "event_day": D, "status": "completed_empty",
        "source_available_at": D + "T07:30:00+08:00",
        "recipes": {recipe: {"candidates": [], "supported": [], "clocks": {}, "panel_observations": [],
            "input_refs": []} for recipe in ("legacy_repeated_sum", "stockday_unique")}})
    def load(path, now=None):
        value = versions[str(Path(path).resolve())]
        for ref in value["references"]:
            inputs._verify(ref)
        return deepcopy(value)
    monkeypatch.setattr(catalog, "load_catalog", load)
    monkeypatch.setattr(dated, "_now", lambda: e.clock["value"])
    e.clock["value"] = datetime.fromisoformat(D + "T07:00:00+08:00")
    idir = e.root / "independent_method"
    idir.mkdir()
    ireg = idir / "inputs.json"
    registration.register_inputs(e.registration, c1, e.calendar, D, ireg)
    ipo_dir = e.inputs / "ipo"
    ipo_dir.mkdir()
    ipo, master = write_ipo_fixture(ipo_dir)
    master_record = read(master)
    pd.DataFrame([{"canonical_symbol": symbol, "provider_ipo_date": "2026-01-02", "board": "main"}
        for symbol in (A, B)]).to_csv(ipo_dir / master_record["master_file"], index=False)
    master_record["master_sha256"] = inputs._ref(ipo_dir / master_record["master_file"])["sha256"]
    save(master, master_record)
    e.clock["value"] = datetime.fromisoformat(D + "T07:05:00+08:00")
    freg = e.root / "independent_forward_method" / "forward.json"
    registration.register_forward(ireg, ipo, master, freg)
    original_rows = raw_fixture.rows
    def rows(day):
        if day not in (G, H):
            return original_rows(day)
        values = original_rows(F)
        for row in values:
            row["date"] = day
            before = 10. if row["code"] == "sh.600099" else 12.1 if day == G else 12.2
            close = 10. if row["code"] == "sh.600099" else 12.2
            row.update(open=str(before), close=str(close), preclose=str(before),
                high=str(max(before, close) + .1), low=str(min(before, close) - .1),
                amount=str((before + close) / 2 * 1000000), pctChg=str((close / before - 1) * 100))
        return values
    monkeypatch.setattr(raw_fixture, "rows", rows)
    sdk = {"wheel": str(e.inputs / "synthetic.whl"),
        "sha256": inputs._ref(e.inputs / "synthetic.whl")["sha256"], "distribution_version": "0.9.4"}
    closes = {**e.closes, **{day: raw_fixture.strict_close(e.inputs / ("close_" + day), day, e.calendar, sdk)
        for day in (G, H)}}
    def publish(day, version):
        e.clock["value"] = datetime.fromisoformat(day + "T20:00:00+08:00")
        registered = dated.load_registration(ireg)
        arms = dated.mature_arms(load(version), registered["sessions"], day)
        required = dated.required_audit_symbols(arms)
        compare_dir = e.inputs / ("comparison_append_" + day)
        compare_dir.mkdir()
        csv = compare_dir / "prices.csv"
        raw = pd.read_csv(closes[day] / "raw_daily.csv", dtype=str, keep_default_na=False)
        raw["symbol"] = raw.code.str.split(".").str[1] + ".SSE"
        raw.rename(columns={"preclose": "pre_close"}).loc[raw.symbol.isin(required),
            ["date", "symbol", *inputs.FIELDS]].to_csv(csv, index=False)
        metadata = compare_dir / "source.json"
        save(metadata, {**read(e.metadata), "day": day, "source_available_at": day + "T18:45:00+08:00"})
        audit = e.inputs / ("append_audit_" + day + ".json")
        inputs.build_exact_source_audit(closes[day], csv, metadata, audit, decision_day=day, symbols=required)
        out = e.root / ("append_publication_" + day)
        dated.build_dated_inputs(ireg, version, e.calendar, closes[day], audit, out, decision_day=day)
        return out / "publication.json"
    first_pub = publish(D, c1)
    e.clock["value"] = datetime.fromisoformat(F + "T20:00:00+08:00")
    return SimpleNamespace(base=e, registration=freg, input_registration=ireg,
        catalogs=[c1, c2], loaded=versions, publications=[first_pub], closes=closes,
        publish=publish, run_dir=e.root / "append_four_books", clock=e.clock, ipo=ipo, master=master)


def execute(e, *, publications=None, days=(D, F), registration_file=None, through=None):
    return forward.run(e.publications if publications is None else publications,
        [e.closes[day] for day in days], e.registration if registration_file is None else registration_file,
        e.run_dir, through=through)


def test_real_Engine_append_resume_idempotency_and_published_NO_CHANGE_preserves_holdings(evidence):
    e = evidence
    original = {file: file.read_bytes() for file in (e.base.registration, e.input_registration, e.catalogs[0])}
    result = execute(e)
    assert result["research_accounts_created"] == 4 and result["newly_committed_execution_sessions"] == 1
    assert [case["fill_count"] for case in result["cases"]] == [4, 0, 4, 0]
    assert all(case["context"]["mode"] == TradingMode.REPLAY.value and case["metrics"] is None for case in result["cases"])
    head_file = e.run_dir / "head.json"
    before = head_file.read_bytes()
    duplicate = execute(e)
    assert duplicate["new_order_count"] == 0 and duplicate["newly_committed_execution_sessions"] == 0
    assert head_file.read_bytes() == before
    e.publications.append(e.publish(F, e.catalogs[1]))
    e.clock["value"] = datetime.fromisoformat(G + "T20:00:00+08:00")
    result = execute(e, days=(D, F, G))
    assert result["newly_committed_execution_sessions"] == 1 and result["research_accounts_created"] == 0
    assert [case["fill_count"] for case in result["cases"]] == [4, 4, 4, 4]
    before_NC = read(head_file)
    assert before_NC["consumed_catalog_prefixes"] == [inputs._ref(p) for p in e.catalogs]
    e.publications.append(e.publish(G, e.catalogs[1]))
    e.clock["value"] = datetime.fromisoformat(H + "T20:00:00+08:00")
    result = execute(e, days=(D, F, G, H))
    after_NC = read(head_file)
    assert result["new_order_count"] == 0 and result["newly_committed_execution_sessions"] == 1
    for arm in inputs.ARMS:
        for field in ("cash", "positions", "orders", "fills"):
            assert before_NC["cases"][arm]["state"][field] == after_NC["cases"][arm]["state"][field]
        assert after_NC["cases"][arm]["state"]["day"] == H
    assert len(after_NC["journal_files"]) == 3
    assert all(file.read_bytes() == data for file, data in original.items())


def test_fourth_real_Engine_trial_failure_discards_all_before_first_commit(evidence, monkeypatch):
    e = evidence
    calls, real = [], forward.advance
    def fail(state, feed, day, config, session_number, **kwargs):
        row = real(state, feed, day, config, session_number, **kwargs)
        calls.append(kwargs["context"].account_id)
        if len(calls) == 4:
            raise ValueError("synthetic fourth trial failure after real Engine advance")
        return row
    monkeypatch.setattr(forward, "advance", fail)
    result = execute(e)
    assert len(calls) == 4
    assert result["status"] == "failed_four_arm_trial_no_commit" and result["all_four_trials_discarded"]
    assert result["account_count"] == 0 and result["new_order_count"] == 0 and not e.run_dir.exists()


def test_fourth_trial_failure_on_resume_preserves_existing_head_and_journal(evidence, monkeypatch):
    e = evidence
    execute(e)
    original = {file: file.read_bytes() for file in e.run_dir.rglob("*.json")}
    e.publications.append(e.publish(F, e.catalogs[1]))
    e.clock["value"] = datetime.fromisoformat(G + "T20:00:00+08:00")
    calls, real = [], forward.advance
    def fail(state, feed, day, config, session_number, **kwargs):
        row = real(state, feed, day, config, session_number, **kwargs)
        calls.append(kwargs["context"].account_id)
        if len(calls) == 4:
            raise ValueError("synthetic fourth resumed trial failure after actual Engine advance")
        return row
    monkeypatch.setattr(forward, "advance", fail)
    result = execute(e, days=(D, F, G))
    assert len(calls) == 4 and result["status"] == "failed_four_arm_trial_no_commit"
    assert result["account_count"] == 4 and result["research_accounts_created"] == 0
    assert result["newly_committed_execution_sessions"] == 0 and result["new_order_count"] == 0
    assert set(e.run_dir.rglob("*.json")) == set(original)
    assert all(file.read_bytes() == data for file, data in original.items())


@pytest.mark.parametrize("change", ["old_registration", "skip_pub", "bad_catalog", "source", "journal", "catalog_prefix", "head_prefix"])
def test_original_registration_source_and_committed_prefix_rejections(evidence, change):
    e = evidence
    selected_registration = None
    if change == "old_registration":
        selected_registration = e.base.registration
    elif change == "skip_pub":
        e.publications = [e.publish(F, e.catalogs[1])]
    elif change == "bad_catalog":
        e.publications.append(e.publish(F, e.catalogs[1]))
        e.loaded[str(e.catalogs[1].resolve())]["lineage_refs"] = [inputs._ref(e.catalogs[1])]
    elif change == "source":
        file = e.closes[D] / "raw_daily.csv"
        file.write_bytes(file.read_bytes() + b"\n")
    else:
        execute(e)
        head_file = e.run_dir / "head.json"
        head = read(head_file)
        if change == "journal":
            file = Path(head["journal_files"][0]["file"])
            journal = read(file)
            journal["publication"]["execution_day"] = G
            save(file, journal)
            head["journal_files"][0] = inputs._ref(file)
        elif change == "catalog_prefix":
            head["consumed_catalog_prefixes"] = [inputs._ref(e.catalogs[1])]
        else:
            head["raw_sources"][0]["raw_sha256"] = "0" * 64
        save(head_file, head)
    with pytest.raises(ValueError):
        execute(e, registration_file=selected_registration)
    if change in {"old_registration", "skip_pub", "bad_catalog", "source"}:
        assert not e.run_dir.exists()


def test_registration_helper_rejects_past_first_day_and_old_method_without_creating_output(evidence):
    e = evidence
    out = e.base.root / "invalid_registration" / "inputs.json"
    with pytest.raises(ValueError, match="Prospective first D"):
        registration.register_inputs(e.base.registration, e.catalogs[0], e.base.calendar, D, out)
    assert not out.exists()
    other = e.base.root / "invalid_forward" / "forward.json"
    with pytest.raises(ValueError, match="Exact independent append input registration"):
        registration.register_forward(e.base.registration, e.ipo, e.master, other)
    assert not other.exists()


def test_reference_mismatch_preserves_unknown_without_account_or_fake_fill(evidence):
    e = evidence
    sdk = {"wheel": str(e.base.inputs / "synthetic.whl"),
        "sha256": inputs._ref(e.base.inputs / "synthetic.whl")["sha256"], "distribution_version": "0.9.4"}
    other = raw_fixture.strict_close(e.base.inputs / "changed_reference", F, e.base.calendar, sdk,
        reference_override={A: 10.5})
    result = forward.run(e.publications, [e.closes[D], other], e.registration, e.run_dir)
    assert result["status"] == "blocked_exact_reference_raw_or_actual_IPO_knowledge"
    assert any("E_reference_differs_from_D_close_action_evidence_unresolved" in issue["reasons"]
        for arm in result["blocking_evidence"].values() for issue in arm["issues"])
    assert not e.run_dir.exists() and result["new_order_count"] == 0 and result["metrics"] is None
