from hashlib import sha256
import json

import pandas as pd
import pytest

from trading.research import chinaamc_fund_history as source
from trading.research.etf_reconciliation import distribution_coverage


def pages(code="510050", *, total="0.1190", rows=True):
    page = (f'<title>{source.NAMES[code]} fixture</title>'
            f'<div>\uff08\u57fa\u91d1\u4ee3\u7801\uff1a{code}\uff09</div>'
            '<div class="box2"><div class="item"><div class="t2">\u51c0\u503c\uff082026-09-30\uff09</div></div></div>'
            f'<iframe id="iframepage" src="/product/fundLishifenhong.do?fundcode={code}"></iframe>')
    history = (f'<form id="queryForm" action="fundLishifenhong.do" method="post">'
               f'<input name="fundcode" value="{code}"></form>'
               '<script>let totalPage = "1"; let currentPage = "1";</script>'
               '<table id="hoverBox1"><tr><th>\u5e74\u4efd</th><th>\u6743\u76ca\u767b\u8bb0\u65e5</th>'
               '<th>\u6bcf\u4efd\u5206\u7ea2\uff08\u5143\uff09</th><th>\u5206\u7ea2\u53d1\u653e\u65e5</th></tr></table>'
               '<table id="hoverBox1_1">')
    if rows:
        history += ('<tr><td>2025</td><td>2025-12-16</td><td>0.0800</td><td>2025-12-22</td></tr>'
                    '<tr><td>2023</td><td>2023-11-24</td><td>0.0390</td><td>2023-11-30</td></tr>')
    history += f'<tr><td></td><td></td><td>\u5408\u8ba1:{total}</td><td></td></tr></table>'
    return page.encode(), history.encode()


def fixture(folder, code="510050", *, empty=False):
    folder.mkdir()
    content = dict(zip(source.urls(code), pages(code, total="" if empty else "0.1190", rows=not empty)))
    receipts = {}
    for name, raw in content.items():
        (folder / name).write_bytes(raw)
        receipts[name] = {"file": name, "source_url": source.urls(code)[name], "sha256": sha256(raw).hexdigest()}
    parsed = source.parse(*content.values(), code)
    (folder / "result.json").write_text(json.dumps({"kind": source.KIND, "codes": [code],
        "funds": {code: parsed}, "receipts": receipts, "status": "completed_public_issuer_cash_history"}))
    return pd.DataFrame([{"instrument_code": code, "record_date": record, "ex_date": ex,
                          "pay_date": pay, "cash_rmb_per_unit": cash}
                         for record, ex, pay, cash in [("2023-11-24", "2023-11-27", "2023-11-30", "0.0390"),
                                                      ("2025-12-16", "2025-12-17", "2025-12-22", "0.0800")]])


def test_observed_full_history_keeps_absent_ex_dates_unknown():
    parsed = source.parse(*pages(), "510050")
    assert parsed["as_of"] == "2026-09-30" and parsed["complete_cash_history"]
    assert parsed["reported_total_cash_per_unit"] == "0.1190"
    assert not parsed["ex_dates_provided"] and "ex_date" not in parsed["records"][0]


@pytest.mark.parametrize("problem", ["identity", "frame", "pagination", "filter", "units", "missing_row", "duplicate"])
def test_changed_identity_incomplete_query_or_cash_semantics_cannot_be_coverage(problem):
    page, history = [raw.decode() for raw in pages()]
    if problem == "identity":
        page = page.replace("510050", "510300")
    elif problem == "frame":
        page = page.replace("fundcode=510050", "fundcode=588000")
    elif problem == "pagination":
        history = history.replace('totalPage = "1"', 'totalPage = "2"')
    elif problem == "filter":
        history = history.replace('</form>', '<input name="startDate" value="2026-01-01"></form>')
    elif problem == "units":
        history = history.replace("\u6bcf\u4efd", "\u6bcf10\u4efd")
    elif problem == "missing_row":
        history = history.replace('<tr><td>2023</td><td>2023-11-24</td><td>0.0390</td><td>2023-11-30</td></tr>', '')
    else:
        row = '<tr><td>2023</td><td>2023-11-24</td><td>0.0390</td><td>2023-11-30</td></tr>'
        history = history.replace(row, row + row)
    with pytest.raises(ValueError):
        source.parse(page, history, "510050")


def test_empty_blank_total_is_not_zero_or_complete_cash_coverage(tmp_path):
    fixture(tmp_path / "history", "588000", empty=True)
    fund = source.load(tmp_path / "history")["funds"]["588000"]
    assert fund["reported_total_cash_per_unit"] is None and not fund["complete_cash_history"]
    empty = pd.DataFrame(columns=["instrument_code", "record_date", "ex_date", "pay_date", "cash_rmb_per_unit"])
    with pytest.raises(ValueError, match="does not establish coverage"):
        source.period_evidence(tmp_path / "history", empty, "2026-07-01", "2026-09-30", "588000")


def test_registered_history_requires_all_modern_pdf_events_not_only_q3(tmp_path):
    events = fixture(tmp_path / "history")
    proof = source.period_evidence(tmp_path / "history", events, "2026-07-01", "2026-09-30", "510050")
    assert proof["events"] == [] and proof["independent_pdf_events_matched"] == 2
    assert proof["ex_dates_from"] == "independently_reviewed_issuer_pdfs"
    proof.update(history_folder="history", source_file="history/" + proof["source_file"])
    (tmp_path / "distribution_periods.json").write_text(json.dumps([proof]))
    assert distribution_coverage(tmp_path, events, "2026-07-01", "2026-09-30", "510050")["complete_for_requested_period"]
    changed = events.copy()
    changed.loc[changed.record_date.eq("2023-11-24"), "cash_rmb_per_unit"] = "0.3900"
    with pytest.raises(ValueError, match="independently reviewed"):
        distribution_coverage(tmp_path, changed, "2026-07-01", "2026-09-30", "510050")
    with pytest.raises(ValueError, match="does not establish coverage"):
        source.period_evidence(tmp_path / "history", events, "2026-07-01", "2026-10-01", "510050")


def test_period_ex_dates_come_from_pdf_not_guessed_next_calendar_day(tmp_path):
    events = fixture(tmp_path / "history")
    proof = source.period_evidence(tmp_path / "history", events, "2023-01-03", "2026-09-30", "510050")
    assert proof["events"][0]["ex_date"] == "2023-11-27"
    changed = events.copy()
    changed["instrument_code"] = "510300"
    with pytest.raises(ValueError, match="does not establish coverage"):
        source.period_evidence(tmp_path / "history", changed, "2023-01-03", "2026-09-30", "510050")
