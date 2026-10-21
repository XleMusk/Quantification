"""Saved per-plan listing prefixes resume real isolated Engine/Ledger books."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from trading import engine, ledger
from trading.contracts import RunContext
from trading.ledger import Ledger
from trading.research import ipo_listing_supplement as listing
from trading.research import shortline_native_preopen as policy
from trading.research import shortline_native_preopen_forward as forward
from trading.research import shortline_native_preopen_status as readonly
from trading.research.baostock_daily_slice import digest
from trading.research.shortline_panel_replay import _config
from trading.research.tests import test_shortline_native_preopen_forward as saved


D, E, F = saved.D, saved.E, saved.F
OLD, NEW = "301569.SZSE", "301570.SZSE"
FALSE_FLAGS = {key: False for key in listing.FALSE_FLAGS}


def _write(file, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")
    return file


def _read(file):
    return json.loads(file.read_text(encoding="utf8"))


def _snapshot(folder):
    return {str(file.relative_to(folder)): (file.read_bytes(), file.stat().st_mtime_ns)
        for file in folder.rglob("*") if file.is_file()}


def _issuer(folder, base, symbol, *, curated, name="SYN"):
    """Real PDF text/receipt/curation, but explicitly synthetic market facts."""
    folder.mkdir()
    pdf = folder / "listing.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(600, 800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data((f"BT /F1 12 Tf 20 700 Td (Synthetic Issuer Ltd; {name}; {symbol[:6]}; ChiNext; {D}) Tj ET").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with pdf.open("wb") as handle:
        writer.write(handle)
    retrieved = (datetime.fromisoformat(curated) - timedelta(hours=1)).isoformat()
    source_id = "synthetic_listing_" + symbol[:6] + "_" + folder.name
    receipt = _write(folder / "receipt.json", {
        "schema": listing.SOURCE_SCHEMA, "status": "downloaded", "source_id": source_id,
        "url": "https://static.cninfo.com.cn/synthetic_" + symbol[:6] + ".pdf",
        "final_url": "https://static.cninfo.com.cn/synthetic_" + symbol[:6] + ".pdf",
        "raw_file": str(pdf), "sha256": digest(pdf), "byte_count": pdf.stat().st_size,
        "http_status": 200, "media_type": "application/pdf", "retrieved_at": retrieved,
        "publication_date": "2026-10-14", "publication_time_known": False,
        "execution_admitted": False, "pit_verified": False, "historical_publication_archive_verified": False})
    return _write(folder / "supplement.json", {
        "schema": listing.SUPPLEMENT_SCHEMA, "created_at": curated,
        "base_evidence_sha256": digest(base), "source_receipt": str(receipt),
        "source_receipt_sha256": digest(receipt), "source_id": source_id,
        "instrument": {"symbol": symbol, "issuer": "Synthetic Issuer Ltd", "name": name,
            "board": "chinext", "ipo_date": D, "available_at": curated,
            "rule_effective_from": "2026-07-06", "first_no_limit_sessions": 5,
            "following_price_limit_pct": 20},
        "source_assertions": {key: {"page_1based": 1, "quote": quote} for key, quote in {
            "issuer": "Synthetic Issuer Ltd", "name": name, "symbol": symbol[:6],
            "board": "ChiNext", "ipo_date": D}.items()}, **FALSE_FLAGS})


def _bundle(folder, base, supplements, *, created):
    return _write(folder / "bundle.json", {"schema": listing.BUNDLE_SCHEMA, "created_at": created,
        "base_evidence": str(base), "base_evidence_sha256": digest(base),
        "supplements": [{"path": str(file), "sha256": digest(file)} for file in supplements],
        **FALSE_FLAGS})


@pytest.fixture
def append_evidence(tmp_path, monkeypatch):
    # Reuse the real tiny native producer, strict bulk/roster and shared Engine
    # setup. Only frozen score arithmetic is substituted; no source/IPO or
    # accounting validator is replaced.
    e = saved.evidence.__wrapped__(tmp_path, monkeypatch)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return e.clock["value"].astimezone(tz) if tz is not None else e.clock["value"]
    monkeypatch.setattr(listing, "datetime", Clock)
    supplements = tmp_path / "listing_sources"
    supplements.mkdir()
    old = _issuer(supplements / "old", e.ipo, OLD, curated=D + "T19:00:00+08:00")
    new = _issuer(supplements / "new", e.ipo, NEW, curated=E + "T19:00:00+08:00")
    anchor = _bundle(supplements / "anchor", e.ipo, [old], created=D + "T19:01:00+08:00")
    extended = _bundle(supplements / "extended", e.ipo, [old, new], created=E + "T19:01:00+08:00")

    def make_plan(bundle, *, second=True, name, master=None):
        index, execution = (1, F) if second else (0, E)
        e.clock["value"] = datetime.fromisoformat(execution + "T03:45:00+08:00")
        original = e.plans[index]
        raw_publication = original.parent.parent / "scores/publication.json"
        out = tmp_path / name
        policy.build_preopen_plan(raw_publication, e.calendar, out, ipo_evidence=bundle,
            research_master_run=e.master if master is None else master)
        e.clock["value"] = datetime.fromisoformat("2026-10-20T20:00:00+08:00")
        return out / "plan.json"

    plans = [make_plan(anchor, second=False, name="qualified_first"),
        make_plan(extended, name="qualified_second")]
    return SimpleNamespace(**vars(e), anchor=anchor, extended=extended,
        supplements=supplements, old_supplement=old, new_supplement=new,
        qualified_plans=plans, qualified_run=tmp_path / "append_book", make_qualified_plan=make_plan)


def _execute(e, *, plans=None, through=None, directory=None, anchor=None, master=None, append=True, days=(D, E, F)):
    return forward.run(e.qualified_plans if plans is None else plans,
        [e.closes[day] for day in days], e.anchor if anchor is None else anchor,
        e.master if master is None else master, e.qualified_run if directory is None else directory,
        through=through, ipo_append_only=append)


def test_each_saved_IPO_prefix_resumes_same_two_books_without_rewriting_prior_journal(append_evidence, monkeypatch):
    e = append_evidence
    loaded = [policy.load_preopen_plan(file) for file in e.qualified_plans]
    prefix = forward.load_qualification_plan_prefix(loaded, e.anchor, e.master, e.calendar)
    assert prefix["inception"]["qualification_inputs"] == loaded[0]["registration"]["qualification_inputs"]
    first_qualification, later_qualification = [entry["qualification"] for entry in prefix["entries"]]
    assert NEW not in first_qualification.records
    assert later_qualification.records[NEW]["available_at"] == E + "T19:00:00+08:00"
    assert later_qualification.assess(NEW, E, decision_as_of=E + "T09:30:00+08:00")["knowledge_status"] == "unknown"
    assert later_qualification.assess(NEW, F, decision_as_of=F + "T09:30:00+08:00")["knowledge_status"] == "known_after_source_availability"
    immutable_sources = _snapshot(e.supplements)
    first = _execute(e, plans=e.qualified_plans[:1], days=(D, E))
    assert first["status"] == "advanced_isolated_native_research_books"
    assert first["newly_committed_case_sessions"] == 2 and first["new_order_count"] == 2
    initial = _read(e.qualified_run / "head.json")
    assert initial["binding"]["method_id"] == forward.IPO_APPEND_METHOD_ID
    assert initial["binding"]["ipo_append_only"] is True
    assert initial["binding"]["rule_spec"] == forward.forward_rule_spec(policy.QUALIFIED_POLICY_ID, ipo_append_only=True)
    old_journals = _snapshot(e.qualified_run / "journal")
    continued = _execute(e)
    assert continued["status"] == "advanced_isolated_native_research_books"
    assert continued["newly_committed_case_sessions"] == 2 and continued["new_order_count"] == 0
    assert continued["metrics"] is None
    final = _read(e.qualified_run / "head.json")
    assert len(final["consumed_plans"]) == len(final["journal_files"]) == 2
    assert final["binding"] == initial["binding"]
    assert [p["qualification_evidence"] for p in final["consumed_plans"]] == [p["proof"] for p in prefix["entries"]]
    for original_file, evidence in old_journals.items():
        assert _snapshot(e.qualified_run / "journal")[original_file] == evidence
    for kind, case in final["cases"].items():
        assert case["context"] == initial["cases"][kind]["context"]
        assert case["state"]["fills"] == initial["cases"][kind]["state"]["fills"]
        assert len(case["state"]["orders"]) == 1 and len(case["state"]["fills"]) == 2
        assert case["state"]["cash"] == 8_397_884 and case["state"]["day"] == F
        book = Ledger(case["state"], _config(100000, 5), RunContext.from_dict(case["context"]))
        assert book.quantity(saved.TARGET[kind]) == book.available(saved.TARGET[kind], F) == 1600
        assert case["daily"][-1]["original_plan_kind"] == "NO_CHANGE"
        assert case["daily"][-1]["qualification_evidence"] == prefix["entries"][1]["proof"]
    for index, journal in enumerate(final["journal_files"]):
        assert _read(Path(journal["file"]))["qualification_evidence"] == prefix["entries"][index]["proof"]
    unchanged = _snapshot(e.qualified_run / "journal")
    head_bytes = (e.qualified_run / "head.json").read_bytes()
    repeated = _execute(e)
    assert repeated["status"] == "already_consumed_original_plan_prefix" and repeated["new_order_count"] == 0
    assert (e.qualified_run / "head.json").read_bytes() == head_bytes
    assert _snapshot(e.qualified_run / "journal") == unchanged
    assert _snapshot(e.supplements) == immutable_sources

    # The status path validates the stored IPO chain but never starts an
    # account, scores, rebuilds a plan, or mutates the saved accounting.
    def forbidden(*args, **kwargs):
        pytest.fail("A read-only IPO append status cannot run accounting or build/score a plan")
    with monkeypatch.context() as patch:
        for module, name in ((forward, "run"), (forward, "advance"), (forward, "new_state"),
                (forward, "Ledger"), (engine, "advance"), (ledger, "Ledger"),
                (policy, "build_preopen_plan"), (saved.scoring, "score_saved_generation"),
                (saved.scoring, "score_day")):
            patch.setattr(module, name, forbidden)
        def read_status():
            return readonly.native_preopen_status(e.root, e.root, policy_id=policy.QUALIFIED_POLICY_ID,
                plan_file=e.qualified_plans[1], run_dir=e.qualified_run,
                observation_file=e.root / "not_run_observation.json", result_file=e.root / "not_run_result.json",
                now=e.clock["value"])
        before_read = _snapshot(e.root)
        view = read_status()
        assert view["status"] == "active_research_books", view.get("error")
        assert view["account_count"] == 2 and view["execution_method_id"] == forward.IPO_APPEND_METHOD_ID
        assert view["ipo_append_only"] is True
        for account in view["accounts"]:
            original_case = final["cases"][account["model_kind"]]
            assert account["cash_cents"] == original_case["state"]["cash"]
            assert account["equity_cents"] == int(Decimal(str(original_case["daily"][-1]["equity"])) * 100)
            assert account["order_count"] == 1 and account["fill_count"] == 2
        assert _snapshot(e.root) == before_read

        # Change both the historical entry and its journal proof, including
        # the journal hash chain. A fresh hash does not authorize replacing
        # the original first plan's IPO prefix by its later descendant.
        originals = {e.qualified_run / "head.json": ((e.qualified_run / "head.json").read_bytes(),
            (e.qualified_run / "head.json").stat().st_mtime_ns)}
        corrupted = deepcopy(final)
        corrupted["consumed_plans"][0]["qualification_evidence"] = deepcopy(prefix["entries"][1]["proof"])
        previous = None
        for index, reference in enumerate(corrupted["journal_files"]):
            file = Path(reference["file"])
            originals[file] = (file.read_bytes(), file.stat().st_mtime_ns)
            journal = _read(file)
            journal["plan"] = deepcopy(corrupted["consumed_plans"][index])
            journal["qualification_evidence"] = deepcopy(corrupted["consumed_plans"][index]["qualification_evidence"])
            journal["previous_journal_sha256"] = previous
            _write(file, journal)
            reference.update(sha256=digest(file), byte_count=file.stat().st_size)
            previous = reference["sha256"]
        _write(e.qualified_run / "head.json", corrupted)
        try:
            corrupted_before_read = _snapshot(e.root)
            rejected = read_status()
            assert rejected["status"] == "unknown_saved_evidence" and rejected["accounts"] == []
            assert rejected["fallback_to_older_success"] is False
            assert _snapshot(e.root) == corrupted_before_read
        finally:
            for file, (data, mtime) in originals.items():
                file.write_bytes(data)
                os.utime(file, ns=(file.stat().st_atime_ns, mtime))


def test_changed_old_listing_facts_proofs_clocks_or_plan_prefix_reject_before_account_mutation(append_evidence, monkeypatch):
    e = append_evidence
    _execute(e, plans=e.qualified_plans[:1], days=(D, E))
    initial = _snapshot(e.qualified_run)
    bad_plans = []
    for label, name, curated in (
        ("old_issuer", "CHANGED", D + "T19:00:00+08:00"),
        ("old_clock", "SYN", D + "T20:00:00+08:00"),
        ("old_proof_new_shell", "SYN", D + "T19:00:00+08:00")):
        replacement = _issuer(e.supplements / label, e.ipo, OLD, curated=curated, name=name)
        bundle = _bundle(e.supplements / (label + "_bundle"), e.ipo,
            [replacement, e.new_supplement], created=E + "T19:01:00+08:00")
        bad_plans.append((label, e.make_qualified_plan(bundle, name=label + "_plan")))
    deleted = _bundle(e.supplements / "deleted_old", e.ipo, [e.new_supplement],
        created=E + "T19:01:00+08:00")
    bad_plans.append(("deleted_old_issuer", e.make_qualified_plan(deleted, name="deleted_plan")))
    def no_advance(*args, **kwargs):
        pytest.fail("Changed qualification inputs must reject before account initialization or Engine mutation")
    with monkeypatch.context() as patch:
        patch.setattr(forward, "advance", no_advance)
        patch.setattr(forward, "new_state", no_advance)
        for label, plan in bad_plans:
            with pytest.raises(ValueError):
                _execute(e, plans=[e.qualified_plans[0], plan])
            assert _snapshot(e.qualified_run) == initial, label
        replacement_master = e.master.with_name("another_master_run.json")
        altered = _read(e.master)
        altered["changed_input_identity"] = True
        _write(replacement_master, altered)
        with pytest.raises(ValueError):
            _execute(e, master=replacement_master)
        assert _snapshot(e.qualified_run) == initial
        with pytest.raises(ValueError):
            _execute(e, anchor=e.extended)
        assert _snapshot(e.qualified_run) == initial
    _execute(e)
    final = _snapshot(e.qualified_run)
    with monkeypatch.context() as patch:
        patch.setattr(forward, "advance", no_advance)
        patch.setattr(forward, "new_state", no_advance)
        with pytest.raises(ValueError):
            _execute(e, plans=e.qualified_plans[:1])
        assert _snapshot(e.qualified_run) == final
        original_plan = e.qualified_plans[0].read_bytes()
        old = _read(e.qualified_plans[0])
        old["registered_at"] = E + "T03:44:59+08:00"
        _write(e.qualified_plans[0], old)
        try:
            with pytest.raises(ValueError):
                _execute(e)
            assert _snapshot(e.qualified_run) == final
        finally:
            e.qualified_plans[0].write_bytes(original_plan)


def test_append_method_requires_qualified_inception_and_cannot_upgrade_a_fixed_bundle_book(append_evidence, monkeypatch):
    e = append_evidence
    unqualified = e.root / "cannot_accept_legacy_plan"
    with pytest.raises(ValueError):
        _execute(e, plans=e.plans[:1], directory=unqualified, days=(D, E))
    assert not unqualified.exists()
    fixed = e.root / "original_fixed_qualification_book"
    _execute(e, plans=e.qualified_plans[:1], directory=fixed, append=False, days=(D, E))
    before = _snapshot(fixed)
    def forbidden(*args, **kwargs):
        pytest.fail("An established fixed method cannot silently acquire the append method")
    with monkeypatch.context() as patch:
        patch.setattr(forward, "advance", forbidden)
        patch.setattr(forward, "new_state", forbidden)
        with pytest.raises(ValueError):
            _execute(e, plans=e.qualified_plans[:1], directory=fixed, days=(D, E))
    assert _snapshot(fixed) == before


def test_CLI_export_cannot_overwrite_a_later_plans_listing_bundle_or_primary_source_directory(append_evidence, monkeypatch):
    e = append_evidence
    def forbidden(*args, **kwargs):
        pytest.fail("Protected output must be rejected before the runner is called")
    monkeypatch.setattr(forward, "run", forbidden)
    run = e.root / "cli_must_not_begin"
    args = ["--plan", str(e.qualified_plans[0]), "--plan", str(e.qualified_plans[1]),
        "--raw-close", str(e.closes[D]), "--raw-close", str(e.closes[E]),
        "--raw-close", str(e.closes[F]), "--ipo-proof", str(e.anchor),
        "--research-master-run", str(e.master), "--run", str(run), "--ipo-append-only"]
    before = _snapshot(e.root)
    for target in (e.extended, e.new_supplement.parent / "receipt.json",
            e.new_supplement.parent / "must_not_create_beside_primary_PDF.json"):
        with pytest.raises(ValueError):
            forward.main([*args, "--result-json", str(target)])
        assert _snapshot(e.root) == before
        assert not run.exists()
