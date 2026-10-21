"""Saved candidates plus observed quotes remain descriptive and read-only."""
from copy import deepcopy

from trading.research.leader_candidate_intraday_view import build_candidate_tracking


D, E = "2026-10-16", "2026-10-19"
NOW = E + "T10:00:30+08:00"
A, B, C, T = "000001.SZSE", "000002.SZSE", "600000.SSE", "600001.SSE"


def fixture():
    definitions = [
        ("trend_leader", "FULL_TARGET", A, {A: .16}, 10., 9., 9.2),
        ("sector_catchup_leader", "NO_CHANGE", B, None, 20., 19., None),
        ("rebound_repair_candidate", "EXIT_ALL", C, {}, 30., 29., None),
        ("turnover_leader", "FULL_TARGET", T, {T: .16}, 4.8, 4.5, None),
    ]
    rows, quotes, raw = [], [], []
    for index, (kind, decision, symbol, weights, reference, low, sma) in enumerate(definitions):
        name = "原保存名称" + str(index)
        rows.append({"case_id": kind + "__unguarded__uncapped", "type": kind,
            "decision": decision, "target_weights": weights,
            "candidates": [{"symbol": symbol, "score": 100 - index}],
            "candidate_details": [{"symbol": symbol, "name": name,
                "setup_reference": {"symbol": symbol, "source_day": D,
                    "reference_close_cny": reference, "setup_low_cny": low,
                    "sma20_observed": sma, "previous_open_cny": None},
                "trade_ready": False}]})
        last = {A: 10.5, B: 19.5, C: 32., T: 4.5}[symbol]
        quote_time = E + "T10:00:00+08:00"
        quotes.append({"symbol": symbol, "source_quote_time": quote_time,
            "last_price_rmb": str(last), "current_at_read": True, "read_status": "current",
            "source_status": "00", "received_at": E + "T10:00:01+08:00"})
        raw.append({"symbol": symbol, "source_date": E, "source_quote_time": quote_time,
            "current_quote": True, "source_status": "00", "previous_close_price_rmb": str(reference),
            "last_price_rmb": str(last), "high_price_rmb": str(max(last, reference) + .5),
            "low_price_rmb": str(low - .2), "received_at": E + "T10:00:01+08:00"})
    paper = {"source_close_day": D, "execution_day_bound": E,
        "recorded_at": D + "T18:45:00+08:00", "source_file": "saved/paper_plan.json", "rows": rows}
    cohort = {"quotes": quotes, "source_file": "saved/receipt.json",
        "session": {"session_day": E, "session_name": "morning",
            "session_start": E + "T09:30:00+08:00", "stop_at": E + "T11:30:00+08:00"},
        "snapshot": {"response_received_at": E + "T10:00:01+08:00",
            "started_at": E + "T10:00:00+08:00", "finished_at": E + "T10:00:02+08:00"}}
    receipt = {"session_day": E, "quotes": raw,
        "response_received_at": E + "T10:00:01+08:00", "finished_at": E + "T10:00:02+08:00"}
    return paper, cohort, receipt


def candidate(result, symbol):
    return next(item for row in result["rows"] for item in row["candidates"] if item["symbol"] == symbol)


def test_exact_join_preserves_original_cases_decisions_weights_and_does_not_mutate_inputs():
    paper, cohort, receipt = fixture()
    original = deepcopy((paper, cohort, receipt))
    # Raw and view quote order intentionally differ from saved case order.
    receipt["quotes"].reverse()
    cohort["quotes"].reverse()
    before = deepcopy((paper, cohort, receipt))
    result = build_candidate_tracking(paper, cohort, receipt, NOW)
    assert result["schema"] == "saved_leader_candidate_intraday_tracking_v1"
    assert [row["case_id"] for row in result["rows"]] == [row["case_id"] for row in paper["rows"]]
    for old, row in zip(paper["rows"], result["rows"]):
        assert (row["type"], row["decision"], row["target_weights"]) == (
            old["type"], old["decision"], old["target_weights"])
        item = row["candidates"][0]
        assert item["name"] == old["candidate_details"][0]["name"]
        assert item["plan_decision"] == old["decision"]
        assert item["tracking"]["trade_ready"] is False
    assert candidate(result, A)["target_weight"] == .16
    assert candidate(result, B)["target_weight"] is None
    trend, turn = candidate(result, A)["tracking"], candidate(result, T)["tracking"]
    assert trend["snapshot_price_cny"] == 10.5 and trend["threshold_cny"] == 9.2
    assert trend["snapshot_relation"] == trend["current_relation"] == "above"
    assert trend["fresh_at_read"] is True
    assert turn["threshold_cny"] == 4.5 and turn["current_relation"] == "equal"
    assert before == (paper, cohort, receipt)
    assert original[0] == paper


def test_stale_wrong_clock_reference_or_day_never_becomes_current_or_uses_another_quote():
    paper, cohort, receipt = fixture()
    stale = deepcopy(cohort)
    for row in stale["quotes"]:
        row.update(current_at_read=False, read_status="stale")
    result = build_candidate_tracking(paper, stale, receipt, E + "T10:03:00+08:00")
    tracked = candidate(result, A)["tracking"]
    assert tracked["snapshot_relation"] == "above"
    assert tracked["current_relation"] is None and tracked["fresh_at_read"] is False
    mismatch = deepcopy(receipt)
    next(row for row in mismatch["quotes"] if row["symbol"] == A)["previous_close_price_rmb"] = "9.99"
    tracked = candidate(build_candidate_tracking(paper, cohort, mismatch, NOW), A)["tracking"]
    assert tracked["reference_status"] == "changed_or_incompatible"
    assert tracked["snapshot_relation"] is tracked["current_relation"] is None
    for fault in ("missing_symbol", "quote_clock", "raw_source_date", "receipt_day", "publication_after_quote"):
        p, c, r = deepcopy((paper, cohort, receipt))
        row = next(item for item in r["quotes"] if item["symbol"] == A)
        if fault == "missing_symbol":
            r["quotes"] = [item for item in r["quotes"] if item["symbol"] != A]
        elif fault == "quote_clock":
            row["source_quote_time"] = E + "T09:59:59+08:00"
        elif fault == "raw_source_date":
            row["source_date"] = D
        elif fault == "receipt_day":
            r["session_day"] = D
        else:
            p["recorded_at"] = E + "T10:00:02+08:00"
        tracked = candidate(build_candidate_tracking(p, c, r, NOW), A)["tracking"]
        assert tracked["fresh_at_read"] is False, fault
        assert tracked["current_relation"] is None, fault
        if fault in {"missing_symbol", "quote_clock"}:
            assert tracked["snapshot_price_cny"] is None, fault


def test_intraday_low_touch_is_not_close_failure_and_missing_structural_anchors_stay_unknown():
    paper, cohort, receipt = fixture()
    result = build_candidate_tracking(paper, cohort, receipt, NOW)
    for symbol in (A, B, T):
        tracked = candidate(result, symbol)["tracking"]
        assert tracked["snapshot_low_relation"] == "below"
        assert tracked["close_confirmation"] == "not_evaluated_intraday_snapshot"
        assert tracked["held_entry_anchor"] is None and tracked["trade_ready"] is False
    rebound = candidate(result, C)["tracking"]
    assert rebound["threshold_cny"] is None and rebound["current_relation"] is None
    # The saved setup low and the observed low cannot replace a missing prior open.
    assert rebound["snapshot_relation"] is None
    for kind in ("space_height_leader", "relay_continuation_leader", "liquidity_capacity_proxy", "cross_regime_candidate"):
        altered = deepcopy(paper)
        altered["rows"][0]["type"] = kind
        altered["rows"][0]["case_id"] = kind + "__unguarded__uncapped"
        tracked = candidate(build_candidate_tracking(altered, cohort, receipt, NOW), A)["tracking"]
        assert tracked["threshold_cny"] is None and tracked["current_relation"] is None, kind
        assert tracked["held_entry_anchor"] is None and tracked["trade_ready"] is False, kind


def test_exact_candidate_loader_uses_full_saved_union_keeps_latest_failure_and_reparses_raw(tmp_path, monkeypatch):
    from datetime import datetime
    from hashlib import sha256
    import json
    import pytest
    from trading.research import leader_candidate_intraday_view as view
    from trading.research import leader_candidate_observation as collector
    from trading.research.leader_cohort_observation import parse_batch, _member

    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    paper, _, _ = fixture()
    paper["rows"] = paper["rows"][:2]
    paper_path = tmp_path / "paper" / "paper_plan.json"
    save(paper_path, {"synthetic_saved_publication_bytes": "unchanged"})
    paper["source_file"] = str(paper_path.resolve())
    paper_bytes = paper_path.read_bytes()
    paper_ref = dict(file=paper["source_file"], sha256=sha256(paper_bytes).hexdigest(), byte_count=len(paper_bytes))
    details = [row["candidate_details"][0] for row in paper["rows"]]
    members = [{"symbol": item["symbol"], "name": item["name"], "labels": []} for item in reversed(details)]
    session = {"session_start": datetime.fromisoformat(E + "T09:30:00+08:00"),
               "stop_at": datetime.fromisoformat(E + "T11:30:00+08:00")}
    received = datetime.fromisoformat(E + "T10:00:01+08:00")
    lines = []
    for detail in details:
        symbol = detail["symbol"]
        previous, last, high, low = ("10", "10.5", "11", "8.8") if symbol == A else ("20", "19.5", "20.5", "18.8")
        fields = ["0"] * 34
        for index, value in {0: detail["name"], 1: previous, 2: previous, 3: last, 4: high, 5: low,
                             8: "000100", 9: "001000.123000", 30: E, 31: "10:00:00", 32: "00", 33: ""}.items():
            fields[index] = value
        lines.append('var hq_str_sz' + symbol[:6] + '="' + ','.join(fields) + '";')
    raw = "\n".join(lines).encode("gb18030")
    quotes, _ = parse_batch(raw, members, received, session=session,
        registered_at=datetime.fromisoformat(paper["recorded_at"]), batch_id=0)
    data_root = tmp_path / "data"
    root = data_root / "research" / "leader_candidate_observation"
    receipt_path, raw_path = root / "attempt_ok" / "receipt.json", root / "attempt_ok" / "batch_000.raw"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(raw)
    receipt = dict(schema=collector.SCHEMA, status=collector.SUCCESS, source_scope=collector.SOURCE_SCOPE,
        paper_ref=paper_ref, target_symbols=[member["symbol"] for member in members], quotes=quotes,
        session_day=E, session_start=session["session_start"].isoformat(), stop_at=session["stop_at"].isoformat(),
        response_received_at=received.isoformat(), finished_at=E + "T10:00:02+08:00",
        raw_file=str(raw_path.resolve()), raw_sha256=sha256(raw).hexdigest(), **collector.FLAGS)
    save(receipt_path, receipt)
    pointer = dict(schema=collector.LATEST_SCHEMA, paper_ref=paper_ref,
                   receipt_file=str(receipt_path.resolve()))
    save(root / "latest.json", pointer)
    def no_registry_fallback(*args, **kwargs):
        pytest.fail("Exact saved candidate loader must not borrow the 590 registry source")
    monkeypatch.setattr(view, "leader_cohort_intraday_view", no_registry_fallback)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = view.leader_candidate_intraday_view(tmp_path, data_root, paper=paper, now=NOW)
    assert result["observation_scope"] == "exact_saved_candidate_union"
    assert result["summary"]["unique_candidates"] == result["summary"]["observed_unique"] == 2
    assert result["summary"]["current_unique"] == 2 and result["summary"]["missing_unique"] == 0
    assert candidate(result, A)["tracking"]["snapshot_price_cny"] == 10.5
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    failed_path = root / "attempt_failed" / "receipt.json"
    failed = {**receipt, "status": collector.FAILURE, "error": "synthetic new transport failure",
        "raw_file": None, "raw_sha256": None, "finished_at": E + "T10:00:03+08:00",
        "quotes": [_member(member, 0) for member in members]}
    save(failed_path, failed)
    save(root / "latest.json", {**pointer, "receipt_file": str(failed_path.resolve())})
    latest = view.leader_candidate_intraday_view(tmp_path, data_root, paper=paper, now=NOW)
    assert latest["status"] == "unavailable_latest_candidate_attempt"
    assert latest["summary"]["current_unique"] == latest["summary"]["observed_unique"] == 0
    assert all(item["tracking"]["snapshot_price_cny"] is None for row in latest["rows"] for item in row["candidates"])

    save(root / "latest.json", pointer)
    altered_raw = raw.replace(b"10.5", b"10.6", 1)
    raw_path.write_bytes(altered_raw)
    with pytest.raises(ValueError, match="raw response changed"):
        view.leader_candidate_intraday_view(tmp_path, data_root, paper=paper, now=NOW)
    # Even an updated raw hash cannot make unchanged parsed quote fields agree.
    save(receipt_path, {**receipt, "raw_sha256": sha256(altered_raw).hexdigest()})
    with pytest.raises(ValueError, match="fields differ"):
        view.leader_candidate_intraday_view(tmp_path, data_root, paper=paper, now=NOW)
    assert paper_path.read_bytes() == paper_bytes
