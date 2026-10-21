"""Consumer persistence uses real two-phase Engine/Ledger and synthetic inputs.

Plan/source preparation is the mocked boundary here. These cases do not certify
the production quote or IPO parser, train a model, or use market source files.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading import engine, intraday_session, native_intraday_paper as paper
from trading.contracts import PortfolioIntent, PreopenPortfolioIntent, RunContext
from trading.ledger import Ledger
from trading.research.shortline_native_preopen import QUALIFIED_POLICY_ID
from trading.tests.test_published_close_intent import A, B, CONFIG, D, E, F


G = "2025-01-09"
POLICY = "native_verified_daily_projection_v1"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return Path(path)


def ref(path):
    data = Path(path).read_bytes()
    return {"file": str(Path(path).resolve()), "sha256": sha256(data).hexdigest(), "byte_count": len(data)}


class OpenRow(dict):
    def get(self, key, default=None):
        if key in {"close", "high", "low", "volume", "amount"}:
            raise AssertionError("Opening read future/current-session data: " + key)
        return super().get(key, default)

    def __getitem__(self, key):
        if key in {"close", "high", "low", "volume", "amount"}:
            raise AssertionError("Opening read future/current-session data: " + key)
        return super().__getitem__(key)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    folder, run_dir = tmp_path / "inputs", tmp_path / "paper_run"
    folder.mkdir()
    calendar = ["2025-01-03", D, E, F, G]
    calendar_file = write(folder / "calendar.json", {"sessions": calendar})
    model = write(folder / "models.json", {"generation": "synthetic_frozen_generation_v1"})
    clock = {"now": datetime.fromisoformat(E + "T09:30:10+08:00")}
    monkeypatch.setattr(engine, "_published_now", lambda: clock["now"])

    def prepare(plan, prior_package, *, ipo_proof=None, master_run=None, now=None):
        plan, prior_package = Path(plan), Path(prior_package)
        item = read(plan)
        if read(prior_package)["date"] != item["source_day"]:
            raise ValueError("Fixture exact D source mismatch")
        loaded = {"record": {"policy_id": QUALIFIED_POLICY_ID},
            "source_day": item["source_day"], "execution_day": item["execution_day"],
            "plan_ref": ref(plan)}
        proof = dict(plan_ref=loaded["plan_ref"], calendar_ref=ref(calendar_file),
            model_manifest=ref(model), source_day=item["source_day"], execution_day=item["execution_day"],
            source_close_proof=ref(prior_package), qualification_inputs={"fixture_only": True})

        def make_feed(quote_path, phase, context):
            quote_path, quote = Path(quote_path), read(quote_path)
            e, d = item["execution_day"], item["source_day"]
            if quote["date"] != e or quote["phase"] != phase:
                raise ValueError("Fixture exact phase quote mismatch")
            rows = {symbol: OpenRow(row) if phase == "opening" else row
                    for symbol, row in quote["rows"].items()}
            value = SimpleNamespace(calendar=calendar, symbols=[A, B], rows={e: rows},
                volumes=pd.DataFrame(1_000_000., index=[d], columns=[A, B]),
                instruments={s: {"ipoDate": "2000-01-01", "outDate": ""} for s in (A, B)},
                actions=[], instrument_profiles={}, manifest={"replay_only": False},
                next_day=lambda day: calendar[calendar.index(day) + 1],
                daily_source_policy=POLICY, input_paths=[quote_path],
                intraday_phase_binding={"schema_version": 1, "source_id": "fixture-observed-provider",
                    "provider_id": "fixture-observed-provider", "quote_source_kind": "observed_market_snapshot",
                    "daily_source_policy": POLICY})
            if d == E:
                # A legitimate next-D metadata projection may expand; this
                # unheld, unselected identity does not change economic targets.
                value.instruments["603000.SSE"] = {"ipoDate": "2001-01-01", "outDate": ""}
            if quote.get("alter_instruments"):
                value.instruments[A]["ipoDate"] = "1998-01-01"
            for name in ("opening", "closing"):
                setattr(value, "intraday_" + name + "_source_ref", ref(quote_path)["sha256"] + ":" + name)
                setattr(value, "intraday_" + name + "_observed_at", quote["observed_at"])
                setattr(value, "intraday_" + name + "_source_available_at", quote["observed_at"])
            value.validate_opening_session = lambda state, day: None
            value.validate_closing_session = lambda state, day: None
            def no_signal(*args, **kwargs):
                raise AssertionError("Consumer requested a future signal")
            value.scores = value.signal_frame = no_signal
            return value

        return SimpleNamespace(loaded=loaded, config=CONFIG, calendar=calendar, proof=proof,
            input_paths=[plan, prior_package, calendar_file, model], feed=make_feed)

    def bind(plan, kind, context):
        item = read(plan)
        weights = item["weights"][kind]
        intent = (PortfolioIntent.no_change("fixture_unknown_hold_exact_shares", context=context,
                      source="fixture_saved_native_plan") if weights is None else
                  PortfolioIntent.full_target(weights, context=context, source="fixture_saved_native_plan"))
        return PreopenPortfolioIntent(item["source_day"], item["execution_day"], item["published_at"], intent)

    monkeypatch.setattr(paper, "prepare_inputs", prepare)
    monkeypatch.setattr(paper, "bind_preopen_plan", bind)

    def day(d, e, weights):
        plan = write(folder / (e + "_plan.json"), dict(source_day=d, execution_day=e,
            published_at=d + "T18:30:00+08:00", weights=weights))
        prior = write(folder / (d + "_close.json"), {"date": d})
        result = dict(plan=plan, prior_package=prior)
        for phase, observed in (("opening", "09:30:00"), ("closing", "15:00:00")):
            rows = ({s: {"open": 10., "preclose": 10., "isST": 0, "tradestatus": 1, "adjustflag": "3"}
                     for s in (A, B)} if phase == "opening" else
                    {A: {"close": 11., "tradestatus": 1}, B: {"close": 10.5, "tradestatus": 1}})
            result[phase] = write(folder / (e + "_" + phase + ".json"),
                {"date": e, "phase": phase, "observed_at": e + "T" + observed + "+08:00", "rows": rows})
        return result

    first = day(D, E, {"lr": {A: .4}, "lgbm": {B: .4}})
    second = day(E, F, {"lr": {B: .4}, "lgbm": None})
    following_open = read(second["opening"])
    for symbol, price in ((A, 11.), (B, 10.5)):
        following_open["rows"][symbol].update(open=price, preclose=price)
    write(second["opening"], following_open)

    def call(bundle, phase, **changes):
        e = read(bundle["plan"])["execution_day"]
        clock["now"] = datetime.fromisoformat(e + ("T09:30:10+08:00" if phase == "opening" else "T15:05:00+08:00"))
        options = dict(run_dir=run_dir, plan=bundle["plan"], prior_package=bundle["prior_package"],
            quote=bundle[phase], phase=phase, now=clock["now"])
        options.update(changes)
        return paper.run(**options)

    return SimpleNamespace(folder=folder, run_dir=run_dir, first=first, second=second, call=call, clock=clock)


def head(fixture):
    return read(fixture.run_dir / "head.json")


def test_two_days_open_close_reloads_real_accounts_and_preserves_T1_fees_no_change(inputs):
    opened = inputs.call(inputs.first, "opening")
    assert opened["account_count"] == 2 and opened["phase_committed"] is True
    first = head(inputs)
    for case in first["cases"].values():
        state = case["state"]
        book = Ledger(state, CONFIG, RunContext.from_dict(case["context"]))
        symbol = next(s for s in (A, B) if book.quantity(s))
        assert state["day"] is None and book.quantity(symbol) == 400
        assert book.available(symbol, E) == 0 and book.available(symbol, F) == 400
        assert state["cash"] < 600000 and case["daily"] is None
    fills = {kind: deepcopy(case["state"]["fills"]) for kind, case in first["cases"].items()}
    inputs.call(inputs.first, "closing")  # Every call restores the durable JSON head.
    closed = head(inputs)
    assert closed["sequence"] == 2
    for kind, case in closed["cases"].items():
        assert case["state"]["fills"] == fills[kind] and case["state"]["day"] == E
        assert case["completed_sessions"] == 1 and case["daily"]["fees"] > 0
    inputs.call(inputs.second, "opening")
    second = head(inputs)
    lr, lgbm = second["cases"]["lr"]["state"], second["cases"]["lgbm"]["state"]
    assert sum(lot["qty"] for lot in lr["positions"].get(A, [])) == 0
    assert any(order["side"] == "sell" and order["day"] == F for order in lr["orders"].values())
    assert sum(lot["qty"] for lot in lgbm["positions"][B]) == 400
    assert lgbm["fills"] == fills["lgbm"]  # NO_CHANGE is exact shares, not a rebalance.
    second_fills = {kind: deepcopy(case["state"]["fills"]) for kind, case in second["cases"].items()}
    inputs.call(inputs.second, "closing")
    final = head(inputs)
    assert final["sequence"] == 4 and final["execution_day"] == F
    assert all(case["completed_sessions"] == 2 and case["state"]["day"] == F for case in final["cases"].values())
    assert {kind: case["state"]["fills"] for kind, case in final["cases"].items()} == second_fills


def test_one_bad_model_or_close_keeps_whole_phase_uncommitted_and_no_future_fields(inputs):
    original = read(inputs.first["opening"])
    broken = deepcopy(original)
    broken["rows"][B].pop("open")  # LR succeeds locally; LGB must not expose that trial.
    write(inputs.first["opening"], broken)
    with pytest.raises((ValueError, KeyError)):
        inputs.call(inputs.first, "opening")
    assert not (inputs.run_dir / "head.json").exists()
    assert not list((inputs.run_dir / "journal").glob("*.json"))
    write(inputs.first["opening"], original)
    inputs.call(inputs.first, "opening")  # OpenRow and D-only volumes poison forbidden reads.
    before = (inputs.run_dir / "head.json").read_bytes()
    closing = read(inputs.first["closing"])
    changed_semantics = {**closing, "alter_instruments": True}
    write(inputs.first["closing"], changed_semantics)
    with pytest.raises(ValueError):
        inputs.call(inputs.first, "closing")
    assert (inputs.run_dir / "head.json").read_bytes() == before
    closing["rows"][A].pop("close")
    write(inputs.first["closing"], closing)
    with pytest.raises((ValueError, KeyError)):
        inputs.call(inputs.first, "closing")
    assert (inputs.run_dir / "head.json").read_bytes() == before
    assert len(list((inputs.run_dir / "journal").glob("*.json"))) == 1


def test_saved_journal_recovery_installs_phase_without_reexecuting_fills(inputs, monkeypatch):
    original_write = paper.write_json
    def interrupt_head(path, value):
        if Path(path).name == "head.json":
            raise OSError("synthetic crash after durable whole-phase journal")
        return original_write(path, value)
    monkeypatch.setattr(paper, "write_json", interrupt_head)
    with pytest.raises(OSError, match="synthetic crash"):
        inputs.call(inputs.first, "opening")
    assert not (inputs.run_dir / "head.json").exists()
    journal = read(next((inputs.run_dir / "journal").glob("*.json")))
    saved_cases = journal["after_head"]["cases"]
    monkeypatch.setattr(paper, "write_json", original_write)
    with pytest.raises(ValueError):
        inputs.call(inputs.second, "opening")
    assert not (inputs.run_dir / "head.json").exists()
    assert read(next((inputs.run_dir / "journal").glob("*.json"))) == journal
    def do_not_execute(*args, **kwargs):
        raise AssertionError("Recovery/idempotence called opening again")
    monkeypatch.setattr(intraday_session, "opening", do_not_execute)
    recovered = inputs.call(inputs.first, "opening")
    assert recovered["recovered_commits"] == 1 and recovered["phase_committed"] is False
    assert recovered["status"] == "already_committed_saved_phase"
    assert head(inputs)["cases"] == saved_cases
    assert inputs.call(inputs.first, "opening")["recovered_commits"] == 0
    changed = read(inputs.first["opening"])
    changed["rows"][A]["open"] = 10.1
    write(inputs.first["opening"], changed)
    before = (inputs.run_dir / "head.json").read_bytes()
    with pytest.raises(ValueError, match="quote"):
        inputs.call(inputs.first, "opening")
    assert (inputs.run_dir / "head.json").read_bytes() == before


def test_status_only_reads_saved_head_without_preparation_quotes_recovery_or_writes(inputs, monkeypatch):
    assert paper.status(inputs.run_dir)["status"] == "not_created" and not inputs.run_dir.exists()
    inputs.call(inputs.first, "opening")
    before = {str(path.relative_to(inputs.run_dir)): path.read_bytes()
              for path in inputs.run_dir.rglob("*") if path.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError("Read-only status invoked a producer or recovery")
    for method in ("prepare_inputs", "bind_preopen_plan", "_recover", "write_json"):
        monkeypatch.setattr(paper, method, forbidden)
    monkeypatch.setattr(intraday_session, "opening", forbidden)
    monkeypatch.setattr(intraday_session, "closing", forbidden)
    result = paper.status(inputs.run_dir)
    assert result["account_count"] == 2 and result["phase"] == "opening"
    assert all(case["closing_equity_rmb"] is None for case in result["cases"])
    assert paper.run(run_dir=inputs.run_dir, phase="status") == result
    after = {str(path.relative_to(inputs.run_dir)): path.read_bytes()
             for path in inputs.run_dir.rglob("*") if path.is_file()}
    assert after == before
