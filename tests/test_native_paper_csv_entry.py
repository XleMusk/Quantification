"""CSV entry uses the real parser, NativeQuoteFeed and two-model PAPER core."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading import intraday_session, native_intraday_feed as quote_feed
from trading import native_intraday_paper as paper
from trading.contracts import RunContext
from trading.ledger import Ledger
from trading.tests.test_native_intraday_paper import inputs, read, ref
from trading.tests.test_published_close_intent import A, B, CONFIG, D, E, F


SOURCE = "synthetic-public-export-with-explicit-status"


def _stamp(value):
    return datetime.fromisoformat(value)


@pytest.fixture
def csv_inputs(inputs, tmp_path, monkeypatch):
    original_prepare = paper.prepare_inputs
    actual_import = quote_feed.import_quote
    prepared_calls, import_calls = [], []

    def prepare(plan, prior_package, *, ipo_proof=None, master_run=None, now=None):
        prepared_calls.append((Path(plan), Path(prior_package)))
        base = original_prepare(plan, prior_package, ipo_proof=ipo_proof, master_run=master_run, now=now)
        item = read(plan)
        loaded = {**base.loaded, "decisions": {
            kind: {"weights": weights} for kind, weights in item["weights"].items()}}
        raw = dict(open="10.00", high="10.10", low="9.90", close="10.00",
                   preclose="10.00", volume="1000000", amount="10000000",
                   tradestatus=1, isST=0, adjustflag="3")

        def assess(symbol, day, *, decision_as_of):
            assert symbol in (A, B) and decision_as_of == day + "T09:30:00+08:00"
            return {"engine_block_reason": None}

        qualification = SimpleNamespace(
            records={symbol: {"ipo_date": "2000-01-01"} for symbol in (A, B)}, assess=assess)
        return quote_feed.NativeInputs(
            loaded, base.config, base.calendar, base.proof, base.input_paths,
            {"original_rows": {symbol: dict(raw) for symbol in (A, B)},
             "canonical_rows": {symbol: dict(raw) for symbol in (A, B)}},
            qualification, now,
        )

    def counted_import(*args, **kwargs):
        import_calls.append((args, kwargs.copy()))
        return actual_import(*args, **kwargs)

    monkeypatch.setattr(paper, "prepare_inputs", prepare)
    monkeypatch.setattr(quote_feed, "import_quote", counted_import)
    csv_folder, quote_folder = tmp_path / "raw_export", tmp_path / "saved_quotes"
    csv_folder.mkdir()

    def csv(name, *, phase="opening", is_st="0", tradable="1"):
        path = csv_folder / (name + ".csv")
        prices = ("10.00", "10.00") if phase == "opening" else ("11.00", "10.50")
        path.write_text(
            "symbol,market,price_rmb,preclose_rmb,is_st,tradable\n"
            f"600000,SSE,{prices[0]},10.00,{is_st},{tradable}\n"
            f"000001,SZSE,{prices[1]},10.00,{is_st},{tradable}\n", encoding="utf-8")
        return path

    opening_csv = csv("opening")
    closing_csv = csv("closing", phase="closing")

    def call(phase, **changes):
        moment = E + ("T09:30:10+08:00" if phase == "opening" else "T15:05:00+08:00")
        options = dict(
            run_dir=inputs.run_dir, plan=inputs.first["plan"], prior_package=inputs.first["prior_package"],
            csv=opening_csv if phase == "opening" else closing_csv,
            quote_out=quote_folder / (phase + ".json"), source=SOURCE, source_kind="public_export",
            quote_asof=E + ("T09:30:00+08:00" if phase == "opening" else "T15:00:00+08:00"),
            phase=phase, now=_stamp(moment),
        )
        options.update(changes)
        inputs.clock["now"] = options["now"]
        return paper.run_from_csv(**options)

    return SimpleNamespace(base=inputs, csv=csv, call=call, opening_csv=opening_csv,
        closing_csv=closing_csv, quote_folder=quote_folder, prepared_calls=prepared_calls,
        import_calls=import_calls)


def test_csv_open_close_prepares_once_and_restores_without_second_import_or_fill(csv_inputs, monkeypatch):
    fixture = csv_inputs
    opened = fixture.call("opening")
    assert opened["phase_committed"] is True and opened["account_count"] == 2
    assert len(fixture.prepared_calls) == len(fixture.import_calls) == 1
    opening_path = fixture.quote_folder / "opening.json"
    saved_quote = read(opening_path)
    assert saved_quote["source_csv"] == ref(fixture.opening_csv)
    assert saved_quote["source"] == SOURCE and saved_quote["source_kind"] == "public_export"
    assert saved_quote["quote_asof"] == E + "T09:30:00+08:00"
    assert saved_quote["broker_quote"] is False and saved_quote["execution_admitted"] is False
    head_path = fixture.base.run_dir / "head.json"
    head = read(head_path)
    assert head["phase_sources"]["opening"]["quote_file"] == ref(opening_path)
    initial_fills = {}
    for kind, case in head["cases"].items():
        initial_fills[kind] = deepcopy(case["state"]["fills"])
        assert initial_fills[kind]
        book = Ledger(case["state"], CONFIG, RunContext.from_dict(case["context"]))
        symbol = A if kind == "lr" else B
        assert book.quantity(symbol) == 400 and book.available(symbol, E) == 0

    def no_second_open(*args, **kwargs):
        raise AssertionError("Already committed opening was simulated again")

    monkeypatch.setattr(intraday_session, "opening", no_second_open)
    repeated = fixture.call("opening")
    assert repeated["status"] == "already_committed_saved_phase"
    assert len(fixture.prepared_calls) == 2 and len(fixture.import_calls) == 1
    assert read(head_path)["sequence"] == 1

    closed = fixture.call("closing")
    assert closed["phase_committed"] is True and len(fixture.prepared_calls) == 3
    assert len(fixture.import_calls) == 2
    final = read(head_path)
    assert final["sequence"] == 2 and final["phase"] == "closing"
    for kind, case in final["cases"].items():
        assert case["state"]["fills"] == initial_fills[kind]
        assert case["state"]["day"] == E and case["daily"]["fees"] > 0

    def no_second_close(*args, **kwargs):
        raise AssertionError("Already committed closing was simulated again")

    monkeypatch.setattr(intraday_session, "closing", no_second_close)
    assert fixture.call("closing")["status"] == "already_committed_saved_phase"
    assert len(fixture.prepared_calls) == 4 and len(fixture.import_calls) == 2
    assert read(head_path) == final


def test_stale_missing_current_status_or_wrong_day_cannot_create_a_paper_head(csv_inputs, tmp_path):
    fixture = csv_inputs
    cases = [
        ("stale", {"quote_asof": E + "T09:28:00+08:00"}),
        ("missing-st", {"csv": fixture.csv("missing-st", is_st="")}),
        ("missing-tradable", {"csv": fixture.csv("missing-tradable", tradable="")}),
        ("wrong-quote-day", {"quote_asof": D + "T09:30:00+08:00"}),
        ("wrong-current-day", {"now": _stamp(F + "T09:30:10+08:00")}),
    ]
    for name, changes in cases:
        run_dir, quote_out = tmp_path / (name + "_run"), fixture.quote_folder / (name + ".json")
        with pytest.raises(ValueError):
            fixture.call("opening", run_dir=run_dir, quote_out=quote_out, **changes)
        assert not (run_dir / "head.json").exists() and not quote_out.exists()
        status = paper.status(run_dir)
        assert status["account_count"] == 0 and status["financial_performance"] is None
        assert status["cases"] == []


def test_quote_reuse_cannot_rebind_csv_source_or_write_inside_input_and_account_packages(csv_inputs):
    fixture = csv_inputs
    fixture.call("opening")
    head_path = fixture.base.run_dir / "head.json"
    quote_path = fixture.quote_folder / "opening.json"
    head_bytes, quote_bytes = head_path.read_bytes(), quote_path.read_bytes()
    original_csv = fixture.opening_csv.read_bytes()
    another_csv = fixture.csv("same-values-different-file")
    for changes in ({"csv": another_csv}, {"source": "different-provider"}):
        with pytest.raises(ValueError, match="cannot be rebound"):
            fixture.call("opening", **changes)
        assert head_path.read_bytes() == head_bytes and quote_path.read_bytes() == quote_bytes
        assert fixture.opening_csv.read_bytes() == original_csv

    plan = fixture.base.first["plan"]
    prior = fixture.base.first["prior_package"]
    originals = {path: path.read_bytes() for path in (plan, prior, fixture.opening_csv, head_path, quote_path)}
    forbidden = [
        fixture.opening_csv, plan, plan.parent / "injected_quote.json",
        fixture.opening_csv.parent / "injected_quote.json", fixture.base.run_dir / "injected_quote.json",
    ]
    for output in forbidden:
        existed = output.exists()
        with pytest.raises((ValueError, FileExistsError)):
            fixture.call("opening", quote_out=output)
        if not existed:
            assert not output.exists()
        assert all(path.read_bytes() == value for path, value in originals.items())
    assert len(fixture.import_calls) == 1


def test_cli_csv_pairing_mutual_exclusion_and_status_have_no_execution_side_effects(csv_inputs, tmp_path, monkeypatch, capsys):
    fixture = csv_inputs

    def forbidden(*args, **kwargs):
        raise AssertionError("CLI validation/status invoked a producer or PAPER execution")

    monkeypatch.setattr(paper, "run_from_csv", forbidden)
    monkeypatch.setattr(paper, "prepare_inputs", forbidden)
    monkeypatch.setattr(quote_feed, "import_quote", forbidden)
    run_dir = tmp_path / "status_only"
    common = ["--run-dir", str(run_dir)]
    complete = [
        "--phase", "opening", "--plan", str(fixture.base.first["plan"]),
        "--prior-package", str(fixture.base.first["prior_package"]),
        "--quote-csv", str(fixture.opening_csv),
        "--quote-out", str(fixture.quote_folder / "cli.json"),
        "--quote-source", SOURCE, "--quote-source-kind", "public_export",
        "--quote-asof", E + "T09:30:00+08:00",
    ]
    invalid = [complete + ["--quote", str(tmp_path / "saved.json")]]
    for flag in ("--plan", "--prior-package", "--quote-out", "--quote-source", "--quote-source-kind", "--quote-asof"):
        index = complete.index(flag)
        invalid.append(complete[:index] + complete[index + 2:])
    invalid.append(["--phase", "status", *complete[2:]])
    invalid.append(["--quote-out", str(fixture.quote_folder / "orphan.json")])
    for args in invalid:
        with pytest.raises(SystemExit) as error:
            paper.main(common + args)
        assert error.value.code == 2
        assert not run_dir.exists()
    assert paper.main(common) == 0
    assert '"account_count": 0' in capsys.readouterr().out
    assert not run_dir.exists() and not (fixture.quote_folder / "cli.json").exists()


def test_slow_import_cannot_freeze_the_phase_clock_or_refresh_a_saved_quote(csv_inputs, monkeypatch):
    fixture = csv_inputs
    clock = fixture.base.clock
    clock["now"] = _stamp(E + "T09:30:10+08:00")
    original_now = paper._now
    normal_import = quote_feed.import_quote

    def actual_now(value=None):
        return clock["now"] if value is None else original_now(value)

    def slow_import(*args, **kwargs):
        record = normal_import(*args, **kwargs)
        # The original observation is already durable before the phase starts.
        clock["now"] += timedelta(seconds=100)
        return record

    monkeypatch.setattr(paper, "_now", actual_now)
    monkeypatch.setattr(quote_feed, "import_quote", slow_import)
    quote_out = fixture.quote_folder / "slow_opening.json"
    options = dict(
        run_dir=fixture.base.run_dir, plan=fixture.base.first["plan"],
        prior_package=fixture.base.first["prior_package"], csv=fixture.opening_csv,
        quote_out=quote_out, source=SOURCE, source_kind="public_export",
        quote_asof=E + "T09:30:00+08:00", phase="opening",
    )
    # No fixed now is supplied: both the consumer and the real Engine use clock.
    with pytest.raises(ValueError, match="stale"):
        paper.run_from_csv(**options)
    saved = quote_out.read_bytes()
    record = read(quote_out)
    assert record["quote_asof"] == E + "T09:30:00+08:00"
    assert record["observed_at"] == record["source_available_at"] == E + "T09:30:10+08:00"
    assert record["source_csv"] == ref(fixture.opening_csv)
    assert len(fixture.import_calls) == 1
    assert not (fixture.base.run_dir / "head.json").exists()
    assert not list((fixture.base.run_dir / "journal").glob("*.json"))

    with pytest.raises(ValueError, match="stale"):
        paper.run_from_csv(**options)
    assert quote_out.read_bytes() == saved and len(fixture.import_calls) == 1
    assert not (fixture.base.run_dir / "head.json").exists()
    assert not list((fixture.base.run_dir / "journal").glob("*.json"))
