from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

from trading.research import f2_issuer_source_evidence as evidence


def test_extract_pdf_text_falls_back_when_pypdf_is_mojibake(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture")

    class FakeReader:
        def __init__(self, _path):
            self.pages = [SimpleNamespace(extract_text=lambda: "000002 2023")]

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=FakeReader))
    monkeypatch.setattr(evidence.shutil, "which", lambda _name: "pdftotext.exe")
    monkeypatch.setattr(
        evidence.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="总资产 归属于上市公司股东的净利润"),
    )
    text, engine = evidence.extract_pdf_text(pdf)
    assert engine == "pdftotext"
    assert "总资产" in text


def test_map_field_scope_distinguishes_flow_and_instant_stock():
    result = evidence.map_field_scope(
        "单位：元 本集团 报告期末 总资产 归属于上市公司股东的净利润 人民币",
        {
            "parent_net_profit": {
                "scope": "parent_attributable", "period_kind": "single_quarter",
                "statement_role": "flow", "unit": "yuan", "currency": "CNY",
            },
            "total_assets": {
                "scope": "consolidated_group", "period_kind": "instant",
                "statement_role": "stock", "unit": "yuan", "currency": "CNY",
            },
        },
    )

    assert result["all_verified"] is True
    assert result["fields"]["parent_net_profit"]["period_kind"] == "single_quarter"
    assert result["fields"]["total_assets"]["period_kind"] == "instant"


def test_map_field_scope_keeps_missing_group_word_blocked():
    result = evidence.map_field_scope(
        "单位：元 报告期末 总资产 归属于上市公司股东的净利润 人民币",
        {
            "parent_net_profit": {"scope": "parent_attributable"},
            "total_assets": {"scope": "consolidated_group"},
        },
    )

    assert result["all_verified"] is False
    assert result["fields"]["total_assets"]["status"] == "blocked"


def test_capture_records_hash_and_keeps_current_fetch_blocked(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "000002.SZSE_2023Q1.pdf"
    pdf.write_bytes(b"fixture-pdf")
    monkeypatch.setattr(
        evidence,
        "extract_pdf_text",
        lambda _path: (
            "万科企业股份有限公司 2023 年第一季度报告 证券代码：000002 "
            "报告期指 2023 年 1-3 月 本报告所用记账本位币均为人民币",
            "fixture",
        ),
    )

    result = evidence.capture_issuer_source(
        symbol="000002.SZSE",
        period="2023Q1",
        publication_date="2023-04-29",
        decision_day="2024-05-01",
        source_url="https://static.cninfo.com.cn/finalpage/2023-04-29/1216686497.PDF",
        local_pdf=pdf,
        fetcher=lambda _url: {
            "requested_at": "2026-10-26T02:30:00+08:00",
            "retrieved_at": "2026-10-26T02:30:01+08:00",
            "http_status": 200,
            "content_type": "application/pdf",
            "content_length": len(b"fixture-pdf"),
            "response_sha256": evidence._hash_bytes(b"fixture-pdf"),
            "server_last_modified_at": "2023-04-28T17:07:58+08:00",
            "etag": "",
            "payload": b"fixture-pdf",
            "error": None,
        },
    )

    assert result["status"] == "BLOCKED_DATA"
    assert result["retrieved_source_hash_matches_saved"] is True
    assert result["identity_checks"]["quarter_report_title_present"] is True
    assert result["historical_retrieval_asof_verified"] is False
    assert "historical_retrieval_asof_unproven" in result["reason_codes"]
    assert result["execution_admitted"] is False


def test_capture_fails_closed_for_missing_or_mismatched_source(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"saved")
    monkeypatch.setattr(evidence, "extract_pdf_text", lambda _path: ("000002", "fixture"))
    result = evidence.capture_issuer_source(
        symbol="000002.SZSE", period="2023Q1", publication_date="2023-04-29",
        decision_day="2024-05-01", source_url="https://example.invalid/report.pdf", local_pdf=pdf,
        fetcher=lambda _url: {
            "requested_at": "2026-10-26T02:30:00+08:00", "retrieved_at": "2026-10-26T02:30:01+08:00",
            "http_status": 404, "content_type": "", "content_length": 0,
            "response_sha256": "", "server_last_modified_at": None, "etag": "",
            "payload": b"", "error": "HTTPError",
        },
    )
    assert result["status"] == "BLOCKED_DATA"
    assert "issuer_source_http_not_200" in result["reason_codes"]
    assert "retrieved_source_hash_does_not_match_saved_artifact" in result["reason_codes"]
