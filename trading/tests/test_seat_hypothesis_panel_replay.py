import csv

from trading.research.seat_hypothesis_panel_replay import load_panel


def test_panel_loader_excludes_bse_and_builds_delayed_signal_path(tmp_path):
    candidates = tmp_path / "candidates.csv"
    with candidates.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["trade_date", "symbol", "net_buy_to_reported_turnover", "hypothesis_h1"])
        writer.writeheader()
        writer.writerow({"trade_date": "2026-10-08", "symbol": "000001.SZSE", "net_buy_to_reported_turnover": "0.02", "hypothesis_h1": "True"})
        writer.writerow({"trade_date": "2026-10-08", "symbol": "920001.BJ", "net_buy_to_reported_turnover": "0.03", "hypothesis_h1": "True"})
    bars = tmp_path / "bars.csv"
    with bars.open("w", encoding="utf-8", newline="") as stream:
        fields = ["symbol", "date", "available", "error", "open", "high", "low", "close", "volume", "amount", "pre_close", "is_st"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for day, price in (("2026-10-08", 10), ("2026-10-09", 11), ("2026-10-10", 12)):
            writer.writerow({"symbol": "000001.SZSE", "date": day, "available": "True", "error": "", "open": price, "high": price, "low": price, "close": price, "volume": 1000000, "amount": 10000000, "pre_close": price, "is_st": "False"})
    feed, candidates_frame, symbols, path_dates = load_panel(candidates, bars, lag=1)
    assert symbols == ["000001.SZSE"]
    assert path_dates == ["2026-10-09", "2026-10-10"]
    assert feed.signals["2026-10-09"][0]["symbol"] == "000001.SZSE"
    assert len(candidates_frame) == 1
