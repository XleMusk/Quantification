"""Read-only pagination checks. Repeated disclosures are flagged, never deleted."""
import argparse
from collections import Counter
import gzip
from hashlib import sha256
import json
from pathlib import Path

from .seat_dataset import DEFAULT_DATASET, fetch_page, page_file, params_for, stamp
from .seats import save_json


FACT_FIELDS = ("TRADE_DATE", "SECUCODE", "CHANGE_TYPE", "TRADE_ID",
               "OPERATEDEPT_CODE", "OPERATEDEPT_NAME", "EXPLANATION",
               "BUY", "SELL", "NET", "ACCUM_AMOUNT")


def fingerprint(row):
    # Exclude retrospective win rates and subsequent returns supplied by the vendor.
    return sha256(json.dumps([row.get(key) for key in FACT_FIELDS],
                             ensure_ascii=False, separators=(",", ":")).encode()).digest()


def board_key(row):
    return row["TRADE_DATE"][:10], row["SECUCODE"], str(row["TRADE_ID"])


def load_overlap_reviews(root, kind):
    reviews = {}
    for path in sorted((root / "overlap_reviews" / kind).glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            doc = json.load(stream)
        key = tuple(doc["review_key"])
        day, symbol, trade_id = key
        expected_request = params_for(kind, 1, day, day)
        expected_request["filter"] += f'(SECURITY_CODE="{symbol.split(".")[0]}")(TRADE_ID="{trade_id}")'
        result = doc["response"]["result"]
        rows = result["data"]
        if (doc["request"] != expected_request or doc["response"].get("success") is not True
                or result["pages"] != 1 or result["count"] != len(rows) or not rows
                or any(board_key(row) != key for row in rows) or key in reviews):
            raise ValueError(f"Invalid independent board review: {path}")
        reviews[key] = {"path": str(path.relative_to(root)),
                        "source": Counter(fingerprint(row) for row in rows),
                        "local": Counter(), "pages": set(), "eligible_repetitions": 0}
    return reviews


def verify_pages(root=DEFAULT_DATASET):
    root = Path(root)
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    report = {"checked_at": stamp(), "kinds": {}, "exchange_history_completeness_proven": False,
              "private_account_completeness_proven": False}
    for kind in ("buy", "sell"):
        expected = plan["reports"][kind]
        reviews = load_overlap_reviews(root, kind)
        missing, repeated_pages, repeated_examples = [], [], []
        seen_rows, seen_pages = {}, {}
        physical_rows = cross_page_repetitions = same_page_repetitions = 0
        date_counts = Counter()
        for page in range(1, expected["pages"] + 1):
            if not page_file(root, kind, page).exists():
                missing.append(page)
                continue
            _, _, doc, _ = fetch_page(None, root, kind, page, plan)
            rows = doc["response"]["result"]["data"]
            digests = []
            for row in rows:
                digest = fingerprint(row)
                review = reviews.get(board_key(row)) if reviews else None
                if review is not None:
                    review["local"][digest] += 1
                    review["pages"].add(page)
                previous = seen_rows.get(digest)
                if previous is not None:
                    if previous == page:
                        same_page_repetitions += 1
                    else:
                        cross_page_repetitions += 1
                        # Only anonymous institution listings can use this reconciliation.
                        if (review is not None and row.get("OPERATEDEPT_NAME") == "\u673a\u6784\u4e13\u7528"
                                and str(row.get("OPERATEDEPT_CODE")) == "0"):
                            review["eligible_repetitions"] += 1
                        if len(repeated_examples) < 20:
                            repeated_examples.append({"first_page": previous, "page": page,
                                "trade_date": row["TRADE_DATE"], "symbol": row["SECUCODE"],
                                "seat_name": row.get("OPERATEDEPT_NAME"),
                                "trade_id": row.get("TRADE_ID")})
                else:
                    seen_rows[digest] = page
                digests.append(digest)
                physical_rows += 1
                date_counts[row["TRADE_DATE"][:10]] += 1
            signature = sha256(b"".join(sorted(digests))).digest()
            if signature in seen_pages:
                repeated_pages.append({"first_page": seen_pages[signature], "page": page})
            seen_pages[signature] = page
        reconciliations = []
        resolved = 0
        for key, review in reviews.items():
            matches = review["local"] == review["source"]
            count = review["eligible_repetitions"] if matches else 0
            resolved += count
            reconciliations.append({"board_key": key, "evidence_file": review["path"],
                "source_rows": sum(review["source"].values()), "local_rows": sum(review["local"].values()),
                "pages": sorted(review["pages"]), "full_multiset_matches": matches,
                "resolved_cross_page_repetitions": count})
        unresolved = cross_page_repetitions - resolved
        complete = not missing and physical_rows == expected["rows"]
        report["kinds"][kind] = {
            "physical_rows": physical_rows, "expected_rows": expected["rows"],
            "missing_pages": missing, "repeated_pages": repeated_pages,
            "unique_factual_fingerprints": len(seen_rows),
            "same_page_repeated_facts": same_page_repetitions,
            "cross_page_repeated_facts": cross_page_repetitions,
            "resolved_cross_page_repetitions": resolved,
            "unresolved_cross_page_repetitions": unresolved,
            "independent_board_reviews": reconciliations,
            "repeated_examples": repeated_examples,
            "first_date": min(date_counts) if date_counts else None,
            "last_date": max(date_counts) if date_counts else None,
            "status": ("incomplete" if not complete else "needs_overlap_review"
                       if repeated_pages or unresolved else "source_pagination_verified"),
            "interpretation": "Fingerprints describe public listings, not unique persons or orders. "
                              "Anonymous repeats are resolved only by matching the full factual multiset "
                              "of an independently queried board. No rows were removed."}
    report["status"] = ("source_pagination_verified" if all(
        item["status"] == "source_pagination_verified" for item in report["kinds"].values())
        else "incomplete_or_needs_review")
    save_json(root / "seat_integrity.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    report = verify_pages(args.root)
    compact = {**report, "kinds": {kind: {key: value for key, value in item.items()
        if key != "missing_pages"} | {"missing_page_count": len(item["missing_pages"])}
        for kind, item in report["kinds"].items()}}
    print(json.dumps(compact, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
