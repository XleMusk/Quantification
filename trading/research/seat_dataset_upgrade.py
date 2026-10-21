"""Preserve the unstable legacy snapshot; reuse only its verified summary pages."""
import json
from pathlib import Path
import shutil
import sqlite3

from .seat_dataset import (DEFAULT_DATASET, SCHEMA, fetch_page, import_media_links,
                           ingest, publish_status, stamp, verify_summary)
from .seats import DEFAULT_HOME, save_json


def prepare():
    old = DEFAULT_HOME / "public_history_2004_20260924"
    root = DEFAULT_DATASET
    if root.exists():
        raise ValueError("Target already exists; resume it without rerunning this preparation")
    integrity = verify_summary(old)
    if integrity["status"] != "source_pagination_verified":
        raise ValueError("Summary failed the independent pagination check")
    root.mkdir()
    plan = json.loads((old / "plan.json").read_text(encoding="utf-8"))
    plan.update(pagination_version=2, prepared_at=stamp(), summary_reused_from=str(old),
                legacy_seat_pages_reused=False)
    save_json(root / "plan.json", plan)
    shutil.copytree(old / "raw" / "summary", root / "raw" / "summary")
    shutil.copyfile(old / "summary_integrity.json", root / "summary_integrity.json")
    with sqlite3.connect(root / "disclosures.sqlite3") as db:
        db.executescript(SCHEMA)
        import_media_links(db)
        for page in range(1, plan["reports"]["summary"]["pages"] + 1):
            kind, page, doc, path = fetch_page(None, root, "summary", page, plan)
            ingest(db, kind, page, doc, path)
        result = publish_status(db, root, plan)
    save_json(old / "SUPERSEDED.json", {"at": stamp(), "replacement": str(root),
        "reason": "Seat pagination sorted only by date/security has cross-page overlaps; "
                  "row totals do not establish completeness. Original files retained."})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    prepare()
