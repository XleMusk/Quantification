from decimal import Decimal
from hashlib import sha256
import json

import pandas as pd
import pytest

from a_stock_committee.market_data import BAOSTOCK_FIELDS
from trading.research.adjusted_prices import baseline_coverage, reconstruct


def raw_frame():
    rows = []
    for day, opening, close, preclose in [("2017-05-24", "15.3800", "15.4700", "15.4300"),
                                        ("2017-05-25", "11.7500", "12.9300", "11.7500")]:
        row = dict.fromkeys(BAOSTOCK_FIELDS.split(","), "")
        row.update(date=day, code="sh.600000", open=opening, close=close, preclose=preclose,
                   high="16.0000", low="11.0000", volume="1000", amount="15470.0000",
                   adjustflag="3", tradestatus="1", isST="0")
        rows.append(row)
    return pd.DataFrame(rows)


def factor_frame():
    return pd.DataFrame({"code": ["sh.600000"] * 2, "dividOperateDate": ["2016-06-23", "2017-05-25"],
                         "foreAdjustFactor": ["0.759535", "1.000000"], "backAdjustFactor": ["7.128788", "9.385732"],
                         "adjustFactor": ["99", "99"]})


def test_official_example_uses_latest_level_not_products_and_preserves_raw():
    raw, factors = raw_frame(), factor_frame()
    before = raw.copy(deep=True)
    result = reconstruct(raw, factors, "2017-05-25")
    assert Decimal(result.loc[0, "open"]) == Decimal("11.6816483")
    assert Decimal(result.loc[1, "open"]) == Decimal("11.7500")
    assert Decimal(result.loc[0, "preclose"]) == Decimal("15.4300") * Decimal("0.759535")
    assert result.source_forward_factor.tolist() == ["0.759535", "1.000000"]
    assert result.factor_effective_date.tolist() == ["2016-06-23", "2017-05-25"]
    assert result.adjustflag.tolist() == ["2", "2"]
    assert result[["volume", "amount", "tradestatus"]].equals(raw[["volume", "amount", "tradestatus"]])
    assert raw.equals(before) and factors.adjustFactor.eq("99").all()
    assert result.attrs["price_basis"] == "reconstructed_research_only_not_order_prices"


def test_missing_baseline_or_terminal_anchor_never_defaults_one():
    with pytest.raises(ValueError, match="initial factor"):
        reconstruct(raw_frame(), factor_frame().iloc[1:], "2017-05-25")
    with pytest.raises(ValueError, match="Missing factor history"):
        reconstruct(raw_frame(), factor_frame().iloc[:0], "2017-05-25")
    factors = factor_frame()
    factors.at[1, "foreAdjustFactor"] = "0.95"
    with pytest.raises(ValueError, match="Terminal forward anchor"):
        reconstruct(raw_frame(), factors, "2017-05-25")


def test_future_or_duplicate_factors_block_before_conversion():
    factors = factor_frame()
    factors.at[1, "dividOperateDate"] = "2017-05-26"
    with pytest.raises(ValueError, match="after cutoff"):
        reconstruct(raw_frame(), factors, "2017-05-25")
    factors = pd.concat([factor_frame(), factor_frame().iloc[:1]])
    with pytest.raises(ValueError, match="Duplicate raw/factor"):
        reconstruct(raw_frame(), factors, "2017-05-25")


def test_suspended_empty_prices_stay_empty_not_zero_or_carried():
    raw = raw_frame()
    raw.loc[0, ["open", "high", "low", "close", "preclose", "volume", "amount"]] = ""
    raw.at[0, "tradestatus"] = "0"
    result = reconstruct(raw, factor_frame(), "2017-05-25")
    assert result.loc[0, ["open", "high", "low", "close", "preclose", "volume", "amount"]].eq("").all()


@pytest.mark.parametrize("value", ["", "inf", "NaN", "0", "-1", 15.38])
def test_invalid_active_prices_fail_without_coercion(value):
    raw = raw_frame()
    raw.at[0, "open"] = value
    with pytest.raises(ValueError):
        reconstruct(raw, factor_frame(), "2017-05-25")


def test_raw_adjusted_inputs_and_duplicate_price_rows_are_rejected():
    raw = raw_frame()
    raw.at[0, "adjustflag"] = "2"
    with pytest.raises(ValueError, match="raw prices"):
        reconstruct(raw, factor_frame(), "2017-05-25")
    raw = pd.concat([raw_frame(), raw_frame().iloc[:1]])
    with pytest.raises(ValueError, match="Duplicate raw/factor"):
        reconstruct(raw, factor_frame(), "2017-05-25")


def test_multiple_securities_with_repeated_dataframe_index_do_not_cross_write():
    second_raw, second_factors = raw_frame(), factor_frame()
    second_raw["code"] = "sz.000001"
    second_factors["code"] = "sz.000001"
    second_factors.at[0, "foreAdjustFactor"] = "0.800000"
    raw = pd.concat([raw_frame(), second_raw])
    factors = pd.concat([factor_frame(), second_factors])
    result = reconstruct(raw, factors, "2017-05-25")
    assert raw.index.tolist() == [0, 1, 0, 1]
    assert Decimal(result.iloc[0].open) == Decimal("15.3800") * Decimal("0.759535")
    assert Decimal(result.iloc[2].open) == Decimal("15.3800") * Decimal("0.800000")
    assert result.code.tolist() == raw.code.tolist()


def test_empty_price_sample_cannot_be_labelled_converted():
    with pytest.raises(ValueError, match="Empty raw price observations"):
        reconstruct(raw_frame().iloc[:0], factor_frame(), "2017-05-25")


def test_project_cli_routes_price_checks_only(monkeypatch):
    import run_committee
    from trading.research import adjusted_prices
    calls = []
    monkeypatch.setattr(adjusted_prices, "main", lambda args: calls.append(args) or 0)
    assert run_committee.main(["adjusted-price-check", "--probe", "local"] ) == 0
    assert calls == [["--probe", "local"]]


def write_views(tmp_path, raw=None, factors=None, factor_cutoff="2017-05-25"):
    raw = raw_frame() if raw is None else raw
    factors = factor_frame() if factors is None else factors
    views = [tmp_path / "raw", tmp_path / "factors"]
    for view, frame, name, kind, cutoff in zip(
            views, (raw, factors), ("raw_daily.parquet", "adjustment_events.parquet"),
            ("stock", "adjustment"), ("2017-05-25", factor_cutoff)):
        view.mkdir(parents=True)
        frame.to_parquet(view / name, index=False)
        (view / "plan.json").write_text(json.dumps({"requested_end": cutoff}), encoding="utf-8")
        digest = sha256((view / name).read_bytes()).hexdigest()
        result = {"kind": kind, "rows": len(frame), "symbols": int(frame.code.nunique()), "panel_file": name,
                  "panel_sha256": digest, "raw_daily_sha256": digest,
                  "plan_sha256": sha256((view / "plan.json").read_bytes()).hexdigest()}
        (view / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return views


def test_coverage_retains_every_code_and_missing_history_never_defaults_one(tmp_path):
    missing_start, missing_history = raw_frame().copy(), raw_frame().copy()
    missing_start["code"], missing_history["code"] = "sz.000001", "sz.000002"
    late_factor = factor_frame().iloc[1:].copy()
    late_factor["code"] = "sz.000001"
    raw, factors = write_views(tmp_path, pd.concat([raw_frame(), missing_start, missing_history]),
                               pd.concat([factor_frame(), late_factor]))
    folder, result = baseline_coverage(raw, factors, tmp_path / "check")
    assert result["raw_codes"] == 3 and result["codes_with_start_and_terminal_levels"] == 1
    assert result["missing_history_codes"] == ["sz.000002"]
    assert result["missing_start_level_codes"] == ["sz.000001", "sz.000002"]
    assert result["terminal_not_one_codes"] == ["sz.000002"]
    assert not result["execution_admitted"] and not result["adjusted_prices_verified"]
    assert {item["code"] for item in result["outcomes"]} == {"sh.600000", "sz.000001", "sz.000002"}
    assert not list(folder.glob("*.parquet"))


def test_earlier_raw_observations_change_baseline_availability(tmp_path):
    raw = raw_frame().iloc[1:].copy()
    factors = factor_frame().iloc[1:].copy()
    views = write_views(tmp_path / "later", raw, factors)
    _, result = baseline_coverage(*views, tmp_path / "later_check")
    assert result["status"] == "baseline_levels_present"
    assert not result["adjusted_prices_verified"]
    views = write_views(tmp_path / "earlier", raw_frame(), factors)
    _, result = baseline_coverage(*views, tmp_path / "earlier_check")
    assert result["status"] == "baseline_incomplete"
    assert result["missing_start_level_codes"] == ["sh.600000"]


def test_coverage_does_not_relabel_other_cutoff_or_accept_corrupted_payload(tmp_path):
    views = write_views(tmp_path / "cutoff", factor_cutoff="2017-05-26")
    with pytest.raises(ValueError, match="share a canonical cutoff"):
        baseline_coverage(*views, tmp_path / "check")
    views = write_views(tmp_path / "corrupt")
    (views[1] / "adjustment_events.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Source hash changed"):
        baseline_coverage(*views, tmp_path / "other_check")


def test_coverage_duplicate_and_invalid_factors_fail_before_green_label(tmp_path):
    factors = pd.concat([factor_frame(), factor_frame().iloc[:1]])
    views = write_views(tmp_path / "duplicate", factors=factors)
    with pytest.raises(ValueError, match="Duplicate factor identities"):
        baseline_coverage(*views, tmp_path / "check")
    factors = factor_frame()
    factors.at[0, "foreAdjustFactor"] = "0"
    views = write_views(tmp_path / "zero", factors=factors)
    with pytest.raises(ValueError, match="Nonpositive forward factor"):
        baseline_coverage(*views, tmp_path / "other_check")


def test_coverage_cli_reports_incomplete_without_discarding_lists(tmp_path, capsys):
    from trading.research.adjusted_prices import main
    views = write_views(tmp_path, factors=factor_frame().iloc[:0])
    target = tmp_path / "check"
    assert main(["--coverage-only", "--raw-view", str(views[0]), "--factor-view", str(views[1]),
                 "--out", str(target)]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["missing_history_count"] == 1 and printed["missing_start_level_count"] == 1
    saved = json.loads((target / "result.json").read_text(encoding="utf-8"))
    assert saved["missing_history_codes"] == ["sh.600000"]
