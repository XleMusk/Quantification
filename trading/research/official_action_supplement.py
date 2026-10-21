"""Explicit issuer-notice supplement for a missing scoped provider event.

Only reviewed cash-only implementation notices are supported.  The original
provider response, canonical bundle and source clocks are never rewritten.
"""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from urllib.parse import urlparse

from ..contracts import MARKET_TZ
from ..symbols import normalize_symbol
from . import recent_action_extension as contract
from .leader_daily_signals import _calendar_view


SCHEMA = "reviewed_official_missing_provider_cash_event_v1"
METHOD = "issuer_implementation_notice_missing_provider_cash_event_v1"
MODES = {"prospective_actual_knowledge", "posthoc_component_diagnostic"}
FLAGS = {"execution_admitted": False, "selection_enabled": False,
    "strategy_validated": False, "pit_verified": False, "returns_reportable": False,
    "provider_raw_terms_modified": False, "historical_knowledge_backfilled": False}


def _ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}


def load_review(path, *, created, calendar, refs):
    """Verify the actual acquisition/review evidence without reading prices."""
    path = Path(path).resolve()
    contract._pin(_ref(path), refs)
    value = contract._json(path)
    if (value.get("schema") != SCHEMA or value.get("method") != METHOD
            or any(value.get(key) is not expected for key, expected in FLAGS.items())):
        raise ValueError("Explicit unadmitted official-only cash event review required")
    action, reference = deepcopy(value["action"]), deepcopy(value["reference"])
    symbol = normalize_symbol(action["symbol"]).vt_symbol
    if (symbol != action["symbol"] or normalize_symbol(action["code"]).vt_symbol != symbol
            or reference.get("code") != action["code"]
            or reference.get("symbol") != symbol
            or reference.get("dividOperateDate") != action["dividOperateDate"]):
        raise ValueError("Official supplement action/reference identity differs")
    for item in (action, reference):
        contract._official(item, created, refs)
    if (action.get("terms_basis") != "issuer_confirmed_cash_only_terms"
            or contract._number(action["dividStocksPs"]) != 0
            or contract._number(action["dividReserveToStockPs"]) != 0
            or any(contract._number(reference[k]) != contract._number(action[k]) for k in contract.TERMS)
            or contract._number(reference["reference_adjustment_share_ratio"]) != 0):
        raise ValueError("Official supplement supports explicit cash-only complete terms")
    for field in contract.DATES:
        if action.get(field, "") or field != "dividStockMarketDate":
            contract._day(action.get(field))
    record, ex, pay = [action[k] for k in contract.DATES[:3]]
    if not action["source_date"] <= record <= ex <= pay:
        raise ValueError("Official supplement source/record/ex/payment chronology conflicts")
    receipt_path = contract._pin(value["retrieval_receipt"], refs)
    documents = contract._json(receipt_path)
    matches = [row for row in documents.get("documents", [])
        if row.get("file") and Path(row["file"]).resolve() == Path(action["source_file"]).resolve()]
    if len(matches) != 1:
        raise ValueError("Exact saved official PDF acquisition receipt required")
    document, source = matches[0], matches[0]["source"]
    source_clock = contract._clock(source["retrieved_at"])
    reviewed = contract._clock(action["reviewed_at"])
    if (source.get("status_code") != 200 or source.get("status") != "downloaded"
            or source.get("sha256") != action["source_sha256"]
            or source.get("url") != action["source_url"]
            or reference.get("source_file") != action["source_file"]
            or reference.get("source_sha256") != action["source_sha256"]
            or reference.get("source_url") != action["source_url"]
            or reference.get("reviewed_at") != action["reviewed_at"]
            or source.get("bytes") != Path(action["source_file"]).stat().st_size
            or urlparse(source["url"]).hostname not in {"static.cninfo.com.cn", "www.sse.com.cn", "www.szse.cn"}
            or not source_clock <= reviewed <= contract._clock(value["review_available_at"]) <= created):
        raise ValueError("Official supplement actual source/review clock or PDF receipt differs")
    pages = value["rendered_pages"]
    count = document.get("page_count")
    if (type(count) is not int or count < 1 or len(pages) != count
            or value.get("reviewed_page_numbers") != list(range(1, count + 1))):
        raise ValueError("All implementation notice pages must be explicitly reviewed")
    for page in pages:
        contract._pin(page, refs)
    cash = contract._number(action["dividCashPsBeforeTax"])
    reference_cash = contract._number(reference["reference_adjustment_cash_ps"])
    basis = reference.get("reference_basis")
    if basis == "issuer_confirmed_standard_cash_only_reference":
        if reference_cash != cash:
            raise ValueError("Standard cash reference cannot differ from actual cash")
    elif basis == "issuer_confirmed_differential_cash_only_reference":
        formula = reference.get("stated_reference_formula", {})
        total, participating, excluded = [contract._number(formula.get(k)) for k in
            ("total_shares", "participating_shares", "excluded_repurchase_shares")]
        if (formula.get("kind") != "prior_close_minus_issuer_stated_diluted_cash"
                or not formula.get("literal_formula") or formula.get("page") not in range(1, count + 1)
                or any(v != v.to_integral_value() for v in (total, participating, excluded))
                or not 0 < participating < total or total - participating != excluded
                or not 0 < reference_cash <= cash
                or contract._number(formula.get("issuer_stated_diluted_cash_ps")) != reference_cash):
            raise ValueError("Differential reference needs separate issuer-stated diluted cash and formula")
        # The notice's displayed approximation is retained literally.  Do not
        # invent a rounding rule or substitute actual holder cash for it.
    else:
        raise ValueError("Explicit issuer-stated cash reference basis required")
    available = contract._clock(value["review_available_at"])
    sessions, _, mode = _calendar_view(calendar, str(calendar["year"]) + "-01-01",
        str(calendar["year"]) + "-12-31", [])
    eligible = [d for d in sessions if contract._clock(d + "T09:30:00+08:00") >= available]
    if mode != "pinned_static_exchange_calendar" or not eligible:
        raise ValueError("Official supplement needs an exact future knowledge-eligible session")
    return {"action": action, "reference": reference,
        "review_available_at": available.isoformat(), "source_retrieved_at": source_clock.isoformat(),
        "first_knowledge_eligible_session": eligible[0], "review_ref": _ref(path)}


def match_provider(action, raw):
    """A later provider report may corroborate, never revise, official facts."""
    if raw["symbol"] != action["symbol"] or raw["code"] != action["code"]:
        raise ValueError("Later provider report conflicts with official action identity")
    for field in contract.DATES + contract.TERMS:
        value = raw.get(field, "")
        if value == "":
            continue  # Missing provider facts do not replace explicit official terms.
        if field in contract.TERMS:
            equal = contract._number(value) == contract._number(action[field])
        else:
            equal = value == action.get(field, "")
        if not equal:
            raise ValueError("Later provider report conflicts with saved official action terms")


def supplement_entries(bundle, *, created, calendar, refs, reported_keys=(), reported_rows=None, old_keys=()):
    declarations = bundle.get("official_event_supplements", [])
    if not declarations:
        if "official_event_supplement_mode" in bundle:
            raise ValueError("Official supplement mode requires an explicit saved review")
        return []
    if bundle.get("official_event_supplement_mode") not in MODES:
        raise ValueError("Explicit official supplement knowledge mode required")
    entries, keys = [], set()
    for declaration in declarations:
        path = contract._pin(declaration, refs)
        entry = load_review(path, created=created, calendar=calendar, refs=refs)
        action = entry["action"]
        key = action["symbol"], action["dividOperateDate"]
        if (key in keys or (key in set(reported_keys) and key not in (reported_rows or {})) or key in set(old_keys)
                or action["symbol"] not in bundle["scope_symbols"]
                or not bundle["window_start"] <= action["dividRegistDate"] <= action["dividOperateDate"] <= bundle["window_end"]):
            raise ValueError("Official-only event overlaps provider/canonical prefix or escapes scoped window")
        if key in (reported_rows or {}):
            match_provider(action, reported_rows[key])
        keys.add(key)
        entries.append(entry)
    return entries


def build(base_bundle, reviews, out, *, knowledge_mode="prospective_actual_knowledge"):
    """Save a new opt-in bundle; never mutate the ready provider bundle."""
    base_bundle, out = Path(base_bundle).resolve(), Path(out).resolve()
    original = contract._json(base_bundle)
    if out.exists():
        raise FileExistsError("Official supplement output must be a new directory")
    if knowledge_mode not in MODES:
        raise ValueError("Explicit supported official supplement knowledge mode required")
    ready = contract.extension_readiness(base_bundle, Path(original["base_event"]["path"]).parent,
        Path(original["base_reference"]["path"]).parent, original["calendar"]["path"])
    refs = list(ready["refs"])
    calendar = contract._json(original["calendar"]["path"])
    now = datetime.now(MARKET_TZ)
    value = deepcopy(original)
    value["official_event_supplements"] = list(value.get("official_event_supplements", []))
    for review in reviews:
        reference = _ref(review)
        if reference not in value["official_event_supplements"]:
            value["official_event_supplements"].append(reference)
    value.update(created_at=now.isoformat(), known_at=now.isoformat(),
        official_event_supplement_mode=knowledge_mode,
        official_event_supplement_parent=_ref(base_bundle))
    entries = supplement_entries(value, created=now, calendar=calendar, refs=refs,
        reported_keys=[(a["symbol"], a["dividOperateDate"]) for a in original["actions"]])
    protected = set(map(Path, ready["input_directories"])) | {base_bundle.parent}
    protected.update(Path(ref["path"]).parent for ref in refs)
    if any(out == p or out in p.parents or p in out.parents for p in protected):
        raise ValueError("Official supplement output must remain outside all source packages")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="." + out.name + "-", dir=out.parent) as temporary:
        stage = Path(temporary)
        (stage / "bundle.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        result = {"schema": "official_missing_provider_action_bundle_build_v1", "status": "ready",
            "bundle": str(out / "bundle.json"), "created_at": now.isoformat(), "knowledge_mode": knowledge_mode,
            "official_event_count": len(entries), "events": [{k: v for k, v in e.items() if k not in {"action", "reference"}} for e in entries],
            "source_bundle": _ref(base_bundle), "input_refs": refs, "engine_called": False, "ledger_mutated": False, **FLAGS}
        (stage / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        stage.rename(out)
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-bundle", required=True)
    parser.add_argument("--review", action="append", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--knowledge-mode", choices=sorted(MODES), default="prospective_actual_knowledge")
    args = parser.parse_args(argv)
    result = build(args.base_bundle, args.review, args.out, knowledge_mode=args.knowledge_mode)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
