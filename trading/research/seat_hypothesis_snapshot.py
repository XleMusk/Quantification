"""Derive two fixed public-seat research snapshots from a ready local cache.

No collector, price source or account is called. The archive preserves saved
JSON.gz cache bytes, not the original HTTP response bytes.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import gzip
import json
from pathlib import Path
import re
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

import pandas as pd

from ..contracts import MARKET_TZ
from ..rules import stock_board
from .seats import REPORTS, URL, normalize, period, read_raw
from .seat_hypothesis_panel_replay import (
    HYPOTHESIS_ID, AVAILABILITY_SCHEMA, STOCKDAY_HYPOTHESIS_ID,
    STOCKDAY_AVAILABILITY_SCHEMA, STOCKDAY_SCOPE,
)

SCHEMA = "public_seat_hypothesis_snapshot_v1"
FLAGS = {"execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "historical_publication_verified": False,
    "future_labels_used_for_score": False, "ledger_mutated": False,
    "ths_payload_generated": False}
RULE_SPEC = {
    "thresholds": {"positive_net_buy_side_seats_at_least": 2,
        "net_cny_strictly_positive": True, "net_to_turnover_at_least": .01},
    "legacy": "buy_net=sum(buy-side net, including negative); sell_net=-sum(negative sell-side net); numerator=buy_net-sell_net; denominator=sum(eligible canonical event turnovers)",
    "stockday_v2": "numerator=sum(each eligible canonical event net once); denominator=one stock-day turnover only if every single-day summary and raw single-day seat amount agrees",
    "identity": "Existing normalization, named-seat eligibility and deduplication only; no person attribution or new aliases",
    "stockday_scope": STOCKDAY_SCOPE,
    "unknown": "Missing/conflicting single-day turnover evidence stays unknown; no max, default zero or replacement source",
    "holding": "Existing event-update consumer; no-event is NO_CHANGE, no forced terminal liquidation; right-censored holdings retained",
}
PANEL_COLUMNS = ["trade_date", "symbol", "eligible_seat_events", "buy_seats", "sell_seats",
    "buy_net_cny", "sell_net_cny", "turnover_cny", "reasons", "net_buy_cny",
    "net_buy_to_reported_turnover", "hypothesis_h1", "hypothesis_status", "unknown_reasons",
    "scope_status", "scope_reason", "positive_event_net_cny", "negative_event_net_cny",
    "raw_ids", "event_ids", "summary_raw_ids"]


def _now():
    return datetime.now(MARKET_TZ)


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _ref(path, data=None):
    path = Path(path).resolve()
    data = path.read_bytes() if data is None else data
    return {"file": str(path), "sha256": sha256(data).hexdigest(), "byte_count": len(data)}


def _clock(value, now):
    if not isinstance(value, str):
        raise ValueError("Every cache page and completed manifest needs an actual aware retrieval clock")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None or result > now:
        raise ValueError("Naive or future cache clock")
    return result.astimezone(MARKET_TZ)


def _day(value):
    if not isinstance(value, str) or datetime.strptime(value, "%Y-%m-%d").date().isoformat() != value:
        raise ValueError("Expected canonical ISO source day")
    return value


def _symbol(row):
    text, code = row.get("SECUCODE"), row.get("SECURITY_CODE")
    if not isinstance(text, str) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", text) or text[:6] != code:
        raise ValueError("Source security code and exchange identity disagree")
    return text.replace(".SH", ".SSE").replace(".SZ", ".SZSE")


def _amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _load_cache(source_root):
    root, actual = Path(source_root).resolve(), _now()
    manifest_file = root / "manifest.json"
    manifest_bytes = manifest_file.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    query = manifest.get("query") or {}
    start, end = _day(query.get("start")), _day(query.get("end"))
    codes = query.get("codes")
    if (manifest.get("status") != "ready" or query.get("source") != "Eastmoney"
            or query.get("url") != URL or query.get("pagination_version") != 2
            or start > end or not isinstance(codes, list) or codes != sorted(set(codes))
            or any(not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) for code in codes)
            or manifest.get("publication_timestamps_available") is not False
            or manifest.get("completeness") != "provider_pages_only"
            or set(manifest.get("reports") or {}) != set(REPORTS)):
        raise ValueError("Expected one completed, bounded public-seat cache, not a partial collection")
    completed = _clock(manifest.get("completed_at"), actual)
    where = f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')"
    if codes:
        where += '(SECURITY_CODE in (' + ','.join(f'"{code}"' for code in codes) + '))'
    blobs, refs, records, clocks = {"manifest.json": manifest_bytes}, [_ref(manifest_file, manifest_bytes)], {}, []
    for kind, report in REPORTS.items():
        declaration = manifest["reports"][kind]
        pages, count = declaration.get("pages"), declaration.get("rows")
        if type(pages) is not int or not 1 <= pages <= 100 or type(count) is not int or count <= 0:
            raise ValueError("Invalid completed cache page/row declaration")
        observed = 0
        for page in range(1, pages + 1):
            name = f"{kind}_{page:04d}.json.gz"
            data = (root / name).read_bytes()
            doc = json.loads(gzip.decompress(data).decode("utf-8"))
            sort = "TRADE_DATE,SECURITY_CODE" + (",TRADE_ID,OPERATEDEPT_CODE,BUY,SELL" if kind != "summary" else "")
            expected = {"reportName": report, "columns": "ALL", "filter": where,
                "pageSize": 500, "pageNumber": page, "sortColumns": sort,
                "sortTypes": ",".join("1" for _ in sort.split(",")), "source": "WEB", "client": "WEB"}
            response = doc.get("response") or {}
            result = response.get("result") or {}
            rows = result.get("data")
            if (doc.get("request") != expected or response.get("success") is not True
                    or result.get("pages") != pages or result.get("count") != count
                    or not isinstance(rows, list) or not rows):
                raise ValueError("Cache request, pagination or successful response changed")
            retrieved = _clock(doc.get("retrieved_at"), actual)
            if doc.get("published_at") is not None:
                raise ValueError("Saved cache declares unsupported publication evidence")
            clocks.append(retrieved)
            for row in rows:
                day = _day(str(row.get("TRADE_DATE", ""))[:10])
                _symbol(row)
                if not start <= day <= end or (codes and row["SECURITY_CODE"] not in codes):
                    raise ValueError("Cached row lies outside the pinned query")
            observed += len(rows)
            blobs[name] = data
            refs.append(_ref(root / name, data))
        if observed != count:
            raise ValueError("Incomplete cached report")
        records[kind] = read_raw(root, kind)
    # read_raw is the existing parser. Its input must still be the exact bytes
    # that will be archived, rather than a changing cache during derivation.
    if any(Path(ref["file"]).read_bytes() != blobs[Path(ref["file"]).name] for ref in refs):
        raise ValueError("Cache changed while deriving the snapshot")
    return root, manifest, records, blobs, refs, max([completed] + clocks)


def _groups(events, summary, raw_seats):
    summary_groups, raw_groups = {}, {}
    for row in summary:
        key = (row["TRADE_DATE"][:10], _symbol(row))
        summary_groups.setdefault(key, []).append(row)
    for row in raw_seats:
        key = (row["TRADE_DATE"][:10], _symbol(row))
        if period(row.get("EXPLANATION") or "")[0] == "single":
            raw_groups.setdefault(key, []).append(row)
    eligible = events.loc[events.eligible]
    return {key: frame for key, frame in eligible.groupby(["trade_date", "symbol"], sort=True)}, summary_groups, raw_groups


def _panels(events, summary, raw_seats):
    groups, summaries, raw_groups = _groups(events, summary, raw_seats)
    legacy, unique = [], []
    for (day, symbol), frame in groups.items():
        net = frame.net_cny
        buy, sell = frame.listed_side.str.contains("buy"), frame.listed_side.str.contains("sell")
        buy_net, sell_net = float(net[buy].sum()), float(-net[sell & net.lt(0)].sum())
        numerator, denominator = buy_net - sell_net, float(frame.period_turnover_cny.sum())
        ratio = numerator / denominator
        common = {"trade_date": day, "symbol": symbol, "eligible_seat_events": len(frame),
            "buy_seats": int((buy & net.gt(0)).sum()), "sell_seats": int((sell & net.lt(0)).sum()),
            "buy_net_cny": buy_net, "sell_net_cny": sell_net,
            "reasons": " | ".join(sorted(set(frame.reason))),
            "raw_ids": "|".join(frame.raw_id), "event_ids": "|".join(frame.event_id),
            "positive_event_net_cny": float(net[net.gt(0)].sum()),
            "negative_event_net_cny": float(-net[net.lt(0)].sum()),
            "summary_raw_ids": "|".join(row["_raw_id"] for row in summaries.get((day, symbol), []))}
        matched = common["buy_seats"] >= 2 and numerator > 0 and ratio >= .01
        legacy.append({**common, "turnover_cny": denominator, "net_buy_cny": numerator,
            "net_buy_to_reported_turnover": ratio, "hypothesis_h1": matched,
            "hypothesis_status": "matched" if matched else "not_matched", "unknown_reasons": "",
            "scope_status": "legacy_saved_normalized_eligible_events", "scope_reason": "original_scope_unchanged"})
    # v2 keeps an inventory for every observed stock-day, including cumulative,
    # unknown-window and unsupported-security records; none is a replacement.
    keys = sorted(set(summaries) | set(zip(events.trade_date, events.symbol)))
    legacy_by_key = {(row["trade_date"], row["symbol"]): row for row in legacy}
    for day, symbol in keys:
        frame = groups.get((day, symbol))
        common = legacy_by_key.get((day, symbol))
        if common is None:
            common = {"trade_date": day, "symbol": symbol, "eligible_seat_events": 0,
                "buy_seats": 0, "sell_seats": 0, "buy_net_cny": None, "sell_net_cny": None,
                "reasons": " | ".join(sorted({row.get("EXPLANATION") or "" for row in summaries.get((day, symbol), [])})),
                "raw_ids": "", "event_ids": "", "positive_event_net_cny": None, "negative_event_net_cny": None,
                "summary_raw_ids": "|".join(row["_raw_id"] for row in summaries.get((day, symbol), []))}
        else:
            common = {key: value for key, value in common.items() if key not in
                {"turnover_cny", "net_buy_cny", "net_buy_to_reported_turnover", "hypothesis_h1", "hypothesis_status", "unknown_reasons", "scope_status", "scope_reason"}}
        issues = []
        single = [row for row in summaries.get((day, symbol), []) if period(row.get("EXPLANATION") or "")[0] == "single"]
        amounts = [_amount(row.get("ACCUM_AMOUNT")) for row in single]
        seat_amounts = [_amount(row.get("ACCUM_AMOUNT")) for row in raw_groups.get((day, symbol), [])]
        if not single:
            issues.append("single_day_summary_missing")
        elif any(value is None for value in amounts):
            issues.append("single_day_summary_turnover_missing_or_invalid")
        elif len(set(amounts)) != 1:
            issues.append("single_day_summary_turnover_conflict")
        if not seat_amounts or any(value is None for value in seat_amounts):
            issues.append("single_day_seat_turnover_missing_or_invalid")
        elif len(set(seat_amounts)) != 1:
            issues.append("single_day_seat_turnover_conflict")
        if amounts and seat_amounts and None not in amounts + seat_amounts and len(set(amounts + seat_amounts)) != 1:
            issues.append("summary_and_seat_turnover_disagree")
        if frame is None:
            issues.append("no_known_eligible_canonical_single_day_seat_events")
        try:
            stock_board(symbol)
            scope_status, scope_reason = "ordinary_sh_sz_prefix_proxy", "not_a_dated_roster_or_PIT_identity_verification"
        except ValueError:
            scope_status, scope_reason = "excluded_security_scope", "unsupported_by_existing_ordinary_SH_SZ_stock_board_rule"
        turnover = float(amounts[0]) if not issues else None
        numerator = float(frame.net_cny.sum()) if frame is not None else None
        ratio = numerator / turnover if numerator is not None and turnover is not None else None
        matched = (not issues and scope_status != "excluded_security_scope"
            and common["buy_seats"] >= 2 and numerator > 0 and ratio >= .01)
        status = "scope_excluded" if scope_status == "excluded_security_scope" else "unknown" if issues else "matched" if matched else "not_matched"
        unique.append({**common, "turnover_cny": turnover, "net_buy_cny": numerator,
            "net_buy_to_reported_turnover": ratio, "hypothesis_h1": None if status == "unknown" else matched,
            "hypothesis_status": status, "unknown_reasons": "|".join(issues),
            "scope_status": scope_status, "scope_reason": scope_reason})
    return pd.DataFrame(legacy, columns=PANEL_COLUMNS), pd.DataFrame(unique, columns=PANEL_COLUMNS)


def _compare_legacy(panel, reference):
    if reference is None:
        return None
    reference = Path(reference).resolve()
    old = pd.read_csv(reference, dtype={"trade_date": str, "symbol": str})
    if old.duplicated(["trade_date", "symbol"]).any():
        raise ValueError("Duplicate legacy reference stock-day")
    a, b = old.set_index(["trade_date", "symbol"]).sort_index(), panel.set_index(["trade_date", "symbol"]).sort_index()
    if not a.index.equals(b.index):
        raise ValueError("Legacy reference and saved cache stock-day scope differ")
    differences = {}
    for field in ["eligible_seat_events", "buy_seats", "sell_seats", "buy_net_cny", "sell_net_cny", "turnover_cny", "net_buy_cny", "net_buy_to_reported_turnover", "hypothesis_h1"]:
        if field == "hypothesis_h1":
            count = int((a[field].astype(str).str.lower() != b[field].astype(str).str.lower()).sum())
        else:
            count = int((pd.to_numeric(a[field]) - pd.to_numeric(b[field])).abs().gt(1e-12 if field.endswith("turnover") else 1e-6).sum())
        differences[field] = count
    return {"reference": _ref(reference), "stock_days": len(a), "different_rows_by_field": differences,
        "matched": not any(differences.values()), "future_labels_or_returns_read": False}


def build(source_root, out, *, legacy_reference=None):
    root, manifest, raw, blobs, source_refs, available = _load_cache(source_root)
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError("Snapshot output must be a new directory")
    packages = [root] + ([Path(legacy_reference).resolve().parent] if legacy_reference else [])
    if any(out == package or out in package.parents or package in out.parents for package in packages):
        raise ValueError("Snapshot output must not overwrite or sit inside an input package")
    events = normalize(raw["buy"], raw["sell"])
    legacy, unique = _panels(events, raw["summary"], raw["buy"] + raw["sell"])
    comparison = _compare_legacy(legacy, legacy_reference)
    # Everything above is read-only. Stage only after inputs are complete.
    stage = out.with_name(out.name + ".building_" + uuid4().hex[:10])
    stage.mkdir(parents=True, exist_ok=False)
    archive_manifest = {"schema": "public_seat_saved_cache_archive_v1", "source_root": str(root),
        "query": manifest["query"], "source_manifest": source_refs[0], "files": source_refs,
        "retrieved_at": available.isoformat(), "source_kind": "saved_parsed_JSON_gzip_cache_not_HTTP_response_bytes",
        "historical_publication_verified": False}
    _json(stage / "source_bundle_manifest.json", archive_manifest)
    with ZipFile(stage / "source_bundle.zip", "x", compression=ZIP_DEFLATED) as archive:
        for name, data in blobs.items():
            archive.writestr(name, data)
        archive.writestr("archive_manifest.json", (stage / "source_bundle_manifest.json").read_bytes())
    archive_ref = _ref(stage / "source_bundle.zip")
    archive_ref["file"] = str(out / "source_bundle.zip")
    events.to_parquet(stage / "normalized_events.parquet", index=False)
    methods = {}
    for directory, panel, method, schema in (
        ("legacy_repeated_sum", legacy, HYPOTHESIS_ID, AVAILABILITY_SCHEMA),
        ("stockday_unique", unique, STOCKDAY_HYPOTHESIS_ID, STOCKDAY_AVAILABILITY_SCHEMA),
    ):
        target = stage / directory
        target.mkdir()
        panel = panel.assign(hypothesis_id=method, snapshot_method_id=method)
        panel.to_csv(target / "panel.csv", index=False)
        candidates = panel.loc[panel.hypothesis_status.eq("matched")].copy()
        candidates.to_csv(target / "candidates.csv", index=False)
        candidate_ref = _ref(target / "candidates.csv")
        candidate_ref["file"] = str(out / directory / "candidates.csv")
        evidence = {"schema": schema, "hypothesis_id": method, "snapshot_method_id": method,
            "candidates_file": candidate_ref["file"], "candidates_sha256": candidate_ref["sha256"],
            "panel_file": str(out / directory / "panel.csv"), "panel_sha256": _ref(target / "panel.csv")["sha256"],
            "source_archive": archive_ref, "source_available_at": available.isoformat(),
            "source_scope": STOCKDAY_SCOPE if method == STOCKDAY_HYPOTHESIS_ID else "original_normalized_eligible_event_scope",
            "source_archive_kind": archive_manifest["source_kind"], "source_manifest_sha256": source_refs[0]["sha256"],
            "records": [{"event_date": row.trade_date, "symbol": row.symbol,
                "source_file": archive_ref["file"], "source_sha256": archive_ref["sha256"],
                "retrieved_at": available.isoformat(), "published_at": None,
                "raw_ids": row.raw_ids.split("|") if row.raw_ids else [],
                "event_ids": row.event_ids.split("|") if row.event_ids else [],
                "summary_raw_ids": row.summary_raw_ids.split("|") if row.summary_raw_ids else []}
                for row in candidates.itertuples(index=False)], **FLAGS}
        _json(target / "availability_evidence.json", evidence)
        methods[directory] = {"hypothesis_id": method, "snapshot_method_id": method,
            "panel_file": str(out / directory / "panel.csv"), "panel_sha256": _ref(target / "panel.csv")["sha256"],
            "candidates_file": candidate_ref["file"], "candidates_sha256": candidate_ref["sha256"],
            "availability_evidence": str(out / directory / "availability_evidence.json"),
            "stock_days": len(panel), "candidates": len(candidates),
            "status_counts": {str(k): int(v) for k, v in panel.hypothesis_status.value_counts().items()},
            "scope_counts": {str(k): int(v) for k, v in panel.scope_status.value_counts().items()}}
    result = {"schema": SCHEMA, "status": "completed_research_snapshot_only",
        "created_at": _now().isoformat(), "source_available_at": available.isoformat(),
        "source_manifest": source_refs[0], "source_files": source_refs, "source_query": manifest["query"],
        "source_reports": manifest["reports"], "source_archive": archive_ref,
        "normalized_events": len(events), "eligible_single_seat_events": int(events.eligible.sum()),
        "normalized_events_file": str(out / "normalized_events.parquet"),
        "normalized_events_sha256": _ref(stage / "normalized_events.parquet")["sha256"],
        "window_counts": {str(k): int(v) for k, v in events.window_kind.value_counts().items()},
        "methods": methods, "rule_spec": RULE_SPEC, "legacy_comparison": comparison,
        "limitations": ["Public named-seat subsets are not personal trades or complete stock-day money flow.",
            "v2 sums each existing eligible canonical net once, including sell-only positive net; old listed-side components are not the v2 numerator formula.",
            "All cache pages and completion clocks determine first system observation, not independently verified historical publication.",
            "Ordinary SH/SZ stock_board scope is a prefix proxy, not a historical roster, IPO or execution qualification.",
            "No prices, future returns, strategy performance or account were read or generated; terminal holdings in a later replay are right-censored."], **FLAGS}
    _json(stage / "result.json", result)
    report = ["# 公开席位固定口径研究快照", "",
        f"源范围：{manifest['query']['start']} 至 {manifest['query']['end']}；完整缓存最晚取得时钟：{available.isoformat()}。",
        "这是匿名公开披露缓存的派生研究输入，不是私人交割单。原件包保存已有 JSON.gz 的字节，不是 HTTP 响应原字节。", "",
        "|口径|事实证券日|满足固定条件|未知|范围排除|", "|---|---:|---:|---:|---:|"]
    for name, label in (("legacy_repeated_sum", "旧重复加总口径"), ("stockday_unique", "独立 v2 单次净额与单次成交额口径")):
        counts = methods[name]["status_counts"]
        report.append(f"|{label}|{methods[name]['stock_days']}|{methods[name]['candidates']}|{counts.get('unknown', 0)}|{counts.get('scope_excluded', 0)}|")
    report += ["", "两种口径都固定要求至少两个买榜内净买入为正的已辨营业部事件、净额为正、净额占报告成交额至少 1%。",
        "旧口径逐项保留原买卖榜分组分子和逐席位重复成交额。v2 每个既有合格 canonical 事件净额只加一次；买卖双榜负净额不再双扣，卖榜独有正净额也保留。",
        "v2 仅在所有单日 summary 与单日席位原成交额一致时使用一次证券日成交额。缺值、累计窗口和冲突留未知；不取最大值、不补零。",
        "v2 普通沪深股票范围沿既有 stock_board 前缀规则，北交所、转债等保留库存但不入候选；这不构成历史名册、IPO 或成交资格验证。",
        "未知事件不发自己的新信号，不否决另一天已知事件；后续 consumer 无事件时保持持仓，末端不强制平仓，残仓属于右删失。",
        "本包没有读价格或未来收益，没有运行账户。全部 selection/execution/strategy/publication 验证标志仍为 false。", ""]
    (stage / "REPORT.md").write_text("\n".join(report), encoding="utf-8")
    stage.rename(out)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--legacy-reference", type=Path)
    args = parser.parse_args(argv)
    result = build(args.source_root, args.out, legacy_reference=args.legacy_reference)
    print(json.dumps({key: result[key] for key in ("schema", "status", "created_at", "methods", "execution_admitted")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
