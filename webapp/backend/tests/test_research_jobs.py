from pathlib import Path

import pytest

from webapp.backend.app import research_jobs


def _dataset(**overrides):
    value = {
        "id": "a" * 32,
        "start": "2024-01-01",
        "end": "2024-12-31",
        "symbols": [
            "000001.SZSE", "000002.SZSE", "000003.SZSE", "000004.SZSE", "600000.SSE",
        ],
        "quality": {"invalid_rows": 0},
    }
    value.update(overrides)
    return value


def test_submit_use_case_validates_and_registers_one_request(monkeypatch):
    captured = {}
    monkeypatch.setattr(research_jobs.store, "dataset", lambda identifier: _dataset(id=identifier))
    def create_job(dataset_id, config, request_key):
        captured.update(dataset_id=dataset_id, config=config, request_key=request_key)
        return {"id": "job-1", "status": "queued"}
    monkeypatch.setattr(research_jobs.store, "create_job", create_job)

    result = research_jobs.submit(
        research_jobs.SubmitRequest("a" * 32, "2024-02-01", "2024-03-01", 5),
        "request-1",
    )

    assert result["status"] == "queued"
    assert captured["dataset_id"] == "a" * 32
    assert captured["request_key"] == "request-1"
    assert captured["config"]["holding_sessions"] == 5


@pytest.mark.parametrize("dataset, message", [
    (_dataset(quality={"invalid_rows": 1}), "数据质量阻断"),
    (_dataset(symbols=["000001.SZSE"]), "证券数量不足"),
])
def test_submit_use_case_keeps_data_failures_explicit(monkeypatch, dataset, message):
    monkeypatch.setattr(research_jobs.store, "dataset", lambda identifier: dataset)
    with pytest.raises(ValueError, match=message):
        research_jobs.submit(
            research_jobs.SubmitRequest("a" * 32, "2024-02-01", "2024-03-01", 5),
            "request-2",
        )


def test_result_use_cases_read_only_and_reject_missing_files(monkeypatch, tmp_path):
    job_id = "b" * 32
    monkeypatch.setattr(research_jobs.store, "job", lambda identifier: {"id": identifier, "status": "completed"})
    result_path = tmp_path / "results" / job_id
    result_path.mkdir(parents=True)
    (result_path / "result.json").write_text('{"science_evidence": false}', encoding="utf-8")
    (result_path / "REPORT.md").write_text("report", encoding="utf-8")

    assert research_jobs.read_result(job_id, tmp_path) == {"science_evidence": False}
    assert research_jobs.resolve_result_file(job_id, "REPORT.md", tmp_path).name == "REPORT.md"
    with pytest.raises(research_jobs.ResultUnavailable):
        research_jobs.resolve_result_file(job_id, "missing.csv", tmp_path)
    (result_path / "result.json").unlink()
    with pytest.raises(research_jobs.ResultUnavailable):
        research_jobs.read_result(job_id, tmp_path)
