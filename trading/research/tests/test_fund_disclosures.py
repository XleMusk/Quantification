import json

import pytest

from trading.research import fund_disclosures as source


def payload(count=1):
    return {"sqlId": source.SQL_ID, "actionErrors": [], "fieldErrors": {},
            "pageHelp": {"total": count, "pageCount": int(count > 0), "pageNo": 1, "pageSize": 100},
            "result": [{"SECURITY_CODE": "510300", "SSEDATE": "2026-08-29",
                        "TITLE": "issuer interim report",
                        "URL": "/disclosure/fund/announcement/c/new/2026-08-29/510300_report.pdf"}]
            if count else []}


def test_official_recipe_and_exact_security_dates():
    params = source.parameters("510300", "2026-01-01", "2026-09-24", 1)
    assert params["SECURITY_CODE"] == "510300" and params["sqlId"] == source.SQL_ID
    rows, total, pages = source.validate_page(payload(), "510300", "2026-01-01", "2026-09-24", 1)
    assert (total, pages) == (1, 1)
    assert rows[0]["source_url"].startswith("https://www.sse.com.cn/disclosure/fund/announcement/c/new/")
    assert source.validate_page(payload(0), "510300", "2026-01-01", "2026-09-24", 1) == ([], 0, 0)


@pytest.mark.parametrize("code,first,last,page", [
    ("159915", "2026-01-01", "2026-09-24", 1),
    ("510300", "2026-09-24", "2026-01-01", 1),
    ("510300", "2022-01-01", "2026-09-24", 1),
    ("510300", "2026-01-01", "2026-09-24", 11),
])
def test_wrong_exchange_ranges_and_unbounded_pages_refused(code, first, last, page):
    with pytest.raises(ValueError):
        source.parameters(code, first, last, page)


@pytest.mark.parametrize("mutation", ["security", "date", "url", "total", "duplicate"])
def test_query_conflicts_never_become_complete(mutation):
    data = payload()
    if mutation == "security":
        data["result"][0]["SECURITY_CODE"] = "510050"
    elif mutation == "date":
        data["result"][0]["SSEDATE"] = "2025-08-29"
    elif mutation == "url":
        data["result"][0]["URL"] = "https://example.org/report.pdf"
    elif mutation == "total":
        data["pageHelp"]["total"] = 2
    else:
        data["result"].append(data["result"][0].copy())
        data["pageHelp"]["total"] = 2
    with pytest.raises(ValueError):
        source.validate_page(data, "510300", "2026-01-01", "2026-09-24", 1)


def test_actual_consumer_archives_raw_query_but_never_admits_actions(tmp_path, monkeypatch):
    raw = json.dumps(payload()).encode()
    monkeypatch.setattr(source, "fetch", lambda session, params: raw)
    folder, result = source.collect(["510300"], "2026-01-01", "2026-09-24", tmp_path / "index", object())
    assert (folder / "510300_page_1.json").read_bytes() == raw
    assert result["completed_codes"] == ["510300"]
    assert not result["execution_admitted"] and not result["strategy_validated"]
    assert not (folder / "actions.csv").exists()


def test_source_failure_retains_raw_page_and_failed_code(tmp_path, monkeypatch):
    data = payload()
    data["result"][0]["SECURITY_CODE"] = "510050"
    monkeypatch.setattr(source, "fetch", lambda session, params: json.dumps(data).encode())
    with pytest.raises(ValueError, match="security"):
        source.collect(["510300"], "2026-01-01", "2026-09-24", tmp_path / "failed", object())
    result = json.loads((tmp_path / "failed/result.json").read_text())
    assert result["status"] == "stopped_source_error" and result["failed_code"] == "510300"
    assert result["completed_codes"] == [] and len(result["receipts"]) == 1


def test_saved_index_hash_and_records_are_consumed_before_document_download(tmp_path, monkeypatch):
    data = payload()
    data["result"][0]["TITLE"] = "fund 2026\u5e74\u4e2d\u671f\u62a5\u544a"
    monkeypatch.setattr(source, "fetch", lambda session, params: json.dumps(data).encode())
    folder, _ = source.collect(["510300"], "2026-01-01", "2026-09-24", tmp_path / "index", object())
    monkeypatch.setattr(source, "fetch_pdf", lambda session, url: b"%PDF-issuer report")
    docs, result = source.download_interim(folder, tmp_path / "docs", object())
    assert result["status"] == "completed_primary_document_acquisition"
    assert (docs / result["receipts"][0]["file"]).read_bytes() == b"%PDF-issuer report"
    assert not result["execution_admitted"]
    (folder / "510300_page_1.json").write_bytes(b"altered")
    with pytest.raises(ValueError, match="hash changed"):
        source.download_interim(folder, tmp_path / "should_not_exist", object())
    assert not (tmp_path / "should_not_exist").exists()


def test_challenge_html_is_archived_but_not_accepted_as_a_report(tmp_path, monkeypatch):
    data = payload()
    data["result"][0]["TITLE"] = "fund 2026\u5e74\u4e2d\u671f\u62a5\u544a"
    monkeypatch.setattr(source, "fetch", lambda session, params: json.dumps(data).encode())
    folder, _ = source.collect(["510300"], "2026-01-01", "2026-09-24", tmp_path / "index", object())
    monkeypatch.setattr(source, "fetch_pdf", lambda session, url: b"<html>blocked</html>")
    with pytest.raises(ValueError, match="not a PDF"):
        source.download_interim(folder, tmp_path / "docs", object())
    result = json.loads((tmp_path / "docs/result.json").read_text())
    assert result["status"] == "stopped_source_error" and result["receipts"] == []
    assert (tmp_path / "docs/510300_2026-08-29_interim.pdf").read_bytes().startswith(b"<html>")


@pytest.mark.parametrize("suffix", ["\u5206\u7ea2\u516c\u544a", "\u6536\u76ca\u5206\u914d\u516c\u544a"])
def test_multiple_cash_distributions_selected_by_explicit_type_and_saved_index(tmp_path, monkeypatch, suffix):
    data = payload()
    first = data["result"][0]
    first.update(TITLE="fund" + suffix, ORG_BULLETIN_TYPE_DESC="\u6536\u76ca\u5206\u914d")
    second = {**first, "SSEDATE": "2026-09-01", "URL": "/disclosure/fund/announcement/c/new/2026-09-01/510300_cash.pdf"}
    data["result"].append(second)
    data["pageHelp"]["total"] = 2
    monkeypatch.setattr(source, "fetch", lambda session, params: json.dumps(data).encode())
    index, _ = source.collect(["510300"], "2026-01-01", "2026-09-24", tmp_path / "index", object())
    monkeypatch.setattr(source, "fetch_pdf", lambda session, url: b"%PDF-cash distribution")
    with pytest.raises(ValueError, match="saved disclosure index"):
        source.download_documents(index, "distribution", tmp_path / "wrong", object(), codes=["510880"])
    assert not (tmp_path / "wrong").exists()
    folder, result = source.download_documents(index, "distribution", tmp_path / "cash", object(), codes=["510300"])
    assert len(result["receipts"]) == 2
    assert len(set(item["file"] for item in result["receipts"])) == 2
    assert all(item["file"].endswith("_distribution.pdf") for item in result["receipts"])
    assert result["document_kind"] == "distribution" and not result["execution_admitted"]
    assert not (folder / "actions.csv").exists()


def test_same_day_distinct_documents_cannot_overwrite_an_archive(tmp_path, monkeypatch):
    data = payload()
    first = data["result"][0]
    first.update(TITLE="fund\u5206\u7ea2\u516c\u544a", ORG_BULLETIN_TYPE_DESC="\u6536\u76ca\u5206\u914d")
    data["result"].append({**first, "URL": "/disclosure/fund/announcement/c/new/2026-08-29/510300_second.pdf"})
    data["pageHelp"]["total"] = 2
    monkeypatch.setattr(source, "fetch", lambda session, params: json.dumps(data).encode())
    index, _ = source.collect(["510300"], "2026-01-01", "2026-09-24", tmp_path / "index", object())
    with pytest.raises(ValueError, match="overwriting"):
        source.download_documents(index, "distribution", tmp_path / "cash", object())
    assert not (tmp_path / "cash").exists()


def test_annual_report_year_not_publication_year_or_summary(tmp_path, monkeypatch):
    data = payload()
    first = data["result"][0]
    first["TITLE"] = "fund2025\u5e74\u5e74\u5ea6\u62a5\u544a"
    data["result"].append({**first, "TITLE": first["TITLE"] + "\u6458\u8981",
                           "URL": "/disclosure/fund/announcement/c/new/2026-08-29/510300_summary.pdf"})
    data["pageHelp"]["total"] = 2
    monkeypatch.setattr(source, "fetch", lambda session, params: json.dumps(data).encode())
    index, _ = source.collect(["510300"], "2026-01-01", "2026-09-24", tmp_path / "index", object())
    monkeypatch.setattr(source, "fetch_pdf", lambda session, url: b"%PDF-annual report")
    folder, result = source.download_documents(index, "annual", tmp_path / "annual", object(),
                                              codes=["510300"], report_year=2025)
    assert len(result["receipts"]) == 1 and result["report_year"] == 2025
    assert result["requested_documents"][0]["TITLE"].endswith("\u5e74\u5ea6\u62a5\u544a")
    assert (folder / result["receipts"][0]["file"]).read_bytes().startswith(b"%PDF-")
    for year in (None, 2024, 2026):
        with pytest.raises(ValueError):
            source.download_documents(index, "annual", tmp_path / ("bad" + str(year)), object(), report_year=year)
        assert not (tmp_path / ("bad" + str(year))).exists()
    with pytest.raises(ValueError, match="only valid"):
        source.download_documents(index, "interim", tmp_path / "wrong_kind", object(), report_year=2025)
