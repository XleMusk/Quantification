from copy import deepcopy
from datetime import date, timedelta
from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research import gt_fund_history as source
from trading.research.etf_reconciliation import distribution_coverage


def payload():
    def instant(day):
        return (date.fromisoformat(day) - timedelta(days=1)).isoformat() + "T16:00:00.000Z"
    return [{"date": instant(ex), "registerDate": instant(record), "payDate": instant(pay), "dividend": cash,
             "date2": None, "registerDate2": None, "payDate2": None}
            for record, ex, pay, cash in (("2026-09-17", "2026-09-18", "2026-09-23", "0.6204"),
                                          ("2026-06-24", "2026-06-25", "2026-06-30", "0.8144"))]


def archive_fixture(folder):
    folder.mkdir()
    detail = {"code": "511010", "displayCode": "511010", "fullName": "synthetic fixture only",
              "daily": {"date": "2026-09-29T16:00:00.000Z"}}
    contents = {
        "page.html": ('<div id="jijin_fenhong"></div><script>var gtOptions = { detail: '
                      + json.dumps(detail) + '};</script>').encode(),
        "common.js": b"/Etrade/Public/cochin/",
        "view.js": b"info.fund.dividend fundCode: gtOptions.fundcode",
        "511010_dividends.json": json.dumps(payload()).encode()}
    urls = {"page.html": source.PAGE, "common.js": source.COMMON, "view.js": source.VIEW,
            "511010_dividends.json": source.API}
    receipts = {}
    for name, content in contents.items():
        (folder / name).write_bytes(content)
        receipts[name] = {"file": name, "source_url": urls[name], "sha256": sha256(content).hexdigest()}
    parsed = source.validate_sources(contents["page.html"], contents["common.js"].decode(),
                                     contents["view.js"].decode(), payload())
    (folder / "result.json").write_text(json.dumps({**parsed, "status": "completed_public_issuer_cash_history",
                                                   "kind": source.KIND, "query": source.PARAMS, "receipts": receipts}))
    return pd.DataFrame(source.normalize(payload()))


def test_issuer_zoned_midnights_are_china_dates_not_utc_dates():
    rows = source.normalize(payload())
    assert rows[-1]["record_date"] == "2026-09-17"
    assert rows[-1]["ex_date"] == "2026-09-18"
    assert rows[-1]["pay_date"] == "2026-09-23"
    assert rows[-1]["cash_rmb_per_unit"] == "0.6204"


@pytest.mark.parametrize("problem", ["empty", "naive", "duplicate", "second_class", "zero"])
def test_incomplete_or_ambiguous_history_is_not_empty_cash_coverage(problem):
    data = deepcopy(payload())
    if problem == "empty":
        data = []
    elif problem == "naive":
        data[0]["date"] = "2026-09-18T00:00:00"
    elif problem == "duplicate":
        data.append(deepcopy(data[0]))
    elif problem == "second_class":
        data[0]["date2"] = data[0]["date"]
    else:
        data[0]["dividend"] = "0"
    with pytest.raises(ValueError):
        source.normalize(data)


def test_cash_coverage_consumes_primary_history_and_all_known_pdf_events(tmp_path):
    relative = "history"
    events = archive_fixture(tmp_path / relative)
    proof = source.period_evidence(tmp_path / relative, events, "2026-07-01", "2026-09-30")
    proof.update(history_folder=relative, source_file=relative + "/" + proof["source_file"])
    (tmp_path / "distribution_periods.json").write_text(json.dumps([proof]))
    result = distribution_coverage(tmp_path, events, "2026-07-01", "2026-09-30", "511010")
    assert result["complete_for_requested_period"] and not proof["share_actions_complete"]
    assert proof["independent_pdf_events_matched"] == 2
    changed = events.copy()
    changed.loc[changed.ex_date.eq("2026-06-25"), "cash_rmb_per_unit"] = "0.8000"
    with pytest.raises(ValueError, match="independently reviewed"):
        distribution_coverage(tmp_path, changed, "2026-07-01", "2026-09-30", "511010")
    proof["instrument_code"] = "510300"
    proof["events"] = []
    (tmp_path / "distribution_periods.json").write_text(json.dumps([proof]))
    with pytest.raises(ValueError, match="requested code or dates"):
        distribution_coverage(tmp_path, events.iloc[:0], "2026-07-01", "2026-09-30", "510300")


def test_stale_history_page_cannot_cover_later_period(tmp_path):
    events = archive_fixture(tmp_path / "history")
    with pytest.raises(ValueError, match="requested code or dates"):
        source.period_evidence(tmp_path / "history", events, "2026-07-01", "2026-10-01")
