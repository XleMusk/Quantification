from contextlib import contextmanager
from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from trading.research import baostock_daily_slice as source

DAYS = ["2026-10-09", "2026-10-12", "2026-10-13"]
SYMBOLS = ["000001.SZSE", "000017.SZSE", "000029.SZSE"]
SDK = {"sha256": source.WHEEL_SHA256}


@pytest.fixture
def calendar(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "calendar_id": "test", "year": 2026,
                                "closed_ranges": [], "open_sessions": []}), encoding="utf-8")
    return path


def frame(code):
    return pd.DataFrame([dict(zip(source.FIELDS.split(","),
        [day, code, "7.0500", "7.3300", "6.7900", "7.2600", "7.1200", "40553860", "286860546.6000", "3", "1", "1.966300", "0"])) for day in DAYS])


def result(arguments):
    return SimpleNamespace(error_code="0", error_msg="success", get_data=lambda: frame(arguments["code"]))


def test_failed_second_query_preserves_first_and_stops_without_hot_retry(tmp_path, calendar, monkeypatch):
    calls = []
    @contextmanager
    def session():
        def query(**arguments):
            calls.append(arguments["code"])
            if len(calls) == 2:
                raise TimeoutError("bounded provider timeout")
            return result(arguments)
        yield SimpleNamespace(query_history_k_data_plus=query)
    monkeypatch.setattr(source.time, "sleep", lambda duration: None)
    folder, receipt = source.collect(SYMBOLS, DAYS, calendar, SDK, tmp_path / "source", session_factory=session)
    assert receipt["status"] == "failed_stopped"
    assert list(receipt["completed"]) == SYMBOLS[:1]
    assert calls == ["sz.000001", "sz.000017"]
    assert pd.read_csv(folder / "raw_daily.csv", dtype=str).volume.tolist() == ["40553860"] * 3
    assert receipt["execution_admitted"] is False
    with pytest.raises(ValueError, match="automatic retry"):
        source.collect(SYMBOLS, DAYS, calendar, SDK, folder, session_factory=session)
    assert len(calls) == 2


def test_bounded_resume_verifies_bytes_and_queries_only_pending(tmp_path, calendar, monkeypatch):
    calls = []
    @contextmanager
    def session():
        def query(**arguments):
            calls.append(arguments["code"])
            return result(arguments)
        yield SimpleNamespace(query_history_k_data_plus=query)
    monkeypatch.setattr(source.time, "sleep", lambda duration: None)
    folder, receipt = source.collect(SYMBOLS[:2], DAYS, calendar, SDK, tmp_path / "source", max_new_queries=1, session_factory=session)
    assert receipt["status"] == "partial_bounded_slice"
    _, receipt = source.collect(SYMBOLS[:2], DAYS, calendar, SDK, folder, session_factory=session)
    assert receipt["status"] == "complete_raw_slice_not_admitted"
    assert calls == ["sz.000001", "sz.000017"]
    (folder / "sz.000001.csv").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash changed"):
        source.collect(SYMBOLS[:2], DAYS, calendar, SDK, folder, session_factory=session)
    assert len(calls) == 2


def test_duplicate_or_omitted_open_session_cannot_start_source(tmp_path, calendar):
    for symbols, days in [(SYMBOLS[:1] * 2, DAYS), (SYMBOLS, DAYS[::2])]:
        with pytest.raises(ValueError):
            source.collect(symbols, days, calendar, SDK, tmp_path / "source")
    assert not (tmp_path / "source").exists()


def test_seed_reuses_exact_raw_with_precise_share_units(tmp_path, calendar):
    seed = tmp_path / "seed"
    seed.mkdir()
    raw = seed / "raw.csv"
    frame("sz.000017").to_csv(raw, index=False)
    receipt = {"schema": "bounded_dailyplus_raw_tail_probe_v1", "status": "completed_raw_probe_not_admitted",
               "kwargs": source.kwargs(SYMBOLS[1], DAYS), "provider_error_code": "0", "sdk": SDK,
               "execution_admitted": False, "raw_csv": str(raw), "raw_sha256": sha256(raw.read_bytes()).hexdigest(),
               "rows": 3, "observed_days": DAYS}
    (seed / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    @contextmanager
    def no_network():
        raise AssertionError("Verified seed should not query provider")
        yield
    folder, saved = source.collect([SYMBOLS[1]], DAYS, calendar, SDK, tmp_path / "source", seeds=[seed], session_factory=no_network)
    assert saved["status"] == "complete_raw_slice_not_admitted" and saved["logical_queries"] == 0
    assert (folder / "sz.000017.csv").read_bytes() == raw.read_bytes()
    precise = pd.read_csv(folder / "raw_daily.csv", dtype=str)
    assert precise.volume.tolist() == ["40553860"] * 3 and precise.amount.tolist() == ["286860546.6000"] * 3
    bad = precise.copy()
    bad.loc[0, "volume"] = "405538.6"
    assert "amount_volume_price_inconsistent" in source.quality(bad, receipt["kwargs"], DAYS)["issues"]
    raw.write_text(raw.read_text().replace("2026-10-12", "2026-10-13"), encoding="utf-8")
    with pytest.raises(ValueError, match="hash changed"):
        source.collect([SYMBOLS[1]], DAYS, calendar, SDK, tmp_path / "other", seeds=[seed], session_factory=no_network)


def suspension(code):
    halted = frame(code)
    halted.loc[:, ["open", "high", "low", "close", "preclose"]] = "2.4600"
    halted.loc[:, ["volume", "amount", "pctChg"]] = ""
    halted.loc[:, "tradestatus"] = "0"
    halted.loc[:, "isST"] = "1"
    return halted


def test_nullable_suspension_is_inventory_but_not_execution_bar():
    payload = suspension("sz.000017")
    assessment = source.quality(payload, source.kwargs(SYMBOLS[1], DAYS), DAYS)
    assert assessment["issues"] == [] and assessment["inventory_complete"] is True
    assert assessment["execution_bar_coverage_complete"] is False and assessment["execution_bar_rows"] == 0
    assert len(assessment["unpriceable_source_rows"]) == 3
    assert payload.volume.tolist() == [""] * 3
    payload.loc[0, "tradestatus"] = "1"
    assert "missing_nonfinite_numeric_field" in source.quality(payload, source.kwargs(SYMBOLS[1], DAYS), DAYS)["issues"]


def test_reassesses_preserved_nullable_failure_and_queries_only_unattempted(tmp_path, calendar, monkeypatch):
    calls = []
    @contextmanager
    def session():
        def query(**arguments):
            calls.append(arguments["code"])
            return result(arguments)
        yield SimpleNamespace(query_history_k_data_plus=query)
    monkeypatch.setattr(source.time, "sleep", lambda duration: None)
    package, saved = source.collect(SYMBOLS, DAYS, calendar, SDK, tmp_path / "old", max_new_queries=1, session_factory=session)
    raw = package / "sz.000017.csv"
    suspension("sz.000017").to_csv(raw, index=False)
    old_assessment = {"rows": 3, "issues": ["missing_nonfinite_numeric_field"]}
    failed = {"symbol": SYMBOLS[1], "kwargs": source.kwargs(SYMBOLS[1], DAYS), "raw_file": raw.name,
              "raw_sha256": source.digest(raw), "provider_error_code": "0", "provider_error_msg": "success", "quality": old_assessment}
    source.write_json(package / "sz.000017.receipt.json", failed)
    saved.update(status="failed_stopped", in_flight={"symbol": SYMBOLS[1], "kwargs": failed["kwargs"]})
    source.write_json(package / "receipt.json", saved)
    original_failure_bytes = (package / "receipt.json").read_bytes()
    folder, receipt = source.collect(SYMBOLS, DAYS, calendar, SDK, tmp_path / "new", reuse_package=package, session_factory=session)
    assert calls == ["sz.000001", "sz.000029"]
    assert receipt["status"] == "complete_raw_slice_not_admitted" and receipt["logical_queries"] == 1
    assert receipt["execution_bar_rows"] == 6 and receipt["execution_bar_coverage_complete"] is False
    assert (folder / raw.name).read_bytes() == raw.read_bytes()
    assert (package / "receipt.json").read_bytes() == original_failure_bytes
    assert receipt["completed"][SYMBOLS[1]]["original_quality"] == old_assessment
