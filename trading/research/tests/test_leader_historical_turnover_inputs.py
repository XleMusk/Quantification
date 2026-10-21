"""Small synthetic rows on the fixed real-shaped 183-session calendar."""
from copy import deepcopy
from datetime import datetime
import json

import pandas as pd
import pytest

from trading.research import leader_historical_turnover_inputs as m

CALENDAR = {"schema": "cn_a_share_exchange_calendar_v1", "year": 2026,
    "closed_ranges": ["2026-01-01/2026-01-03", "2026-02-15/2026-02-23", "2026-04-04/2026-04-06",
        "2026-05-01/2026-05-05", "2026-06-19/2026-06-21", "2026-09-25/2026-09-27", "2026-10-01/2026-10-07"],
    "open_sessions": []}


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    clock = datetime.fromisoformat("2026-10-15T23:00:00+08:00")
    monkeypatch.setattr(m, "_now", lambda: clock)
    calendar = tmp_path / "calendar.json"
    write(calendar, CALENDAR)
    _, days, _ = m._sessions(calendar)
    roots = [tmp_path / "base", tmp_path / "extension"]
    for root, role in zip(roots, ("base", "extension")):
        root.mkdir()
        selected = [d for d in days if (d <= "2026-09-30") == (role == "base")]
        tasks = {}
        for day in selected:
            row = dict.fromkeys(m.ORIGINAL_FIELDS, "1")
            row.update(date=day, code="sh.600000", adjustflag="3", tradestatus="1", isST="0",
                turn="2.024900", volume="1000000", amount="10000000")
            halt = {**row, "code": "sz.000001", "tradestatus": "0", "turn": "", "volume": "", "amount": ""}
            path = root / (day + "_stock.csv")
            pd.DataFrame([row, halt], columns=m.ORIGINAL_FIELDS).to_csv(path, index=False)
            tasks[day] = {"date": day, "kind": "stock", "method": "query_daily_history_k_AStock",
                "error_code": "0", "file": path.name, "sha256": m._digest(path), "quality": {"rows": 2}}
        write(root / "plan.json", {"kind": "stock", "days": selected, "execution_admitted": False})
        write(root / "receipts.json", {"status": "source_cache_complete", "execution_admitted": False,
            "tasks": tasks, "failures": [{"error": "synthetic preserved prior timeout"}],
            "updated_at": "2026-10-14T10:00:00+08:00"})
    units = tmp_path / "units.json"
    write(units, {"status": "official_provider_document_read", "turn_unit": "percent",
        "retrieved_at": "2026-10-15T12:00:00+08:00", "synthetic_only": True})
    monkeypatch.setattr(m, "UNIT_EVIDENCE", units)
    registration = tmp_path / "registration.json"
    write(registration, {"schema": m.REGISTRATION_SCHEMA, "registered_at": "2026-10-15T22:00:00+08:00",
        "start": m.START, "end": m.END, "sessions": days,
        "calendar": {"file": str(calendar), "sha256": m._digest(calendar)},
        "sources": [m._source(root, role)["proof"] for root, role in zip(roots, ("base", "extension"))],
        "protocol": m.PROTOCOL, **m.FLAGS})
    return {"roots": roots, "calendar": calendar, "registration": registration, "out": tmp_path / "result", "days": days}


def test_fixed_183_all_observed_rows_halt_null_and_failures_preserved(evidence):
    e = evidence
    path, value = m.build(*e["roots"], e["calendar"], e["out"], registration=e["registration"])
    loaded = m.load_historical_turnover_inputs(path)
    assert value["counts"]["observed_rows"] == 366 and value["counts"]["reported_turn_unknown"] == 183
    assert value["sessions"] == e["days"] and value["first_previous_session"] is None
    assert loaded["day_batches"][0]["previous_exact_session"] is None
    assert all(len(s["original_failures"]) == 1 for s in value["sources"])
    halt = loaded["rows"].loc[loaded["rows"].tradestatus.eq("0")]
    assert halt.turn.eq("").all() and halt.turn_percent.isna().all()
    assert loaded["rows"].loc[loaded["rows"].tradestatus.eq("1"), "turn_percent"].eq(2.0249).all()
    assert loaded["contract"] == "observed_rows_without_historical_dated_roster"
    assert value["historical_dated_roster_verified"] is value["strict_full_close_admitted"] is False
    with pytest.raises(ValueError, match="future"):
        m.load_historical_turnover_inputs(path, observed_at="2026-10-16T00:00:00+08:00")
    original = deepcopy(value)
    for key, wrong in (("previous_exact_session", "2026-01-02"), ("raw_source_available_at", "2026-10-16T10:00:00+08:00")):
        tampered = deepcopy(original)
        tampered["day_batches"][0][key] = wrong
        write(path, tampered)
        with pytest.raises(ValueError, match="metadata changed"):
            m.load_historical_turnover_inputs(path)
    write(path, original)


def test_original_task_hash_or_raw_basis_cannot_be_repaired_silently(evidence):
    e = evidence
    source = m._source(e["roots"][0], "base")
    day = e["days"][0]
    path = e["roots"][0] / (day + "_stock.csv")
    raw = pd.read_csv(path, dtype="string", keep_default_na=False)
    raw.loc[0, "adjustflag"] = "2"
    raw.to_csv(path, index=False)
    with pytest.raises(ValueError, match="bytes/18-field"):
        m._day(source, day, {}, m._now())
    source["receipt"]["tasks"][day]["sha256"] = m._digest(path)
    with pytest.raises(ValueError, match="raw basis"):
        m._day(source, day, {}, m._now())
    assert not e["out"].exists()


def test_missing_exact_task_and_registration_changes_do_not_make_output(evidence):
    e = evidence
    source = m._source(e["roots"][0], "base")
    source["receipt"]["tasks"].pop(e["days"][0])
    with pytest.raises(ValueError, match="Missing or unsuccessful"):
        m._day(source, e["days"][0], {}, m._now())
    value = json.loads(e["registration"].read_text())
    value["sessions"][0] = "2026-01-02"
    write(e["registration"], value)
    with pytest.raises(ValueError, match="registration"):
        m.build(*e["roots"], e["calendar"], e["out"], registration=e["registration"])
    assert not e["out"].exists()


def test_explicit_future_observer_cannot_authorize_build(evidence):
    e = evidence
    with pytest.raises(ValueError, match="future"):
        m.build(*e["roots"], e["calendar"], e["out"], registration=e["registration"], observed_at="2026-10-16T00:00:00+08:00")
    assert not e["out"].exists()
