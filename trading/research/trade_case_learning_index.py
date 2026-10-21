"""Read saved case learning without collecting data, recomputing prices or trading.

The legacy book and a completed incremental reading package remain separate
sources. Only the requested legacy fact/interpretation is opened on detail.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path


FLAGS = {"research_only": True, "candidate_panel_only": True,
         "strategy_validated": False, "selection_enabled": False,
         "execution_admitted": False, "orders_generated": False,
         "ledger_mutated": False}
BOUNDARIES = [
    "案例数是不同来源中的档案数，不是交易笔数、账户数或自然人数。",
    "AI逐案解读与人工深审分开；席位汇总不能归为某位游资的完整交割单。",
    "事件日、实际取得时间、解读时间和原始发表时间分别保留，缺失时钟不补造。",
    "已存价格、停牌、缺价与可成交证据分别保留；不从原始数值推算缺失收益。",
    "这里只读取已保存的研究资料，不生成当前交易信号、目标或委托。",
]


def _read(path: Path, expected_sha: str | None = None):
    payload = path.read_bytes()
    if expected_sha and sha256(payload).hexdigest() != expected_sha:
        raise ValueError(f"Saved case source changed: {path}")
    return json.loads(payload.decode("utf-8-sig"))


def _path(value, parent: Path):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (parent / path).resolve()


def _unique(rows, context):
    result = {}
    for row in rows:
        identity = row["case_id"]
        if not identity or identity in result:
            raise ValueError(f"Duplicate or empty case ID in {context}: {identity}")
        result[identity] = row
    return result


class TradeCaseLearningIndex:
    """An explicit, read-only union; never rebuild the old casebook or SQLite."""

    def __init__(self, legacy_manifest: str | Path, progress: str | Path):
        self.legacy_path = Path(legacy_manifest).resolve()
        self.progress_path = Path(progress).resolve()
        self.legacy = _read(self.legacy_path)
        self.progress = _read(self.progress_path)
        if self.legacy.get("schema") != "individual_case_learning_book_v1":
            raise ValueError("Expected saved legacy casebook manifest")
        if self.progress.get("schema") != "public_seat_source_case_reading_completion_v1":
            raise ValueError("Expected completed source-reading package")
        self.queue_path = _path(self.progress["queue_file"], self.progress_path.parent)
        self.queue = _read(self.queue_path, self.progress["queue_sha256"])
        self._legacy = _unique(self.legacy["cases"], "legacy manifest")
        queue = _unique(self.queue["cases"], "incremental queue")
        self._new = {}
        self._records = []
        for identity, row in self._legacy.items():
            kind = row["kind"]
            self._records.append({
                "case_id": identity, "symbol": row.get("symbol"),
                "day": row.get("day"), "kind": kind, "name": None,
                "scope": "legacy_saved_scope", "source_generation": "legacy_casebook",
                "interpretation_status": row.get("individual_interpretation_status")
                    if kind == "seat_disclosure" else "saved_learning_card",
                "price_path_status": "not_aggregated_from_legacy_manifest",
                "price_path_review_complete": None,
                "source_file": str(self.legacy_path),
            })
        for batch in self.progress["batches"]:
            review_refs = [ref for ref in batch["files"] if Path(ref["file"]).name == "review.json"]
            if len(review_refs) != 1:
                raise ValueError("Each incremental batch needs exactly one saved review JSON")
            ref = review_refs[0]
            review_path = _path(ref["file"], self.progress_path.parent)
            document = _read(review_path, ref["sha256"])
            rows = _unique(document["reviews"], f"batch {batch['batch']}")
            if list(rows) != batch["ids"]:
                raise ValueError("Incremental review order/identity differs from completion receipt")
            for identity, interpretation in rows.items():
                if identity not in queue or identity in self._legacy or identity in self._new:
                    raise ValueError(f"Incremental case overlaps or is outside queue: {identity}")
                entry = queue[identity]
                if interpretation.get("symbol") != entry["symbol"]:
                    raise ValueError("Incremental case symbol differs from fixed queue")
                # The fixed queue is the original source scope; no outcome selection.
                evidence = interpretation.get("case_evidence")
                bars = evidence.get("raw_bars") if isinstance(evidence, dict) else None
                if bars is None:
                    price_status = "independent_price_path_pending"
                elif not bars or any(value is None for value in bars.values()):
                    price_status = "embedded_price_slots_missing"
                elif any(value.get("tradestatus") != "1" for value in bars.values()):
                    price_status = "embedded_price_slots_include_nonactive_or_unknown"
                else:
                    price_status = "embedded_pair_present_not_full_trade_path"
                self._new[identity] = {"interpretation": interpretation,
                    "document": document, "review_path": review_path,
                    "queue_entry": entry}
                self._records.append({"case_id": identity, "symbol": entry["symbol"],
                    "day": entry.get("date"), "kind": "seat_source_snapshot",
                    "name": interpretation.get("name"), "scope": entry.get("scope_status"),
                    "source_generation": "incremental_source_snapshot",
                    "interpretation_status": "agent_interpreted",
                    "price_path_status": price_status, "price_path_review_complete": False,
                    "source_file": str(review_path)})
        if set(self._new) != set(queue):
            raise ValueError("Completed incremental package does not cover its exact fixed queue")
        if len(self._new) != self.progress["source_case_readings_completed"]:
            raise ValueError("Incremental case count differs from saved completion receipt")
        self._by_id = {row["case_id"]: row for row in self._records}

    def summary(self):
        kinds = Counter(row["kind"] for row in self._records)
        old_bodies = sum(row["kind"] == "seat_disclosure" and
                         row["interpretation_status"] == "agent_interpreted"
                         for row in self._records)
        return {"schema": "trade_case_learning_index_summary_v1",
            "archive_records": len(self._records), "records_by_kind": dict(kinds),
            "legacy_seat_source_interpretations": old_bodies,
            "incremental_seat_source_interpretations": len(self._new),
            "seat_source_interpretations": old_bodies + len(self._new),
            "incremental_price_status_counts": dict(Counter(
                row["price_path_status"] for row in self._records
                if row["source_generation"] == "incremental_source_snapshot")),
            "legacy_price_path_completeness_not_recomputed": True,
            "complete_price_path_reviews": None, "personal_trade_count": None,
            "attributable_person_count": None, "manual_human_deep_reviews_claimed": 0,
            "private_complete_trade_documents": 0,
            "legacy_manifest": str(self.legacy_path),
            "incremental_completion_receipt": str(self.progress_path),
            "incremental_reading_completed_at": self.progress.get("completed_at"),
            "boundaries": list(BOUNDARIES), **FLAGS}

    def list_cases(self, *, symbol=None, day=None, scope=None, kind=None,
                   offset=0, limit=50):
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("offset must be >= 0 and limit must be between 1 and 200")
        rows = self._records
        for field, value in (("symbol", symbol), ("day", day), ("scope", scope), ("kind", kind)):
            if field == "symbol" and (value is None or value == ""):
                continue
            if (field == "symbol" and isinstance(value, str) and len(value) == 6
                    and all("0" <= char <= "9" for char in value)):
                rows = [row for row in rows if (row.get("symbol") or "")[:6] == value]
            elif value is not None:
                rows = [row for row in rows if row.get(field) == value]
        return {"schema": "trade_case_learning_index_page_v1", "summary": self.summary(),
                "total_filtered": len(rows), "offset": offset, "limit": limit,
                "cases": deepcopy(rows[offset:offset + limit]), **FLAGS}

    def detail(self, case_id: str):
        if case_id not in self._by_id:
            raise KeyError(case_id)
        item = self._by_id[case_id]
        if case_id in self._new:
            saved = self._new[case_id]
            interpretation, document = saved["interpretation"], saved["document"]
            facts = interpretation.get("case_evidence")
            if facts is None:
                ref = document.get("evidence_file")
                if not ref:
                    raise ValueError("Incremental source facts are not saved")
                evidence_path = _path(ref["file"] if isinstance(ref, dict) else ref,
                                      saved["review_path"].parent)
                expected = ref.get("sha256") if isinstance(ref, dict) else document.get("evidence_sha256")
                evidence = _read(evidence_path, expected)
                facts = _unique(evidence.get("records", evidence.get("cases", [])),
                                "incremental source evidence").get(case_id)
                if facts is None:
                    raise ValueError("Saved source evidence does not contain requested case")
            metadata = {key: document.get(key) for key in (
                "created_at", "completed_at", "source_available_at", "published_at",
                "actual_retrieval_clocks", "source_files", "price_source", "environment_source",
                "historical_publication_verified", "evidence_boundaries")}
            metadata.update({"review_file": str(saved["review_path"]),
                             "queue_entry": saved["queue_entry"]})
        else:
            saved = self._legacy[case_id]
            fact_path = _path(saved["json_file"], self.legacy_path.parent)
            facts = _read(fact_path)
            if facts.get("case_id") != case_id:
                raise ValueError("Legacy fact identity differs from saved manifest")
            interpretation = None
            if saved.get("individual_interpretation_file"):
                review_path = _path(saved["individual_interpretation_file"], self.legacy_path.parent)
                document = _read(review_path)
                interpretation = _unique(document["reviews"], "legacy review").get(case_id)
                if interpretation is None:
                    raise ValueError("Requested legacy interpretation is absent from its saved review")
                if (interpretation.get("case_file") and
                        _path(interpretation["case_file"], review_path.parent) != fact_path):
                    raise ValueError("Legacy interpretation points to a different fact card")
                if interpretation.get("case_sha256"):
                    _read(fact_path, interpretation["case_sha256"])
            elif saved["kind"] == "public_source":
                interpretation = {"learning_notes": facts.get("learning_notes"),
                                  "source_record": facts.get("source_record")}
            metadata = {"fact_file": str(fact_path),
                "interpretation_file": saved.get("individual_interpretation_file"),
                "markdown_file": saved.get("markdown_file"), "sources": facts.get("sources"),
                "historical_publication_verified": facts.get("historical_publication_verified")}
        return deepcopy({"schema": "trade_case_learning_detail_v1", "case": item,
            "interpretation": interpretation, "source_facts": facts,
            "source_metadata": metadata, "boundaries": list(BOUNDARIES), **FLAGS})


def main(argv=None):
    import argparse
    from ..settings import ROOT
    root = Path(ROOT) / "outputs/trade_case_learning"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-manifest", type=Path,
        default=root / "20261015_initial_v1/casebook_v19/manifest.json")
    parser.add_argument("--progress", type=Path,
        default=root / "20261016_source1015_pending_v1/progress_T_completed.json")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--summary", action="store_true")
    selection.add_argument("--case-id")
    for name in ("symbol", "day", "kind", "scope"):
        parser.add_argument("--" + name)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)
    index = TradeCaseLearningIndex(args.legacy_manifest, args.progress)
    value = index.summary() if args.summary else index.detail(args.case_id) if args.case_id else index.list_cases(
        symbol=args.symbol.strip().upper() if args.symbol else None, day=args.day,
        kind=args.kind, scope=args.scope, offset=args.offset, limit=args.limit)
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return 0
