"""Only the explicit historical calendar-prefix boundaries; no market replay."""
from copy import deepcopy
import json

from trading.research import leader_eight_type_historical_native as history
from trading.research.tests.test_leader_eight_type_historical_native import (
    A, B, CALENDAR, SESSIONS, OBSERVED, bars,
)


def calendar_prefix(tmp_path):
    notice, receipt = tmp_path / "notice.html", tmp_path / "receipt.json"
    notice.write_text("synthetic official schedule fixture", encoding="utf-8")
    receipt.write_text('{"fixture": true}', encoding="utf-8")
    path = tmp_path / "calendar2025.json"
    path.write_text(json.dumps({"schema": "cn_a_share_exchange_calendar_v1", "year": 2025,
        "closed_ranges": ["2025-01-01/2025-01-01"], "open_sessions": [],
        "source_status": "official_static_schedule_snapshot", "source_published": "2024-12-23",
        "source_url": "https://www.sse.com.cn/synthetic-fixture",
        "source_file": str(notice), "source_sha256": history.digest(notice),
        "source_receipt": str(receipt), "retrieved_at": "2026-10-16T22:00:00+08:00"}), encoding="utf-8")
    return history._calendar_prefix(SESSIONS, [path], observed_at=OBSERVED)


def qualification(records, full, proofs):
    return history.PosthocListingQualification(records, tuple(SESSIONS), {
        "posthoc_observed_at": OBSERVED, "prior_calendars": proofs}, full)


def test_four_listing_identities_keep_exact_crossyear_first_five_and_original_clocks(tmp_path):
    full, proofs = calendar_prefix(tmp_path)
    symbols = ["301001.SZSE", "301002.SZSE", "603001.SSE", "603002.SSE"]
    records = {symbol: {"ipo_date": "2025-12-30" if index % 2 == 0 else "2025-12-31",
        "board": "chinext" if index < 2 else "main", "source_basis": "synthetic_posthoc_metadata",
        "available_at": "2026-10-08T12:00:00+08:00"} for index, symbol in enumerate(symbols)}
    original = deepcopy(records)
    q = qualification(records, full, proofs)
    assert q.calendar == tuple(SESSIONS) and not any(day.startswith("2025") for day in q.calendar)
    for index, symbol in enumerate(symbols):
        fifth = q.assess(symbol, "2026-01-05")
        last = q.assess(symbol, "2026-01-08")
        assert fifth["listing_session_ordinal"] == (3 if index % 2 == 0 else 2)
        assert fifth["engine_block_reason"] == "unsupported_first_five_listing_sessions"
        assert last["listing_session_ordinal"] == (6 if index % 2 == 0 else 5)
        assert (last["engine_block_reason"] is None) is (index % 2 == 0)
        assert last["original_available_at"] == records[symbol]["available_at"]
        assert last["historical_cutoff_knowledge_verified"] is False
    assert q.records == original and proofs[0]["actual_read_at"] == OBSERVED
    assert proofs[0]["source_receipt_sha256"] and proofs[0]["source_sha256"]


def test_crossyear_stage_and_rebound_use_exact_fixed_predecessor_without_future_rows(tmp_path):
    full, proofs = calendar_prefix(tmp_path)
    records = {symbol: {"ipo_date": "1991-04-03", "board": "main", "source_basis": "synthetic_posthoc_metadata",
        "available_at": "2026-10-08T12:00:00+08:00"} for symbol in (A, B)}
    q = qualification(records, full, proofs)
    feature_q = history.PosthocListingQualification(q.records, full, q.verification, full)
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    raw = bars(["2025-12-29", "2025-12-30", "2025-12-31"] + days)
    def setrow(day, symbol, values):
        raw.loc[raw.date.eq(day) & raw.code.eq("sz."+symbol[:6]),
            ["open", "high", "low", "close", "preclose", "pctChg"]] = values
    setrow("2025-12-30", A, ["10.5", "11", "10.3", "11", "10", "10"])
    setrow("2025-12-31", A, ["11.7", "12.1", "11.5", "12.1", "11", "10"])
    setrow(days[0], A, ["12.8", "13.31", "12.5", "13.31", "12.1", "10"])
    setrow("2025-12-30", B, ["10.5", "11", "10.3", "11", "10", "10"])
    setrow("2025-12-31", B, ["11.1", "11.2", "10.4", "10.5", "11", "-4.545454545"])
    setrow(days[0], B, ["10.6", "11.3", "10.5", "11.2", "10.5", "6.66666667"])
    panels = history.derive_panels(raw, days, [A, B], CALENDAR, q, observed_at=OBSERVED,
        feature_qualification=feature_q)
    first = panels[days[0]]
    assert first["risk_features"][A]["qualified_stage"] == 3
    rebound = next(item for item in first["types"] if item["id"] == "rebound_repair_candidate")
    assert [row["symbol"] for row in rebound["candidates"]] == [B]
    changed = raw.copy()
    changed.loc[changed.date.eq(days[-1]), ["amount", "turn", "close"]] = ["999999999", "900", "10.01"]
    later = history.derive_panels(changed, days, [A, B], CALENDAR, q, observed_at=OBSERVED,
        feature_qualification=feature_q)
    assert later[days[0]] == first
