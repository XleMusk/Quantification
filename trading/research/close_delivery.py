"""Publish one saved post-close research delivery through the Google workflow.

The command consumes a completed post-close D0/D1 receipt and the explicitly
selected strategy evidence.  It rebuilds the saved Data-to-Cash/Position
lineage, adoption rollup and candidate library, then emits one internal
release manifest and one delivery receipt.  It never fetches a provider,
opens a broker/THS client, retries an unknown task, or mutates an account.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .candidate_library_binding import bind_library
from .internal_release_candidate import build_release
from .paper_pipeline_lineage import build_lineage
from .research_adoption_rollup import write_outputs


SCHEMA = "quantification_close_delivery_v1"
STATUS = "COMPLETED_RESEARCH_ONLY_CLOSE_DELIVERY"
FAILED_STATUS = "FAILED_RESEARCH_ONLY_CLOSE_DELIVERY"


def _read(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"delivery input must be a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ref(path: str | Path) -> dict[str, str]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"delivery input is missing: {resolved}")
    return {"path": str(resolved), "sha256": _sha256(resolved)}


def _closed(name: str, payload: Mapping[str, Any]) -> None:
    if payload.get("execution_admitted") is not False:
        raise ValueError(f"{name}.execution_admitted must be false")
    for key in ("broker_interacted", "ths_interacted", "persistent_ledger_mutated"):
        if key in payload and payload.get(key) is not False:
            raise ValueError(f"{name}.{key} must be false")


def _fingerprint(inputs: Mapping[str, Path]) -> tuple[str, dict[str, Any]]:
    refs = {name: _ref(path) for name, path in sorted(inputs.items())}
    body = {"inputs": refs, "schema": SCHEMA}
    digest = sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")).hexdigest()
    return digest, body


def _lineage_post_close(path: Path, payload: Mapping[str, Any]) -> Path:
    """Resolve the inner D0/D1 receipt consumed by the lineage builder."""

    if payload.get("status") == "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1":
        return path
    if payload.get("status") != "COMPLETED_RESEARCH_ONLY_POST_CLOSE_D0_D1":
        raise ValueError("post-close input is not a completed research-only delivery")
    d0 = payload.get("d0_d1")
    receipt_path = d0.get("receipt_path") if isinstance(d0, Mapping) else None
    if isinstance(receipt_path, str):
        candidate = Path(receipt_path).expanduser().resolve()
        if candidate.is_file():
            inner = _read(candidate)
            if inner.get("status") == "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1":
                return candidate
    artifact_dir = (payload.get("artifacts") or {}).get("d0_d1")
    if isinstance(artifact_dir, str):
        candidate = Path(artifact_dir).expanduser().resolve() / "contract_receipt.json"
        if candidate.is_file() and _read(candidate).get("status") == "COMPLETED_RESEARCH_ONLY_NEXT_CLOSE_D0_D1":
            return candidate
    raise ValueError("completed post-close receipt has no resolvable inner D0/D1 receipt")


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def run_delivery(
    *,
    post_close: str | Path,
    fixed: str | Path,
    joint: str | Path,
    ths_readback: str | Path,
    candidate_adoption: str | Path,
    mature_factor_comparison: str | Path,
    shortline_adoption: str | Path,
    d0_d1_receipt: str | Path,
    current_window_comparison: str | Path,
    full_panel_comparison: str | Path,
    candidate_same_day_gate: str | Path,
    full_panel_cost_pressure: str | Path,
    catalog: str | Path,
    output_dir: str | Path,
    release_dir: str | Path,
    release_version: str,
    retained_joint_comparison: str | Path | None = None,
    value_size_joint_paper: str | Path | None = None,
    leader_joint_paper: str | Path | None = None,
) -> dict[str, Any]:
    """Run the complete saved-evidence delivery once, with idempotent reuse."""

    required = {
        "post_close": Path(post_close), "fixed": Path(fixed),
        "joint": Path(joint), "ths_readback": Path(ths_readback),
        "candidate_adoption": Path(candidate_adoption),
        "mature_factor_comparison": Path(mature_factor_comparison),
        "shortline_adoption": Path(shortline_adoption),
        "d0_d1_receipt": Path(d0_d1_receipt),
        "current_window_comparison": Path(current_window_comparison),
        "full_panel_comparison": Path(full_panel_comparison),
        "candidate_same_day_gate": Path(candidate_same_day_gate),
        "full_panel_cost_pressure": Path(full_panel_cost_pressure),
        "catalog": Path(catalog),
    }
    optional = {
        name: Path(value) for name, value in {
            "retained_joint_comparison": retained_joint_comparison,
            "value_size_joint_paper": value_size_joint_paper,
            "leader_joint_paper": leader_joint_paper,
        }.items() if value is not None
    }
    post = _read(required["post_close"])
    _closed("post_close", post)
    lineage_input = _lineage_post_close(required["post_close"], post)
    inputs = {**required, **optional, "lineage_post_close": lineage_input}
    fingerprint, fingerprint_inputs = _fingerprint(inputs)
    output = Path(output_dir).expanduser().resolve()
    receipt_path = output / "close_delivery_receipt.json"
    if receipt_path.is_file():
        existing = _read(receipt_path)
        if existing.get("input_fingerprint") != fingerprint:
            raise ValueError("close delivery output exists for different inputs; use a new output directory")
        _closed("saved close delivery", existing)
        return {**existing, "reused_existing_run": True}
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("close delivery output exists without a receipt; preserve it first")
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    stage_trace: list[dict[str, Any]] = []
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "RUNNING_RESEARCH_ONLY_CLOSE_DELIVERY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution_admitted": False,
        "broker_interacted": False,
        "ths_interacted": False,
        "persistent_ledger_mutated": False,
        "input_fingerprint": fingerprint,
        "fingerprint_inputs": fingerprint_inputs,
        "output_dir": str(output),
        "release_version": release_version,
    }
    try:
        lineage_path = output / "paper_pipeline_lineage.json"
        lineage = build_lineage(
            lineage_input, required["fixed"], required["joint"],
            required["ths_readback"], lineage_path,
        )
        _closed("lineage", lineage)
        stage_trace.append({"stage": "paper_pipeline_lineage", "status": lineage["status"],
                            "path": str(lineage_path)})

        rollup_path = output / "research_adoption_rollup.json"
        rollup_report = output / "research_adoption_rollup.md"
        rollup_path, rollup_report = write_outputs(
            candidate_path=required["candidate_adoption"],
            factor_path=required["mature_factor_comparison"],
            shortline_path=required["shortline_adoption"],
            retained_joint_path=optional.get("retained_joint_comparison"),
            d0_d1_path=required["d0_d1_receipt"],
            current_window_path=required["current_window_comparison"],
            full_panel_path=required["full_panel_comparison"],
            same_day_gate_path=required["candidate_same_day_gate"],
            value_size_path=optional.get("value_size_joint_paper"),
            leader_path=optional.get("leader_joint_paper"),
            cost_pressure_path=required["full_panel_cost_pressure"],
            output=rollup_path,
            report=rollup_report,
        )
        rollup = _read(rollup_path)
        _closed("adoption_rollup", rollup)
        stage_trace.append({"stage": "research_adoption_rollup", "status": rollup["status"],
                            "path": str(rollup_path)})

        library_path = output / "candidate_library.json"
        library = bind_library(
            catalog_path=required["catalog"], rollup_path=rollup_path,
            panel_path=required["full_panel_comparison"],
            ths_readback_path=required["ths_readback"], lineage_path=lineage_path,
            output_path=library_path, cost_pressure_path=required["full_panel_cost_pressure"],
        )
        _closed("candidate_library", library)
        stage_trace.append({"stage": "candidate_library", "status": library["status"],
                            "path": str(library_path)})

        release = build_release(
            Path(__file__).resolve().parents[2], Path(release_dir),
            version=release_version,
            paper_evidence_files=[
                str(required["fixed"].resolve()), str(required["joint"].resolve()),
                str(required["ths_readback"].resolve()), str(lineage_path),
                str(rollup_path), str(library_path),
            ],
        )
        if release.get("execution_admitted") is not False or release.get("live_execution_ready") is not False:
            raise ValueError("internal release crossed the execution boundary")
        stage_trace.append({"stage": "internal_release", "status": release["status"],
                            "path": release["manifest_path"]})
        result = {
            **base,
            "status": STATUS,
            "stage_trace": stage_trace,
            "artifacts": {
                "lineage_post_close_input": str(lineage_input),
                "lineage": str(lineage_path),
                "rollup": str(rollup_path),
                "rollup_report": str(rollup_report),
                "candidate_library": str(library_path),
                "release_manifest": release["manifest_path"],
            },
            "execution_boundary": {"execution_admitted": False,
                                    "candidate_panel_only": True,
                                    "live_execution_ready": False},
            "timing": {"elapsed_seconds": round(max(0.0, time.monotonic() - started), 3),
                       "finished_at": datetime.now(timezone.utc).isoformat()},
        }
        _write(receipt_path, result)
        return result
    except Exception as error:
        failure = {
            **base,
            "status": FAILED_STATUS,
            "stage_trace": stage_trace,
            "error_type": type(error).__name__,
            "error": str(error),
            "failure_evidence": {"receipt_path": str(receipt_path), "output_dir": str(output)},
            "timing": {"elapsed_seconds": round(max(0.0, time.monotonic() - started), 3),
                       "finished_at": datetime.now(timezone.utc).isoformat()},
        }
        _write(receipt_path, failure)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--post-close", type=Path, required=True)
    parser.add_argument("--fixed", type=Path, required=True)
    parser.add_argument("--joint", type=Path, required=True)
    parser.add_argument("--ths-readback", type=Path, required=True)
    parser.add_argument("--candidate-adoption", type=Path, required=True)
    parser.add_argument("--mature-factor-comparison", type=Path, required=True)
    parser.add_argument("--shortline-adoption", type=Path, required=True)
    parser.add_argument("--d0-d1-receipt", type=Path, required=True)
    parser.add_argument("--current-window-comparison", type=Path, required=True)
    parser.add_argument("--full-panel-comparison", type=Path, required=True)
    parser.add_argument("--candidate-same-day-gate", type=Path, required=True)
    parser.add_argument("--full-panel-cost-pressure", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--retained-joint-comparison", type=Path)
    parser.add_argument("--value-size-joint-paper", type=Path)
    parser.add_argument("--leader-joint-paper", type=Path)
    args = parser.parse_args(argv)
    result = run_delivery(**vars(args))
    print(json.dumps({"status": result["status"],
                      "execution_admitted": result["execution_admitted"],
                      "reused_existing_run": result.get("reused_existing_run", False),
                      "receipt": str(Path(args.output_dir).resolve() / "close_delivery_receipt.json")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

