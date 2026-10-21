"""Synthetic proof bytes for source/Engine behavior, never market evidence."""
from hashlib import sha256
import csv
import json

from trading.research.ipo_qualification import ISSUER, SHORT_NAME, SYMBOL


def write_ipo_fixture(folder):
    sources = []
    ids = ["szse_listing_notice", "issuer_listing_notice", "szse_2026_trading_rules", "szse_2026_rules_effective_notice"]
    for identity in ids:
        raw = folder / (identity + ".fixture")
        raw.write_bytes(("synthetic not an official document: " + identity).encode())
        published = "2026-10-12" if "listing" in identity else "2026-04-24"
        sources.append({"source_id": identity, "url": "https://www.szse.cn/fixture/" + identity,
            "status": "downloaded", "raw_path": str(raw), "sha256": sha256(raw.read_bytes()).hexdigest(),
            "byte_count": raw.stat().st_size, "retrieved_at": "2026-10-15T08:00:00+08:00",
            "publication_date": published, "available_at": published + "T23:59:59+08:00", "publication_time_known": False})
    receipt = folder / "sources.json"
    receipt.write_text(json.dumps({"schema": "ipo_qualification_official_sources_v1", "sources": sources, "execution_admitted": False}), encoding="utf-8")
    evidence = folder / "ipo_evidence.json"
    evidence.write_text(json.dumps({"schema": "research_ipo_qualification_v1", "source_receipt": str(receipt),
        "instrument": {"symbol": SYMBOL, "issuer": ISSUER, "name": SHORT_NAME, "board": "chinext",
        "ipo_date": "2026-10-13", "available_at": "2026-10-12T23:59:59+08:00", "rule_effective_from": "2026-07-06",
        "first_no_limit_sessions": 5, "following_price_limit_pct": 20, "source_ids": ids},
        "pit_verified": False, "execution_admitted": False}), encoding="utf-8")
    master = folder / "master.csv"
    with master.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["canonical_symbol", "provider_ipo_date", "board"])
        writer.writeheader()
        writer.writerows({"canonical_symbol": symbol, "provider_ipo_date": "2026-01-02", "board": "main"}
                        for symbol in ("600001.SSE", "600002.SSE"))
    run = folder / "master_run.json"
    run.write_text(json.dumps({"schema": "instrument_master_v1", "execution_admitted": False,
        "master_file": master.name, "master_sha256": sha256(master.read_bytes()).hexdigest(),
        "created_at": "2026-10-08T12:00:00+08:00", "instrument_count": 2}), encoding="utf-8")
    return evidence, run
