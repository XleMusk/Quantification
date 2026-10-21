from hashlib import sha256
from io import BytesIO
import json
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from trading.research import fund_replay_snapshot as source
from trading.rules import FUND_H1_2026_PROFILES, FUND_Q3_2026_PROFILES, execution_rule
from .test_fund_replay_snapshot import inputs


def reviewed_fixture(folder, symbol):
    def archive(name, content):
        (folder / name).write_bytes(content)
        return {"source_file": name, "source_sha256": sha256(content).hexdigest()}

    def docx(paragraphs):
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        root = ElementTree.Element(ns + "document")
        body = ElementTree.SubElement(root, ns + "body")
        for text in paragraphs:
            p = ElementTree.SubElement(body, ns + "p")
            r = ElementTree.SubElement(p, ns + "r")
            ElementTree.SubElement(r, ns + "t").text = text
        output = BytesIO()
        with ZipFile(output, "w") as zipfile:
            zipfile.writestr("word/document.xml", ElementTree.tostring(root))
        return output.getvalue()

    required = source.FUND_RULE_CLAUSES | {"6.6"}
    deferred = archive("deferred.docx", docx(["Fixture only, not an exchange rule"]))
    replacement = {**archive("rules.docx", docx([clause + " fixture" for clause in sorted(required)])),
                   "source_url": source.RULE_2026_URL, "effective_date": source.RULE_REPLACEMENT_DATE,
                   **{"deferred_" + key: value for key, value in deferred.items()},
                   "deferred_source_url": source.RULE_DEFERRED_URL,
                   "reviewed_deferred_scope": "none_of_reviewed_secondary_fund_auction_clauses",
                   "reviewed_clauses": sorted(required)}
    old = archive("old.pdf", b"%PDF- fixture only")
    return {**archive("identity.pdf", b"%PDF- fixture only"),
            "instrument_type": "equity_etf", "ipoDate": "2012-05-28",
            "profile": FUND_Q3_2026_PROFILES[symbol], "rule_span": ["2024-01-01", "2026-09-30"],
            "rule_source_file": old["source_file"], "rule_source_sha256": old["source_sha256"],
            "rule_eras": [["2024-01-01", "2026-07-05"], ["2026-07-06", "2026-09-30"]],
            "rule_replacement_notice": {**archive("notice.html", b"fixture only"),
                                        "source_url": source.RULE_NOTICE_URL, "effective_date": "2026-07-06"},
            "replacement_rule": replacement}


def test_q3_profiles_preserve_fund_units_and_settlement_without_extending_h1():
    for symbol in FUND_Q3_2026_PROFILES:
        for day in ("2026-07-03", "2026-07-06", "2026-09-30"):
            rule = execution_rule(symbol, day, FUND_Q3_2026_PROFILES)
            assert rule.price_unit == "mill" and rule.quantity.minimum == rule.quantity.step == 100
            assert rule.quantity.maximum == 1_000_000
            assert rule.same_day_resale == (symbol in {"511010.SSE", "518880.SSE"})
            assert float(rule.fund_limit) == (.2 if symbol == "588000.SSE" else .1)
        with pytest.raises(ValueError, match="outside reviewed"):
            execution_rule(symbol, "2026-10-01", FUND_Q3_2026_PROFILES)
        if symbol != "510300.SSE":
            with pytest.raises(ValueError, match="outside reviewed"):
                execution_rule(symbol, "2026-07-06", FUND_H1_2026_PROFILES)


@pytest.mark.parametrize("missing", ["replacement_rule", "rule_eras", "star_clause"])
def test_q3_requires_archived_replacement_both_eras_and_star_exception(tmp_path, missing):
    symbol = "588000.SSE"
    review = reviewed_fixture(tmp_path, symbol)
    if missing == "star_clause":
        review["replacement_rule"]["reviewed_clauses"].remove("6.6")
    else:
        review.pop(missing)
    with pytest.raises(ValueError, match="both dated rule eras"):
        source.reviewed_fund_rule(tmp_path, review, symbol, "2026-09-30", FUND_Q3_2026_PROFILES)


def test_rule_evidence_does_not_replace_missing_cash_coverage_in_snapshot_consumer(tmp_path, monkeypatch):
    paths = inputs(tmp_path, monkeypatch)
    review = reviewed_fixture(paths[-1], "510300.SSE")
    (paths[-1] / "instrument_reviews.json").write_text(json.dumps({"510300": review}))
    (paths[-1] / "distribution_periods.json").write_text("[]")
    with pytest.raises(ValueError, match="entire diagnostic"):
        source.build(*paths, out=tmp_path / "uncovered", profiles=FUND_Q3_2026_PROFILES)
    assert not (tmp_path / "uncovered").exists()
