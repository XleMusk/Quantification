"""Bind a published native shortline preopen plan to panel-replay candidates.

The native scorer deliberately publishes raw probabilities with all candidate
flags disabled.  This adapter consumes a separately published preopen plan and
creates a new, immutable handoff containing only the plan's selected rows.  It
never edits the raw score files, recomputes ranks, reads labels, creates an
account, or sends anything to a broker/THS.
"""

from __future__ import annotations

import argparse
import csv
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .shortline_native_preopen import MODEL_KINDS, load_preopen_plan


SCHEMA = "native_shortline_candidate_handoff_v1"
STATUS = "COMPLETED_RESEARCH_ONLY_CANDIDATE_HANDOFF"
REQUIRED_COLUMNS = {
    "event_id", "symbol", "date", "stage", "model_hit5_probability",
    "paper_candidate_only", "candidate_selected",
}


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        missing = sorted(REQUIRED_COLUMNS - set(columns))
        if missing:
            raise ValueError(f"ranked preview missing columns: {missing}")
        return columns, list(reader)


def _bool(value: Any, *, field: str) -> bool:
    value = str(value).strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    raise ValueError(f"{field} must be an explicit boolean: {value!r}")


def _selected_ids(decision: Mapping[str, Any]) -> set[str]:
    rows = decision.get("selected_events")
    if not isinstance(rows, list):
        raise ValueError("preopen decision selected_events must be a list")
    selected: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("event_id"), str):
            raise ValueError("selected event identity is malformed")
        if row["event_id"] in selected:
            raise ValueError("selected event identity is duplicated")
        selected.add(row["event_id"])
    expected = decision.get("selected_count")
    if type(expected) is not int or expected != len(selected):
        raise ValueError("selected_count does not match selected event identities")
    return selected


def _model_handoff(plan_dir: Path, model_kind: str, decision: Mapping[str, Any],
                   output_dir: Path, source_day: str) -> dict[str, Any]:
    preview = plan_dir / f"{model_kind}_ranked_preview.csv"
    if not preview.is_file():
        raise ValueError(f"missing ranked preview: {preview}")
    columns, rows = _read_csv(preview)
    if any(_bool(row["paper_candidate_only"], field="paper_candidate_only") for row in rows):
        raise ValueError("raw ranked preview already contains a candidate flag")
    selected_ids = _selected_ids(decision)
    by_id: dict[str, dict[str, str]] = {}
    for row in rows:
        event_id = str(row.get("event_id") or "")
        if not event_id or event_id in by_id:
            raise ValueError("ranked preview event identities must be unique")
        if _bool(row["candidate_selected"], field="candidate_selected") != (event_id in selected_ids):
            raise ValueError("decision selected identities differ from ranked preview")
        if str(row.get("date")) != source_day:
            raise ValueError("candidate handoff must contain one exact source day")
        if str(row.get("stage")) not in {"1", "2", "3"}:
            raise ValueError("candidate stage must be one of 1, 2, 3")
        probability = float(row["model_hit5_probability"])
        if not 0.0 <= probability <= 1.0:
            raise ValueError("model_hit5_probability must be within [0, 1]")
        by_id[event_id] = row
    if not selected_ids <= set(by_id):
        raise ValueError("decision selected identities are not present in ranked preview")
    chosen = [by_id[event_id] for event_id in sorted(selected_ids)]
    symbols = [str(row["symbol"]).upper() for row in chosen]
    if len(symbols) != len(set(symbols)):
        raise ValueError("selected candidate symbols must be unique per model")

    output_columns = [*columns, "model_kind", "source_plan_day"]
    output_file = output_dir / f"{model_kind}_candidates.csv"
    with output_file.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_columns)
        writer.writeheader()
        for row in chosen:
            copied = {name: row.get(name, "") for name in columns}
            copied["paper_candidate_only"] = "True"
            copied["candidate_selected"] = "True"
            copied["model_kind"] = model_kind
            copied["source_plan_day"] = source_day
            writer.writerow(copied)
    return {
        "model_kind": model_kind,
        "raw_preview": {"path": str(preview.resolve()), "sha256": _sha256(preview)},
        "candidate_file": {"path": str(output_file.resolve()), "sha256": _sha256(output_file)},
        "raw_rows": len(rows),
        "selected_count": len(chosen),
        "selected_event_ids": sorted(selected_ids),
        "raw_preview_modified": False,
        "candidate_panel_input": bool(chosen),
    }


def build_candidate_handoff(plan: str | Path, out: str | Path) -> dict[str, Any]:
    plan_path = Path(plan).resolve()
    out_dir = Path(out).resolve()
    if not plan_path.is_file() or plan_path.name != "plan.json":
        raise ValueError("plan must be a published preopen plan.json")
    if out_dir.exists():
        raise FileExistsError("candidate handoff requires a new output directory")
    loaded = load_preopen_plan(plan_path)
    record = loaded["record"]
    if record.get("execution_admitted") is not False or record.get("paper_allowed") is True:
        raise ValueError("preopen plan crossed the execution boundary")
    source_day, execution_day = loaded["source_day"], loaded["execution_day"]
    decisions = loaded["decisions"]
    if set(decisions) != set(MODEL_KINDS):
        raise ValueError("published plan must contain both frozen model decisions")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{out_dir.name}.", dir=out_dir.parent) as temporary:
        stage_dir = Path(temporary)
        models = [_model_handoff(plan_path.parent, kind, decisions[kind], stage_dir, source_day)
                  for kind in MODEL_KINDS]
        receipt = {
            "schema": SCHEMA,
            "status": STATUS,
            "source_plan": {"path": str(plan_path), "sha256": _sha256(plan_path)},
            "policy_id": record.get("policy_id"),
            "source_day": source_day,
            "execution_day": execution_day,
            "models": models,
            "candidate_files": [row["candidate_file"] for row in models],
            "raw_score_payloads_modified": False,
            "source_plan_modified": False,
            "research_only": True,
            "candidate_panel_only": True,
            "target_generation_allowed": False,
            "paper_orders_generated": False,
            "execution_admitted": False,
            "broker_interacted": False,
            "ths_interacted": False,
            "limitations": [
                "Candidate flags are written only to this new handoff; raw score files remain unchanged.",
                "The handoff is suitable for research panel replay only and does not prove PIT membership, company-action safety or strategy validation.",
            ],
        }
        receipt["receipt"] = {"path": str(out_dir / "receipt.json"), "sha256": None}
        stage_dir.rename(out_dir)
        for model in receipt["models"]:
            filename = Path(model["candidate_file"]["path"]).name
            model["candidate_file"]["path"] = str(out_dir / filename)
        receipt["candidate_files"] = [row["candidate_file"] for row in receipt["models"]]
        receipt["receipt"]["path"] = str(out_dir / "receipt.json")
        receipt_path = out_dir / "receipt.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = build_candidate_handoff(args.plan, args.out)
    print(json.dumps({"status": receipt["status"], "source_day": receipt["source_day"],
                      "execution_day": receipt["execution_day"],
                      "selected_counts": {row["model_kind"]: row["selected_count"] for row in receipt["models"]},
                      "receipt": str((Path(args.out).resolve() / "receipt.json")),
                      "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

