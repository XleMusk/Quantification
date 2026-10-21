"""Dated positive-risk evidence: serial raw capture, narrow scope, no retries."""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from trading.research import current_risk_warning_source as risk


DAY = "2026-10-19"
START = datetime.fromisoformat(DAY + "T11:00:00+08:00")


def _payloads():
    codes = [["000001", "300001"], ["200001"]]
    return {page: [{"metadata": {"tabkey": "tab1", "catalogid": "fxjsb", "name": "风险警示股票", "subname": DAY,
                "pageno": page, "pagesize": 2, "recordcount": 3, "pagecount": 2, "error": "0"},
            "data": [{"zqdm": code, "gsjc": "合成风险样本" + code} for code in values]}]
        for page, values in enumerate(codes, 1)}


class _Clock:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        value = START + timedelta(seconds=self.calls)
        self.calls += 1
        return value


class _HTTP:
    def __init__(self, payloads, *, fail_first=False):
        self.payloads = payloads
        self.fail_first = fail_first
        self.pages = []
        self.active = False
        self.closed = []
        self.raw = {}

    def get(self, url, **kwargs):
        assert not self.active, "Official pages must be fetched serially"
        assert url == risk.URL
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert kwargs["timeout"] == (10, 20)
        assert kwargs["params"]["CATALOGID"] == "fxjsb" and kwargs["params"]["TABKEY"] == "tab1"
        page = int(kwargs["params"]["PAGENO"])
        self.pages.append(page)
        self.active = True
        raw = json.dumps(self.payloads[page], ensure_ascii=False).encode("utf-8")
        self.raw[page] = raw
        owner = self

        class Response:
            status_code = 503 if owner.fail_first and page == 1 else 200
            headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                owner.active = False
                owner.closed.append(page)

            def iter_content(self, size):
                # Preserve the exact original stream rather than a JSON re-serialization.
                yield raw[:17]
                yield raw[17:]

        return Response()


def _collect(folder, payloads=None, *, fail_first=False):
    http = _HTTP(payloads or _payloads(), fail_first=fail_first)
    receipt = risk.collect(DAY, folder, clock=_Clock(), get=http.get)
    return receipt, http


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _rewrite_raw(folder, page, mutate):
    receipt = _read(folder / "receipt.json")
    raw_file = folder / receipt["pages"][page - 1]["raw_file"]
    value = _read(raw_file)
    mutate(value)
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    raw_file.write_bytes(raw)
    # A matching envelope hash must not bypass the underlying report semantics.
    receipt["pages"][page - 1].update(byte_count=len(raw), sha256=sha256(raw).hexdigest())
    _write(folder / "receipt.json", receipt)


def test_collect_preserves_both_original_pages_serially_with_one_dated_unique_inventory(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("No actual network is permitted in this focused check")

    monkeypatch.setattr("requests.get", no_network)
    folder = tmp_path / "complete"
    receipt, http = _collect(folder)
    assert receipt["status"] == "completed_full_dated_risk_warning_report"
    assert receipt["full_report_complete"] is True and receipt["raw_response_preserved"] is True
    assert receipt["reported_count"] == 3 and receipt["reported_page_count"] == 2
    assert receipt["all_reported_codes"] == ["000001", "300001", "200001"]
    assert len(set(receipt["all_reported_codes"])) == 3
    assert receipt["ordinary_risk_symbols"] == ["000001.SZSE", "300001.SZSE"]
    assert http.pages == http.closed == [1, 2] and http.active is False
    for observation in receipt["pages"]:
        raw = (folder / observation["raw_file"]).read_bytes()
        assert raw == http.raw[observation["page"]]
        assert observation["byte_count"] == len(raw) and observation["sha256"] == sha256(raw).hexdigest()
        assert observation["metadata"]["subname"] == DAY
        assert observation["requested_at"] < observation["response_completed_at"]
    assert receipt["source_available_at"] == receipt["pages"][-1]["response_completed_at"]
    assert receipt["source_available_at"] < receipt["completed_at"]
    assert receipt["automatic_retry"] is False and receipt["execution_admitted"] is False
    assert _read(folder / "receipt.json") == receipt


def test_assess_only_positive_same_day_ordinary_members_never_infers_non_ST_or_tradability(tmp_path):
    folder = tmp_path / "positive"
    receipt, _ = _collect(folder)
    after = datetime.fromisoformat(receipt["completed_at"]) + timedelta(seconds=1)
    view = risk.load(folder, as_of=after)
    assert view["full_report_complete"] is True
    assert view["current_ST_false_inference_admitted"] is False
    assert view["tradable_inference_admitted"] is False
    for symbol in ("000001.SZSE", "300001.SZSE"):
        value = risk.assess(folder, symbol, as_of=after)
        assert value["is_st"] is True and value["reported_risk_warning_member"] is True
        assert value["tradable"] is None
    for symbol in ("000002.SZSE", "600000.SSE", "200001.SZSE"):
        value = risk.assess(folder, symbol, as_of=after)
        assert value["is_st"] is None and value["reported_risk_warning_member"] is None
        assert value["tradable"] is None
    for unavailable_clock in (
            datetime.fromisoformat(receipt["source_available_at"]) - timedelta(microseconds=1),
            datetime.fromisoformat(receipt["source_available_at"]),  # Receipt not complete yet.
            after + timedelta(days=1)):
        unavailable = risk.load(folder, as_of=unavailable_clock)
        assert unavailable["status"] == "unavailable" and unavailable["ordinary_risk_symbols"] == []
        value = risk.assess(folder, "000001.SZSE", as_of=unavailable_clock)
        assert value["is_st"] is None and value["tradable"] is None


def test_missing_duplicate_changed_inventory_or_raw_are_rejected_and_first_failure_never_retries(tmp_path):
    source = tmp_path / "original"
    receipt, _ = _collect(source)
    after = datetime.fromisoformat(receipt["completed_at"]) + timedelta(seconds=1)

    def duplicate(payload):
        payload[0]["data"][0]["zqdm"] = "000001"

    def different_day(payload):
        payload[0]["metadata"]["subname"] = "2026-10-16"

    def changed_total(payload):
        payload[0]["metadata"]["recordcount"] = 4
        payload[0]["data"].append({"zqdm": "000003", "gsjc": "合成另一风险样本"})

    for name, mutate in (("duplicate", duplicate), ("changed_day", different_day), ("changed_total", changed_total)):
        altered = tmp_path / ("load_" + name)
        shutil.copytree(source, altered)
        _rewrite_raw(altered, 2, mutate)
        with pytest.raises(ValueError):
            risk.load(altered, as_of=after)
        bad_payloads = _payloads()
        mutate(bad_payloads[2])
        failed, http = _collect(tmp_path / ("collect_" + name), bad_payloads)
        assert failed["status"] == "failed_single_pass_no_retry"
        assert failed["full_report_complete"] is False and failed["source_available_at"] is None
        assert http.pages == [1, 2] and len(http.pages) == len(set(http.pages))
        assert risk.load(tmp_path / ("collect_" + name), as_of=after)["status"] == "unavailable"

    missing = tmp_path / "load_missing"
    shutil.copytree(source, missing)
    (missing / "page_002.raw.json").unlink()
    with pytest.raises((OSError, ValueError)):
        risk.load(missing, as_of=after)
    tampered = tmp_path / "load_tampered"
    shutil.copytree(source, tampered)
    path = tampered / "page_001.raw.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="bytes changed"):
        risk.load(tampered, as_of=after)

    incomplete_payloads = _payloads()
    incomplete_payloads[2][0]["data"] = []
    incomplete, missing_http = _collect(tmp_path / "collect_missing", incomplete_payloads)
    assert incomplete["status"] == "failed_single_pass_no_retry" and missing_http.pages == [1, 2]
    assert (tmp_path / "collect_missing" / "page_002.raw.json").read_bytes() == missing_http.raw[2]
    first_failure, first_http = _collect(tmp_path / "first_http_failure", fail_first=True)
    assert first_failure["status"] == "failed_single_pass_no_retry"
    assert first_http.pages == first_http.closed == [1]
    assert (tmp_path / "first_http_failure" / "page_001.raw.json").read_bytes() == first_http.raw[1]
    assert not (tmp_path / "first_http_failure" / "page_002.raw.json").exists()
