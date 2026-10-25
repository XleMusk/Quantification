"""Bind canonical corporate-action events to dated StockDB score rows.

The event view carries operation dates, issuer publication dates and source
hashes, but it is not a historical as-of database.  This module therefore
produces an explicit action context diagnostic and refuses to claim PIT or
action application.  It is intended as the input contract for a later
corporate-action-aware replay.
"""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

import pandas as pd

from ..settings import DATA_ROOT, ROOT
from .stockdb_eligibility_view import load_eligibility_view
from .publication_event_contract import evaluate_publication_evidence, missing_publication_evidence


REQUIRED_ACTION_TERMS = (
    "dividRegistDate", "dividOperateDate", "dividCashPsBeforeTax",
    "dividStocksPs", "dividReserveToStockPs",
)


def _read_json(path: Path) -> dict:
    path = Path(path).resolve()
    if path.is_dir():
        path = path / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _resolve_action_view(value: Path) -> tuple[Path, dict, Path]:
    root = Path(value).resolve()
    result_path = root / "result.json"
    record = _read_json(result_path)
    if record.get("status") != "research_action_events_projected":
        raise ValueError("Projected research action event view required")
    if record.get("execution_admitted") is not False:
        raise ValueError("Action view must remain execution-unadmitted")
    if record.get("source_status") != "source_actions_complete":
        raise ValueError("Complete action source view required")
    files = record.get("files") or {}
    relative = "canonical_actions.parquet"
    action_path = root / relative
    expected = files.get(relative)
    if not action_path.is_file() or not expected or sha256(action_path.read_bytes()).hexdigest() != expected:
        raise ValueError("Pinned canonical action parquet is missing or changed")
    return root, record, action_path


def load_action_events(action_view: Path) -> tuple[pd.DataFrame, dict]:
    root, record, path = _resolve_action_view(action_view)
    events = pd.read_parquet(path)
    required = {"symbol", "dividOperateDate", "dividPlanAnnounceDate", *REQUIRED_ACTION_TERMS}
    missing = sorted(required - set(events.columns))
    if missing:
        raise ValueError(f"Canonical action view missing fields: {missing}")
    events = events.copy()
    events["symbol"] = events["symbol"].astype(str).str.upper()
    for col in ["dividOperateDate", "dividPlanAnnounceDate", "dividRegistDate"]:
        events[col] = events[col].fillna("").astype(str).str.strip()
    if events.duplicated(["symbol", "dividOperateDate"]).any():
        raise ValueError("Canonical action view has duplicate symbol/operate-date events")
    return events, {
        "action_view": str(root),
        "action_result": record,
        "canonical_events": int(len(events)),
        "event_start": record.get("start"),
        "event_end": record.get("end"),
        "canonical_sha256": sha256(path.read_bytes()).hexdigest(),
    }


def _terms_complete(row: pd.Series) -> bool:
    return all(str(row.get("date" if field == "dividOperateDate" else field, "")).strip() != ""
               for field in REQUIRED_ACTION_TERMS)


def _publication_context(score_rows: pd.DataFrame,
                         publication_evidence: pd.DataFrame | None) -> pd.DataFrame:
    """Evaluate optional per-event publication receipts at each score date."""

    keys = score_rows[["date", "symbol"]].copy()
    keys["date"] = pd.to_datetime(keys["date"], errors="raise").dt.strftime("%Y-%m-%d")
    keys["symbol"] = keys["symbol"].astype(str).str.upper()
    if publication_evidence is None:
        status = missing_publication_evidence()
        for key, value in status.items():
            keys[f"publication_{key}" if key == "pit_asof_safe" else key] = [value] * len(keys)
        return keys
    evidence = publication_evidence.copy()
    required = {"date", "symbol"}
    if not required <= set(evidence.columns):
        raise ValueError("Publication evidence requires date and symbol")
    evidence["date"] = pd.to_datetime(evidence["date"], errors="raise").dt.strftime("%Y-%m-%d")
    evidence["symbol"] = evidence["symbol"].astype(str).str.upper()
    if evidence.duplicated(["date", "symbol"]).any():
        raise ValueError("Publication evidence must have unique date/symbol identity")
    evaluated = []
    for row in evidence.to_dict("records"):
        result = evaluate_publication_evidence(row, row["date"])
        evaluated.append({"date": row["date"], "symbol": row["symbol"], **result})
    evaluated = pd.DataFrame(evaluated)
    if evaluated.empty:
        evaluated = pd.DataFrame(columns=["date", "symbol", "publication_evidence_present",
                                          "publication_event_proven", "retrieved_asof_proven",
                                          "revision_identity_proven", "source_provenance_proven",
                                          "pit_asof_safe", "publication_evidence_reasons"])
    evaluated = evaluated.rename(columns={"pit_asof_safe": "publication_pit_asof_safe"})
    return keys.merge(evaluated, on=["date", "symbol"], how="left", validate="one_to_one")


def build_context(score_rows: pd.DataFrame, events: pd.DataFrame,
                  publication_evidence: pd.DataFrame | None = None) -> pd.DataFrame:
    """Join exact operate-date events to score rows without filling unknowns."""

    required = {"date", "symbol", "score"}
    if not required <= set(score_rows.columns):
        raise ValueError("Score rows missing date/symbol/score")
    rows = score_rows[["date", "symbol", "score"]].copy()
    rows["date"] = pd.to_datetime(rows["date"], errors="raise").dt.strftime("%Y-%m-%d")
    rows["symbol"] = rows["symbol"].astype(str).str.upper()
    if rows.duplicated(["date", "symbol"]).any():
        raise ValueError("Score rows must have unique date/symbol identity")
    action_columns = ["symbol", "dividPlanAnnounceDate", *REQUIRED_ACTION_TERMS]
    action_columns = list(dict.fromkeys(action_columns))
    action = events[action_columns].copy()
    action = action.rename(columns={"dividOperateDate": "date"})
    action = action.loc[action["date"].ne("")].copy()
    action["action_event_present"] = True
    action["publication_known_by_date"] = [
        bool(pub and pub <= day) for pub, day in zip(action["dividPlanAnnounceDate"], action["date"])
    ]
    action["terms_complete"] = action.apply(_terms_complete, axis=1)
    action = action[["date", "symbol", "action_event_present", "publication_known_by_date", "terms_complete"]]
    joined = rows.merge(action, on=["date", "symbol"], how="left", validate="one_to_one")
    for field in ["action_event_present", "publication_known_by_date", "terms_complete"]:
        joined[field] = joined[field].astype("boolean").fillna(False).astype(bool)
    joined["action_context_ready_without_pit"] = (~joined["action_event_present"]
                                                   | (joined["publication_known_by_date"] & joined["terms_complete"]))
    publication = _publication_context(rows, publication_evidence)
    joined = joined.merge(publication, on=["date", "symbol"], how="left", validate="one_to_one")
    for field in ["publication_evidence_present", "publication_event_proven", "retrieved_asof_proven",
                  "revision_identity_proven", "source_provenance_proven", "publication_pit_asof_safe"]:
        joined[field] = joined[field].fillna(False).astype(bool)
    joined["publication_evidence_reasons"] = joined["publication_evidence_reasons"].apply(
        lambda value: value if isinstance(value, list) else ["publication_evidence_missing"])
    # A same-day join is useful for diagnostics, but only a complete external
    # publication receipt can make an action event PIT-safe.  No adjustment is
    # applied by this view.
    joined["pit_asof_safe"] = joined["action_event_present"] & joined["publication_pit_asof_safe"]
    joined["company_action_applied"] = False
    return joined


def _read_publication_evidence(path: Path) -> pd.DataFrame:
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("Publication evidence JSON must contain a list or rows field")
        return pd.DataFrame(rows)
    raise ValueError("Publication evidence must be parquet, csv or json")


def build_view(eligibility_view: Path, action_view: Path, *, output: Path | None = None,
               publication_evidence: Path | None = None) -> tuple[Path, dict]:
    eligibility_run = _read_json(eligibility_view)
    rows = load_eligibility_view(eligibility_view)
    events, action_identity = load_action_events(action_view)
    publication = _read_publication_evidence(publication_evidence) if publication_evidence else None
    context = build_context(rows, events, publication)
    identifier = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y%m%d_%H%M%S")
    out = Path(output).resolve() if output else ROOT / "outputs" / "stockdb_action_context_views" / identifier
    artifacts = DATA_ROOT / "research" / "stockdb_action_context_views" / identifier
    out.mkdir(parents=True, exist_ok=False)
    artifacts.mkdir(parents=True, exist_ok=False)
    context_path = artifacts / "dated_action_context.parquet"
    context.to_parquet(context_path, index=False)
    result = {
        "status": "completed_stockdb_dated_action_context_view",
        "context_path": str(context_path),
        "score_rows": int(len(context)),
        "score_dates": int(context["date"].nunique()),
        "symbols": int(context["symbol"].nunique()),
        "action_rows_on_score_date": int(context["action_event_present"].sum()),
        "publication_known_rows": int(context["publication_known_by_date"].sum()),
        "terms_complete_rows": int(context["terms_complete"].sum()),
        "context_ready_rows": int(context["action_context_ready_without_pit"].sum()),
        "pit_asof_safe_rows": int(context["pit_asof_safe"].sum()),
        "company_action_applied_rows": 0,
        "action_events": action_identity["canonical_events"],
    }
    record = {
        "schema": "a_share_stockdb_dated_action_context_view_v1",
        "status": result["status"],
        "eligibility_view": str(Path(eligibility_view).resolve()),
        "eligibility_result": eligibility_run,
        "action_identity": action_identity,
        "publication_evidence": str(Path(publication_evidence).resolve()) if publication_evidence else None,
        "result": result,
        "pit_verified": False,
        "revision_safe": False,
        "company_actions_applied": False,
        "execution_admitted": False,
        "strategy_validated": False,
        "limitations": [
            "Canonical operate-date and issuer plan-announcement fields are bound to score rows where exact identity matches.",
            "The action view is not an as-of revision history; pit_asof_safe remains false for every row.",
            "No cash, share, price or position adjustment is applied by this context artifact.",
        ],
    }
    (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "REPORT.md").write_text("\n".join([
        "# StockDB dated corporate-action context", "",
        f"Score rows: `{result['score_rows']}`; same-date action rows: `{result['action_rows_on_score_date']}`; canonical events: `{result['action_events']}`.",
        f"PIT/as-of safe rows: `{result['pit_asof_safe_rows']}`; company-action application rows: `{result['company_action_applied_rows']}`.", "",
        *["- " + item for item in record["limitations"]], "",
    ]) + "\n", encoding="utf-8")
    receipt = {
        "schema": "stockdb_dated_action_context_receipt_v1",
        "status": "COMPLETED_ACTION_CONTEXT_RESEARCH_ONLY",
        "run": str(out / "run.json"),
        "context_path": str(context_path),
        "action_view": action_identity["action_view"],
        "metrics": result,
        "pit_verified": False,
        "revision_safe": False,
        "company_actions_applied": False,
        "execution_admitted": False,
    }
    receipt_path = ROOT / "outputs" / "pipeline_receipts" / "20261024_stockdb_dated_action_context_receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out / "run.json", record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eligibility-view", required=True, type=Path)
    parser.add_argument("--action-view", required=True, type=Path)
    parser.add_argument("--publication-evidence", type=Path,
                        help="optional per-event publication/retrieval/revision receipt")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path, record = build_view(args.eligibility_view, args.action_view, output=args.out,
                              publication_evidence=args.publication_evidence)
    print(json.dumps({"run": str(path), **record}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
