"""Download a bounded, pre-period-selected historical pilot, never today's pool."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import socket
import uuid

import pandas as pd

from a_stock_committee.market_data import BAOSTOCK_FIELDS, baostock_session, normalize_symbol
from .settings import DATASET


def checked(result):
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock: {result.error_code}: {result.error_msg}")
    frame = result.get_data()
    if result.error_code != "0":
        raise RuntimeError(result.error_msg)
    return frame


def prepare_data(folder=DATASET, end=None):
    index_folder = Path(folder)
    index_folder.mkdir(parents=True, exist_ok=True)
    folder = index_folder / "versions" / f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=True)
    end = end or date.today().isoformat()
    socket.setdefaulttimeout(20)
    stamp = datetime.now().astimezone().isoformat()
    manifest = {"status": "downloading", "downloaded_at": stamp, "source": "BaoStock",
                "selection_date": "2023-12-29", "start": "2024-01-02", "requested_end": end, "folder": str(folder),
                "scope": "Historical CSI300 snapshot; first five main-board codes on each exchange",
                "limitations": ["10-name engineering pilot, not a full-market strategy validation",
                                "Provider corporate-action and historical-status coverage is not independently verified",
                                "Daily open matching and prior-volume caps do not reproduce auction queues",
                                "Dividend tax uses a conservative fixed reserve, not individual holding-period tax settlement"]}
    def save():
        (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    try:
        with baostock_session() as bs:
            members = checked(bs.query_hs300_stocks(date="2023-12-29"))
            if members.empty or pd.to_datetime(members.updateDate).gt(pd.Timestamp("2023-12-29")).any():
                raise ValueError("Provider did not return a pre-period membership snapshot")
            parts = [members[members.code.str.match(pattern)].sort_values("code").head(5)
                     for pattern in (r"sh\.60\d{4}$", r"sz\.00\d{4}$")]
            universe = pd.concat(parts).copy()
            if len(universe) != 10:
                raise ValueError("Historical pilot membership incomplete")
            universe.to_csv(folder / "selection.csv", index=False)
            calendar = checked(bs.query_trade_dates(start_date="2023-01-01", end_date=end))
            calendar = calendar.loc[calendar.is_trading_day.eq("1"), ["calendar_date"]].rename(columns={"calendar_date": "date"})
            calendar.to_csv(folder / "calendar.csv", index=False)
            raw_frames, adjusted_frames, basic_frames, action_frames = [], [], [], []
            for code in universe.code:
                symbol = normalize_symbol(code).vt_symbol
                for flag, target in (("3", raw_frames), ("2", adjusted_frames)):
                    path = folder / f"{symbol}.{flag}.parquet"
                    frame = checked(bs.query_history_k_data_plus(code, BAOSTOCK_FIELDS, start_date="2023-01-01", end_date=end, frequency="d", adjustflag=flag))
                    if frame.empty:
                        raise ValueError(f"Missing history: {code}")
                    frame["symbol"] = symbol
                    frame.to_parquet(path, index=False)
                    target.append(frame)
                basic = checked(bs.query_stock_basic(code=code))
                basic["symbol"] = symbol
                basic_frames.append(basic)
                for year in range(2023, pd.Timestamp(end).year + 1):
                    actions = checked(bs.query_dividend_data(code, str(year), "operate"))
                    actions["symbol"] = symbol
                    action_frames.append(actions)
                print(f"Prepared {symbol}", flush=True)
            pd.concat(raw_frames).to_parquet(folder / "raw.parquet", index=False)
            pd.concat(adjusted_frames).to_parquet(folder / "adjusted.parquet", index=False)
            pd.concat(basic_frames).to_csv(folder / "instruments.csv", index=False)
            pd.concat(action_frames).drop_duplicates().to_csv(folder / "actions.csv", index=False)
            expected = str(calendar.date.iloc[-1])
            if any(str(frame.date.max()) != expected for frame in raw_frames + adjusted_frames):
                raise ValueError(f"Incomplete provider publication for {expected}; keep previous snapshot")
            manifest.update(status="ready", symbols=[normalize_symbol(c).vt_symbol for c in universe.code],
                            end=expected, rows=sum(map(len, raw_frames)))
            save()
            from .feed import HistoricalFeed
            HistoricalFeed(folder)
            pointer = index_folder / "manifest.next.json"
            pointer.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            pointer.replace(index_folder / "manifest.json")
    except Exception as error:
        manifest.update(status="failed", error=f"{type(error).__name__}: {error}")
        save()
        raise
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", default=None)
    args = parser.parse_args()
    print(json.dumps(prepare_data(end=args.end), ensure_ascii=False, indent=2))
