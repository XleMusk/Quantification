"""Real scoped-action loader and Engine/Ledger with tiny synthetic sources.

No market history, provider, model, user account, worker or UI is run here.
The known publication/source/IPO fixture substitutes remain explicit.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pandas as pd
import pytest

from trading.contracts import RunContext
from trading.ledger import Ledger
from trading.research import native_lowvol_forward as forward
from trading.research.tests.test_native_lowvol_forward import (
    D, E, F, fixture, head, read, ref, save,
)
from trading.research.tests.test_recent_action_extension import fixture_extension, pin, write
from trading.research.tests.test_stockdb_action_accounting import SYMBOL, DAYS


UNUSED = "600001.SSE"


@pytest.fixture
def actual_extension_fixture(fixture):
    e = fixture
    (e.root / "calendars").mkdir()
    calendar = e.root / "calendars" / "calendar.json"
    calendar.write_bytes(e.calendar.read_bytes())
    e.calendar = calendar
    publications = e.root / "publications"
    publications.mkdir()
    first = publications / e.first.name
    e.first.replace(first)
    e.publications[0] = e.first = first
    initial = read(first)
    initial.update(weights={SYMBOL: .2}, calendar_ref=ref(calendar))
    save(first, initial)
    original_publish = e.publish
    def publish(*args, **kwargs):
        temporary = original_publish(*args, **kwargs)
        record = read(temporary)
        record["calendar_ref"] = ref(calendar)
        target = publications / temporary.name
        temporary.replace(target)
        save(target, record)
        e.publications[-1] = target
        return target
    e.publish = publish
    for source in e.raw.values():
        active = source["canonical"].copy()
        extra = active.copy()
        active["symbol"] = SYMBOL
        extra["symbol"] = UNUSED
        source["canonical"] = pd.concat([active, extra], ignore_index=True)

    event, reference, old_file, old_bundle = fixture_extension(e.root / "base_evidence")
    # The base scope contains another original symbol, but the first fresh
    # extension deliberately queries only the account's actual first target.
    universe_path, coverage_path = event / "source_input_universe.csv", event / "input_coverage.csv"
    universe = pd.read_csv(universe_path, dtype=str, keep_default_na=False)
    universe = pd.concat([universe, pd.DataFrame([{"code": "sh.600001", "symbol": UNUSED}])], ignore_index=True)
    universe.to_csv(universe_path, index=False)
    coverage = pd.read_csv(coverage_path, dtype=str, keep_default_na=False)
    empty = coverage.iloc[0].copy()
    empty.update({"code": "sh.600001", "symbol": UNUSED, "rows": "0", "empty_response": "True"})
    pd.concat([coverage, empty.to_frame().T], ignore_index=True).to_csv(coverage_path, index=False)
    events = read(event / "result.json")
    events.update(expected_codes=2, queried=2)
    for file in (universe_path, coverage_path):
        events["files"][file.name] = pin(file)["sha256"]
    write(event / "result.json", events)
    (reference / "input_events.json").write_bytes((event / "result.json").read_bytes())
    references = read(reference / "result.json")
    references["files"]["input_events.json"] = pin(reference / "input_events.json")["sha256"]
    write(reference / "result.json", references)
    old_reported = pd.read_parquet(event / "input_reported_actions.parquet").to_dict("records")
    original_action = deepcopy(old_bundle["actions"][0])
    original_action.update(dividRegistDate=E, dividOperateDate=F, dividPayDate=F)
    original_evidence = deepcopy(old_bundle["reference_evidence"][0])
    original_evidence.update(dividRegistDate=E, dividOperateDate=F, dividPayDate=F)

    def bundle(end, name, *, revision=False, add_unused=False):
        directory = e.root / name
        root = directory / "provider"
        (root / "receipts").mkdir(parents=True)
        action, evidence = deepcopy(original_action), deepcopy(original_evidence)
        if revision:
            action["dividCashPsBeforeTax"] = evidence["dividCashPsBeforeTax"] = "0.06"
            evidence["reference_adjustment_cash_ps"] = "0.06"
        actions, evidences = [action], [evidence]
        scope = [SYMBOL]
        if add_unused:
            scope.append(UNUSED)
            other = deepcopy(action)
            other.update(code="sh.600001", symbol=UNUSED, dividRegistDate=D,
                dividOperateDate=E, dividPayDate=E)
            proof = deepcopy(evidence)
            proof.update(code="sh.600001", symbol=UNUSED, dividRegistDate=D,
                dividOperateDate=E, dividPayDate=E)
            actions.append(other)
            evidences.append(proof)
        sessions = [day.strftime("%Y-%m-%d") for day in pd.bdate_range("2026-06-26", end)
            if not "2026-10-01" <= day.strftime("%Y-%m-%d") <= "2026-10-07"]
        pd.DataFrame({"date": DAYS + sessions}).to_csv(root / "calendar.csv", index=False)
        pd.DataFrame([{"symbol": symbol, "code": "sh." + symbol[:6]} for symbol in scope]).to_csv(root / "universe.csv", index=False)
        queries, count = [], 0
        fields = list(old_reported[0])
        for symbol, item in zip(scope, actions):
            payload = root / "actions" / symbol / "2026.csv"
            payload.parent.mkdir(parents=True)
            rows = [row for row in old_reported if row["symbol"] == symbol]
            raw = {key: item[key] for key in fields}
            raw["dividReserveToStockPs"] = ""
            rows.append(raw)
            pd.DataFrame(rows, columns=fields).to_csv(payload, index=False)
            count += len(rows)
            task = {"path": payload.relative_to(root).as_posix(), "retrieved_at": end + "T18:00:00+08:00",
                "quality": {"rows": len(rows)}, "sha256": pin(payload)["sha256"], "source_error_code": "0"}
            receipt = root / "receipts" / (symbol + ".json")
            write(receipt, {"symbol": symbol, "tasks": {"actions_2026": task}})
            queries.append({"symbol": symbol, "year": 2026, "receipt": pin(receipt), "payload": pin(payload)})
        write(root / "collection_plan.json", {"source": "BaoStock", "summary_stage": "actions", "end": end,
            "symbols": len(scope), "expected_queries": len(scope), "universe_sha256": pin(root / "universe.csv")["sha256"],
            "calendar_sha256": pin(root / "calendar.csv")["sha256"]})
        write(root / "status.json", {"status": "source_cache_complete", "error": None, "summary_stage": "actions",
            "end": end, "complete_symbols": len(scope), "action_queries": len(scope), "action_rows": count})
        value = {**deepcopy(old_bundle), "created_at": end + "T18:01:00+08:00",
            "base_event": pin(event / "result.json"), "base_reference": pin(reference / "result.json"),
            "calendar": pin(calendar), "window_start": "2026-06-26", "window_end": end,
            "scope_symbols": scope, "provider_root": str(root), "queries": queries,
            "provider_files": {key: pin(root / key) for key in ("collection_plan.json", "universe.csv", "calendar.csv", "status.json")},
            "actions": actions, "reference_evidence": evidences}
        path = directory / "bundle.json"
        write(path, value)
        return path
    e.event, e.reference, e.make_bundle = event, reference, bundle
    e.extension_first = bundle(E, "extension_first")
    e.extension_next = bundle(F, "extension_next")
    # A reviewed cash-only ex-reference is independent of unchanged native
    # rows. It is not obtained by forcing old source fields to agree.
    mask = e.raw[F]["canonical"].symbol.eq(SYMBOL)
    e.raw[F]["canonical"].loc[mask, ["open", "close", "preclose", "preclose_for_limits"]] = 9.95
    return e


def execute(e, extension, *, days=(D, E, F), through=None):
    return forward.run(e.publications, list(days), "fixture_ipo", "fixture_master",
        e.event, e.reference, e.run_dir, through=through, recent_action_extension=extension)


def first(e):
    result = execute(e, e.extension_first, days=(D, E))
    assert result["status"] == "advanced_independent_native_lowvol_research_account", result
    return head(e)


def test_real_extension_first_book_cash_only_update_and_interrupted_recovery_no_old_entitlements(actual_extension_fixture, monkeypatch):
    e = actual_extension_fixture
    initial = first(e)
    quantity = Ledger(initial["state"], e.config, RunContext.from_dict(initial["context"])).quantity(SYMBOL)
    assert quantity > 0 and initial["state"]["entitlements"] == {f"{SYMBOL}:{F}": quantity}
    assert initial["action_scope"] == [SYMBOL]  # Not every symbol in the raw source.
    assert len(initial["state"]["fills"]) > 0
    assert f"{SYMBOL}:2026-06-24" not in initial["state"]["entitlements"]
    inception = initial["binding"]["recent_action_extension"]["inception_extension"]
    assert inception == pin(e.extension_first)
    e.publish(E, F, account=initial)
    before_head = (e.run_dir / "head.json").read_bytes()
    writer = forward.write_json
    def crash(path, value):
        if Path(path).name == "head.json":
            raise OSError("synthetic interruption after new extension session journal")
        return writer(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(forward, "write_json", crash)
        with pytest.raises(OSError, match="synthetic interruption"):
            execute(e, e.extension_next)
    assert (e.run_dir / "head.json").read_bytes() == before_head
    journal_file = e.run_dir / "journal" / (F + ".json")
    journal_bytes = journal_file.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(forward, "advance", lambda *args, **kwargs: pytest.fail("Do not execute the durable session twice"))
        recovered = execute(e, e.extension_next)
    final = head(e)
    assert recovered["newly_committed_sessions"] == 1 and recovered["new_order_count"] == 0
    assert journal_file.read_bytes() == journal_bytes
    assert final["binding"] == initial["binding"]
    assert final["latest_recent_action_extension"] == pin(e.extension_next)
    assert final["state"]["cash"] - initial["state"]["cash"] == quantity * 4
    assert final["state"]["positions"] == initial["state"]["positions"]
    assert final["state"]["fills"] == initial["state"]["fills"]
    before = (e.run_dir / "head.json").read_bytes()
    again = execute(e, e.extension_next)
    assert again["status"] == "already_consumed_original_publication_prefix"
    assert (e.run_dir / "head.json").read_bytes() == before
    assert recovered["returns_reportable"] is recovered["execution_admitted"] is False
    assert recovered["recent_action_method"] == forward.RECENT_ACTION_METHOD
    original_payload = Path(read(e.extension_first)["queries"][0]["payload"]["path"])
    original_payload.write_bytes(original_payload.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="Previously consumed action evidence changed"):
        execute(e, e.extension_next)
    assert (e.run_dir / "head.json").read_bytes() == before


@pytest.mark.parametrize("first_opt_in", [False, True])
def test_fixed_default_and_explicit_extension_methods_cannot_upgrade_or_disable_existing_head(actual_extension_fixture, first_opt_in):
    e = actual_extension_fixture
    result = execute(e, e.extension_first if first_opt_in else None, days=(D, E))
    assert result["status"] == "advanced_independent_native_lowvol_research_account", result
    before = (e.run_dir / "head.json").read_bytes()
    with pytest.raises(ValueError, match="method/context/config cannot change"):
        execute(e, None if first_opt_in else e.extension_first, days=(D, E))
    assert (e.run_dir / "head.json").read_bytes() == before


def test_fresh_window_insufficient_and_revised_consumed_recent_action_do_not_commit(actual_extension_fixture, monkeypatch):
    e = actual_extension_fixture
    initial = first(e)
    e.publish(E, F, account=initial)
    before = (e.run_dir / "head.json").read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(forward.recent_actions, "load_extension", lambda *args, **kwargs: pytest.fail("Old saved window must not call the full loader"))
        waiting = execute(e, e.extension_first)
    assert waiting["status"] == "waiting_for_exact_recent_action_extension"
    assert waiting["missing_action_sessions"] == [F] and waiting["newly_committed_sessions"] == 0
    assert (e.run_dir / "head.json").read_bytes() == before
    changed = e.make_bundle(F, "extension_revised", revision=True)
    rejected = execute(e, changed)
    assert rejected["status"] == "failed_required_evidence_or_engine_session_no_commit", rejected
    assert "already relevant economic action prefix" in rejected["error"]
    assert (e.run_dir / "head.json").read_bytes() == before
    assert not (e.run_dir / "journal" / (F + ".json")).exists()


def test_first_exposure_to_original_unheld_symbol_does_not_backfill_old_cash_rights(actual_extension_fixture):
    e = actual_extension_fixture
    initial = first(e)
    assert UNUSED not in initial["action_scope"]
    extension = e.make_bundle(F, "extension_added_symbol", add_unused=True)
    e.publish(E, F, {SYMBOL: .1, UNUSED: .1}, account=initial)
    result = execute(e, extension)
    assert result["status"] == "advanced_independent_native_lowvol_research_account", result
    final = head(e)
    book = Ledger(final["state"], e.config, RunContext.from_dict(final["context"]))
    assert book.quantity(UNUSED) > 0
    assert final["action_scope"] == [SYMBOL, UNUSED]
    assert f"{UNUSED}:{E}" not in final["state"]["entitlements"]
    assert not any(event.get("symbol") == UNUSED and event["kind"] in {"dividend_accrued", "dividend_paid"}
        for event in final["state"]["events"])
    assert any(action["symbol"] == UNUSED for action in final["action_prefix"])
    assert all(action["symbol"] == SYMBOL for action in initial["action_prefix"])


def test_CLI_protects_indirect_provider_and_official_source_directories(actual_extension_fixture, monkeypatch):
    e = actual_extension_fixture
    value = read(e.extension_first)
    protected = [Path(value["provider_root"]) / "receipts", Path(value["actions"][0]["source_file"]).parent]
    monkeypatch.setattr(forward, "run", lambda *args, **kwargs: pytest.fail("Protection must precede account work"))
    for index, directory in enumerate(protected):
        out = directory / (f"unrelated_new_output_{index}.json")
        argv = ["--publication", str(e.first), "--raw-close", D, "--raw-close", E,
            "--ipo-proof", str(e.root / "ipo_inputs" / "bundle.json"),
            "--master-run", str(e.root / "master_inputs" / "run.json"),
            "--action-event-view", str(e.event), "--action-reference-view", str(e.reference),
            "--run-dir", str(e.run_dir), "--recent-action-extension", str(e.extension_first),
            "--through", E, "--out", str(out)]
        with pytest.raises(ValueError, match="outside accounts and source packages"):
            forward.main(argv)
        assert not out.exists()
