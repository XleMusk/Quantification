"""Reconcile cached 510300 sources without admitting an executable feed."""

import argparse
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uuid

import numpy as np
import pandas as pd

from ..contracts import Price, PriceBasis
from ..market_dataset import write_json
from ..settings import DATA_ROOT
from .full_market_etf import SOURCES


CODE = "510300"
FIELDS = ("open", "high", "low", "close")


def archived(root, receipt):
    root = Path(root).resolve()
    path = (root / receipt["file"]).resolve()
    path.relative_to(root)
    content = path.read_bytes()
    if sha256(content).hexdigest() != receipt["sha256"]:
        raise ValueError("ETF source receipt hash changed")
    return content


def tencent_days(payload):
    if payload.get("code") != 0:
        raise ValueError("Tencent source did not return success")
    source = payload.get("data", {}).get("sh" + CODE, {})
    rows = source.get("day")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Explicit unadjusted day rows required; no qfq/hfq fallback")
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 11 or not isinstance(row[6], dict):
            raise ValueError("Unexpected Tencent daily row layout")
        day = date.fromisoformat(row[0]).isoformat()
        if day != row[0]:
            raise ValueError("Tencent date is not canonical")
        prices = [float(Price.from_rmb(row[i], PriceBasis.RAW, "mill").rmb) for i in (1, 3, 4, 2)]
        volume, amount = Decimal(row[5]), Decimal(row[8])
        if (not volume.is_finite() or not amount.is_finite() or volume <= 0 or amount <= 0
                or volume != int(volume)):
            raise ValueError("Unexpected Tencent volume/amount candidates")
        result.append(dict(zip(("date", *FIELDS, "tencent_volume_field", "tencent_amount_field"),
                               (day, *prices, int(volume), float(amount)))))
    frame = pd.DataFrame(result)
    if frame.date.duplicated().any() or not frame.date.is_monotonic_increasing:
        raise ValueError("Duplicate or unordered Tencent dates")
    if (frame.high.lt(frame[list(FIELDS)].max(axis=1)).any()
            or frame.low.gt(frame[list(FIELDS)].min(axis=1)).any()):
        raise ValueError("Invalid Tencent OHLC")
    return frame


def cash_events(folder, code=CODE):
    folder = Path(folder)
    path = folder / "supplements/cash_events.csv"
    events = pd.read_csv(path, dtype=str, keep_default_na=False)
    events = events.loc[events.instrument_code.eq(code) & events.exchange.eq("SSE")].copy()
    if events.ex_date.duplicated().any():
        raise ValueError("Duplicate ETF cash entitlement")
    for row in events.to_dict("records"):
        dates = [date.fromisoformat(row[k]) for k in ("announced_at", "record_date", "ex_date", "pay_date")]
        cash = Decimal(row["cash_rmb_per_unit"])
        if dates != sorted(dates) or dates[1] >= dates[2] or not cash.is_finite() or cash <= 0:
            raise ValueError("Invalid ETF cash event chronology/value")
        if not row["source_url"].startswith("https://") or not archived(folder, {
                "file": row["source_file"], "sha256": row["source_sha256"]}).startswith(b"%PDF-"):
            raise ValueError("ETF cash event needs its archived issuer PDF")
    return events


def distribution_coverage(folder, events, start, end, code=CODE):
    path = Path(folder) / "distribution_periods.json"
    periods = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    covered, reviewed = set(), []
    for item in periods:
        if item.get("instrument_code", CODE) != code:
            continue
        first, last = date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
        if first > last or not item["source_url"].startswith("https://"):
            raise ValueError("Invalid distribution-period evidence")
        if item.get("source_kind") in {"gt_complete_cash_dividend_history_v1", "chinaamc_complete_cash_dividend_history_v1",
                                       "huaan_public_distribution_table_v1"}:
            if item["source_kind"] == "gt_complete_cash_dividend_history_v1":
                from .gt_fund_history import period_evidence
            elif item["source_kind"] == "chinaamc_complete_cash_dividend_history_v1":
                from .chinaamc_fund_history import period_evidence
            else:
                from .huaan_fund_history import period_evidence
            history = (Path(folder) / item["history_folder"]).resolve()
            history.relative_to(Path(folder).resolve())
            proof = period_evidence(history, events, item["start"], item["end"], code)
            proof["source_file"] = str(Path(item["history_folder"]) / proof["source_file"]).replace("\\", "/")
            if any(item.get(key) != value for key, value in proof.items()):
                raise ValueError("Issuer-history period differs from its archived source and PDF reconciliation")
        elif (item.get("source_kind") not in (None, "issuer_report_pdf")
              or not archived(folder, {"file": item["source_file"], "sha256": item["source_sha256"]}).startswith(b"%PDF-")):
            raise ValueError("Invalid distribution-period evidence")
        expected = sorted((row["ex_date"], Decimal(row["cash_rmb_per_unit"])) for row in item["events"])
        if any(not item["start"] <= day <= item["end"] for day, _ in expected):
            raise ValueError("Reported distribution outside its reporting period")
        selected = events.loc[events.ex_date.between(item["start"], item["end"])]
        actual = sorted((row["ex_date"], Decimal(row["cash_rmb_per_unit"])) for row in selected.to_dict("records"))
        if actual != expected:
            raise ValueError("Cash events conflict with the reviewed period report")
        covered.update(pd.date_range(first, last).strftime("%Y-%m-%d"))
        reviewed.append(item)
    uncovered = [day for day in pd.date_range(start, end).strftime("%Y-%m-%d") if day not in covered]
    gaps = []
    for day in uncovered:
        if gaps and date.fromisoformat(day) == date.fromisoformat(gaps[-1]["end"]) + timedelta(days=1):
            gaps[-1]["end"] = day
        else:
            gaps.append({"start": day, "end": day})
    return {"reviewed_periods": reviewed, "uncovered_periods": gaps, "complete_for_requested_period": not gaps}


def reconcile(sina, tencent, calendar, events):
    if calendar != sorted(set(calendar)) or not calendar:
        raise ValueError("Invalid reconciliation calendar")
    s = sina.copy()
    if s.empty or not s.symbol.eq(CODE).all() or s.date.duplicated().any():
        raise ValueError("Sina identity/date mismatch")
    s["date"] = pd.to_datetime(s.date.astype(str), format="%Y%m%d", errors="raise").dt.strftime("%Y-%m-%d")
    s = s.loc[s.date.between(calendar[0], calendar[-1])].sort_values("date")
    values = s[[*FIELDS, "volume"]].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(values).all().all() or values.volume.le(0).any() or values.volume.mod(1).ne(0).any():
        raise ValueError("Invalid Sina quantities/prices")
    for field in FIELDS:
        for value in values[field]:
            Price.from_rmb(value, PriceBasis.RAW, "mill")
    s.loc[:, [*FIELDS, "volume"]] = values
    t = tencent.loc[tencent.date.between(calendar[0], calendar[-1])]
    joined = s.merge(t, on="date", how="outer", suffixes=("_sina", "_tencent"),
                     indicator=True, validate="one_to_one").sort_values("date")
    if not joined.date.isin(calendar).all():
        raise ValueError("Source bars occur outside the pinned exchange calendar")
    paired = joined.loc[joined._merge.eq("both")].copy()
    if paired.empty:
        raise ValueError("No paired source observations")
    paired["ohlc_match"] = True
    for field in FIELDS:
        paired["ohlc_match"] &= (paired[field + "_sina"] - paired[field + "_tencent"]).abs().le(1e-9)
    # Candidate interpretation only: documentation contradicts observed payload units.
    paired["volume_rounding_residual_units"] = paired.volume - paired.tencent_volume_field * 100
    paired["volume_candidate_match"] = np.floor(paired.volume / 100 + .5).eq(paired.tencent_volume_field)
    paired["amount_rmb_candidate"] = paired.tencent_amount_field.map(lambda value: float(Decimal(str(value)) * 10000))
    paired["vwap_candidate"] = paired.amount_rmb_candidate / paired.volume
    paired["vwap_within_ohlc"] = paired.vwap_candidate.between(paired.low_sina, paired.high_sina)
    # Do not bridge a missing exchange session or invent the first reference close.
    previous = s.set_index("date").close.reindex(calendar).shift()
    by_date = events.set_index("ex_date").cash_rmb_per_unit.map(float)
    paired["cash_rmb_per_unit"] = paired.date.map(by_date).fillna(0)
    paired["reference_close_candidate_rmb"] = paired.date.map(previous) - paired.cash_rmb_per_unit
    paired["symbol"] = CODE + ".SSE"
    missing = sorted(set(calendar) - set(paired.date))
    conflicts = paired.loc[~(paired.ohlc_match & paired.volume_candidate_match & paired.vwap_within_ohlc), "date"].tolist()
    report = {"paired_rows": len(paired), "missing_calendar_sessions": missing,
              "source_only_dates": joined.loc[joined._merge.ne("both"), ["date", "_merge"]].astype(str).to_dict("records"),
              "ohlc_conflict_rows": int((~paired.ohlc_match).sum()),
              "volume_candidate_conflict_rows": int((~paired.volume_candidate_match).sum()),
              "vwap_outside_ohlc_rows": int((~paired.vwap_within_ohlc).sum()), "conflict_dates": conflicts,
              "volume_residual_min_units": float(paired.volume_rounding_residual_units.min()),
              "volume_residual_max_units": float(paired.volume_rounding_residual_units.max()),
              "price_volume_candidate_reconciled": not missing and not conflicts}
    return paired.drop(columns="_merge"), report


def inspect_baostock(folder, paired, events, calendar):
    folder = Path(folder)
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    if receipt.get("status") != "complete_source_acquisition" or receipt.get("symbol") != CODE + ".SSE":
        raise ValueError("Expected completed 510300 BaoStock acquisition")
    entries = receipt["entries"]
    frames = {}
    actions = []
    for entry in entries:
        archived(folder, entry)
        path = folder / entry["file"]
        frame = (pd.read_parquet(path) if entry["kind"] in {"raw", "adjusted"}
                 else pd.read_csv(path, dtype=str, keep_default_na=False))
        if len(frame) != entry["rows"]:
            raise ValueError("BaoStock ETF source row count changed")
        if entry["kind"] in {"raw", "adjusted"}:
            if entry["kind"] in frames:
                raise ValueError("Duplicate BaoStock ETF price source")
            frames[entry["kind"]] = frame
        else:
            actions.extend(frame.to_dict("records"))
    raw, adjusted = frames["raw"].copy(), frames["adjusted"]
    if (raw.empty or not raw.code.eq("sh" + "." + CODE).all() or raw.date.duplicated().any()
            or not raw.date.isin(calendar).all() or not raw.adjustflag.eq("3").all()):
        raise ValueError("Invalid BaoStock raw ETF identity/period/adjustment")
    for field in (*FIELDS, "preclose", "volume", "amount"):
        raw[field] = pd.to_numeric(raw[field], errors="raise")
        if not np.isfinite(raw[field]).all():
            raise ValueError("Non-finite BaoStock ETF field")
    if raw.volume.le(0).any() or raw.amount.le(0).any():
        raise ValueError("Inactive ETF observations need separate reconciliation")
    joined = raw.merge(paired, on="date", suffixes=("_baostock", "_paired"), validate="one_to_one")
    if len(joined) != len(raw):
        raise ValueError("BaoStock ETF observations have no paired source")
    ohlc = pd.Series(False, index=joined.index)
    for field in FIELDS:
        ohlc |= (joined[field] - joined[field + "_sina"]).abs().gt(1e-9)
    ref = (joined.preclose - joined.reference_close_candidate_rmb).abs().gt(.0005)
    raw_status = raw.tradestatus.value_counts().to_dict()
    risk_status = raw.isST.value_counts().to_dict()
    known = set(events.ex_date)
    reported = {row["dividOperateDate"] for row in actions}
    return {"folder": str(folder), "receipt_sha256": sha256((folder / "receipt.json").read_bytes()).hexdigest(),
            "requested_start": receipt["start"], "requested_end": receipt["end"],
            "rows": len(raw), "first_date": raw.date.min(), "last_date": raw.date.max(),
            "missing_requested_calendar_sessions": sorted(set(calendar) - set(raw.date)),
            "ohlc_conflict_rows": int(ohlc.sum()), "volume_conflict_rows": int(joined.volume_baostock.ne(joined.volume_paired).sum()),
            "amount_rounding_conflict_rows": int((joined.amount - joined.amount_rmb_candidate).abs().gt(50.001).sum()),
            "reference_candidate_conflict_rows": int(ref.sum()), "tradestatus_raw_counts": raw_status,
            "isST_raw_counts": risk_status, "requested_adjusted_flag": "2",
            "returned_adjusted_flags": sorted(adjusted.adjustflag.unique().tolist()),
            "known_primary_cash_events_missing_from_provider": sorted(known - reported),
            "execution_admitted": False,
            "limits": ["Requested full period is not returned; do not invent earlier status/reference bars",
                       "Requested qfq may return flag3; do not relabel those quotes as adjusted",
                       "Fund isST field semantics are not established; keep values unchanged",
                       "Empty provider dividend results are not proof of zero distributions"]}


def run(sina_folder, tencent_folder, calendar_path, baostock_folder=None):
    sina_folder, tencent_folder, calendar_path = map(Path, (sina_folder, tencent_folder, calendar_path))
    state = json.loads((sina_folder / "status.json").read_text(encoding="utf-8"))
    if state.get("provider") != "sina-ohlcv" or state.get("source") != SOURCES["sina-ohlcv"]:
        raise ValueError("Expected saved Sina source receipts")
    receipt = state["receipts"][CODE]
    if receipt["status"] != "acquired_unvalidated" or receipt["file"] != CODE + ".parquet":
        raise ValueError("510300 source is unavailable")
    archived(sina_folder, receipt)
    frame = pd.read_parquet(sina_folder / receipt["file"])
    if len(frame) != receipt["rows"]:
        raise ValueError("Sina row count changed")
    tx_receipt = json.loads((tencent_folder / "receipt.json").read_text(encoding="utf-8"))
    url = urlparse(tx_receipt["url"])
    param = parse_qs(url.query).get("param", [""])[0].split(",")
    if (url.scheme != "https" or url.hostname != "proxy.finance.qq.com" or len(param) != 6
            or param[:2] != ["sh" + CODE, "day"] or param[-1] != ""):
        raise ValueError("Tencent receipt must request explicit raw daily prices")
    tencent = tencent_days(json.loads(archived(tencent_folder, tx_receipt)))
    calendar = pd.read_csv(calendar_path, dtype=str).date.tolist()
    events = cash_events(sina_folder)
    paired, result = reconcile(frame, tencent, calendar, events)
    coverage = distribution_coverage(tencent_folder, events, calendar[0], calendar[-1])
    destination = DATA_ROOT / "research/etf_reconciled" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    destination.mkdir(parents=True, exist_ok=False)
    target = destination / "paired_candidates.parquet"
    paired.to_parquet(target, index=False)
    result.update(status="completed_source_reconciliation_diagnostic", execution_admitted=False,
                  strategy_validated=False, symbol=CODE + ".SSE", start=calendar[0], end=calendar[-1],
                  sources={"sina_folder": str(sina_folder), "sina": receipt,
                           "tencent_folder": str(tencent_folder), "tencent": tx_receipt,
                           "calendar": str(calendar_path), "calendar_sha256": sha256(calendar_path.read_bytes()).hexdigest(),
                           "cash_events_sha256": sha256((sina_folder / "supplements/cash_events.csv").read_bytes()).hexdigest()},
                  derived_file=target.name, derived_sha256=sha256(target.read_bytes()).hexdigest(),
                  distributions=coverage,
                  blockers=["A complete independently verified fund status and raw reference-close series is unavailable",
                            "Volume/amount units are empirically reconciled candidates, not independent exchange-unit verification",
                            "Sina AKShare documentation says lots, inconsistent with observed payload values",
                            "Full-period reference closes are derived candidates, not a verified published historical series",
                            "Distribution-period reports do not establish complete fund share actions or suspension history"],
                  limits=["510300 only; no conversion of other funds or StockDB fields",
                          "No tradestatus/isST/adjustflag columns or HistoricalFeed manifest generated",
                          "No orders, account changes or return comparison"])
    if not coverage["complete_for_requested_period"]:
        result["blockers"].append("Distribution reports do not cover the full requested period")
    if baostock_folder is not None:
        result["baostock_comparison"] = inspect_baostock(baostock_folder, paired, events, calendar)
    write_json(destination / "result.json", result)
    return destination, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sina-folder", type=Path, required=True)
    parser.add_argument("--tencent-folder", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--baostock-folder", type=Path)
    args = parser.parse_args(argv)
    folder, result = run(args.sina_folder, args.tencent_folder, args.calendar, args.baostock_folder)
    print(json.dumps({"folder": str(folder), **result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
