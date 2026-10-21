"""Immutable exact-day public-seat batches, separate from the frozen single source.

Only saved local disclosures are read. Missing/failed sources are not empty
events; complete empty disclosures additionally require an explicit receipt.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import gzip
import json
from math import isfinite
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import pandas as pd

from ..contracts import MARKET_TZ
from ..rules import stock_board
from . import seat_hypothesis_snapshot as snapshot
from . import seat_hypothesis_panel_replay as original
from . import seats


SCHEMA = "public_seat_disclosure_catalog_v1"
METHOD_ID = "public_seat_exact_day_append_catalog_v1"
BATCH_SCHEMA = "public_seat_disclosure_batch_v1"
DERIVED_SCHEMA = "public_seat_append_recipe_snapshot_v1"
EMPTY_RECEIPT_SCHEMA = "public_seat_complete_empty_disclosure_receipt_v1"
RECIPES = ("legacy_repeated_sum", "stockday_unique")
FLAGS = {"candidate_panel_only": True, "execution_admitted": False,
    "selection_enabled": False, "strategy_validated": False, "engine_run": False,
    "account_created": False, "ledger_mutated": False, "network_accessed": False,
    "historical_publication_verified": False, "pit_verified": False}


def _now(now=None):
    value = datetime.now(MARKET_TZ) if now is None else now
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("An aware actual validation clock is required")
    return value.astimezone(MARKET_TZ)


def _stamp(value, now):
    if not isinstance(value, str):
        raise ValueError("Explicit aware source/creation clock required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None or result > now:
        raise ValueError("Naive or future source/creation clock")
    return result.astimezone(MARKET_TZ)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _ref(path, data=None):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": sha256(path.read_bytes() if data is None else data).hexdigest()}


def _verify(reference):
    actual = _ref(reference["file"])
    if actual["sha256"] != reference.get("sha256"):
        raise ValueError("Pinned disclosure/source file changed: " + actual["file"])
    return actual


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _unique_refs(references):
    found = {}
    for reference in references:
        key = str(Path(reference["file"]).resolve())
        value = {"file": key, "sha256": reference["sha256"]}
        if key in found and found[key] != value:
            raise ValueError("One path cannot identify different immutable source bytes")
        found[key] = value
    return list(found.values())


def _protect(out, references, packages=()):
    target = Path(out).resolve()
    if target.exists():
        raise FileExistsError("Disclosure outputs must use a new immutable path")
    for reference in references:
        source = Path(reference["file"]).resolve()
        if target == source or target in source.parents:
            raise ValueError("Output cannot cover an input/source file")
    for package in packages:
        source = Path(package).resolve()
        if target == source or target in source.parents or source in target.parents:
            raise ValueError("Output cannot cover or sit inside an input package")
    return target


def inspect_source(source_root, *, completion_receipt=None, now=None):
    """Read a bounded saved cache; never infer successful emptiness from absence.

    A fully empty cache additionally needs EMPTY_RECEIPT_SCHEMA, status
    completed_exact_day_empty_disclosure, event_day, as_of, completed_at,
    source_manifest {file,sha256}, and all three reports with report_name,
    provider_count=0, all_pages_completed=true and exact page_refs.
    """
    actual, root = _now(now), Path(source_root).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return {"status": "pending", "reason": "source_manifest_missing", "source_root": str(root)}
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    if manifest.get("status") != "ready":
        return {"status": "failed" if manifest.get("status") == "failed" else "pending",
            "reason": "source_not_complete", "source_root": str(root), "source_status": manifest.get("status"),
            "references": [_ref(manifest_path, manifest_bytes)]}
    query = manifest.get("query") or {}
    day = snapshot._day(query.get("start"))
    if (query.get("end") != day or query.get("source") != "Eastmoney" or query.get("url") != seats.URL
            or query.get("codes") != [] or query.get("pagination_version") != 2
            or manifest.get("publication_timestamps_available") is not False
            or manifest.get("completeness") != "provider_pages_only"
            or set(manifest.get("reports") or {}) != set(seats.REPORTS)):
        raise ValueError("Exactly one unfiltered event-day and all three saved reports required")
    clocks = [_stamp(manifest.get("completed_at"), actual)]
    refs, raw, report_refs, blobs = [_ref(manifest_path, manifest_bytes)], {}, {}, {"manifest.json": manifest_bytes}
    for kind, report in seats.REPORTS.items():
        declaration = manifest["reports"][kind]
        pages, count = declaration.get("pages"), declaration.get("rows")
        if type(pages) is not int or not 1 <= pages <= 100 or type(count) is not int or count < 0:
            raise ValueError("Complete source needs explicit bounded page and nonnegative row counts")
        records, kind_refs = [], []
        for page in range(1, pages + 1):
            path = root / f"{kind}_{page:04d}.json.gz"
            data = path.read_bytes()
            doc = json.loads(gzip.decompress(data).decode("utf-8"))
            sort = "TRADE_DATE,SECURITY_CODE" + (",TRADE_ID,OPERATEDEPT_CODE,BUY,SELL" if kind != "summary" else "")
            request = {"reportName": report, "columns": "ALL", "filter": f"(TRADE_DATE>='{day}')(TRADE_DATE<='{day}')",
                "pageSize": 500, "pageNumber": page, "sortColumns": sort,
                "sortTypes": ",".join("1" for _ in sort.split(",")), "source": "WEB", "client": "WEB"}
            response = doc.get("response") or {}
            result = response.get("result") or {}
            rows = result.get("data")
            declared_pages = result.get("pages")
            valid_pages = declared_pages == pages or (count == 0 and pages == 1 and declared_pages == 0)
            if (doc.get("request") != request or response.get("success") is not True
                    or result.get("count") != count or not valid_pages or not isinstance(rows, list)
                    or (count > 0 and not rows) or doc.get("published_at") is not None):
                raise ValueError("Exact saved request/success/pagination evidence differs")
            retrieved = _stamp(doc.get("retrieved_at"), actual)
            clocks.append(retrieved)
            for index, row in enumerate(rows):
                if snapshot._day(str(row.get("TRADE_DATE", ""))[:10]) != day:
                    raise ValueError("Disclosure record belongs to another event-day")
                snapshot._symbol(row)
                records.append({**row, "_raw_id": f"{kind}:{page}:{index}", "_retrieved_at": doc["retrieved_at"]})
            reference = _ref(path, data)
            refs.append(reference)
            kind_refs.append(reference)
            blobs[path.name] = data
        if len(records) != count:
            raise ValueError("Complete report row inventory differs")
        raw[kind], report_refs[kind] = records, kind_refs
    empty = not any(raw.values())
    receipt_ref = None
    if empty:
        if completion_receipt is None:
            return {"status": "pending", "reason": "empty_disclosure_completion_evidence_missing",
                "source_root": str(root), "event_day": day, "references": refs}
        receipt_bytes = Path(completion_receipt).read_bytes()
        receipt = json.loads(receipt_bytes.decode("utf-8-sig"))
        receipt_ref = _ref(completion_receipt, receipt_bytes)
        if (receipt.get("schema") != EMPTY_RECEIPT_SCHEMA or receipt.get("status") != "completed_exact_day_empty_disclosure"
                or receipt.get("event_day") != day or _verify(receipt["source_manifest"]) != refs[0]
                or set(receipt.get("reports") or {}) != set(seats.REPORTS)):
            raise ValueError("Complete-empty receipt must bind this exact day, source and three reports")
        as_of, completed = (_stamp(receipt.get(key), actual) for key in ("as_of", "completed_at"))
        if not max(clocks) <= as_of <= completed or as_of < datetime.fromisoformat(day + "T15:00:00+08:00"):
            raise ValueError("Empty disclosure receipt cannot backdate its exact-day completion/as-of")
        for kind, report in seats.REPORTS.items():
            proof = receipt["reports"][kind]
            if (proof.get("report_name") != report or type(proof.get("provider_count")) is not int or proof.get("provider_count") != 0
                    or proof.get("all_pages_completed") is not True
                    or [_verify(r) for r in proof.get("page_refs", [])] != report_refs[kind]):
                raise ValueError("Empty disclosure needs actual complete successful pages for every report")
        clocks.append(completed)
        refs.append(receipt_ref)
    available = max(clocks)
    if available < datetime.fromisoformat(day + "T15:00:00+08:00"):
        raise ValueError("A full event-day disclosure cannot be observed before its close")
    for reference in refs:
        _verify(reference)
    fingerprint = sha256(json.dumps({"query": query, "source_files": sorted(
        [(Path(r["file"]).name, r["sha256"]) for r in refs]), "rule_spec": snapshot.RULE_SPEC},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return {"status": "completed_empty" if empty else "completed_nonempty", "event_day": day,
        "source_root": str(root), "manifest": manifest, "references": refs, "raw": raw, "blobs": blobs,
        "source_available_at": available.isoformat(), "source_fingerprint": fingerprint,
        "completion_receipt": receipt_ref}


def _derive_snapshot(source, out, now):
    """Use unchanged normalizer and panel recipes; guard only genuine no-seat input."""
    out.mkdir()
    raw = source["raw"]
    if raw["buy"] or raw["sell"]:
        events = seats.normalize(raw["buy"], raw["sell"])
    else:
        events = pd.DataFrame(columns=["trade_date", "symbol", "net_cny", "listed_side", "period_turnover_cny",
            "reason", "raw_id", "event_id", "eligible"])
        events["eligible"] = pd.Series(dtype=bool)
    panels = snapshot._panels(events, raw["summary"], raw["buy"] + raw["sell"])
    with ZipFile(out / "source_bundle.zip", "x", compression=ZIP_DEFLATED) as archive:
        for name, data in source["blobs"].items():
            archive.writestr(name, data)
    archive_ref = _ref(out / "source_bundle.zip")
    events.to_parquet(out / "normalized_events.parquet", index=False)
    methods = {}
    for recipe, panel, base, schema in zip(RECIPES, panels,
            (original.HYPOTHESIS_ID, original.STOCKDAY_HYPOTHESIS_ID),
            (original.AVAILABILITY_SCHEMA, original.STOCKDAY_AVAILABILITY_SCHEMA)):
        folder = out / recipe
        folder.mkdir()
        panel = panel.assign(hypothesis_id=base, snapshot_method_id=base)
        candidates = panel.loc[panel.hypothesis_status.eq("matched")].copy()
        panel.to_csv(folder / "panel.csv", index=False)
        candidates.to_csv(folder / "candidates.csv", index=False)
        candidate_ref, panel_ref = _ref(folder / "candidates.csv"), _ref(folder / "panel.csv")
        evidence = {"schema": schema, "hypothesis_id": base, "snapshot_method_id": base,
            "candidates_file": candidate_ref["file"], "candidates_sha256": candidate_ref["sha256"],
            "panel_file": panel_ref["file"], "panel_sha256": panel_ref["sha256"], "source_archive": archive_ref,
            "source_available_at": source["source_available_at"],
            "source_scope": original.STOCKDAY_SCOPE if recipe == "stockday_unique" else "original_normalized_eligible_event_scope",
            "records": [{"event_date": row["trade_date"],
                "symbol": row["symbol"], "source_file": archive_ref["file"], "source_sha256": archive_ref["sha256"],
                "retrieved_at": source["source_available_at"], "published_at": None, "raw_ids": row["raw_ids"]}
                for row in candidates.to_dict("records")], **FLAGS}
        _write(folder / "availability_evidence.json", evidence)
        methods[recipe] = {"hypothesis_id": base, "snapshot_method_id": base,
            "panel_file": panel_ref["file"], "panel_sha256": panel_ref["sha256"],
            "candidates_file": candidate_ref["file"], "candidates_sha256": candidate_ref["sha256"],
            "availability_evidence": str(folder / "availability_evidence.json"),
            "stock_days": len(panel), "candidates": len(candidates)}
    record = {"schema": DERIVED_SCHEMA, "status": "completed_research_snapshot_only", "created_at": now.isoformat(),
        "source_available_at": source["source_available_at"], "source_manifest": source["references"][0],
        "source_files": source["references"], "source_query": source["manifest"]["query"], "source_archive": archive_ref,
        "normalized_events_file": str(out / "normalized_events.parquet"),
        "normalized_events_sha256": _ref(out / "normalized_events.parquet")["sha256"],
        "methods": methods, "rule_spec": snapshot.RULE_SPEC, **FLAGS}
    _write(out / "result.json", record)
    return out / "result.json"


def _load_snapshot(path, source, now):
    reference, record = _ref(path), _read(path)
    day, available = source["event_day"], _stamp(source["source_available_at"], now)
    if (record.get("schema") not in {snapshot.SCHEMA, DERIVED_SCHEMA}
            or record.get("status") != "completed_research_snapshot_only" or record.get("rule_spec") != snapshot.RULE_SPEC
            or record.get("source_query") != source["manifest"]["query"]
            or record.get("source_available_at") != source["source_available_at"]
            or available > _stamp(record.get("created_at"), now)
            or set(record.get("methods", {})) != set(RECIPES)
            or any(record.get(key) is not False for key in ("execution_admitted", "selection_enabled", "strategy_validated", "historical_publication_verified"))):
        raise ValueError("Snapshot rules/source event-day/actual clock or research boundary differs")
    refs = [reference, _verify(record["source_manifest"]), _verify(record["source_archive"])]
    source_refs = [_verify(r) for r in record["source_files"]]
    if _unique_refs(source_refs) != _unique_refs(source["references"]):
        raise ValueError("Snapshot does not bind the complete exact saved source inventory")
    if refs[1] != source["references"][0]:
        raise ValueError("Snapshot source manifest differs")
    with ZipFile(refs[2]["file"]) as archive:
        for name, data in source["blobs"].items():
            if archive.namelist().count(name) != 1 or archive.read(name) != data:
                raise ValueError("Snapshot archive does not retain the exact saved source bytes")
    refs += source_refs
    normalized_ref = _verify({"file": record["normalized_events_file"], "sha256": record["normalized_events_sha256"]})
    refs.append(normalized_ref)
    events = pd.read_parquet(normalized_ref["file"])
    if (not {"trade_date", "symbol", "event_id"} <= set(events)
            or not events.trade_date.eq(day).all() or events.event_id.duplicated().any()):
        raise ValueError("Canonical event identities are missing, duplicated or on another event-day")
    recipes = {}
    for recipe in RECIPES:
        method = record["methods"][recipe]
        base = original.HYPOTHESIS_ID if recipe == RECIPES[0] else original.STOCKDAY_HYPOTHESIS_ID
        panel_ref = _verify({"file": method["panel_file"], "sha256": method["panel_sha256"]})
        candidate_ref = _verify({"file": method["candidates_file"], "sha256": method["candidates_sha256"]})
        evidence_ref = _ref(method["availability_evidence"])
        evidence = _read(evidence_ref["file"])
        if (method.get("hypothesis_id") != base or method.get("snapshot_method_id") != base
                or original._availability_method(evidence)[0] != base
                or _verify({"file": evidence["panel_file"], "sha256": evidence["panel_sha256"]}) != panel_ref
                or _verify({"file": evidence["candidates_file"], "sha256": evidence["candidates_sha256"]}) != candidate_ref
                or _verify(evidence["source_archive"]) != refs[2]
                or evidence.get("source_available_at") != source["source_available_at"]):
            raise ValueError("Recipe sidecar does not bind exact panel/candidates/archive/clock")
        panel = pd.read_csv(panel_ref["file"], dtype=str, keep_default_na=False)
        candidates = pd.read_csv(candidate_ref["file"], dtype=str, keep_default_na=False)
        if (not set(snapshot.PANEL_COLUMNS + ["hypothesis_id", "snapshot_method_id"]) <= set(panel)
                or list(candidates.columns) != list(panel.columns) or panel.duplicated(["trade_date", "symbol"]).any()
                or candidates.to_dict("records") != panel.loc[panel.hypothesis_status.eq("matched")].to_dict("records")
                or not panel.trade_date.eq(day).all() or method["stock_days"] != len(panel)
                or not panel.hypothesis_id.eq(base).all() or not panel.snapshot_method_id.eq(base).all()
                or method["candidates"] != len(candidates)
                or not candidates.hypothesis_h1.map(original._flag).all()
                or original._candidate_method(candidates, empty_method=base) != base):
            raise ValueError("Complete recipe observations and matched candidates differ")
        scores, supported = [], []
        for row in candidates.to_dict("records"):
            score = float(row["net_buy_to_reported_turnover"])
            if not isfinite(score) or score < .01:
                raise ValueError("Nonfinite or below-threshold candidate score")
            item = {"symbol": row["symbol"], "score": score, "event_date": day}
            scores.append(item)
            try:
                stock_board(item["symbol"])
            except ValueError:
                if recipe == "stockday_unique":
                    raise ValueError("Stock-day recipe cannot contain out-of-scope candidates")
            else:
                supported.append(item)
        supported.sort(key=lambda row: (-row["score"], row["symbol"]))
        expected, clocks = {(day, row["symbol"]) for row in scores}, {}
        for row in evidence["records"]:
            key = (snapshot._day(row["event_date"]), row["symbol"])
            if (key not in expected or key in clocks or row.get("source_sha256") != refs[2]["sha256"]
                    or str(Path(row["source_file"]).resolve()) != refs[2]["file"]
                    or row.get("published_at") is not None):
                raise ValueError("Candidate clock has foreign, duplicate or unsupported publication evidence")
            retrieved = _stamp(row.get("retrieved_at"), now)
            if retrieved != available:
                raise ValueError("Event clock must preserve complete raw source availability")
            clocks[key] = {"retrieved_at": row["retrieved_at"], "published_at": None, "available_at": retrieved.isoformat()}
        if set(clocks) != expected:
            raise ValueError("Every candidate requires its exact source clock")
        own_refs = [panel_ref, candidate_ref, evidence_ref]
        refs += own_refs
        recipes[recipe] = {"candidates": scores, "supported": supported, "clocks": clocks,
            "panel_observations": panel.to_dict("records"), "input_refs": own_refs}
    for pinned in refs:
        _verify(pinned)
    identity = {"source_fingerprint": source["source_fingerprint"], "normalized_events_sha256": normalized_ref["sha256"],
        "recipes": {key: [r["sha256"] for r in value["input_refs"][:2]] for key, value in recipes.items()}}
    batch_id = sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    return {"snapshot_ref": reference, "snapshot_created_at": record["created_at"], "recipes": recipes,
        "references": _unique_refs(refs), "event_ids": events.event_id.tolist(), "batch_id": batch_id}


def build_batch(source_root, out, *, snapshot_result=None, completion_receipt=None, now=None):
    """Write a new batch, reusing a saved snapshot or deriving the original recipes.

    Pending/failed sources return an explicit status without creating output.
    Exact saved snapshot bytes are reused, never rederived or overwritten.
    """
    actual = _now(now)
    source = inspect_source(source_root, completion_receipt=completion_receipt, now=actual)
    if source["status"] not in {"completed_empty", "completed_nonempty"}:
        return {key: value for key, value in source.items() if key not in {"raw", "blobs"}}
    prior = _load_snapshot(snapshot_result, source, actual) if snapshot_result is not None else None
    refs = source["references"] + (prior["references"] if prior else [])
    out = _protect(out, refs, [source_root] + ([Path(snapshot_result).resolve().parent] if prior else []))
    out.mkdir(parents=True)
    if prior is None:
        derived_path = _derive_snapshot(source, out / "snapshot", actual)
        prior = _load_snapshot(derived_path, source, actual)
    record = {"schema": BATCH_SCHEMA, "method_id": METHOD_ID, "status": source["status"],
        "batch_id": prior["batch_id"], "source_fingerprint": source["source_fingerprint"],
        "event_day": source["event_day"], "created_at": actual.isoformat(),
        "source_available_at": source["source_available_at"], "source_root": source["source_root"],
        "completion_receipt": source["completion_receipt"], "snapshot_ref": prior["snapshot_ref"],
        "recipe_refs": {key: value["input_refs"] for key, value in prior["recipes"].items()},
        "rules_implementations": [_ref(module.__file__) for module in (snapshot, seats)],
        "revision_policy": "same_event_day_exact_source_idempotent_else_reject_no_replacement", **FLAGS}
    _write(out / "batch.json", record)
    return {"status": source["status"], "batch_id": record["batch_id"], "event_day": record["event_day"],
        "reference": _ref(out / "batch.json"), "source_available_at": record["source_available_at"],
        "created_at": record["created_at"], **FLAGS}


def _load_batch(reference, now):
    reference = _verify(reference)
    record = _read(reference["file"])
    if (record.get("schema") != BATCH_SCHEMA or record.get("method_id") != METHOD_ID
            or record.get("revision_policy") != "same_event_day_exact_source_idempotent_else_reject_no_replacement"
            or any(record.get(key) is not value for key, value in FLAGS.items())):
        raise ValueError("Immutable research disclosure batch contract differs")
    created = _stamp(record.get("created_at"), now)
    receipt = _verify(record["completion_receipt"]) if record.get("completion_receipt") else None
    source = inspect_source(record["source_root"], completion_receipt=receipt["file"] if receipt else None, now=created)
    if (source["status"] not in {"completed_empty", "completed_nonempty"}
            or any(record.get(key) != source[key] for key in ("status", "event_day", "source_available_at", "source_fingerprint"))):
        raise ValueError("Disclosure status/source clock or exact source fingerprint changed")
    pinned = [_verify(r) for r in record["rules_implementations"]]
    if pinned != [_ref(module.__file__) for module in (snapshot, seats)]:
        raise ValueError("The original unchanged normalization and recipe source SHA differs")
    loaded = _load_snapshot(_verify(record["snapshot_ref"])["file"], source, created)
    if (record.get("batch_id") != loaded["batch_id"]
            or record.get("recipe_refs") != {key: value["input_refs"] for key, value in loaded["recipes"].items()}):
        raise ValueError("Batch recipe references changed")
    refs = _unique_refs([reference, *pinned, *loaded["references"]])
    for pinned in refs:
        _verify(pinned)
    return {"batch_id": record["batch_id"], "event_day": record["event_day"], "status": record["status"],
        "created_at": record["created_at"], "source_available_at": record["source_available_at"],
        "snapshot_ref": loaded["snapshot_ref"], "recipes": loaded["recipes"], "reference": reference,
        "references": refs, "source_root": record["source_root"], "event_ids": loaded["event_ids"]}


def load_catalog(path, now=None):
    """Pure readback of the full immutable parent chain and all indirect sources."""
    actual, seen = _now(now), set()

    def visit(file):
        reference = _ref(file)
        if reference["file"] in seen:
            raise ValueError("Cyclic catalog ancestry")
        seen.add(reference["file"])
        record = _read(file)
        if (record.get("schema") != SCHEMA or record.get("method_id") != METHOD_ID
                or any(record.get(key) is not value for key, value in FLAGS.items())):
            raise ValueError("Append catalog contract differs")
        created = _stamp(record.get("created_at"), actual)
        entries = record.get("batches")
        if not isinstance(entries, list) or not entries:
            raise ValueError("A catalog must contain at least one completed batch")
        parent_ref = record.get("parent_ref")
        parent = visit(_verify(parent_ref)["file"]) if parent_ref else None
        if parent:
            if (entries[:-1] != parent["record"]["batches"] or len(entries) != len(parent["record"]["batches"]) + 1
                    or _stamp(parent["created_at"], actual) > created):
                raise ValueError("Parent SHA chain must retain the complete exact prefix and append one batch")
            batches, refs, lineage = list(parent["batches"]), list(parent["references"]), list(parent["lineage_refs"])
        else:
            if len(entries) != 1:
                raise ValueError("Root catalog must contain exactly one initial batch")
            batches, refs, lineage = [], [], []
        entry = entries[-1]
        batch = _load_batch(entry["reference"], created)
        if (entry != {"batch_id": batch["batch_id"], "event_day": batch["event_day"], "reference": batch["reference"]}
                or any(old["event_day"] == batch["event_day"] for old in batches)):
            raise ValueError("Duplicate event-day or changed catalog batch identity")
        batches.append(batch)
        refs = _unique_refs([*refs, reference, *batch["references"]])
        files = [Path(r["file"]) for r in refs]
        packages = list(dict.fromkeys([Path(b["source_root"]) for b in batches]
            + [Path(b["reference"]["file"]).parent for b in batches] + [Path(b["snapshot_ref"]["file"]).parent for b in batches]))
        return {"reference": reference, "record": record, "batches": batches, "references": refs,
            "files": files, "packages": packages, "created_at": record["created_at"], "lineage_refs": lineage + [reference]}

    return visit(Path(path).resolve())


def assert_catalog_prefix(current_loaded, initial_loaded):
    """Require actual ancestry, not a separate branch with superficially same rows."""
    initial = initial_loaded["reference"]
    count = len(initial_loaded["record"]["batches"])
    if (initial not in current_loaded["lineage_refs"]
            or current_loaded["record"]["batches"][:count] != initial_loaded["record"]["batches"]):
        raise ValueError("Current catalog is not a descendant of the registered exact catalog")
    return True


def append_catalog(batch_file, out, *, parent_catalog=None, now=None):
    """Append one observed batch without revising, sorting or erasing its prefix."""
    actual = _now(now)
    batch = _load_batch(_ref(batch_file), actual)
    parent = load_catalog(parent_catalog, now=actual) if parent_catalog else None
    if parent:
        same_day = [old for old in parent["batches"] if old["event_day"] == batch["event_day"]]
        if same_day:
            if same_day[0]["batch_id"] == batch["batch_id"]:
                return {"status": "idempotent_existing_exact_source_batch", "reference": parent["reference"], **FLAGS}
            raise ValueError("revision_conflict: an event-day source cannot be replaced or merged")
    refs = batch["references"] + (parent["references"] if parent else [])
    packages = [Path(batch_file).resolve().parent, Path(batch["source_root"]), Path(batch["snapshot_ref"]["file"]).parent]
    if parent:
        packages += parent["packages"]
    target = _protect(out, refs, packages)
    entries = list(parent["record"]["batches"]) if parent else []
    entries.append({"batch_id": batch["batch_id"], "event_day": batch["event_day"], "reference": batch["reference"]})
    record = {"schema": SCHEMA, "method_id": METHOD_ID, "created_at": actual.isoformat(),
        "parent_ref": parent["reference"] if parent else None, "batches": entries, **FLAGS}
    target.parent.mkdir(parents=True, exist_ok=True)
    _write(target, record)
    checked = load_catalog(target, now=actual)
    return {"status": "completed_research_catalog_only", "reference": checked["reference"],
        "batch_count": len(entries), "created_at": record["created_at"], **FLAGS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    batch = commands.add_parser("batch")
    batch.add_argument("--source", required=True, type=Path)
    batch.add_argument("--out", required=True, type=Path)
    batch.add_argument("--snapshot-result", type=Path)
    batch.add_argument("--completion-receipt", type=Path)
    append = commands.add_parser("append")
    append.add_argument("--batch", required=True, type=Path)
    append.add_argument("--out", required=True, type=Path)
    append.add_argument("--parent", type=Path)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--catalog", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "batch":
        result = build_batch(args.source, args.out, snapshot_result=args.snapshot_result,
            completion_receipt=args.completion_receipt)
    elif args.command == "append":
        result = append_catalog(args.batch, args.out, parent_catalog=args.parent)
    else:
        loaded = load_catalog(args.catalog)
        result = {"status": "verified_research_catalog_only", "reference": loaded["reference"],
            "created_at": loaded["created_at"], "batch_count": len(loaded["batches"]),
            "event_days": [batch["event_day"] for batch in loaded["batches"]], **FLAGS}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
