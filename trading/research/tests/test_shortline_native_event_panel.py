from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from trading.research import shortline_native_event_panel as m
from trading.research.baostock_daily_slice import digest
from trading.research.shortline_split import purge_training_labels

NOW = datetime(2026, 10, 16, 5, tzinfo=timezone.utc)
A, B, C = "000001.SZSE", "000002.SZSE", "000003.SZSE"


def bar(day, symbol=A, *, close=10, preclose=None, amount=100000, volume=10000,
        status="1", st="0", one_price=False):
    preclose = close if preclose is None else preclose
    return {"date": day, "symbol": symbol, "code": "sz." + symbol[:6],
        "open": str(close), "high": str(close if one_price else close + .02),
        "low": str(close if one_price else close - .02), "close": str(close),
        "preclose": str(preclose), "volume": str(volume), "amount": str(amount),
        "pctChg": str((close / preclose - 1) * 100), "adjustflag": "3",
        "tradestatus": status, "isST": st}


class Source:
    def __init__(self, frames, *, fail=None):
        self.frames = {day: pd.DataFrame(rows) for day, rows in frames.items()}
        self.calendar = sorted(self.frames)
        self.provenance = {"source_available_at": "2026-10-01T15:03:23+08:00",
            "raw_file": "synthetic/native.parquet", "raw_sha256": "synthetic",
            "source_sessions": len(self.calendar), "pit_verified": False}
        self.reads, self.closed, self.fail = [], False, fail

    def frame(self, day):
        self.reads.append(day)
        if day == self.fail:
            raise ValueError("source failure")
        return self.frames[day].copy(deep=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(m, "_now", lambda: NOW)


def dates(n, start="2025-01-02"):
    return pd.bdate_range(start, periods=n).strftime("%Y-%m-%d").tolist()


def run_frames(*, witness=True):
    days = dates(7)
    closes = [10., 11., 12.1, 13.31, 14.64, 16.1, 16.]
    frames = {day: [bar(day, close=close, preclose=closes[i-1] if i else 10.)]
              for i, (day, close) in enumerate(zip(days, closes))}
    if not witness:
        frames.pop(days[-1])
    return frames


def test_nine_features_use_whole_valid_dated_scope_average_ties_and_observed_returns():
    days = dates(22)
    frames = {day: [bar(day, A, close=10+i, preclose=10+i-1 if i else 10, amount=100),
                    bar(day, B, close=20, amount=200),
                    bar(day, C, close=50, amount=500, st="1")]
              for i, day in enumerate(days)}
    source = Source(frames)
    got = m.produce(source)
    latest = {row["symbol"]: row for row in got["latest_features"]}
    row = latest[A]
    assert row["amount_rank_pct"] == .5
    assert row["volume_rank_pct"] == .75
    assert row["market_down_rate"] == 0
    assert row["range_pct"] == pytest.approx(.04 / 30)
    assert row["close_location"] == pytest.approx(.5)
    assert row["ret_5_observed_rows"] == pytest.approx(31 / 26 - 1)
    assert row["ret_10_observed_rows"] == pytest.approx(31 / 21 - 1)
    assert row["ret_20_observed_rows"] == pytest.approx(31 / 11 - 1)
    assert got["latest_scope"]["valid_scope_rows"] == 2
    assert got["latest_scope"]["known_ST_symbols"] == [C]
    assert source.reads == days


def test_terminal_fifth_board_remains_nullable_until_exact_nonlimit_witness():
    partial = m.produce(Source(run_frames(witness=False)))
    assert [event["stage"] for event in partial["events"]] == [1, 2, 3]
    assert all(event["run_max_stage_observed"] == 5 for event in partial["events"])
    assert all(event["hit_4"] is None and event["hit_5"] is None for event in partial["events"])
    assert all(event["label_witness_day"] is None for event in partial["events"])
    full = m.produce(Source(run_frames()))
    assert all(event["label_status"] == "complete" and event["hit_5"] == 1 for event in full["events"])
    assert all(event["label_witness_day"] == dates(7)[-1] for event in full["events"])
    assert all(event["label_available_at"] == NOW.isoformat() for event in full["events"])


@pytest.mark.parametrize("fault", ["missing", "halt", "unknownST", "reference"])
def test_gap_halt_unknown_status_or_reference_censors_without_faking_failure_or_new_stage1(fault):
    days = dates(6)
    frames = {days[0]: [bar(days[0])], days[1]: [bar(days[1], close=11, preclose=10)],
        days[2]: [bar(days[2], close=12.1, preclose=11)],
        days[3]: [bar(days[3], close=13.31, preclose=12.1)],
        days[4]: [bar(days[4], close=13.3, preclose=13.31)],
        days[5]: [bar(days[5], close=14.63, preclose=13.3)]}
    if fault == "missing":
        frames[days[2]] = [bar(days[2], B)]
    elif fault == "halt":
        frames[days[2]][0]["tradestatus"] = "0"
    elif fault == "unknownST":
        frames[days[2]][0]["isST"] = ""
    else:
        frames[days[2]][0]["preclose"] = "8"
    got = m.produce(Source(frames))
    old = next(event for event in got["events"] if event["date"] == days[1])
    assert old["label_status"].startswith("right_censored_")
    assert old["hit_4"] is None and old["hit_5"] is None
    assert not any(event["date"] in days[2:4] for event in got["events"])
    assert any(row["date"] == days[3] and row["symbol"] == A for row in got["unresolved_membership"])
    assert next(event for event in got["events"] if event["date"] == days[5])["stage"] == 1


def test_initial_limit_stage_and_zero_range_are_preserved_as_unknown_not_stage1_or_zero():
    day = dates(1)[0]
    got = m.produce(Source({day: [bar(day, close=11, preclose=10, one_price=True)]}))
    row = got["latest_features"][0]
    assert row["stage"] is None and row["stage_reference_known"] is False
    assert row["decision_eligible"] is False
    assert row["close_location"] is None
    assert got["events"] == []
    assert got["latest_scope"]["unknown_stage_symbols"] == [A]
    assert got["latest_scope"]["stage_membership_complete"] is False


def test_features_and_D_eligibility_are_prefix_invariant_and_producer_never_reads_after_through():
    frames = run_frames()
    end = dates(7)[2]
    source = Source(frames)
    prefix = m.produce(source, through=end)
    full = m.produce(Source(frames))
    assert source.reads == dates(7)[:3]
    assert prefix["event_features"] == [row for row in full["event_features"] if row["date"] <= end]
    assert all(not set(m._LABEL_FIELDS).intersection(row) for row in full["event_features"])
    changed = deepcopy(frames)
    changed[dates(7)[3]][0]["isST"] = ""
    other = m.produce(Source(changed))
    assert other["event_features"][:2] == full["event_features"][:2]


def test_cross_year_witness_is_explicit_training_purge_boundary():
    days = ["2025-12-29", "2025-12-30", "2025-12-31", "2026-01-02"]
    frames = {days[0]: [bar(days[0])], days[1]: [bar(days[1], close=11, preclose=10)],
        days[2]: [bar(days[2], close=12.1, preclose=11)],
        days[3]: [bar(days[3], close=12, preclose=12.1)]}
    panel = pd.DataFrame(m.produce(Source(frames))["events"])
    assert panel.run_end_date.le("2025-12-31").all()
    assert panel.label_witness_day.eq("2026-01-02").all()
    train, stats = purge_training_labels(panel, "2025-01-01", "2025-12-31", label_end_column="label_witness_day")
    assert train.empty and stats["purged_rows"] == 2


def test_build_pins_small_outputs_keeps_unknown_scope_and_never_overwrites(tmp_path, monkeypatch):
    source = Source(run_frames())
    monkeypatch.setattr(m, "NativeHistorySource", lambda view: source)
    clocks = iter([NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)])
    monkeypatch.setattr(m, "_now", lambda: next(clocks))
    out = tmp_path / "panel"
    result = m.build("synthetic-view", out)
    assert source.closed
    assert result["events_sha256"] == digest(out / "event_panel.parquet")
    assert result["latest_features_sha256"] == digest(out / "latest_features.csv")
    assert datetime.fromisoformat(result["available_at"]) > datetime.fromisoformat(result["computed_at"])
    assert result["training_label_end_column"] == "label_witness_day"
    assert result["full_market_stage_membership_complete"] is False
    assert result["execution_admitted"] is False
    assert not set(m._LABEL_FIELDS).intersection(pd.read_csv(out / "event_features.csv").columns)
    with pytest.raises(FileExistsError):
        m.build("synthetic-view", out)


def test_source_failure_keeps_final_output_absent_and_closes_source(tmp_path, monkeypatch):
    source = Source(run_frames(), fail=dates(7)[2])
    monkeypatch.setattr(m, "NativeHistorySource", lambda view: source)
    out = tmp_path / "failed"
    with pytest.raises(ValueError, match="source failure"):
        m.build("synthetic-view", out)
    assert source.closed and not out.exists()
