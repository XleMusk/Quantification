import pytest

from trading.research.fetch_receipts import make_receipt, request_hash, validate_receipt


REQUEST = {"issuer": "600000", "periods": ["2025Q4"], "cutoff": "2026-04-30T00:00:00+08:00"}


def page(partition, rows):
    return {"partition": partition, "rows": rows, "raw_blob_sha256": "a" * 64}


def test_complete_and_complete_empty_have_distinct_semantics():
    complete = make_receipt(REQUEST, source="akshare", upstream_source="eastmoney",
                            adapter_version="v1", requested_scope={"period": "2025Q4"},
                            fetched_at="2026-10-23T10:00:00+08:00", raw_content_reference=None,
                            schema_fingerprint="schema-1", expected_partitions=["p1"],
                            completed_partitions=["p1"], page_receipts=[page("p1", 1)],
                            completion_state="COMPLETE")
    empty = make_receipt(REQUEST, source="akshare", upstream_source="eastmoney",
                         adapter_version="v1", requested_scope={"period": "2025Q4"},
                         fetched_at="2026-10-23T10:00:00+08:00", raw_content_reference=None,
                         schema_fingerprint="schema-1", expected_partitions=["p1"],
                         completed_partitions=["p1"], page_receipts=[page("p1", 0)],
                         completion_state="COMPLETE_EMPTY")
    assert validate_receipt(complete, REQUEST)["rows"] == 1
    assert validate_receipt(empty, REQUEST)["completion_state"] == "COMPLETE_EMPTY"
    assert request_hash({"cutoff": REQUEST["cutoff"], "periods": REQUEST["periods"], "issuer": REQUEST["issuer"]}) == request_hash(REQUEST)


@pytest.mark.parametrize("state", ["PARTIAL", "UNKNOWN_COVERAGE"])
def test_incomplete_coverage_cannot_be_marked_complete(state):
    receipt = make_receipt(REQUEST, source="x", upstream_source="x", adapter_version="v1",
                           requested_scope={}, fetched_at="now", raw_content_reference=None,
                           schema_fingerprint="s", expected_partitions=["p1", "p2"],
                           completed_partitions=["p1"], page_receipts=[page("p1", 3)],
                           completion_state=state)
    assert validate_receipt(receipt)["coverage_complete"] is False
    with pytest.raises(ValueError, match="every expected partition"):
        validate_receipt({**receipt, "completion_state": "COMPLETE"})


def test_partial_nonempty_frame_does_not_hide_schema_or_retry_error():
    with pytest.raises(ValueError, match="requires error_code"):
        make_receipt(REQUEST, source="x", upstream_source="x", adapter_version="v1",
                     requested_scope={}, fetched_at="now", raw_content_reference=None,
                     schema_fingerprint="s", expected_partitions=["p1"], completed_partitions=[],
                     page_receipts=[], completion_state="SCHEMA_CHANGED")
    retryable = make_receipt(REQUEST, source="x", upstream_source="x", adapter_version="v1",
                             requested_scope={}, fetched_at="now", raw_content_reference=None,
                             schema_fingerprint="s", expected_partitions=["p1"], completed_partitions=[],
                             page_receipts=[], completion_state="RETRYABLE_ERROR", error_code="TIMEOUT",
                             retryable=True)
    assert retryable["retryable"] is True
