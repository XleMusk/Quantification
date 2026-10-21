"""Full primary-page text fixtures; not HTTP success or native-account evidence."""
import json

import pandas as pd
import pytest

from trading.research import huaan_fund_history as source
from trading.research.etf_reconciliation import distribution_coverage


def extraction(url, lines):
    return ("Source: open(" + json.dumps({"ref_id": url, "lineno": None}) + "); Total lines: "
            + str(len(lines)) + "\n" + "\n".join(f"L{i}: {line}" for i, line in enumerate(lines))).encode("utf-8")


def pages():
    page = extraction(source.PAGE, ["\u6cd5\u5b9a\u540d\u79f0 | " + source.NAME + " | \u57fa\u91d1\u4ee3\u7801 | 518880",
        "* \u65e5\u671f", "2026-09-30", "* \u5355\u4f4d\u51c0\u503c", "8.6278", "* \u7d2f\u8ba1\u51c0\u503c", "3.2580",
        "* \u5386\u53f2\u5206\u7ea2", " | ".join(source.HEADERS), "--- | --- | --- | --- | --- | --- | ---",
        " | ".join(source.SPLIT), "### \u66f4\u591a \u5206\u7ea2\u516c\u544a", "\u672c\u7f51\u7ad9\u6240\u6709\u8d44\u8baf\u4ec5\u4f9b\u53c2\u8003"])
    pcf = extraction(source.PCF, ["\u6700\u65b0\u516c\u544a\u65e5\u671f 20261008", "\u57fa\u91d1\u4ee3\u7801 518880",
        "20260930 \u4fe1\u606f\u5185\u5bb9", "\u57fa\u91d1\u4efd\u989d\u51c0\u503c(\u5355\u4f4d:\u5143) \uffe5 8.6278",
        "20261008 \u4fe1\u606f\u5185\u5bb9"])
    return page, pcf


def events():
    return pd.DataFrame(columns=["instrument_code", "record_date", "ex_date", "pay_date", "cash_rmb_per_unit"])


def test_split_not_cash_and_next_session_pcf_not_observed_nav_date():
    parsed = source.parse(*pages())
    assert parsed["as_of"] == "2026-09-30" and parsed["cash_records"] == []
    assert parsed["observed_split_records"] == [{"date": "2013-07-24", "ratio": "0.37761358"}]
    assert parsed["complete_cash_history"] and not parsed["share_actions_complete"]


@pytest.mark.parametrize("problem", ["truncated", "search_snippet", "wrong_fund", "empty", "new_cash_row",
                                   "filter", "missing_end", "pcf_mismatch", "old_page"])
def test_incomplete_or_changed_primary_evidence_never_becomes_zero_coverage(problem):
    page, pcf = [raw.decode("utf-8") for raw in pages()]
    if problem == "truncated":
        page = page.replace("L10:", "")
    elif problem == "search_snippet":
        page = page.replace('"lineno": null', '"lineno": 8')
    elif problem == "wrong_fund":
        page = page.replace("518880", "000216")
    elif problem == "empty":
        page = page.replace(" | ".join(source.SPLIT), "")
    elif problem == "new_cash_row":
        page = page.replace(source.SPLIT[1], "\u6bcf10\u4efd\u6d3e0.1\u5143").replace("\u62c6\u5206", "\u73b0\u91d1\u5206\u7ea2")
    elif problem == "filter":
        page = page.replace(" | ".join(source.SPLIT), "[Select] 2026 " + " | ".join(source.SPLIT))
    elif problem == "missing_end":
        page = page.replace("### \u66f4\u591a \u5206\u7ea2\u516c\u544a", "")
    elif problem == "pcf_mismatch":
        pcf = pcf.replace("8.6278", "8.6279")
    else:
        page = page.replace("2026-09-30", "2026-09-10")
    with pytest.raises(ValueError):
        source.parse(page.encode("utf-8"), pcf.encode("utf-8"))


def test_archive_is_consumed_by_existing_distribution_coverage(tmp_path):
    page, pcf = pages()
    (tmp_path / "page.txt").write_bytes(page)
    (tmp_path / "pcf.txt").write_bytes(pcf)
    folder, state = source.collect(tmp_path / "page.txt", tmp_path / "pcf.txt", tmp_path / "history")
    assert not state["execution_admitted"]
    proof = source.period_evidence(folder, events(), "2026-07-01", "2026-09-30")
    assert proof["events"] == [] and not proof["share_actions_complete"]
    proof.update(history_folder="history", source_file="history/" + proof["source_file"])
    (tmp_path / "distribution_periods.json").write_text(json.dumps([proof]))
    assert distribution_coverage(tmp_path, events(), "2026-07-01", "2026-09-30", "518880")["complete_for_requested_period"]
    for code, last in [("000216", "2026-09-30"), ("518880", "2026-10-01")]:
        with pytest.raises(ValueError, match="does not establish"):
            source.period_evidence(folder, events(), "2026-07-01", last, code)
    changed = pd.DataFrame([{"instrument_code": "518880", "ex_date": "2026-07-01", "cash_rmb_per_unit": "0.1"}])
    with pytest.raises(ValueError, match="conflicts"):
        distribution_coverage(tmp_path, changed, "2026-07-01", "2026-09-30", "518880")


def test_future_nav_cannot_be_promoted_by_altering_acquisition_time(tmp_path):
    page, pcf = pages()
    (tmp_path / "page.txt").write_bytes(page)
    (tmp_path / "pcf.txt").write_bytes(pcf)
    folder, state = source.collect(tmp_path / "page.txt", tmp_path / "pcf.txt", tmp_path / "history")
    state["retrieved_at"] = "2026-09-10T00:00:00+08:00"
    (folder / "result.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="acquisition date"):
        source.load(folder)
