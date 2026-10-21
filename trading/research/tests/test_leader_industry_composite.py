"""Small source-composition fixtures; no SDK, price, strategy or market calls."""
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research import leader_industry_composite as composite
from trading.research.leader_posthoc_industry import load as load_industry


SOURCE = "BaoStock.query_stock_industry(date=decision_day)"
OBSERVED = "2026-10-18T08:00:00+08:00"
RETRIEVED = "2026-10-17T03:35:15+08:00"
A, B = "000001.SZSE", "000002.SZSE"
DAYS = ["2025-01-02", "2025-01-06", "2025-01-13"]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def source(root, requested, successes, *, failed=None):
    root.mkdir()
    entries = []
    for day in successes:
        filename = day + ".parquet"
        pd.DataFrame({"date": [day, day], "symbol": [A, B],
            "updateDate": [day, day], "industry": ["bank", ""],
            "industryClassification": ["CSRC", "CSRC"]}).to_parquet(root / filename, index=False)
        entries.append({"date": day, "rows": 2, "file": filename,
            "retrieved_at": RETRIEVED, "update_dates": [day]})
    receipt = {"status": "failed_requested_industry_history" if failed else "completed_requested_industry_history",
        "source": SOURCE, "requested_dates": requested, "entries": entries,
        "revision_safe_pit_verified": False, "execution_admitted": False}
    if failed:
        receipt.update(failed_date=failed, error="TimeoutError: original synthetic stopped request")
    write(root / "result.json", receipt)
    return root


def fixture(tmp_path):
    # A complete synthetic calendar with exactly three distinct ISO-week buckets.
    calendar = write(tmp_path / "calendar.json", {"schema": "cn_a_share_exchange_calendar_v1",
        "year": 2025, "closed_ranges": ["2025-01-01/2025-01-01", "2025-01-18/2025-12-31"],
        "open_sessions": []})
    plan = write(tmp_path / "plan.json", {"created_at": "2026-10-17T03:00:00+08:00",
        "selection": "first_actual_2025_exchange_session_per_ISO_week",
        "calendar_file": str(calendar), "calendar_sha256": sha256(calendar.read_bytes()).hexdigest(),
        "requested_dates": DAYS, "requested_date_count": len(DAYS), "source": SOURCE,
        "date_selection_uses_returns": False, "revision_safe_pit_verified": False,
        "historical_publication_verified": False, "execution_admitted": False})
    old = source(tmp_path / "old_failed", DAYS, [DAYS[0]], failed=DAYS[1])
    new = source(tmp_path / "new_unqueried_only", [DAYS[2]], [DAYS[2]])
    return plan, old, new


def test_partial_sources_preserve_failed_week_unknown_and_old_failed_receipt(tmp_path):
    plan, old, new = fixture(tmp_path)
    before = {path: path.read_bytes() for root in (old, new) for path in root.iterdir()}
    out = tmp_path / "composite"
    composite.build(plan, [old, new], out)
    provider = load_industry(out, OBSERVED)
    assert provider("2025-01-01") is None
    first = provider("2025-01-03")
    assert first["snapshot_date"] == DAYS[0]
    assert first["membership"] == {A: "bank", B: None}
    assert first["actual_available_at"] == RETRIEVED
    # Never borrow the earlier week's successful snapshot through a failed bucket.
    assert provider(DAYS[1]) is None
    assert provider("2025-01-10") is None
    last = provider("2025-01-17")
    assert last["snapshot_date"] == DAYS[2] and last["membership"][B] is None
    assert last["actual_available_at"] == RETRIEVED
    assert provider("2024-12-31") is None and provider("2026-01-02") is None
    assert all(path.read_bytes() == value for path, value in before.items())
    with pytest.raises(ValueError, match="completed saved posthoc"):
        load_industry(old, OBSERVED)


def test_changed_source_clock_or_duplicate_success_is_rejected(tmp_path):
    plan, old, new = fixture(tmp_path)
    duplicate = source(tmp_path / "duplicate_success", [DAYS[0]], [DAYS[0]])
    with pytest.raises(ValueError):
        composite.build(plan, [old, new, duplicate], tmp_path / "duplicate_composite")
    out = tmp_path / "composite"
    composite.build(plan, [old, new], out)
    receipt = old / "result.json"
    original_receipt = receipt.read_bytes()
    altered = json.loads(original_receipt)
    altered["error"] = "Different retained original request error"
    write(receipt, altered)
    with pytest.raises(ValueError):
        composite.load(out, OBSERVED)
    receipt.write_bytes(original_receipt)
    parquet = new / (DAYS[2] + ".parquet")
    original_parquet = parquet.read_bytes()
    changed = pd.read_parquet(parquet)
    changed.loc[0, "industry"] = "altered_after_build"
    changed.to_parquet(parquet, index=False)
    with pytest.raises(ValueError):
        composite.load(out, OBSERVED)
    parquet.write_bytes(original_parquet)
    with pytest.raises(ValueError):
        composite.load(out, "2026-10-16T00:00:00+08:00")
    assert composite.load(out, OBSERVED)(DAYS[2])["membership"] == {A: "bank", B: None}
