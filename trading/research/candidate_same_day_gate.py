"""Validate that candidate inputs belong to the requested close day.

This gate is deliberately before target projection.  It records stale or
missing candidate decision days instead of allowing an older terminal target
to appear in a newer D0/D1 package.  It never fetches data or mutates an
account.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "candidate_same_day_gate_v1"
READY = "READY_SAME_DAY_CANDIDATE_INPUT"
BLOCKED = "BLOCKED_STALE_CANDIDATE_INPUT"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _day(value: str, label: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must use canonical YYYY-MM-DD form")
    return value


def _candidates(payload: Any) -> list[Mapping[str, Any]]:
    values = payload if isinstance(payload, list) else payload.get("candidates") if isinstance(payload, Mapping) else None
    if not isinstance(values, list) or not all(isinstance(item, Mapping) for item in values):
        raise ValueError("Manifest must contain a candidates list")
    return list(values)


def _candidate_day(item: Mapping[str, Any], *, base: Path) -> tuple[str | None, str | None]:
    direct = item.get("target")
    if isinstance(direct, str) and direct:
        path = Path(direct).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Candidate target does not exist: {path}")
        target = _read(path)
        if not isinstance(target, Mapping):
            raise ValueError(f"Candidate target must be an object: {path}")
        day = target.get("decision_day")
        return (day if isinstance(day, str) else None), str(path)
    basis = item.get("decision_day")
    if isinstance(basis, str):
        return basis, None
    # Legacy terminal manifests may carry the only known day at manifest scope.
    return None, None


def evaluate_manifest(manifest: str | Path, decision_day: str) -> dict[str, Any]:
    manifest_path = Path(manifest).expanduser().resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Candidate manifest does not exist: {manifest_path}")
    requested = _day(decision_day, "decision_day")
    payload = _read(manifest_path)
    items = _candidates(payload)
    manifest_basis = payload.get("source_basis") if isinstance(payload, Mapping) else None
    basis_day = manifest_basis.get("decision_day") if isinstance(manifest_basis, Mapping) else None
    rows: list[dict[str, Any]] = []
    for item in items:
        candidate_id = str(item.get("candidate_id") or item.get("strategy_id") or "").strip()
        if not candidate_id:
            raise ValueError("Each candidate requires candidate_id or strategy_id")
        observed, target_path = _candidate_day(item, base=manifest_path.parent)
        observed = observed or (basis_day if isinstance(basis_day, str) else None)
        if observed is None:
            reason = "decision_day_missing"
            status = BLOCKED
        elif observed != requested:
            reason = "decision_day_mismatch"
            status = BLOCKED
        else:
            reason = None
            status = READY
        rows.append({
            "candidate_id": candidate_id,
            "decision_day": observed,
            "requested_decision_day": requested,
            "status": status,
            "reason": reason,
            "target_path": target_path,
        })
    overall = READY if rows and all(row["status"] == READY for row in rows) else BLOCKED
    return {
        "schema": SCHEMA,
        "status": overall,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest": {"path": str(manifest_path), "sha256": _sha256(manifest_path)},
        "requested_decision_day": requested,
        "candidate_count": len(rows),
        "candidates": rows,
        "execution_admitted": False,
        "paper_allowed": False,
        "promotion_allowed": False,
        "limitations": [
            "This gate validates candidate input freshness only; it does not create a signal or fetch market data.",
            "A blocked stale/missing candidate must not enter D0/D1 target projection.",
        ],
    }


def render_report(receipt: Mapping[str, Any]) -> str:
    lines = [
        "# Candidate same-day gate",
        "",
        f"Status: `{receipt['status']}`; requested decision day `{receipt['requested_decision_day']}`.",
        "",
        "| Candidate | Source decision day | Status | Reason |",
        "|---|---|---|---|",
    ]
    for row in receipt["candidates"]:
        lines.append(f"| {row['candidate_id']} | {row['decision_day'] or 'unknown'} | {row['status']} | {row['reason'] or ''} |")
    lines += ["", "No target or account was created by this gate; execution remains disabled.", ""]
    return "\n".join(lines)


def write_outputs(manifest: str | Path, decision_day: str, output: str | Path,
                  report: str | Path | None = None) -> dict[str, Any]:
    receipt = evaluate_manifest(manifest, decision_day)
    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = Path(report).expanduser().resolve() if report is not None else output_path.with_suffix(".md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(receipt), encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--decision-day", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    receipt = write_outputs(args.manifest, args.decision_day, args.output, args.report)
    print(json.dumps({"status": receipt["status"], "candidate_count": receipt["candidate_count"],
                      "execution_admitted": False, "output": str(Path(args.output).resolve())}, ensure_ascii=False))
    return 0 if receipt["status"] == READY else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BLOCKED", "READY", "SCHEMA", "evaluate_manifest", "main", "render_report", "write_outputs"]
