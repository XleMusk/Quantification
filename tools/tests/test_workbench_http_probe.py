from tools.workbench_http_probe import run


def test_workbench_http_probe_completes_idempotent_chain(tmp_path):
    receipt = run(tmp_path / "receipt.json", tmp_path / "report.md")

    assert receipt["status"] == "COMPLETED_LOCAL_RESEARCH_HTTP_CHAIN"
    assert receipt["assertions"] == {
        "same_job_id": True,
        "completed": True,
        "lifecycle_result_available": True,
        "downloads_available": True,
        "execution_closed": True,
    }
    assert receipt["execution_admitted"] is False
    assert receipt["broker_interacted"] is False
    assert receipt["ths_interacted"] is False
