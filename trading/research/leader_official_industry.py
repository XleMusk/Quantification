"""Official historical CAPCO bulk lists for an explicit posthoc identity study.

Use the published division-level list only after its page's publication date.
The day after publication is conservative when the exact release time is
unknown. Later retrieval, file migration and revisions do not establish PIT.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re

import pandas as pd

from .leader_posthoc_industry import _day, _digest, _time


SCHEMA = "saved_official_capco_industry_posthoc_v1"
METHOD_ID = "official_bulk_division_after_publication_posthoc_v1"
SOURCE = "China Association for Public Companies official historical industry lists"
COLUMNS = ("symbol", "code", "name", "section_code", "section_name",
           "subclass_code", "subclass_name", "division_code", "division_name", "page")


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _pin(path):
    path = Path(path).resolve()
    return {"file": str(path), "sha256": _digest(path)}


def _sources(batches, observed):
    snapshots, pins = [], []
    for batch in batches:
        period = _day(batch["reference_period_end"])
        publication = _day(batch["publication_date"])
        eligible = (date.fromisoformat(publication) + timedelta(days=1)).isoformat()
        if period > publication or _time(batch["retrieved_at"]) > observed:
            raise ValueError("Official reference period/publication and actual retrieval clocks required")
        files = {key: _pin(batch[key]) for key in ("source_pdf", "source_article", "table_file")}
        for declared, key in (("source_pdf_sha256", "source_pdf"),
                              ("source_article_sha256", "source_article"),
                              ("table_sha256", "table_file")):
            if declared in batch and batch[declared] != files[key]["sha256"]:
                raise ValueError("Original source receipt hash differs from the supplied file")
        frame = pd.read_csv(batch["table_file"], dtype=str, keep_default_na=False)
        if (not set(COLUMNS) <= set(frame) or frame.empty
                or frame.loc[frame.symbol.ne(""), "symbol"].duplicated().any()
                or frame.code.duplicated().any() or len(frame) != batch["rows"]):
            raise ValueError("Complete distinct bulk table and its explicit source row count required")
        members, labels = {}, {}
        for row in frame.itertuples(index=False):
            if (not re.fullmatch(r"[0-9]{6}", row.code)
                    or not row.name.strip() or not re.fullmatch(r"[1-9][0-9]*", row.page)):
                raise ValueError("Explicit saved issuer identity and PDF page required")
            if row.symbol == "":
                # Unsupported exchange identity is retained in the complete
                # table and remains unknown to the existing research scope.
                continue
            if (not re.fullmatch(r"[0-9]{6}\.(SSE|SZSE|BSE)", row.symbol)
                    or row.code != row.symbol.split(".")[0]):
                raise ValueError("Mapped source symbol differs from its saved security code")
            if row.division_code == "" and row.division_name == "":
                members[row.symbol] = None
            elif (re.fullmatch(r"[A-Z]", row.section_code)
                    and re.fullmatch(r"[0-9]{2}", row.division_code) and row.division_name.strip()):
                key = row.section_code + row.division_code
                members[row.symbol] = key
                labels.setdefault(key, set()).add(row.division_name)
            else:
                raise ValueError("Unknown division must remain fully blank, never a partial guessed label")
        if files != {key: _pin(batch[key]) for key in files}:
            raise ValueError("Official bulk source changed while reading")
        descriptor = {**deepcopy(batch), "files": files, "eligible_from": eligible,
            "member_count": len(members), "unmapped_code_count": int(frame.symbol.eq("").sum()),
            "industry_labels": {key: sorted(values) for key, values in sorted(labels.items())},
            "mapped_member_count": sum(value is not None for value in members.values())}
        pins.append(descriptor)
        snapshots.append({"batch": descriptor, "membership": members})
    if (not snapshots or len({item["batch"]["batch_id"] for item in snapshots}) != len(snapshots)
            or [item["batch"]["eligible_from"] for item in snapshots]
                != sorted(set(item["batch"]["eligible_from"] for item in snapshots))
            or [item["batch"]["reference_period_end"] for item in snapshots]
                != sorted(set(item["batch"]["reference_period_end"] for item in snapshots))):
        raise ValueError("Distinct bulk periods in original publication order required")
    return pins, snapshots


class _Provider:
    def __init__(self, snapshots, saved, provenance):
        self._snapshots, self._saved = snapshots, saved
        self.provenance = provenance

    def __call__(self, anchor_day):
        anchor = _day(anchor_day)
        if not self._saved["start"] <= anchor <= self._saved["end"]:
            return None
        choices = [item for item in self._snapshots if item["batch"]["eligible_from"] <= anchor]
        if not choices:
            return None
        chosen = choices[-1]
        batch = chosen["batch"]
        table = batch["files"]["table_file"]
        return {"snapshot_date": batch["eligible_from"], "anchor_day": anchor,
            "reference_period_end": batch["reference_period_end"],
            "reference_period_basis": batch.get("reference_period_basis", "declared_source_period"),
            "source_publication_date": batch["publication_date"],
            "publication_time_known": False, "source_retrieved_at": batch["retrieved_at"],
            "actual_available_at": self._saved["created_at"],
            "source_file": table["file"], "source_sha256": table["sha256"],
            "source_document_refs": deepcopy(batch["files"]),
            "classification": "CAPCO_division_section_plus_two_digit_code",
            "industry_labels": deepcopy(batch["industry_labels"]),
            "member_count": batch["member_count"], "mapped_member_count": batch["mapped_member_count"],
            "unmapped_code_count": batch["unmapped_code_count"],
            "membership": deepcopy(chosen["membership"]),
            "snapshot_staleness_calendar_days":
                (date.fromisoformat(anchor) - date.fromisoformat(batch["eligible_from"])).days,
            "revision_safe_PIT_verified": False, "theme_membership": False, "posthoc_only": True,
            "historical_publication_verified": False,
            "date_basis": "day_after_official_page_publication_not_reference_period_or_actual_PIT"}


def build(batches, out, *, start, end):
    start, end = _day(start), _day(end)
    if start > end or start[:4] != end[:4]:
        raise ValueError("One explicit finite study year is required")
    folder = Path(out).resolve()
    if folder.exists():
        raise FileExistsError("Official bulk research source requires a new directory")
    actual = datetime.now().astimezone().isoformat()
    pins, _ = _sources(batches, _time(actual))
    if any(Path(ref["file"]).is_relative_to(folder) for item in pins for ref in item["files"].values()):
        raise ValueError("New publication must not overwrite an input source directory")
    result = {"schema": SCHEMA, "method_id": METHOD_ID, "source": SOURCE,
        "status": "completed_saved_official_bulk_source", "created_at": actual,
        "start": start, "end": end, "batches": pins,
        "selection": "latest_published_bulk_list_only_from_day_after_page_publication",
        "industry_level": "division", "missing_issuer": "unknown_no_current_backfill",
        "actual_available_at": actual, "revision_safe_PIT_verified": False,
        "theme_membership": False, "posthoc_only": True, "historical_publication_verified": False,
        "execution_admitted": False, "provider_contacted": False}
    folder.mkdir(parents=True)
    with (folder / "result.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return result


def load(root, observed_at):
    root = Path(root).resolve()
    path = root / "result.json"
    saved = _read(path)
    observed = _time(observed_at)
    if (saved.get("schema") != SCHEMA or saved.get("method_id") != METHOD_ID
            or saved.get("source") != SOURCE or saved.get("status") != "completed_saved_official_bulk_source"
            or saved.get("selection") != "latest_published_bulk_list_only_from_day_after_page_publication"
            or saved.get("industry_level") != "division"
            or saved.get("missing_issuer") != "unknown_no_current_backfill"
            or saved.get("posthoc_only") is not True
            or any(saved.get(flag) is not False for flag in ("revision_safe_PIT_verified",
                "theme_membership", "historical_publication_verified", "execution_admitted", "provider_contacted"))
            or _time(saved["created_at"]) > observed or saved.get("actual_available_at") != saved["created_at"]
            or _day(saved["start"]) > _day(saved["end"]) or saved["start"][:4] != saved["end"][:4]):
        raise ValueError("An explicit unchanged official bulk posthoc source is required")
    batches = [{key: value for key, value in item.items() if key not in (
        "files", "eligible_from", "member_count", "mapped_member_count", "unmapped_code_count",
        "industry_labels")} for item in saved["batches"]]
    pins, snapshots = _sources(batches, _time(saved["created_at"]))
    if pins != saved["batches"]:
        raise ValueError("Official source files, parser table or publication facts changed")
    provenance = {**deepcopy(saved), "root": str(root), "receipt_file": str(path),
        "receipt_sha256": _digest(path), "observed_at": observed.isoformat(),
        "snapshot_count": len(pins), "source_entries": pins}
    return _Provider(snapshots, saved, provenance)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches", required=True, help="Saved explicit list of original bulk source descriptors")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = build(_read(args.batches), args.out, start=args.start, end=args.end)
    print(json.dumps({"status": result["status"], "batches": len(result["batches"]),
                      "start": result["start"], "end": result["end"], "execution_admitted": False}))
    return 0
