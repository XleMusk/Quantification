"""Bounded SSE website day-K acquisition and saved-source comparisons, not a feed."""

import argparse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlparse
import uuid

import pandas as pd
import requests

from .etf_reconciliation import archived
from .full_market_etf import SOURCES
from .fund_statistics import load_panel, validate as validate_statistics
from ..market_dataset import write_json
from ..settings import DATA_ROOT


RECIPE = "https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_HQ_2021.js"
ROOT = "https://yunhq.sse.com.cn:32042/v1/sh1/dayk/"
PAGE = "https://www.sse.com.cn/assortment/fund/list/etfinfo/basic/index.shtml"
PARAMS = {"begin": -1000, "end": -1, "period": "day"}
FIELDS = ("open", "high", "low", "close", "volume", "field7_amount_candidate")
SNAP_FIELDS = ("date", "time", "name", "prev_close", "open", "high", "low", "last",
               "volume", "amount", "fp_volume", "fp_amount", "tradephase")


def endpoint(code):
    if not isinstance(code, str) or not re.fullmatch(r"5[0-9]{5}", code):
        raise ValueError("Expected a six-digit SSE fund code, not an inferred fund type")
    return ROOT + code


def number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not an observed day-K number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Invalid day-K number") from error
    if not result.is_finite() or result < 0:
        raise ValueError("Non-finite or negative day-K observation")
    return result


def normalize(payload, code):
    endpoint(code)
    if not isinstance(payload, dict) or payload.get("code") != code:
        raise ValueError("SSE day-K security mismatch")
    total, begin, end = (payload.get(key) for key in ("total", "begin", "end"))
    rows = payload.get("kline")
    if (any(type(value) is not int for value in (total, begin, end))
            or not 0 <= begin <= end <= total or not isinstance(rows, list)
            or len(rows) != end - begin or not 0 < len(rows) <= 1000):
        raise ValueError("SSE day-K positions/count exceed the reviewed window or are incomplete")
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 7 or type(row[0]) is not int:
            raise ValueError("Unexpected SSE day-K seven-field layout")
        day = datetime.strptime(str(row[0]), "%Y%m%d").date().isoformat()
        if len(str(row[0])) != 8:
            raise ValueError("Invalid SSE day-K date")
        values = [number(value) for value in row[1:]]
        if (any(value <= 0 or value * 1000 != int(value * 1000) for value in values[:4])
                or values[1] < max(values[:4]) or values[2] > min(values[:4])
                or values[4] != int(values[4])):
            raise ValueError("Invalid day-K OHLC ticks or volume")
        # Field 7 is unused in the public chart recipe; do not assert its unit/basis.
        result.append(dict(zip(("code", "date", *FIELDS),
                               (code, day, *[str(value) for value in values]))))
    frame = pd.DataFrame(result)
    if frame.date.duplicated().any() or not frame.date.is_monotonic_increasing:
        raise ValueError("Duplicate or unordered SSE day-K dates")
    return frame


def fetch(session, code):
    with session.get(endpoint(code), params=PARAMS, headers={"Referer": PAGE + "?FUNDID=" + code},
                     timeout=(5, 22), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        parsed = urlparse(response.url)
        if (response.status_code != 200 or parsed.scheme != "https"
                or parsed.hostname != "yunhq.sse.com.cn" or parsed.port != 32042
                or parsed.path != "/v1/sh1/dayk/" + code):
            raise ValueError("Day-K response is outside the reviewed public SSE route")
        parts, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 2_000_000:
                raise ValueError("SSE day-K response exceeds 2 MB")
            parts.append(chunk)
    return b"".join(parts)


def collect(codes, recipe, folder=None, session=None, reuse=()):
    codes = list(codes)
    if not 1 <= len(codes) <= 16 or len(set(codes)) != len(codes):
        raise ValueError("Use 1-16 distinct SSE fund codes per bounded day-K batch")
    for code in codes:
        endpoint(code)
    recipe = Path(recipe)
    script = recipe.read_bytes()
    if b'"dayk/"' not in script or b"res.kline[i][5]" not in script:
        raise ValueError("Expected the inspected SSE day-K website recipe")
    cached = {}
    for path in map(Path, reuse):
        raw = path.read_bytes()
        payload = json.loads(raw)
        code = payload.get("code")
        normalize(payload, code)
        if code not in codes or code in cached:
            raise ValueError("Reused day-K security is outside the batch or repeated")
        cached[code] = (path.resolve(), raw)
    folder = Path(folder) if folder else DATA_ROOT / "research/fund_dayk" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "recipe.js").write_bytes(script)
    state = {"status": "collecting", "source": "SSE public website day-K",
             "recipe_url": RECIPE, "recipe_sha256": sha256(script).hexdigest(),
             "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
             "params": PARAMS.copy(), "requested_codes": codes, "completed_codes": [],
             "receipts": [], "execution_admitted": False, "strategy_validated": False,
             "limits": ["Latest bounded website window, not all listed history or PIT data",
                        "OHLC/volume mapped by public JS; field 7 amount/unit/basis is only a candidate",
                        "Adjustment and auction/post-close/block aggregation are not independently established",
                        "No reference-close, suspension, action, rule or account admission"]}
    write_json(folder / "result.json", state)
    owned_session = session is None
    session = requests.Session() if owned_session else session
    try:
        for code in codes:
            if code in cached:
                path, raw = cached[code]
                origin = {"origin": "reused_local_probe", "source_file": str(path),
                          "adopted_at": datetime.now().astimezone().isoformat(),
                          "retrieved_at": None}
            else:
                raw = fetch(session, code)
                origin = {"origin": "public_sse_request", "url": endpoint(code),
                          "params": PARAMS.copy(),
                          "retrieved_at": datetime.now().astimezone().isoformat()}
            relative = code + ".json"
            (folder / relative).write_bytes(raw)
            receipt = {"code": code, "file": relative, "sha256": sha256(raw).hexdigest(), **origin}
            state["receipts"].append(receipt)
            write_json(folder / "result.json", state)
            payload = json.loads(raw)
            frame = normalize(payload, code)
            receipt.update(rows=len(frame), first_date=frame.date.min(), last_date=frame.date.max(),
                           total=payload["total"], begin=payload["begin"], end=payload["end"])
            state["completed_codes"].append(code)
            write_json(folder / "result.json", state)
            print(f"{code}: {len(frame)} SSE day-K rows; {origin['origin']}", flush=True)
    except (Exception, KeyboardInterrupt) as error:
        state.update(status="stopped_source_error", failed_code=code,
                     error=f"{type(error).__name__}: {error}")
        write_json(folder / "result.json", state)
        raise
    finally:
        if owned_session:
            session.close()
    state["status"] = "completed_public_dayk_acquisition"
    write_json(folder / "result.json", state)
    return folder, state


def load(folder):
    folder = Path(folder)
    result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    codes = result.get("requested_codes", [])
    if (result.get("status") != "completed_public_dayk_acquisition"
            or not 1 <= len(codes) <= 16 or len(set(codes)) != len(codes)
            or result.get("params") != PARAMS or result.get("completed_codes") != codes
            or len(result.get("receipts", [])) != len(codes)):
        raise ValueError("Expected complete saved SSE day-K receipts")
    archived(folder, {"file": "recipe.js", "sha256": result["recipe_sha256"]})
    frames = []
    for code, receipt in zip(codes, result["receipts"]):
        if receipt["code"] != code or receipt["file"] != code + ".json":
            raise ValueError("Saved day-K receipt identity changed")
        if receipt["origin"] == "public_sse_request":
            if receipt.get("url") != endpoint(code) or receipt.get("params") != PARAMS:
                raise ValueError("Saved day-K query identity changed")
        elif receipt["origin"] != "reused_local_probe" or receipt.get("retrieved_at") is not None:
            raise ValueError("Unrecognized day-K source origin")
        payload = json.loads(archived(folder, receipt))
        frame = normalize(payload, code)
        observed = {"rows": len(frame), "first_date": frame.date.min(), "last_date": frame.date.max(),
                    **{key: payload[key] for key in ("total", "begin", "end")}}
        if any(receipt[key] != value for key, value in observed.items()):
            raise ValueError("Saved day-K counts/window changed")
        frames.append(frame)
    return pd.concat(frames, ignore_index=True), result


def compare_frames(official, other, calendar, fields):
    if (not calendar or calendar != sorted(set(calendar))
            or other.date.duplicated().any() or official.date.duplicated().any()):
        raise ValueError("Invalid comparison calendar or duplicate source dates")
    start, end = calendar[0], calendar[-1]
    left = official.loc[official.date.between(start, end)]
    right = other.loc[other.date.between(start, end)]
    if not left.date.isin(calendar).all() or not right.date.isin(calendar).all():
        raise ValueError("Day-K/source observations outside the pinned exchange calendar")
    joined = left.merge(right, on="date", how="outer", suffixes=("_sse", "_other"),
                        indicator=True, validate="one_to_one")
    paired = joined.loc[joined._merge.eq("both")]
    conflicts, counts = [], {}
    for field, other_field in fields.items():
        column = field + "_sse" if field in right.columns else field
        other_column = other_field + "_other" if other_field in left.columns else other_field
        differences = [number(b) - number(a) for a, b in zip(paired[column], paired[other_column])]
        counts[field] = {"observations": len(paired), "exact_matches": sum(value == 0 for value in differences)}
        for (day, a, b), difference in zip(zip(paired.date, paired[column], paired[other_column]), differences):
            if difference:
                conflicts.append({"date": day, "field": field, "sse": str(a), "other": str(b),
                                  "other_minus_sse": str(difference)})
    return {"paired_rows": len(paired), "fields": counts, "conflicts": conflicts,
            "official_missing_calendar_sessions": sorted(set(calendar) - set(left.date)),
            "other_missing_calendar_sessions": sorted(set(calendar) - set(right.date)),
            "source_only_dates": joined.loc[joined._merge.ne("both"), ["date", "_merge"]].astype(str).to_dict("records"),
            "official_out_of_comparison_range_rows": len(official) - len(left)}


def compare_saved(folder, sina, panel, calendar, out=None):
    official, receipts = load(folder)
    sina, panel, calendar = map(Path, (sina, panel, calendar))
    days = pd.read_csv(calendar, dtype=str).date.tolist()
    if not days or any(date.fromisoformat(day).isoformat() != day for day in days):
        raise ValueError("Expected a pinned ISO exchange calendar")
    raw, raw_hash = load_panel(panel)
    status = json.loads((sina / "status.json").read_text(encoding="utf-8"))
    if status.get("provider") != "sina-ohlcv" or status.get("source") != SOURCES["sina-ohlcv"]:
        raise ValueError("Expected saved Sina source receipts")
    comparisons = []
    for code in receipts["requested_codes"]:
        sse = official.loc[official.code.eq(code)].drop(columns="code")
        receipt = status["receipts"][code]
        if receipt["status"] != "acquired_unvalidated" or receipt["file"] != code + ".parquet":
            raise ValueError("Sina source security unavailable")
        archived(sina, receipt)
        other = pd.read_parquet(sina / receipt["file"])
        if len(other) != receipt["rows"] or not other.symbol.eq(code).all():
            raise ValueError("Sina source security/count changed")
        other["date"] = pd.to_datetime(other.date.astype(str), format="%Y%m%d", errors="raise").dt.strftime("%Y-%m-%d")
        sina_result = compare_frames(sse, other, days, {field: field for field in FIELDS[:5]})
        baostock = raw.loc[raw.code.eq("sh." + code)].drop(columns="code")
        if baostock.empty:
            raise ValueError("Requested fund has no frozen provider observations")
        panel_days = [day for day in days if raw.date.min() <= day <= raw.date.max()]
        bulk_result = compare_frames(sse, baostock, panel_days,
                                    {**{field: field for field in FIELDS[:5]}, "field7_amount_candidate": "amount"})
        comparisons.append({"code": code, "sina_receipt": receipt, "sina": sina_result, "baostock": bulk_result})
    out = Path(out) if out else DATA_ROOT / "research/fund_dayk_comparison" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    source = Path(folder) / "result.json"
    result = {"status": "completed_saved_dayk_comparison", "start": days[0], "end": days[-1],
              "dayk_folder": str(Path(folder).resolve()), "dayk_receipt_sha256": sha256(source.read_bytes()).hexdigest(),
              "sina_folder": str(sina.resolve()), "panel": str(panel.resolve()), "raw_daily_sha256": raw_hash,
              "calendar": str(calendar.resolve()), "calendar_sha256": sha256(calendar.read_bytes()).hexdigest(),
              "comparisons": comparisons, "execution_admitted": False, "strategy_validated": False,
              "limits": receipts["limits"] + ["Exact sample agreement is not independent status/action/rule admission",
                                               "All differences retained; original provider quotes unchanged"]}
    write_json(out / "result.json", result)
    return out, result


def compare_basis(dayk, snapshot, statistics, code):
    """Match a dated named snapshot, not infer historical after-hours quantities."""
    values = snapshot.get("snap") if isinstance(snapshot, dict) else None
    if (not isinstance(snapshot, dict) or snapshot.get("code") != code
            or type(snapshot.get("date")) is not int or not isinstance(values, list)
            or len(values) != len(SNAP_FIELDS) or values[0] != snapshot["date"]):
        raise ValueError("Named snapshot security/date/layout mismatch")
    observed = dict(zip(SNAP_FIELDS, values))
    day = datetime.strptime(str(snapshot["date"]), "%Y%m%d").date().isoformat()
    row = validate_statistics(statistics, code, day)
    if row is None:
        raise ValueError("Dated official statistics are missing")
    frame = normalize(dayk, code)
    selected = frame.loc[frame.date.eq(day)]
    if len(selected) != 1:
        raise ValueError("Snapshot has no same-date day-K observation")
    bar = selected.iloc[0]
    fields = {**{field: field for field in FIELDS[:3]}, "close": "last",
              "volume": "volume", "field7_amount_candidate": "amount"}
    matches = {field: number(bar[field]) == number(observed[key]) for field, key in fields.items()}
    activity = {}
    for field, key in (("volume", "TRADE_VOL"), ("amount", "TRADE_AMT")):
        regular, after = number(observed[field]), number(observed["fp_" + field])
        if field == "volume" and (regular != int(regular) or after != int(after)):
            raise ValueError("Snapshot fund volumes must be whole units")
        total = number(row[key]) * 10000
        residual = total - regular - after
        activity[field] = {"named_snapshot_regular": str(regular), "named_snapshot_post_close": str(after),
                           "statistics_base_unit_center": str(total),
                           "statistics_minus_regular_minus_post_close": str(residual),
                           "within_reported_rounding_interval": abs(residual) <= 50}
    return {"code": code, "date": day, "dayk_named_snapshot_exact_matches": matches,
            "activity": activity, "snapshot_tradephase_raw": observed["tradephase"],
            "sample_post_close_basis_supported": all(matches.values()) and all(
                item["within_reported_rounding_interval"] for item in activity.values()),
            "execution_admitted": False, "strategy_validated": False,
            "limits": ["Only this dated snapshot, not missing historical post-close receipts",
                       "Field 7 equals named amount in this sample; global historical basis is not proven",
                       "Statistics are rounded; no rewriting provider quotes or status/action/rule admission"]}


def basis_saved(folder, code, snapshot, statistics, out=None):
    _, result = load(folder)
    if code not in result["requested_codes"]:
        raise ValueError("Basis sample security is absent from the saved batch")
    dayk = Path(folder) / (code + ".json")
    paths = {"dayk": dayk, "snapshot": Path(snapshot), "statistics": Path(statistics)}
    raw = {key: path.read_bytes() for key, path in paths.items()}
    report = compare_basis(*(json.loads(raw[key]) for key in ("dayk", "snapshot", "statistics")), code)
    report.update(status="completed_saved_dated_basis_diagnostic",
                  inputs={key: {"path": str(path.resolve()), "sha256": sha256(raw[key]).hexdigest()}
                          for key, path in paths.items()},
                  snapshot_expected_endpoint=ROOT.replace("/dayk/", "/snap/") + code,
                  snapshot_select=",".join(SNAP_FIELDS),
                  input_adopted_at=datetime.now().astimezone().isoformat())
    out = Path(out) if out else DATA_ROOT / "research/fund_basis" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "result.json", report)
    return out, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    acquire = modes.add_parser("collect")
    acquire.add_argument("--codes", required=True)
    acquire.add_argument("--recipe", type=Path, required=True)
    acquire.add_argument("--reuse-json", type=Path, action="append", default=[])
    acquire.add_argument("--out", type=Path)
    compare = modes.add_parser("compare")
    compare.add_argument("--dayk", type=Path, required=True)
    compare.add_argument("--sina", type=Path, required=True)
    compare.add_argument("--panel", type=Path, required=True)
    compare.add_argument("--calendar", type=Path, required=True)
    compare.add_argument("--out", type=Path)
    basis = modes.add_parser("basis")
    basis.add_argument("--dayk", type=Path, required=True)
    basis.add_argument("--code", required=True)
    basis.add_argument("--snapshot", type=Path, required=True)
    basis.add_argument("--statistics", type=Path, required=True)
    basis.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "collect":
        folder, result = collect(args.codes.split(","), args.recipe, args.out, reuse=args.reuse_json)
        summary = {"completed_codes": len(result["completed_codes"]),
                   "rows": sum(item["rows"] for item in result["receipts"])}
    elif args.mode == "compare":
        folder, result = compare_saved(args.dayk, args.sina, args.panel, args.calendar, args.out)
        summary = {"codes": len(result["comparisons"]),
                   "sina_conflicts": sum(len(item["sina"]["conflicts"]) for item in result["comparisons"]),
                   "baostock_conflicts": sum(len(item["baostock"]["conflicts"]) for item in result["comparisons"])}
    else:
        folder, result = basis_saved(args.dayk, args.code, args.snapshot, args.statistics, args.out)
        summary = {"code": result["code"], "date": result["date"],
                   "sample_post_close_basis_supported": result["sample_post_close_basis_supported"]}
    print(json.dumps({"folder": str(folder), "status": result["status"], **summary,
                      "execution_admitted": False}), flush=True)
    return 0
