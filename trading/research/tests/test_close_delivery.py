from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.research import close_delivery


def _inputs(tmp_path: Path) -> dict[str, Path]:
    names = (
        "post_close", "fixed", "joint", "ths_readback", "candidate_adoption",
        "mature_factor_comparison", "shortline_adoption", "d0_d1_receipt",
        "current_window_comparison", "full_panel_comparison",
        "candidate_same_day_gate", "full_panel_cost_pressure", "catalog",
    )
    paths = {name: tmp_path / f"{name}.json" for name in names}
    for name, path in paths.items():
        payload = {"name": name, "execution_admitted": False}
        if name == "post_close":
            payload["status"] = "COMPLETED_RESEARCH_ONLY_POST_CLOSE_D0_D1"
            payload.update({"broker_interacted": False, "ths_interacted": False,
                            "persistent_ledger_mutated": False})
        path.write_text(json.dumps(payload), encoding="utf-8")
    inner = tmp_path / "inner_d0_d1.json"
    inner.write_text(json.dumps({
        "status": "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1",
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "persistent_ledger_mutated": False,
    }), encoding="utf-8")
    outer = json.loads(paths["post_close"].read_text(encoding="utf-8"))
    outer["d0_d1"] = {"receipt_path": str(inner)}
    paths["post_close"].write_text(json.dumps(outer), encoding="utf-8")
    return paths


def _kwargs(paths: dict[str, Path], tmp_path: Path) -> dict[str, object]:
    return {
        **paths,
        "output_dir": tmp_path / "delivery",
        "release_dir": tmp_path / "release",
        "release_version": "test-v1",
    }


def test_run_delivery_orders_lineage_rollup_library_and_release(tmp_path: Path, monkeypatch) -> None:
    paths = _inputs(tmp_path)
    calls: list[str] = []

    def fake_lineage(post, fixed, joint, ths, output):
        calls.append("lineage")
        output.write_text(json.dumps({"execution_admitted": False}), encoding="utf-8")
        return {"status": "VERIFIED_LOCAL_RESEARCH_ONLY_PAPER_LINEAGE",
                "execution_admitted": False, "broker_interacted": False,
                "ths_interacted": False, "persistent_ledger_mutated": False,
                "output": str(output)}

    def fake_rollup(**kwargs):
        calls.append("rollup")
        output = Path(kwargs["output"])
        report = Path(kwargs["report"])
        output.write_text(json.dumps({"status": "COMPLETED_RESEARCH_ONLY_ADOPTION_ROLLUP",
                                      "execution_admitted": False}), encoding="utf-8")
        report.write_text("rollup", encoding="utf-8")
        return output, report

    def fake_library(**kwargs):
        calls.append("library")
        output = Path(kwargs["output_path"])
        output.write_text(json.dumps({"status": "VERIFIED_LOCAL",
                                      "execution_admitted": False}), encoding="utf-8")
        return {"status": "VERIFIED_LOCAL", "execution_admitted": False,
                "output": str(output)}

    def fake_release(root, output_dir, *, version, paper_evidence_files):
        calls.append("release")
        manifest = Path(output_dir) / "release_manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"execution_admitted": False,
                                        "live_execution_ready": False}), encoding="utf-8")
        return {"status": "RESEARCH_ONLY_INTERNAL_RELEASE_CANDIDATE",
                "execution_admitted": False, "live_execution_ready": False,
                "manifest_path": str(manifest)}

    monkeypatch.setattr(close_delivery, "build_lineage", fake_lineage)
    monkeypatch.setattr(close_delivery, "write_outputs", fake_rollup)
    monkeypatch.setattr(close_delivery, "bind_library", fake_library)
    monkeypatch.setattr(close_delivery, "build_release", fake_release)

    result = close_delivery.run_delivery(**_kwargs(paths, tmp_path))
    assert result["status"] == close_delivery.STATUS
    assert result["execution_admitted"] is False
    assert calls == ["lineage", "rollup", "library", "release"]
    assert [item["stage"] for item in result["stage_trace"]] == [
        "paper_pipeline_lineage", "research_adoption_rollup", "candidate_library", "internal_release"
    ]

    reused = close_delivery.run_delivery(**_kwargs(paths, tmp_path))
    assert reused["reused_existing_run"] is True
    assert calls == ["lineage", "rollup", "library", "release"]


def test_run_delivery_preserves_failure_receipt(tmp_path: Path, monkeypatch) -> None:
    paths = _inputs(tmp_path)

    def fake_lineage(post, fixed, joint, ths, output):
        output.write_text(json.dumps({"execution_admitted": False}), encoding="utf-8")
        return {"status": "VERIFIED_LOCAL_RESEARCH_ONLY_PAPER_LINEAGE",
                "execution_admitted": False}

    def fail_rollup(**kwargs):
        raise RuntimeError("rollup fixture failure")

    monkeypatch.setattr(close_delivery, "build_lineage", fake_lineage)
    monkeypatch.setattr(close_delivery, "write_outputs", fail_rollup)

    with pytest.raises(RuntimeError, match="rollup fixture failure"):
        close_delivery.run_delivery(**_kwargs(paths, tmp_path))
    failure = json.loads((tmp_path / "delivery" / "close_delivery_receipt.json").read_text())
    assert failure["status"] == close_delivery.FAILED_STATUS
    assert failure["stage_trace"][-1]["stage"] == "paper_pipeline_lineage"
    assert failure["execution_admitted"] is False
