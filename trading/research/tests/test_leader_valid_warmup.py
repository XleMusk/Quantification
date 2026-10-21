from copy import deepcopy
from types import SimpleNamespace

import pandas as pd

from trading.research.leader_valid_warmup import restore_valid_warmup
from trading.research.tests.test_leader_eight_type_historical_native import bars, A, B


C, D, FUTURE = "000003.SZSE", "000004.SZSE", "000005.SZSE"
SESSIONS = pd.bdate_range("2025-08-01", "2026-01-05").strftime("%Y-%m-%d").tolist()
FIRST = SESSIONS[-1]
WARMUP = SESSIONS[-62:-1]


class FakeSource:
    def __init__(self, frames):
        self.frames, self.calls = frames, []
        self.provenance = {"source_view": "synthetic-original-pinned-native", "raw_sha256": "fixture"}

    def frame(self, day, symbols):
        self.calls.append((day, list(symbols)))
        result = self.frames[day].loc[self.frames[day].symbol.isin(symbols)].copy()
        result.attrs["provenance"] = {"day": day, "row_group": SESSIONS.index(day), "symbols": list(symbols)}
        return result


def fixture():
    raw = bars(WARMUP + [FIRST], [A, B, C, D])
    raw["symbol"] = ""
    raw["symbol"] = raw.code.str[3:] + ".SZSE"
    # A has 59 valid observations; a suspension and an ST row do not count.
    raw.loc[raw.code.eq("sz.000001") & raw.date.isin(WARMUP[:3]),
        ["tradestatus", "open", "high", "low", "volume", "amount"]] = ["0", "0", "0", "0", "", ""]
    raw.loc[raw.code.eq("sz.000001") & raw.date.eq(WARMUP[3]), "isST"] = "1"
    raw.loc[raw.code.eq("sz.000003") & raw.date.isin(WARMUP[:3]), "tradestatus"] = "0"
    ipo = WARMUP[-10]
    raw = raw.loc[~(raw.code.eq("sz.000002") & raw.date.lt(ipo))].copy()
    records = {A: {"ipo_date": "1991-04-03"}, B: {"ipo_date": ipo}, D: {"ipo_date": "1991-04-03"}}
    q = SimpleNamespace(records=records)
    prior_days = [day for day in SESSIONS if day < WARMUP[0]]
    frames = {}
    for day in prior_days:
        frame = bars([day], [A, B, C, D])
        frame["symbol"] = frame.code.str[3:] + ".SZSE"
        frames[day] = frame
    # First older A row is a halt: preserve it while continuing backwards.
    frames[prior_days[-1]].loc[frames[prior_days[-1]].code.eq("sz.000001"),
        ["tradestatus", "open", "high", "low", "volume", "amount"]] = ["0", "0", "0", "0", "", ""]
    source = FakeSource(frames)
    return raw, q, source, {day: source for day in prior_days}


def test_only_deficient_old_first_day_members_are_requested_and_halts_preserved_not_counted():
    raw, q, source, owners = fixture()
    prefix, proof = restore_valid_warmup(raw, first_day=FIRST, warmup_days=WARMUP,
        scope_members=[A, B, C, D], qualification=q, fixed_sessions=SESSIONS, source_by_day=owners)
    assert proof["initial_valid_count"][A] == 58 and proof["final_valid_count"][A] == 61
    assert proof["outcomes"][A] == "restored_61_valid_observations"
    assert len(source.calls) == 4 and all(symbols == [A] for _, symbols in source.calls)
    assert len(prefix) == 4 and prefix.tradestatus.eq("0").sum() == 1
    halted = prefix.loc[prefix.tradestatus.eq("0")].iloc[0]
    assert halted.volume == "" and halted.open == "0"
    assert proof["outcomes"][B] == "known_insufficient_posthoc_listing_history_retains_unknown"
    assert proof["outcomes"][C] == "unknown_listing_metadata_not_restored"
    assert proof["outcomes"][D] == "original_buffer_complete"
    assert all(item["scope"] == "explicit_symbol_projection_not_full_market_history"
        and item["row_group_provenance"] and item["original_source_provenance"] for item in proof["projection_day_sources"])


def test_future_rows_cannot_select_targets_or_fill_old_buffers_and_missing_exact_source_stops():
    raw, q, source, owners = fixture()
    future = bars(["2026-01-06"], [A, FUTURE])
    future["symbol"] = future.code.str[3:] + ".SZSE"
    q.records[FUTURE] = {"ipo_date": "1991-04-03"}
    raw = pd.concat([raw, future], ignore_index=True)
    prefix, proof = restore_valid_warmup(raw, first_day=FIRST, warmup_days=WARMUP,
        scope_members=[A, B, C, D], qualification=q, fixed_sessions=SESSIONS, source_by_day={})
    assert not source.calls and prefix.empty
    assert proof["initial_valid_count"][A] == 58 and FUTURE not in proof["initial_valid_count"]
    assert proof["missing_source"]["day"] < WARMUP[0]
    assert proof["outcomes"][A] == "exact_source_missing_retains_unknown"
    assert proof["future_rows_used_for_selection_or_counting"] is False


def test_three_exact_session_cache_preserves_panels_risks_and_complete_source_day_list():
    from trading.research import leader_eight_type_historical_native as history
    from trading.research.tests.test_leader_eight_type_historical_native import CALENDAR, OBSERVED, qualification
    days = ["2026-01-09", "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-15"]
    raw = bars(days)
    full = history.derive_panels(raw, days, [A, B], CALENDAR, qualification(), observed_at=OBSERVED)
    bounded = history.derive_panels(raw, days, [A, B], CALENDAR, qualification(), observed_at=OBSERVED,
        rolling_observation_cache=True)
    assert bounded == full
    assert bounded[days[-1]]["source_days_used"] == days
