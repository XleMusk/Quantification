from copy import deepcopy

import pandas as pd
import pytest

from trading.research.valuation_gaps import episode_rows, records, stale_observations


SYMBOL = "600499.SSE"


def fixture():
    days = ["2026-01-14", "2026-01-15", "2026-01-16", "2026-01-19"]
    marks = [{"date": day, "equity": 10000., "stale_symbols": [SYMBOL] if i in (1, 2) else []}
             for i, day in enumerate(days)]
    shares = pd.DataFrame([{"date": day, "symbol": SYMBOL, "quantity": 100,
                           "stale_valuation": bool(marks[i]["stale_symbols"])} for i, day in enumerate(days)])
    quotes = pd.DataFrame([{"date": day, "symbol": SYMBOL, "open": price, "close": price,
        "tradestatus": int(i not in (1, 2)), "volume": None if i in (1, 2) else 1000,
        "amount": None if i in (1, 2) else 10000} for i, (day, price) in enumerate(zip(days, [10., 10., 10., 11.]))])
    return marks, shares, quotes


def test_suspension_keeps_prior_trade_age_and_observed_exposure_without_mutation():
    marks, shares, quotes = fixture()
    before = deepcopy(marks)
    rows = stale_observations(marks, shares, quotes)
    assert rows.reason.tolist() == ["provider_suspended"] * 2
    assert rows.last_tradable_date.tolist() == ["2026-01-14"] * 2
    assert rows.account_sessions_since_trade.tolist() == [1, 2]
    assert rows.raw_close_mark_cny.tolist() == [1000., 1000.]
    assert rows.marked_exposure_fraction.tolist() == [.1, .1]
    assert rows.raw_volume.isna().all() and rows.liquidation_value_cny.isna().all()
    episodes = records(episode_rows(rows, marks, quotes, []))
    assert len(episodes) == 1 and episodes[0]["held_stale_sessions"] == 2
    assert episodes[0]["next_tradable_date"] == "2026-01-19"
    assert episodes[0]["raw_reopen_close_ratio"] == pytest.approx(.1)
    assert episodes[0]["next_saved_account_return"] == 0
    assert marks == before


def test_missing_and_unpriceable_close_remain_unknown_not_carried_or_future_filled():
    marks, shares, quotes = fixture()
    quotes = quotes.loc[quotes.date.ne("2026-01-15")].copy()
    quotes.loc[quotes.date.eq("2026-01-16"), "close"] = None
    rows = stale_observations(marks, shares, quotes)
    assert rows.reason.tolist() == ["missing_raw_row", "unpriceable_raw_close"]
    assert rows.raw_close_mark_cny.isna().all()
    assert rows.marked_exposure_fraction.isna().all()
    assert rows.last_tradable_close.tolist() == [10., 10.]
    limited = marks[:-1]
    episodes = records(episode_rows(rows, limited, quotes, [
        {"kind": "corporate_action", "symbol": SYMBOL, "day": "2026-01-16"}]))
    assert episodes[0]["next_tradable_date"] is None
    assert episodes[0]["raw_reopen_close_ratio"] is None
    assert episodes[0]["company_actions_in_interval"] == 1
    assert episodes[0]["reopening_status"] == "not_observed_within_saved_window"


def test_source_disagreement_and_missing_held_quantity_are_not_suspension():
    marks, shares, quotes = fixture()
    quotes.loc[quotes.date.eq("2026-01-15"), "tradestatus"] = 1
    rows = stale_observations(marks, shares, quotes)
    assert rows.iloc[0].reason == "saved_flag_disagrees_with_tradable_quote"
    with pytest.raises(ValueError, match="held quantity"):
        stale_observations(marks, shares.loc[shares.date.ne("2026-01-15")], quotes)


def test_saved_api_distinguishes_unprepared_from_a_matched_zero_stale_case(tmp_path, monkeypatch):
    import json
    from trading.research import valuation_gaps

    identifier, signal = "20261004_194604_e925cb30", "qarp_only"
    root = tmp_path / "accounts"
    monkeypatch.setattr(valuation_gaps.settings, "ROOT", tmp_path)
    monkeypatch.setattr(valuation_gaps.replay_reports, "_read", lambda _: ({"results": [{"signal": signal}]}, root))
    assert valuation_gaps.saved(identifier, signal)["status"] == "not_prepared"
    folder = tmp_path / "outputs/valuation_gaps/20261004_200651_3065d375"
    folder.mkdir(parents=True)
    receipt = {"id": folder.name, "status": "completed_saved_valuation_gaps", "limits": [], "cases": [
        {"case": identifier + ":" + signal, "source_account": str(root / signal / "account.json"),
         "stale_dates": 0, "status": "completed", "episodes": []}]}
    (folder / "run.json").write_text(json.dumps(receipt), encoding="utf-8")
    value = valuation_gaps.saved(identifier, signal)
    assert value["status"] == "prepared" and value["stale_dates"] == 0
    assert value["execution_admitted"] is False
    receipt["cases"][0]["source_account"] = str(tmp_path / "other/account.json")
    (folder / "run.json").write_text(json.dumps(receipt), encoding="utf-8")
    assert valuation_gaps.saved(identifier, signal)["status"] == "not_prepared"
    with pytest.raises(FileNotFoundError):
        valuation_gaps.saved(identifier, "../unknown")
