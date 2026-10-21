"""Build a versioned research instrument master from a pinned listing snapshot.

The artifact gives every covered SH/SZ equity a deterministic exchange/code
identity and an explicit observed listing/status record.  It is deliberately
not an execution master: the source is retrieval-time provider metadata, not a
point-in-time issuer, exchange, corporate-action, or delisting authority.
"""

import argparse
import csv
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import uuid

from ..rules import stock_board, stock_quantity_rule
from ..settings import DATA_ROOT, ROOT


SCHEMA = "instrument_master_v1"
STATUS = "completed_research_instrument_master"
REQUIRED_INPUT = {
    "code", "code_name", "ipoDate", "outDate", "type", "status", "symbol",
    "board", "listing_date_known",
}
MASTER_FIELDS = [
    "instrument_id", "canonical_symbol", "provider_code", "name", "exchange",
    "code", "instrument_type", "board", "market_rule_profile",
    "quantity_minimum", "quantity_step", "quantity_maximum",
    "observed_status", "observed_as_of", "provider_ipo_date", "provider_out_date",
    "observed_listing_start", "observed_listing_end_exclusive",
    "status_history_complete", "pit_qualified", "execution_admitted",
]
SCOPE_FIELDS = [
    "provider_code", "canonical_symbol", "instrument_id", "board", "name",
    "included_in_research_scope", "scope_reason", "observed_status",
    "pit_status_known", "execution_admitted", "provider_ipo_date",
    "provider_out_date", "interval_start", "interval_end_exclusive",
]

_EXCHANGE_BY_PROVIDER_PREFIX = {"sh": "SSE", "sz": "SZSE"}


def _text(value):
    return "" if value is None else str(value).strip()


def _date_or_none(value, field):
    value = _text(value)
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be YYYY-MM-DD: {value}") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must use canonical YYYY-MM-DD: {value}")
    return value


def _provider_identity(row):
    provider_code = _text(row.get("code"))
    parts = provider_code.split(".")
    if len(parts) != 2 or parts[0] not in _EXCHANGE_BY_PROVIDER_PREFIX or len(parts[1]) != 6 or not parts[1].isdigit():
        raise ValueError(f"Invalid provider security code: {provider_code}")
    exchange, code = _EXCHANGE_BY_PROVIDER_PREFIX[parts[0]], parts[1]
    symbol = f"{code}.{exchange}"
    declared_symbol = _text(row.get("symbol"))
    if declared_symbol and declared_symbol != symbol:
        raise ValueError(f"Provider/canonical identity mismatch: {provider_code} -> {declared_symbol}")
    return provider_code, code, exchange, symbol


def _record(row, observed_as_of):
    provider_code, code, exchange, symbol = _provider_identity(row)
    if _text(row.get("type")) != "1":
        raise ValueError(f"Unsupported provider instrument type for {provider_code}: {_text(row.get('type'))}")
    board = _text(row.get("board"))
    if board not in {"main", "chinext", "star"}:
        raise ValueError(f"Unknown SH/SZ equity board for {provider_code}: {board or '<blank>'}")
    if stock_board(symbol) != board:
        raise ValueError(f"Board/code mismatch for {provider_code}: {board}")
    if _text(row.get("listing_date_known")).lower() != "true":
        raise ValueError(f"Listing date is not known for {provider_code}")
    ipo_date = _date_or_none(row.get("ipoDate"), "ipoDate")
    out_date = _date_or_none(row.get("outDate"), "outDate")
    if not ipo_date:
        raise ValueError(f"Missing ipoDate for {provider_code}")
    if out_date and out_date <= ipo_date:
        raise ValueError(f"Invalid listing interval for {provider_code}: {ipo_date}..{out_date}")
    raw_status = _text(row.get("status"))
    if raw_status not in {"0", "1"}:
        raise ValueError(f"Unknown provider status for {provider_code}: {raw_status}")
    observed_status = "active_observed" if raw_status == "1" else "inactive_observed"
    quantity = stock_quantity_rule(symbol)
    return {
        "instrument_id": f"{exchange}:{code}",
        "canonical_symbol": symbol,
        "provider_code": provider_code,
        "name": _text(row.get("code_name")),
        "exchange": exchange,
        "code": code,
        "instrument_type": "ordinary_equity",
        "board": board,
        "market_rule_profile": f"stock_{board}_t1",
        "quantity_minimum": str(quantity.minimum),
        "quantity_step": str(quantity.step),
        "quantity_maximum": str(quantity.maximum),
        "observed_status": observed_status,
        "observed_as_of": observed_as_of,
        "provider_ipo_date": ipo_date,
        "provider_out_date": out_date or "",
        "observed_listing_start": ipo_date,
        "observed_listing_end_exclusive": out_date or "",
        "status_history_complete": "false",
        "pit_qualified": "false",
        "execution_admitted": "false",
    }


def _read_rows(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_INPUT - fields)
        if missing:
            raise ValueError(f"Listing snapshot is missing fields: {missing}")
        return list(reader)


def _read_source_exclusions(input_csv):
    path = Path(input_csv).with_name("excluded_instruments.csv")
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"code", "exclusion_reason"}
        if not required <= set(reader.fieldnames or []):
            raise ValueError(f"Source exclusion table is missing fields: {sorted(required - set(reader.fieldnames or []))}")
        rows = []
        seen = set()
        for row in reader:
            code = _text(row.get("code"))
            if code in seen:
                raise ValueError(f"Duplicate source exclusion code: {code}")
            seen.add(code)
            rows.append({"provider_code": code, "reason": f"source:{_text(row.get('exclusion_reason'))}"})
        return rows


def _write_csv(path, fields, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _new_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _source_plan(input_path, explicit):
    candidate = Path(explicit) if explicit else Path(input_path).with_name("collection_plan.json")
    if not candidate.exists():
        return None, {}
    try:
        plan = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid collection plan: {candidate}") from error
    return candidate.resolve(), plan


def build(input_csv, out=None, *, observed_as_of=None, collection_plan=None, source_label="BaoStock"):
    """Build the master and return ``(output_folder, manifest)``."""
    input_csv = Path(input_csv).resolve()
    rows = _read_rows(input_csv)
    source_exclusions = _read_source_exclusions(input_csv)
    plan_path, plan = _source_plan(input_csv, collection_plan)
    observed_as_of = observed_as_of or plan.get("last_calendar_session")
    observed_as_of = _date_or_none(observed_as_of, "observed_as_of")
    if not observed_as_of:
        raise ValueError("observed_as_of is required when the snapshot has no collection plan")
    if plan.get("last_calendar_session") and observed_as_of != plan["last_calendar_session"]:
        raise ValueError("observed_as_of must match the pinned collection plan session")
    if plan.get("source") and plan["source"] != source_label:
        raise ValueError(f"Source label does not match collection plan: {source_label}")

    records = []
    seen = set()
    for row in rows:
        record = _record(row, observed_as_of)
        if record["instrument_id"] in seen:
            raise ValueError(f"Duplicate permanent instrument identity: {record['instrument_id']}")
        seen.add(record["instrument_id"])
        records.append(record)
    records.sort(key=lambda item: item["instrument_id"])

    identifier = Path(out).name if out else _new_id()
    output = Path(out).resolve() if out else ROOT / "outputs" / "instrument_masters" / identifier
    artifacts = DATA_ROOT / "research" / "instrument_masters" / identifier
    if output.exists() or artifacts.exists():
        raise FileExistsError(f"Instrument master output already exists: {output} or {artifacts}")
    output.mkdir(parents=True)
    artifacts.mkdir(parents=True)

    master_name, excluded_name = "instrument_master.csv", "excluded_instruments.csv"
    _write_csv(output / master_name, MASTER_FIELDS, records)
    _write_csv(artifacts / master_name, MASTER_FIELDS, records)
    _write_csv(output / excluded_name, ["provider_code", "reason"], source_exclusions)
    _write_csv(artifacts / excluded_name, ["provider_code", "reason"], source_exclusions)
    source_hash = sha256(input_csv.read_bytes()).hexdigest()
    manifest = {
        "id": identifier,
        "schema": SCHEMA,
        "status": STATUS,
        "created_at": datetime.now().astimezone().isoformat(),
        "source": source_label,
        "source_snapshot": str(input_csv),
        "source_snapshot_sha256": source_hash,
        "collection_plan": str(plan_path) if plan_path else None,
        "observed_as_of": observed_as_of,
        "instrument_count": len(records),
        "excluded_source_count": len(source_exclusions),
        "active_observed": sum(item["observed_status"] == "active_observed" for item in records),
        "inactive_observed": sum(item["observed_status"] == "inactive_observed" for item in records),
        "board_counts": {board: sum(item["board"] == board for item in records) for board in ("main", "chinext", "star")},
        "master_file": master_name,
        "master_sha256": sha256((output / master_name).read_bytes()).hexdigest(),
        "research_only": True,
        "execution_admitted": False,
        "pit_status_history": False,
        "source_reconciled": False,
        "limitations": [
            "Stable identity is exchange plus six-digit code; issuer lineage and corporate identity are not reconciled",
            "Provider listing/status fields are retrieval-time metadata, not point-in-time facts",
            "Only one observed status record is available; historical status intervals are not available",
            "Listing interval is a provider observation and is not a PIT qualification proof",
            "Source scope excludes BSE/CDR and is not independently exchange-reconciled",
            "This master cannot authorize live orders or promote a symbol into the account",
        ],
    }
    for folder in (output, artifacts):
        _write_json(folder / "run.json", {**manifest, "artifacts": str(artifacts)})
    return output, manifest


def load_master(path):
    """Load and strictly validate a generated master CSV or artifact folder."""
    path = Path(path)
    csv_path = path / "instrument_master.csv" if path.is_dir() else path
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != MASTER_FIELDS:
            raise ValueError("Instrument master schema differs from instrument_master_v1")
        rows = list(reader)
    identities = [row["instrument_id"] for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("Instrument master contains duplicate instrument IDs")
    for row in rows:
        if row["exchange"] not in {"SSE", "SZSE"} or len(row["code"]) != 6 or not row["code"].isdigit():
            raise ValueError(f"Invalid exchange/code in instrument master: {row}")
        expected = f"{row['exchange']}:{row['code']}"
        if row["instrument_id"] != expected or row["canonical_symbol"] != f"{row['code']}.{row['exchange']}":
            raise ValueError(f"Instrument identity mismatch: {row}")
        if row["execution_admitted"] != "false" or row["pit_qualified"] != "false":
            raise ValueError("Research master cannot contain admitted records")
    return rows


def compare_coverage(master_path, source_universe, *, alias_old=None, alias_new=None, out=None):
    """Preflight a source universe against the permanent research master.

    This is intentionally a cheap identity check.  It does not widen the
    master, infer issuer lineage, or make a source eligible for execution.
    """
    master_path = Path(master_path).resolve()
    source_universe = Path(source_universe).resolve()
    master_rows = load_master(master_path)
    master_codes = {row["provider_code"] for row in master_rows}
    with source_universe.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if "code" not in set(reader.fieldnames or []):
            raise ValueError("Source universe must contain a code field")
        source_codes = {_text(row.get("code")) for row in reader}
    source_codes.discard("")
    raw_count = len(source_codes)
    if alias_old or alias_new:
        if not alias_old or not alias_new:
            raise ValueError("alias_old and alias_new must be supplied together")
        if alias_old in master_codes:
            master_codes.remove(alias_old)
            master_codes.add(alias_new)
        if alias_old in source_codes:
            source_codes.remove(alias_old)
            source_codes.add(alias_new)
    source_only = sorted(source_codes - master_codes)
    master_only = sorted(master_codes - source_codes)
    excluded_path = master_path.parent / "excluded_instruments.csv"
    exclusions = {}
    if excluded_path.is_file():
        with excluded_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                exclusions[_text(row.get("provider_code"))] = _text(row.get("reason"))
    payload = {
        "schema": "instrument_master_coverage_v1",
        "status": "compatible" if not source_only and not master_only else "scope_mismatch",
        "master": str(master_path),
        "source_universe": str(source_universe),
        "source_raw_count": raw_count,
        "source_after_alias_count": len(source_codes),
        "master_count": len(master_codes),
        "common_count": len(source_codes & master_codes),
        "source_only": source_only,
        "master_only": master_only,
        "excluded_source_only": [
            {"provider_code": code, "reason": exclusions[code]}
            for code in source_only if code in exclusions
        ],
        "alias": {"old": alias_old, "new": alias_new} if alias_old or alias_new else None,
        "research_only": True,
        "execution_admitted": False,
        "limitations": [
            "Coverage is an identity-set comparison, not PIT or exchange reconciliation",
            "A compatible source scope still cannot authorize orders or account use",
        ],
    }
    identifier = Path(out).name if out else _new_id()
    output = Path(out).resolve() if out else ROOT / "outputs" / "instrument_master_coverage" / identifier
    artifacts = DATA_ROOT / "research" / "instrument_master_coverage" / identifier
    if output.exists() or artifacts.exists():
        raise FileExistsError(f"Instrument master coverage output already exists: {output} or {artifacts}")
    output.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    for folder in (output, artifacts):
        _write_json(folder / "coverage.json", {**payload, "artifacts": str(artifacts)})
    return output, payload


def build_research_scope(master_path, source_universe, start, end, *,
                         alias_old=None, alias_new=None, out=None):
    """Materialize a dated research scope from the permanent identity master.

    The source universe remains the authoritative raw scope.  This consumer
    only classifies observed listing overlap for a requested replay interval;
    it never infers PIT eligibility from the retrieval-time status field and
    never grants account or execution permission.
    """
    start = _date_or_none(start, "interval_start")
    end = _date_or_none(end, "interval_end")
    if not start or not end or start > end:
        raise ValueError("Research interval must be ordered YYYY-MM-DD dates")
    if alias_old or alias_new:
        if not alias_old or not alias_new:
            raise ValueError("alias_old and alias_new must be supplied together")
    master_rows = {row["provider_code"]: row for row in load_master(master_path)}
    source_universe = Path(source_universe).resolve()
    with source_universe.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if "code" not in set(reader.fieldnames or []):
            raise ValueError("Source universe must contain a code field")
        source_codes = sorted({_text(row.get("code")) for row in reader if _text(row.get("code"))})
    if alias_old and alias_old in source_codes:
        source_codes = sorted((set(source_codes) - {alias_old}) | {alias_new})

    rows = []
    for provider_code in source_codes:
        master_code = provider_code
        row = master_rows.get(master_code)
        if row is None:
            rows.append({
                "provider_code": provider_code, "canonical_symbol": "", "instrument_id": "",
                "board": "", "name": "", "included_in_research_scope": "false",
                "scope_reason": "missing_instrument_master", "observed_status": "",
                "pit_status_known": "false", "execution_admitted": "false",
                "provider_ipo_date": "", "provider_out_date": "",
                "interval_start": start, "interval_end_exclusive": end,
            })
            continue
        ipo, out_date = row["provider_ipo_date"], row["provider_out_date"]
        overlaps = bool(ipo and ipo <= end and (not out_date or out_date > start))
        reason = "observed_listing_interval_overlaps_replay"
        if not ipo:
            reason = "listing_start_unknown"
        elif ipo > end:
            reason = "listed_after_replay_end"
        elif out_date and out_date <= start:
            reason = "delisted_before_replay_start"
        if row["observed_status"] != "active_observed" and overlaps:
            reason = "observed_overlap_status_not_active_pit_unknown"
        rows.append({
            "provider_code": provider_code,
            "canonical_symbol": row["canonical_symbol"],
            "instrument_id": row["instrument_id"], "board": row["board"],
            "name": row["name"], "included_in_research_scope": str(overlaps).lower(),
            "scope_reason": reason, "observed_status": row["observed_status"],
            "pit_status_known": "false", "execution_admitted": "false",
            "provider_ipo_date": ipo, "provider_out_date": out_date,
            "interval_start": start, "interval_end_exclusive": end,
        })
    rows.sort(key=lambda item: item["provider_code"])
    identifier = Path(out).name if out else _new_id()
    output = Path(out).resolve() if out else ROOT / "outputs" / "research_scopes" / identifier
    artifacts = DATA_ROOT / "research" / "research_scopes" / identifier
    if output.exists() or artifacts.exists():
        raise FileExistsError(f"Research scope output already exists: {output} or {artifacts}")
    output.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    source_hash = sha256(source_universe.read_bytes()).hexdigest()
    master_csv = Path(master_path) / "instrument_master.csv" if Path(master_path).is_dir() else Path(master_path)
    master_hash = sha256(master_csv.read_bytes()).hexdigest()
    manifest = {
        "id": identifier, "schema": "research_scope_v1",
        "status": "completed_research_scope", "created_at": datetime.now().astimezone().isoformat(),
        "master": str(Path(master_path).resolve()), "master_sha256": master_hash,
        "source_universe": str(source_universe), "source_universe_sha256": source_hash,
        "interval_start": start, "interval_end": end,
        "alias": {"old": alias_old, "new": alias_new} if alias_old or alias_new else None,
        "source_count_after_alias": len(source_codes),
        "included_count": sum(row["included_in_research_scope"] == "true" for row in rows),
        "excluded_count": sum(row["included_in_research_scope"] != "true" for row in rows),
        "missing_master_count": sum(row["scope_reason"] == "missing_instrument_master" for row in rows),
        "inactive_observed_overlap_count": sum(
            row["scope_reason"] == "observed_overlap_status_not_active_pit_unknown" for row in rows),
        "research_only": True, "pit_qualified": False, "execution_admitted": False,
        "scope_file": "research_scope.csv",
        "limitations": [
            "Observed listing intervals are used only to classify replay overlap",
            "Provider status is retrieval-time metadata and is not a point-in-time status history",
            "pit_status_known=false for every row; this scope cannot authorize account use",
            "Missing master identities remain explicit and are not silently dropped from the source audit",
        ],
    }
    for folder in (output, artifacts):
        _write_csv(folder / "research_scope.csv", SCOPE_FIELDS, rows)
        _write_json(folder / "run.json", {**manifest, "artifacts": str(artifacts)})
    return output, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Pinned listing/universe CSV")
    parser.add_argument("--out", type=Path, help="Repository-visible output folder")
    parser.add_argument("--observed-as-of", help="Observed status date; defaults to collection plan session")
    parser.add_argument("--collection-plan", type=Path)
    parser.add_argument("--source", default="BaoStock")
    args = parser.parse_args(argv)
    folder, manifest = build(args.input, args.out, observed_as_of=args.observed_as_of,
                             collection_plan=args.collection_plan, source_label=args.source)
    print(json.dumps({"folder": str(folder), "status": manifest["status"],
                      "instrument_count": manifest["instrument_count"]}, ensure_ascii=False), flush=True)
    return 0


def coverage_main(argv=None):
    parser = argparse.ArgumentParser(description="Compare a source universe with an instrument master")
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--source-universe", type=Path, required=True)
    parser.add_argument("--alias-old")
    parser.add_argument("--alias-new")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    folder, payload = compare_coverage(
        args.master, args.source_universe, alias_old=args.alias_old,
        alias_new=args.alias_new, out=args.out,
    )
    print(json.dumps({"folder": str(folder), "status": payload["status"],
                      "source_only": payload["source_only"],
                      "master_only": payload["master_only"]}, ensure_ascii=False), flush=True)
    return 0


def scope_main(argv=None):
    parser = argparse.ArgumentParser(description="Materialize a dated research scope from instrument_master_v1")
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--source-universe", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--alias-old")
    parser.add_argument("--alias-new")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    folder, manifest = build_research_scope(
        args.master, args.source_universe, args.start, args.end,
        alias_old=args.alias_old, alias_new=args.alias_new,
        out=Path(args.out) if args.out else None,
    )
    print(json.dumps({"folder": str(folder), "status": manifest["status"],
                      "included_count": manifest["included_count"],
                      "excluded_count": manifest["excluded_count"],
                      "execution_admitted": False}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
