"""Consume pinned action sources and dated identities without admitting a trading feed."""
import argparse
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import shutil
import uuid

import pandas as pd
import pyarrow.parquet as pq

from ..market_dataset import normalize_symbol, write_frame, write_json
from ..market_snapshot import documented_price_adjustments, load_action_resolutions, reconcile_provider_actions, supplement_actions
from ..rules import holder_restructuring_ex_price, restructuring_ex_price
from ..settings import DATA_ROOT
from .adjusted_prices import checked_file, numeric
from .price_boundaries import EFFECTIVE, NEW, OLD, RENAME_SHA256, validate_rename


def project_actions(actions, start, end, rename, resolutions=()):
    validate_rename(rename)
    if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end or start > end:
        raise ValueError("Canonical ordered action window required")
    fields = {"code", "symbol", "dividOperateDate", "dividRegistDate", "dividPayDate", "dividStockMarketDate",
              "dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs", "_cache_file"}
    if not fields <= set(actions):
        raise ValueError("Complete source action schema required; no invented missing terms")
    result = actions.copy(deep=True)
    if not result.symbol.eq(result.code.map(lambda code: normalize_symbol(code).vt_symbol)).all():
        raise ValueError("Source action code/symbol identity mismatch")
    for field in ("dividOperateDate", "dividRegistDate", "dividPayDate", "dividStockMarketDate"):
        for value in result[field]:
            if value == "" and field != "dividOperateDate":
                continue
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError("Noncanonical action date")
    for field in ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"):
        if any(numeric(value) < 0 for value in result[field] if value != ""):
            raise ValueError("Negative reported distribution term")
    result["source_code"], result["source_symbol"] = result.code, result.symbol
    alias = result.code.isin([OLD, NEW])
    result["security_id"] = result.code.where(~alias, OLD)
    result["identity_origin"] = "unresolved_provider_code"
    result.loc[alias, "identity_origin"] = "reviewed_dated_code_change"
    result.loc[alias, "code"] = NEW
    result["symbol"] = result.code.map(lambda code: normalize_symbol(code).vt_symbol)
    for source, target in (("dividOperateDate", "tradable_code"), ("dividRegistDate", "registration_code"),
                           ("dividPayDate", "payment_code"), ("dividStockMarketDate", "stock_listing_code")):
        result[target] = result.code.where(result[source].ne(""), "")
        present = alias & result[source].ne("")
        result.loc[present, target] = result.loc[present, source].map(lambda day: OLD if day < EFFECTIVE else NEW)
    within = result.dividOperateDate.between(start, end)
    outside = result.loc[~within].copy()
    observed = set(zip(result.loc[within, "symbol"], result.loc[within, "dividOperateDate"]))
    used_resolutions = [row for row in resolutions if (row["symbol"], row["dividOperateDate"]) in observed]
    canonical, audit = reconcile_provider_actions(result.loc[within], used_resolutions)
    return result, canonical, outside, audit


def build(view, levels, disclosure, supplement_root, destination=None, *, secondary_terms=None):
    view, levels, supplement_root = Path(view), Path(levels), Path(supplement_root).resolve()
    result = json.loads((view / "result.json").read_text(encoding="utf-8"))
    if result["status"] not in {"source_actions_partial", "source_actions_complete"}:
        raise ValueError("Pinned source action view required")
    paths = {"reported_actions.parquet": result["panel_sha256"], "plan.json": result["plan_sha256"],
             "coverage.csv": result["coverage_sha256"], "input_receipts.json": result["input_receipts_sha256"],
             "implementation.py": result["implementation_sha256"]}
    for name, digest in paths.items():
        checked_file(view, name, digest)
    plan = json.loads((view / "plan.json").read_text(encoding="utf-8"))
    coverage = pd.read_csv(view / "coverage.csv", dtype={"code": str, "symbol": str, "year": int})
    years = range(int(plan["start"][:4]), int(plan["end"][:4]) + 1)
    universe = pd.read_csv(view / "input_universe.csv", dtype=str, keep_default_na=False)
    expected = {(code, year) for code in universe.code for year in years}
    if (len(universe) != result["expected_codes"] or universe.code.duplicated().any()
            or len(coverage) != result["expected_queries"] or len(coverage) != len(expected)
            or coverage.duplicated(["code", "year"]).any() or set(zip(coverage.code, coverage.year)) != expected
            or not coverage.queried.isin([True, False]).all()
            or int(coverage.queried.sum()) != result["queried"]
            or int((~coverage.queried).sum()) != result["unqueried"]
            or not coverage.loc[~coverage.queried, "rows"].isna().all()
            or not coverage.symbol.eq(coverage.code.map(lambda code: normalize_symbol(code).vt_symbol)).all()):
        raise ValueError("Full source code/year matrix changed; unqueried is not an empty response")
    receipts = json.loads((view / "input_receipts.json").read_text(encoding="utf-8"))
    if (set(universe.symbol) != set(receipts)
            or not universe.symbol.eq(universe.code.map(lambda code: normalize_symbol(code).vt_symbol)).all()
            or int(coverage.groupby("code").queried.all().sum()) != result["complete_codes"]):
        raise ValueError("Pinned source identity/complete-code count changed")
    for row in coverage.itertuples(index=False):
        entry = receipts[row.symbol]["tasks"].get(f"actions_{row.year}")
        if bool(entry) != row.queried or (entry and (
                entry["sha256"] != row.source_sha256 or entry["path"] != row.source_file
                or entry["quality"]["rows"] != row.rows or (row.rows == 0) != row.empty_response)):
            raise ValueError("Query matrix no longer matches pinned source receipts")
    actions = pd.read_parquet(view / "reported_actions.parquet")
    if (len(actions) != result["rows"] or len(actions) != int(coverage.loc[coverage.queried, "rows"].sum())
            or not set(actions.code) <= set(universe.code)):
        raise ValueError("Source action row/code scope changed")
    identity = json.loads((levels / "result.json").read_text(encoding="utf-8"))
    if identity["status"] != "derived_research_levels" or Path(identity["raw_view"]).resolve() != Path(result["raw_view"]).resolve():
        raise ValueError("Action identities must use the same pinned raw scope as prices")
    validate_rename(identity["rename"])
    if sha256(Path(disclosure).read_bytes()).hexdigest() != RENAME_SHA256:
        raise ValueError("Reviewed rename disclosure changed")
    symbols = universe.symbol.tolist()
    resolutions = load_action_resolutions(supplement_root, symbols)
    projected, canonical, outside, audit = project_actions(actions, plan["start"], plan["end"], identity["rename"], resolutions)
    provider_events = len(canonical)
    canonical, evidence = supplement_actions(supplement_root, canonical, symbols)
    if not canonical.dividOperateDate.between(plan["start"], plan["end"]).all():
        raise ValueError("Reviewed supplements contain events outside the pinned action window")
    for row in evidence:
        key = canonical.symbol.eq(row["symbol"]) & canonical.dividOperateDate.eq(row["dividOperateDate"])
        if "reference_adjustment_cash_ps" in row:
            canonical.loc[key, "reference_adjustment_cash_ps"] = row["reference_adjustment_cash_ps"]
    if evidence:
        # Apply the same dated identity to additions without rewriting provider provenance.
        additions = canonical.source_code.eq("") if "source_code" in canonical else pd.Series(True, index=canonical.index)
        if additions.any():
            extra = canonical.loc[additions].copy()
            extra["_cache_file"] = extra.source_file
            extra_projected, _, _, _ = project_actions(extra, plan["start"], plan["end"], identity["rename"])
            for field in extra_projected:
                canonical.loc[extra.index, field] = extra_projected[field]
            canonical = canonical.drop(columns="_cache_file", errors="ignore")
    canonical = canonical.fillna("")
    if canonical.duplicated(["symbol", "dividOperateDate"]).any():
        raise ValueError("Canonical events still contain duplicate credit identities")
    secondary_receipts = []
    secondary_roots = ([] if secondary_terms is None else
                       [Path(secondary_terms)] if isinstance(secondary_terms, (str, Path)) else
                       [Path(root) for root in secondary_terms])
    if len({root.resolve() for root in secondary_roots}) != len(secondary_roots):
        raise ValueError("Duplicate secondary source bundle")
    if secondary_roots:
        from .action_terms import apply_reported_terms
        for root in secondary_roots:
            canonical, receipts = apply_reported_terms(canonical, root)
            if {(r["symbol"], r["date"]) for r in secondary_receipts} & {(r["symbol"], r["date"]) for r in receipts}:
                raise ValueError("Overlapping secondary completions need an explicit source reconciliation")
            secondary_receipts.extend(receipts)
    folder = Path(destination) if destination else DATA_ROOT / "research/stock_action_events" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    for name in paths:
        shutil.copy2(view / name, folder / ("input_" + name))
    for name in ("input_universe.csv", "input_calendar.csv", "result.json"):
        shutil.copy2(view / name, folder / ("source_" + name))
    for source, name in ((Path(__file__), "implementation.py"), (Path(__file__).parents[1] / "market_snapshot.py", "reconciliation_implementation.py"),
                         (Path(__file__).with_name("price_boundaries.py"), "identity_implementation.py"),
                         (levels / "result.json", "price_identity.json"), (Path(disclosure), "rename_disclosure.pdf")):
        shutil.copy2(source, folder / name)
    if secondary_roots:
        for index, root in enumerate(secondary_roots):
            name = "secondary_reported_terms" if index == 0 else f"secondary_reported_terms_{index}"
            shutil.copytree(root, folder / name)
        shutil.copy2(Path(__file__).with_name("action_terms.py"), folder / "secondary_terms_consumer.py")
        write_json(folder / "secondary_term_completions.json", secondary_receipts)
    for row in evidence + resolutions:
        for prefix in ("", "plan_"):
            if not row.get(prefix + "source_file"):
                continue
            source = supplement_root / row[prefix + "source_file"]
            target = folder / row[prefix + "source_file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            digest = sha256(target.read_bytes()).hexdigest()
            if row.get(prefix + "source_sha256") and row[prefix + "source_sha256"] != digest:
                raise ValueError("Issuer resolution source changed during publication")
            row[prefix + "source_sha256"] = digest
    write_json(folder / "provider_action_duplicates.json", audit)
    write_json(folder / "issuer_resolutions.json", resolutions)
    write_json(folder / "cash_supplements.json", evidence)
    write_frame(folder / "projected_source_actions.parquet", projected)
    write_frame(folder / "outside_window_actions.parquet", outside)
    write_frame(folder / "canonical_actions.parquet", canonical)
    output = {"status": "research_action_events_projected", "source_view": str(view), "raw_view": result["raw_view"],
              "source_status": result["status"], "start": plan["start"], "end": plan["end"],
              "expected_codes": result["expected_codes"], "complete_source_codes": result["complete_codes"],
              "expected_queries": result["expected_queries"], "queried": result["queried"], "unqueried": result["unqueried"],
              "source_rows": len(projected), "outside_window_rows": len(outside), "provider_events": provider_events,
              "duplicate_event_groups": sum(len(row["source_rows"]) > 1 for row in audit),
              "issuer_corrected_event_groups": sum(row["status"] == "issuer_corrected_cash_event" for row in audit),
              "issuer_reserve_bonus_corrections": sum(row["status"] == "issuer_corrected_reserve_bonus_event" for row in audit),
              "issuer_cash_only_term_completions": sum(row["status"] == "issuer_completed_cash_only_terms" for row in audit),
              "issuer_reserve_zero_term_completions": sum(row["status"] == "issuer_completed_reserve_zero_term" for row in audit),
              "issuer_single_cash_precision_corrections": sum(row["status"] == "issuer_corrected_cash_only_precision" for row in audit),
              "issuer_distribution_corrections": sum(row["status"] == "issuer_corrected_distribution_event" for row in audit),
              "issuer_combined_event_groups": sum(row["status"] == "issuer_combined_cash_event" for row in audit),
              "issuer_combined_cash_precision_corrections": sum(
                  row.get("issuer_resolution", {}).get("kind") == "issuer_combined_cash_correction" for row in audit),
              "supplements_added": sum(row["status"] == "added_missing_cash_dividend" for row in evidence),
              "supplements_matching": sum(row["status"] == "matches_provider_not_added" for row in evidence),
              "canonical_events": len(canonical), "dated_alias_source_rows": int(projected.security_id.eq(OLD).sum()),
              "missing_registration_dates": int(canonical.dividRegistDate.eq("").sum()),
              "missing_payment_dates": int(canonical.dividPayDate.eq("").sum()),
              "secondary_reported_reserve_completions": len(secondary_receipts),
              "secondary_terms_source": str(secondary_roots[0].resolve()) if secondary_roots else None,
              "secondary_terms_sources": [str(root.resolve()) for root in secondary_roots],
              "execution_admitted": False, "cash_share_actions_verified": False,
              "files": {path.relative_to(folder).as_posix(): sha256(path.read_bytes()).hexdigest() for path in sorted(folder.rglob("*")) if path.is_file()},
              "limitations": ["Partial source coverage stays partial; all code/year pending identities retained",
                              "Duplicate reconciliation reuses the existing consumer; no new cash/share credit path",
                              "Only one evidenced code rename; no permanent security master or account migration",
                              "Post-cutoff and pre-window reported rows retained separately, never credited",
                              "Unreviewed blank terms preserved; original rows/tax strings are not actual account tax receipts",
                              *(["Opt-in secondary reported reserve zeros are diagnostic inputs, not issuer-confirmed terms"] if secondary_receipts else []),
                              "Cash entitlement is not always the ex-price adjustment; raw reference concordance remains required",
                              "No PIT availability, independent whole-market event coverage or default/account/feed changes"]}
    write_json(folder / "result.json", output)
    return folder, output


def amend_secondary(event_view, reference_view, secondary_root, events, destination=None, reference_destination=None):
    """Apply specific archived terms and refresh only their frozen reference rows."""
    from .action_terms import apply_reported_terms, day
    event_view, reference_view, secondary_root = (Path(p).resolve() for p in (event_view, reference_view, secondary_root))
    before = json.loads((event_view / "result.json").read_text(encoding="utf-8"))
    references = json.loads((reference_view / "result.json").read_text(encoding="utf-8"))
    source = json.loads((secondary_root / "result.json").read_text(encoding="utf-8"))
    keys = [(normalize_symbol(symbol).vt_symbol, day(ex)) for symbol, ex in events]
    if (not keys or len(keys) != len(set(keys)) or before["status"] != "research_action_events_projected"
            or references["status"] != "research_reference_presence_diagnostic"
            or Path(references["event_view"]).resolve() != event_view
            or before["execution_admitted"] or references["execution_admitted"]
            or source["status"] != "completed_secondary_action_terms" or source["execution_admitted"]):
        raise ValueError("Specific unique events and aligned unadmitted terminal sources required")
    selected, found = [], []
    for row in source["records"]:
        candidates = [c for c in row.get("candidates", []) if (c["symbol"], c["date"]) in keys]
        if candidates:
            if row["status"] != "source_histories_compared" or any(c["symbol"] != row["symbol"] for c in candidates):
                raise ValueError("Selected terms require their completed source histories")
            selected.append({**deepcopy(row), "candidates": candidates, "pending": []})
            found.extend((c["symbol"], c["date"]) for c in candidates)
    if sorted(found) != sorted(keys):
        raise ValueError("Every selected event needs exactly one matched archived candidate")
    actions = pd.read_parquet(checked_file(event_view, "canonical_actions.parquet", before["files"]["canonical_actions.parquet"]))
    original_references = pd.read_parquet(checked_file(reference_view, "raw_reference_changes.parquet",
                                                     references["files"]["raw_reference_changes.parquet"]))
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    folder = Path(destination) if destination else DATA_ROOT / "research/stock_action_events" / identifier
    ref_folder = Path(reference_destination) if reference_destination else DATA_ROOT / "research/stock_action_references" / identifier
    if folder.resolve() == ref_folder.resolve():
        raise ValueError("Event and reference artifacts need distinct destinations")
    if folder.exists() or ref_folder.exists():
        raise FileExistsError("Amendments require new artifact folders, never overwrite a saved snapshot")
    scoped = folder / ("secondary_amendment_" + identifier)
    scoped.mkdir(parents=True, exist_ok=False)
    selected_source = {**source, "records": selected, "selected_symbols": [r["symbol"] for r in selected],
        "candidate_events": len(keys), "source_errors": 0, "pending_selected_events": 0,
        "source_selection": {"source_root": str(secondary_root), "events": keys,
            "basis": "Explicit data-blocker events, not a strategy universe or performance selection"}}
    for row in selected:
        for kind in ("primary", "secondary"):
            name = row[kind + "_file"]
            target = scoped / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(checked_file(secondary_root, name, row[kind + "_sha256"]), target)
    write_json(scoped / "result.json", selected_source)
    amended, receipts = apply_reported_terms(actions.fillna(""), scoped)
    # The external receipt carries the new provenance without changing past panel schema.
    amended = amended.loc[:, actions.columns]
    updated = original_references.copy(deep=True)
    mask = pd.Series([(normalize_symbol(code).vt_symbol, ex) in keys
                      for code, ex in zip(updated.code, updated.date)], index=updated.index)
    formula_fields = ["reference_formula_status", "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb"]
    if mask.any():
        selected_rows = updated.loc[mask].drop(columns=formula_fields).copy()
        for index, row in selected_rows.iterrows():
            action = amended.loc[amended.code.eq(row.code) & amended.dividOperateDate.eq(row.date)]
            if len(action) != 1:
                raise ValueError("Selected reference requires one canonical event")
            for field in actions.columns.intersection(selected_rows.columns):
                selected_rows.loc[index, field] = action.iloc[0][field]
        def evidence(name):
            return json.loads(checked_file(reference_view, name, references["files"][name]).read_text(encoding="utf-8"))
        refreshed = reference_concordance(selected_rows, evidence("reviewed_special_references.json"),
                                          evidence("cash_reference_evidence.json") + evidence("distribution_reference_evidence.json"))
        updated.loc[mask, updated.columns] = refreshed.loc[:, updated.columns].to_numpy()
    shutil.copytree(event_view, folder, dirs_exist_ok=True)
    shutil.copytree(reference_view, ref_folder)
    write_frame(folder / "canonical_actions.parquet", amended)
    write_frame(ref_folder / "raw_reference_changes.parquet", updated)
    previous_receipts = (json.loads((event_view / "secondary_term_completions.json").read_text(encoding="utf-8"))
                         if "secondary_term_completions.json" in before["files"] else [])
    write_json(folder / "secondary_term_completions.json", previous_receipts + receipts)
    implementation = "secondary_amendment_implementation_" + identifier + ".py"
    shutil.copy2(Path(__file__), folder / implementation)
    amendment = {"parent_event_view": str(event_view), "parent_reference_view": str(reference_view),
                 "source_root": str(secondary_root), "events": keys, "reference_rows_refreshed": int(mask.sum()),
                 "raw_prices_rescanned": False, "other_events_changed": False, "issuer_confirmed": False}
    output = {**before, "secondary_amendment": amendment,
        "secondary_reported_reserve_completions": before.get("secondary_reported_reserve_completions", 0) + len(receipts),
        "secondary_terms_sources": [*before.get("secondary_terms_sources", []), str(scoped.resolve())],
        "files": dict(before["files"]),
        "limitations": [*before["limitations"], "Selected secondary terms are matched research inputs, not issuer confirmation or execution admission"]}
    for path in [folder / "canonical_actions.parquet", folder / "secondary_term_completions.json", folder / implementation,
                 *scoped.rglob("*")]:
        if path.is_file():
            output["files"][path.relative_to(folder).as_posix()] = sha256(path.read_bytes()).hexdigest()
    write_json(folder / "result.json", output)
    shutil.copy2(folder / "result.json", ref_folder / "input_events.json")
    amended_references = {**references, "event_view": str(folder.resolve()), "secondary_amendment": amendment,
        "reference_formula_status_counts": updated.reference_formula_status.value_counts().to_dict(),
        "reference_formula_basis_counts": updated.reference_formula_basis.value_counts().to_dict(),
        "files": dict(references["files"])}
    for name in ("raw_reference_changes.parquet", "input_events.json"):
        amended_references["files"][name] = sha256((ref_folder / name).read_bytes()).hexdigest()
    write_json(ref_folder / "result.json", amended_references)
    return folder, ref_folder, output, amended_references


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--view", type=Path)
    source.add_argument("--amend-view", type=Path, help="Apply specific archived terms without rebuilding prices or unrelated events")
    parser.add_argument("--reference-view", type=Path)
    parser.add_argument("--only-event", action="append", help="Explicit SYMBOL:EX_DATE for a scoped amendment")
    parser.add_argument("--levels", type=Path)
    parser.add_argument("--rename-disclosure", type=Path)
    parser.add_argument("--supplement-root", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--secondary-terms", type=Path, action="append", help="Repeat for disjoint archived source additions; research only, no active supplement/admission change")
    args = parser.parse_args(argv)
    if args.amend_view:
        if (not args.reference_view or not args.only_event or len(args.secondary_terms or []) != 1
                or any((args.levels, args.rename_disclosure, args.supplement_root))):
            parser.error("Amendment needs aligned --reference-view, one --secondary-terms and --only-event; no rebuild inputs")
        keys = [event.rsplit(":", 1) for event in args.only_event]
        if any(len(key) != 2 for key in keys):
            parser.error("Expected canonical SYMBOL:EX_DATE")
        folder, ref_folder, result, _ = amend_secondary(args.amend_view, args.reference_view,
                                                        args.secondary_terms[0], keys, args.out)
        print(json.dumps({"folder": str(folder), "reference_folder": str(ref_folder),
                          "amendment": result["secondary_amendment"]}))
        return 0
    if not all((args.levels, args.rename_disclosure, args.supplement_root)) or args.reference_view or args.only_event:
        parser.error("Rebuild needs --view, --levels, --rename-disclosure and --supplement-root only")
    folder, result = build(args.view, args.levels, args.rename_disclosure, args.supplement_root, args.out,
                           secondary_terms=args.secondary_terms)
    print(json.dumps({"folder": str(folder), **result}, indent=2))
    return 0


def reference_changes(raw, previous):
    values = raw.copy()
    for field in ("close", "preclose", "tradestatus"):
        values[field] = pd.to_numeric(values[field], errors="coerce")
    values["previous_observed_close"] = values.code.map(previous)
    values["reference_delta"] = values.preclose - values.previous_observed_close
    gaps = values.loc[values.reference_delta.abs().gt(.021) & values.tradestatus.eq(1)
                      & values.previous_observed_close.gt(0)].copy()
    previous.update(dict(zip(values.code, values.close)))
    return gaps


def reference_concordance(gaps, special, cash_evidence=()):
    """Check stated terms, not account entitlement or independent event completeness."""
    specials = {(normalize_symbol(row["symbol"]).baostock_code, row["date"]): row for row in special}
    cash = {(row["code"], row["dividOperateDate"]): row for row in cash_evidence}
    if len(specials) != len(special) or len(cash) != len(cash_evidence):
        raise ValueError("Duplicate reference evidence")
    output = []
    fields = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")
    for row in gaps.to_dict("records"):
        key = row["code"], row["date"]
        prior, actual = Decimal(str(row["previous_observed_close"])), Decimal(str(row["preclose"]))
        if not all(v.is_finite() and v > 0 for v in (prior, actual)):
            raise ValueError("Reference concordance needs finite positive raw quotes")
        expected, basis = None, ""
        if key in specials:
            item = specials[key]
            basis = "reviewed_special_reference"
            if item["kind"] == "rights_issue":
                ratio, price = Decimal(item["rights_ratio"]), Decimal(item["subscription_price"])
                expected = (prior + ratio * price) / (1 + ratio)
            elif item["kind"] == "restructuring_holder_distribution":
                expected = holder_restructuring_ex_price(prior, item)
            else:
                expected = restructuring_ex_price(prior, item)
        elif key in cash:
            evidence = cash[key]
            if Decimal(str(row.get(fields[0], ""))) != Decimal(evidence[fields[0]]):
                raise ValueError("Reviewed reference no longer matches canonical entitlement")
            share_ratio = evidence.get("reference_adjustment_share_ratio", "")
            if share_ratio:
                if any(Decimal(str(row.get(field, ""))) != Decimal(evidence[field]) for field in fields[1:]):
                    raise ValueError("Reviewed reference no longer matches canonical entitlement")
                reference_ratio = Decimal(share_ratio)
            elif all(Decimal(evidence[field]) == 0 for field in fields[1:]):
                reference_ratio = Decimal("0")
            else:
                raise ValueError("Reviewed stock distribution needs a separate reference share ratio")
            if not reference_ratio.is_finite() or reference_ratio < 0:
                raise ValueError("Invalid reviewed reference share ratio")
            reference_cash = evidence.get("reference_adjustment_cash_ps", "")
            if reference_cash:
                value = Decimal(reference_cash)
                if not value.is_finite() or value < 0:
                    raise ValueError("Invalid reviewed reference cash")
                expected = (prior - value) / (1 + reference_ratio)
                basis = "reviewed_distribution_reference" if reference_ratio else "reviewed_cash_reference"
        elif row["has_reported_distribution"]:
            if all(isinstance(row.get(field), str) and row[field] != "" for field in fields):
                values = [Decimal(row[field]) for field in fields]
                if not all(value.is_finite() and value >= 0 for value in values):
                    raise ValueError("Invalid reported reference terms")
                expected = (prior - values[0]) / (1 + values[1] + values[2])
                basis = "reported_entitlement_terms_not_independent_reference"
        error = expected - actual if expected is not None else None
        if expected is None:
            status = "unresolved_blank_terms" if row["has_reported_distribution"] else "missing_source_event"
        else:
            if not expected.is_finite() or expected <= 0:
                raise ValueError("Nonpositive computed reference price")
            status = "reference_match" if abs(error) <= Decimal("0.011") else "reference_mismatch"
        output.append({"reference_formula_status": status, "reference_formula_basis": basis,
                       "expected_reference_rmb": str(expected) if expected is not None else "",
                       "reference_error_rmb": float(error) if error is not None else None})
    columns = ["reference_formula_status", "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb"]
    return pd.concat([gaps.reset_index(drop=True), pd.DataFrame(output, columns=columns)], axis=1)


def distribution_reference_evidence(root, actions, start, end):
    path = Path(root) / "supplements/distribution_references.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    keys = [(row["code"], row["dividOperateDate"]) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate distribution reference")
    result = []
    for row in rows:
        if not start <= row["dividOperateDate"] <= end:
            continue
        if normalize_symbol(row["code"]).vt_symbol != row["symbol"]:
            raise ValueError("Distribution reference code/symbol conflict")
        chronology = [date.fromisoformat(row[field]) for field in
                      ("source_date", "dividRegistDate", "dividOperateDate")]
        if row["dividPayDate"]:
            chronology.append(date.fromisoformat(row["dividPayDate"]))
        elif Decimal(row["dividCashPsBeforeTax"]) != 0:
            raise ValueError("Cash distribution reference needs a payment date")
        if (chronology[0] > chronology[2] or chronology[1] >= chronology[2]
                or (len(chronology) == 4 and chronology[3] < chronology[2])):
            raise ValueError("Distribution reference chronology conflict")
        stock_date = row["dividStockMarketDate"]
        has_shares = any(Decimal(row[field]) != 0 for field in ("dividStocksPs", "dividReserveToStockPs"))
        if (has_shares and not stock_date) or (stock_date and date.fromisoformat(stock_date) < chronology[2]):
            raise ValueError("Distribution reference listing precedes ex date")
        if not row["source_url"].startswith("https://"):
            raise ValueError("Distribution reference needs primary source URL")
        from .action_disclosures import ISSUER_IMPLEMENTATION_NOTICES, validate_primary_notice_source
        if row.get("source_channel") == "issuer_linked_archive" or row["source_url"] in ISSUER_IMPLEMENTATION_NOTICES:
            validate_primary_notice_source(row, root)
        source = checked_file(root, row["source_file"], row["source_sha256"])
        if not source.read_bytes().startswith(b"%PDF-"):
            raise ValueError("Distribution reference source is not a PDF")
        reference_fields = ("reference_source_date", "reference_source_url", "reference_source_file",
                            "reference_source_sha256", "reference_source_pages")
        if any(field in row for field in reference_fields):
            if (any(not isinstance(row.get(field), str) or not row[field] for field in reference_fields[:-1])
                    or not isinstance(row.get("reference_source_pages"), list) or not row["reference_source_pages"]
                    or any(type(page) is not int or page < 1 for page in row["reference_source_pages"])):
                raise ValueError("Separate distribution reference source is incomplete")
            if (not row["reference_source_url"].startswith("https://")
                    or date.fromisoformat(row["reference_source_date"]) > chronology[2]
                    or row["reference_source_file"] == row["source_file"]):
                raise ValueError("Separate distribution reference source URL, date or identity conflicts")
            reference_source = checked_file(root, row["reference_source_file"], row["reference_source_sha256"])
            if not reference_source.read_bytes().startswith(b"%PDF-"):
                raise ValueError("Separate distribution reference source is not a PDF")
        actual = actions.loc[actions.code.eq(row["code"]) & actions.dividOperateDate.eq(row["dividOperateDate"])]
        if len(actual) != 1:
            raise ValueError("Distribution reference needs exactly one existing canonical action")
        for field in ("dividRegistDate", "dividPayDate", "dividStockMarketDate"):
            if actual.iloc[0][field] != row[field]:
                raise ValueError("Distribution reference no longer matches canonical dates")
        for field in ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"):
            value = Decimal(row[field])
            if (not value.is_finite() or value < 0 or actual.iloc[0][field] == ""
                    or Decimal(actual.iloc[0][field]) != value):
                raise ValueError("Distribution reference no longer matches canonical entitlement")
        for field in ("reference_adjustment_cash_ps", "reference_adjustment_share_ratio"):
            value = Decimal(row[field])
            if not value.is_finite() or value < 0:
                raise ValueError("Invalid reviewed distribution reference terms")
        result.append(row)
    return result


def check_references(event_view, price_view, supplement_root, destination=None):
    event_view, price_view = Path(event_view), Path(price_view)
    events = json.loads((event_view / "result.json").read_text(encoding="utf-8"))
    prices = json.loads((price_view / "result.json").read_text(encoding="utf-8"))
    if (events["status"] != "research_action_events_projected" or prices["status"] != "research_reconstruction_completed"
            or Path(events["raw_view"]).resolve() != Path(prices["raw_view"]).resolve()):
        raise ValueError("Pinned event and price views must share the same complete raw scope")
    validate_rename(prices["rename"])
    raw_view = Path(prices["raw_view"])
    raw_path = checked_file(raw_view, "raw_daily.parquet", prices["source_hashes"]["raw"])
    canonical_path = checked_file(price_view, prices["canonical_panel_file"], prices["canonical_panel_sha256"])
    actions = pd.read_parquet(checked_file(event_view, "canonical_actions.parquet", events["files"]["canonical_actions.parquet"]))
    coverage = pd.read_csv(checked_file(event_view, "input_coverage.csv", events["files"]["input_coverage.csv"]))
    universe = pd.read_csv(checked_file(event_view, "source_input_universe.csv", events["files"]["source_input_universe.csv"]))
    action_keys = set(zip(actions.code, actions.dividOperateDate))
    available = {(row.code, row.year): row.queried for row in coverage.itertuples(index=False)}
    special = documented_price_adjustments(supplement_root, universe.symbol.tolist(), events["start"], events["end"])
    special_keys = {(normalize_symbol(row["symbol"]).baostock_code, row["date"]) for row in special}
    raw_file, canonical_file = pq.ParquetFile(raw_path), pq.ParquetFile(canonical_path)
    if raw_file.metadata.num_row_groups != canonical_file.metadata.num_row_groups:
        raise ValueError("Full raw/canonical source day partitions differ")
    previous, pieces, source_rows, canonical_rows, days = {}, [], 0, 0, []
    for index in range(raw_file.metadata.num_row_groups):
        raw = raw_file.read_row_group(index, columns=["code", "date", "close", "preclose", "tradestatus"]).to_pandas()
        canonical = canonical_file.read_row_group(index, columns=["code", "date"]).to_pandas()
        if (raw.date.nunique() != 1 or canonical.date.nunique() != 1 or raw.date.iloc[0] != canonical.date.iloc[0]
                or raw.code.duplicated().any() or canonical.code.duplicated().any()
                or not set(canonical.code) <= set(raw.code)):
            raise ValueError("Exact distinct-code same-day raw/canonical alignment required")
        retained = raw.loc[raw.code.isin(canonical.code)]
        omitted = raw.loc[~raw.code.isin(canonical.code)]
        if not omitted.code.eq(OLD).all() or (not omitted.empty and raw.date.iloc[0] > EFFECTIVE):
            raise ValueError("Only the previously evidenced alias shadow can be omitted from canonical reference checks")
        gaps = reference_changes(retained, previous)
        if not gaps.empty:
            pieces.append(gaps)
        source_rows += len(raw)
        canonical_rows += len(retained)
        days.append(raw.date.iloc[0])
    calendar = pd.read_csv(checked_file(event_view, "source_input_calendar.csv",
                                      events["files"]["source_input_calendar.csv"])).date.tolist()
    if (source_rows != prices["rows"] or canonical_rows != prices["canonical_rows"]
            or source_rows - canonical_rows != prices["retained_alias_shadow_rows"] or days != calendar):
        raise ValueError("Reference scan cannot reduce the pinned complete calendar or canonical price scope")
    gaps = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(columns=[
        "code", "date", "close", "preclose", "tradestatus", "previous_observed_close", "reference_delta"])
    gaps["has_reported_distribution"] = [(code, day) in action_keys for code, day in zip(gaps.code, gaps.date)]
    gaps["has_reviewed_special_reference"] = [(code, day) in special_keys for code, day in zip(gaps.code, gaps.date)]
    gaps["source_year_queried"] = [all(available.get((source, int(day[:4])), False) for source in
                                      ([OLD, NEW] if code == NEW else [code])) for code, day in zip(gaps.code, gaps.date)]
    gaps = gaps.merge(actions, left_on=["code", "date"], right_on=["code", "dividOperateDate"], how="left", validate="one_to_one")
    cash_evidence = []
    if "cash_supplements.json" in events["files"]:
        path = checked_file(event_view, "cash_supplements.json", events["files"]["cash_supplements.json"])
        cash_evidence = json.loads(path.read_text(encoding="utf-8"))
        for row in cash_evidence:
            if events["files"].get(row["source_file"]) != row["source_sha256"]:
                raise ValueError("Cash reference evidence differs from pinned source manifest")
            checked_file(event_view, row["source_file"], row["source_sha256"])
    distribution_evidence = distribution_reference_evidence(supplement_root, actions, events["start"], events["end"])
    gaps = reference_concordance(gaps, special, cash_evidence + distribution_evidence)
    folder = Path(destination) if destination else DATA_ROOT / "research/stock_action_references" / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    for source, name in ((Path(__file__), "implementation.py"), (event_view / "result.json", "input_events.json"),
                         (price_view / "result.json", "input_prices.json"),
                         (Path(__file__).parents[1] / "rules.py", "rules_implementation.py"),
                         (event_view / "input_coverage.csv", "input_coverage.csv")):
        shutil.copy2(source, folder / name)
    write_json(folder / "cash_reference_evidence.json", cash_evidence)
    write_json(folder / "distribution_reference_evidence.json", distribution_evidence)
    for row in distribution_evidence:
        for field in ("source_file", "reference_source_file", "issuer_page_file"):
            if row.get(field):
                target = folder / row[field]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(supplement_root) / row[field], target)
                if sha256(target.read_bytes()).hexdigest() != row[field.replace("_file", "_sha256")]:
                    raise ValueError("Distribution reference evidence changed during publication")
    for row in cash_evidence:
        target = folder / row["source_file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(event_view / row["source_file"], target)
        if sha256(target.read_bytes()).hexdigest() != row["source_sha256"]:
            raise ValueError("Cash reference evidence changed during publication")
    for row in special:
        for field in ("source_file", "holder_source_file"):
            if row.get(field):
                target = folder / row[field]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(supplement_root) / row[field], target)
                if sha256(target.read_bytes()).hexdigest() != row[field.replace("_file", "_sha256")]:
                    raise ValueError("Special-reference evidence changed during publication")
    write_frame(folder / "raw_reference_changes.parquet", gaps)
    write_json(folder / "reviewed_special_references.json", special)
    unexplained = ~gaps.has_reported_distribution & ~gaps.has_reviewed_special_reference
    result = {"status": "research_reference_presence_diagnostic", "event_view": str(event_view), "price_view": str(price_view),
              "supplement_root": str(Path(supplement_root).resolve()),
              "raw_daily_sha256": prices["source_hashes"]["raw"], "canonical_panel_sha256": prices["canonical_panel_sha256"],
              "source_rows": source_rows, "canonical_rows": canonical_rows, "dates": len(days),
              "raw_reference_changes": len(gaps), "with_reported_distribution": int(gaps.has_reported_distribution.sum()),
              "with_reviewed_special_reference": int(gaps.has_reviewed_special_reference.sum()),
              "without_event_pending_source_query": int((unexplained & ~gaps.source_year_queried).sum()),
              "without_event_queried_source_year": int((unexplained & gaps.source_year_queried).sum()),
              "reference_formula_status_counts": gaps.reference_formula_status.value_counts().to_dict(),
              "reference_formula_basis_counts": gaps.reference_formula_basis.value_counts().to_dict(),
              "reference_formula_tolerance_rmb": .011,
              "absolute_change_threshold_rmb": .021, "execution_admitted": False, "amount_concordance_verified": False,
              "files": {path.relative_to(folder).as_posix(): sha256(path.read_bytes()).hexdigest()
                        for path in sorted(folder.rglob("*")) if path.is_file()},
              "limitations": ["Uses existing HistoricalFeed stock discontinuity threshold, not a new execution rule",
                              "Reported entitlement formula matches are diagnostic, not independent reviewed reference terms",
                              "Blank cash/share terms stay unresolved, never imputed zero to make concordance pass",
                              "Only raw discontinuities above the existing threshold checked; subthreshold and nonobserved dates remain unverified",
                              "No cause inferred for a missing event; queried-empty is not proof that no economic event occurred",
                              "Reference scan covers all canonical prices, not PIT metadata, event completeness or a ledger backtest",
                              "No account/default/feed/strategy change or cash/share credit"]}
    write_json(folder / "result.json", result)
    return folder, result


def amend_distribution_reference(event_view, reference_view, distribution_root, destination=None, *,
                                 upgrade_unformulated_cash=False):
    """Refresh issuer-supported formulas on frozen raw gaps, not their entitlements."""
    if type(upgrade_unformulated_cash) is not bool:
        raise ValueError("Cash-reference upgrade requires an explicit boolean opt-in")
    event_view, reference_view, distribution_root = (Path(p).resolve() for p in (event_view, reference_view, distribution_root))
    events = json.loads((event_view / "result.json").read_text(encoding="utf-8"))
    before = json.loads((reference_view / "result.json").read_text(encoding="utf-8"))
    if (events["status"] != "research_action_events_projected"
            or before["status"] != "research_reference_presence_diagnostic"
            or Path(before["event_view"]).resolve() != event_view
            or events["execution_admitted"] or before["execution_admitted"]):
        raise ValueError("Aligned unadmitted event and reference inputs required")
    actions = pd.read_parquet(checked_file(event_view, "canonical_actions.parquet", events["files"]["canonical_actions.parquet"]))
    added = distribution_reference_evidence(distribution_root, actions, events["start"], events["end"])
    if not added:
        raise ValueError("At least one reviewed distribution reference required")
    def evidence(name):
        return json.loads(checked_file(reference_view, name, before["files"][name]).read_text(encoding="utf-8"))
    previous = evidence("distribution_reference_evidence.json")
    original_cash = evidence("cash_reference_evidence.json")
    keys = {(row["code"], row["dividOperateDate"]) for row in added}
    if keys & {(row["code"], row["dividOperateDate"]) for row in previous}:
        raise ValueError("Existing distribution evidence needs explicit reconciliation, not replacement")
    upgraded = set()
    legacy_listing_not_applicable = set()
    if upgrade_unformulated_cash:
        cash_by_key = {(row["code"], row["dividOperateDate"]): row for row in original_cash}
        if len(cash_by_key) != len(original_cash):
            raise ValueError("Duplicate original cash reference evidence cannot be upgraded")
        for row in added:
            key = row["code"], row["dividOperateDate"]
            old = cash_by_key.get(key)
            if old is None:
                continue
            if any(name.startswith("reference_adjustment_") and value not in (None, "")
                   for name, value in old.items()):
                raise ValueError("Existing reviewed cash reference cannot be upgraded or replaced")
            exact = ("symbol", "code", "dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate")
            amounts = ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs")
            absent_listing_not_applicable = False
            if "dividStockMarketDate" not in old and row.get("dividStockMarketDate") == "":
                try:
                    absent_listing_not_applicable = all(
                        isinstance(old.get(field), str) and old[field] != ""
                        and Decimal(old[field]).is_finite() and Decimal(old[field]) == 0
                        for field in ("dividStocksPs", "dividReserveToStockPs"))
                except (InvalidOperation, ValueError):
                    pass
            if (any(field not in old or field not in row or old[field] != row[field] for field in exact
                    if field != "dividStockMarketDate" or not absent_listing_not_applicable)
                    or any(not isinstance(old.get(field), str) or old[field] == "" for field in amounts)):
                raise ValueError("Cash-reference upgrade requires identical complete event terms")
            try:
                same_amounts = all(Decimal(old[field]).is_finite() and Decimal(old[field]) >= 0
                                   and Decimal(old[field]) == Decimal(row[field]) for field in amounts)
            except (InvalidOperation, ValueError) as error:
                raise ValueError("Cash-reference upgrade has invalid event amounts") from error
            if not same_amounts:
                raise ValueError("Cash-reference upgrade requires identical canonical amounts")
            if (not old.get("source_sha256") or old["source_sha256"] != row.get("source_sha256")
                    or before["files"].get(old.get("source_file")) != old["source_sha256"]):
                raise ValueError("Cash-reference upgrade must retain the original issuer source hash")
            checked_file(reference_view, old["source_file"], old["source_sha256"])
            upgraded.add(key)
            if absent_listing_not_applicable:
                legacy_listing_not_applicable.add(key)
    active_cash = [row for row in original_cash if (row["code"], row["dividOperateDate"]) not in upgraded]
    updated = pd.read_parquet(checked_file(reference_view, "raw_reference_changes.parquet", before["files"]["raw_reference_changes.parquet"]))
    mask = pd.Series([(code, ex) in keys for code, ex in zip(updated.code, updated.date)], index=updated.index)
    if set(zip(updated.loc[mask, "code"], updated.loc[mask, "date"])) != keys:
        raise ValueError("Every added reference must address an observed frozen raw gap")
    fields = ["reference_formula_status", "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb"]
    refreshed = reference_concordance(updated.loc[mask].drop(columns=fields), evidence("reviewed_special_references.json"),
                                      active_cash + previous + added)
    updated.loc[mask, updated.columns] = refreshed.loc[:, updated.columns].to_numpy()
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    folder = Path(destination) if destination else DATA_ROOT / "research/stock_action_references" / identifier
    shutil.copytree(reference_view, folder)
    write_frame(folder / "raw_reference_changes.parquet", updated)
    write_json(folder / "distribution_reference_evidence.json", previous + added)
    changed_files = [folder / "raw_reference_changes.parquet", folder / "distribution_reference_evidence.json"]
    upgrade_receipt = None
    if upgraded:
        archive = folder / ("cash_reference_before_distribution_upgrade_" + identifier + ".json")
        shutil.copy2(checked_file(reference_view, "cash_reference_evidence.json", before["files"]["cash_reference_evidence.json"]), archive)
        write_json(folder / "cash_reference_evidence.json", active_cash)
        changed_files.extend([archive, folder / "cash_reference_evidence.json"])
        upgrade_receipt = {"exact_upgraded_keys": sorted(upgraded),
            "archive": {"file": archive.name, "sha256": sha256(archive.read_bytes()).hexdigest()},
            "parent_cash_evidence": {"file": str(reference_view / "cash_reference_evidence.json"),
                                     "sha256": before["files"]["cash_reference_evidence.json"]},
            "legacy_absent_fields_not_applicable": [{"code": code, "dividOperateDate": ex,
                "field": "dividStockMarketDate", "legacy_field_present": False,
                "reason": "explicit_zero_bonus_and_reserve_no_stock_listing_applicable"}
                for code, ex in sorted(legacy_listing_not_applicable)],
            "entitlement_terms_and_issuer_source_hash_unchanged": True}
    for row in added:
        for field in ("source_file", "reference_source_file", "issuer_page_file"):
            if row.get(field):
                name = row[field]
                target = folder / name
                if target.exists():
                    if ((row["code"], row["dividOperateDate"]) in upgraded
                            and before["files"].get(name) == row[field.replace("_file", "_sha256")]):
                        checked_file(folder, name, row[field.replace("_file", "_sha256")])
                        continue
                    raise FileExistsError("New reference source must not overwrite a frozen source file")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(checked_file(distribution_root, name, row[field.replace("_file", "_sha256")]), target)
                changed_files.append(target)
    implementation = folder / ("reference_amendment_implementation_" + identifier + ".py")
    shutil.copy2(Path(__file__), implementation)
    changed_files.append(implementation)
    output = {**before, "distribution_amendment": {"parent_reference_view": str(reference_view),
        "source_root": str(distribution_root), "events": sorted(keys), "raw_prices_rescanned": False,
        "entitlements_changed": False, "reference_rows_refreshed": int(mask.sum())},
        "reference_formula_status_counts": updated.reference_formula_status.value_counts().to_dict(),
        "reference_formula_basis_counts": updated.reference_formula_basis.value_counts().to_dict(), "files": dict(before["files"])}
    for path in changed_files:
        output["files"][path.relative_to(folder).as_posix()] = sha256(path.read_bytes()).hexdigest()
    if upgrade_receipt is not None:
        output["distribution_amendment"]["unformulated_cash_upgrade"] = upgrade_receipt
    write_json(folder / "result.json", output)
    return folder, output


def references_main(argv=None):
    parser = argparse.ArgumentParser(description="Link every raw reference change to observed events without admitting a feed")
    parser.add_argument("--event-view", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--price-view", type=Path)
    source.add_argument("--amend-reference-view", type=Path)
    parser.add_argument("--supplement-root", type=Path)
    parser.add_argument("--distribution-root", type=Path)
    parser.add_argument("--upgrade-unformulated-cash", action="store_true",
                        help="Amendment only: archive identical unformulated cash evidence before adding reviewed reference terms")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.amend_reference_view:
        if not args.distribution_root or args.supplement_root:
            parser.error("Reference amendment needs --distribution-root, not live supplement or price rebuild")
        folder, result = amend_distribution_reference(args.event_view, args.amend_reference_view, args.distribution_root, args.out,
            upgrade_unformulated_cash=args.upgrade_unformulated_cash)
        print(json.dumps({"folder": str(folder), "amendment": result["distribution_amendment"]}))
        return 0
    if not args.supplement_root or args.distribution_root or args.upgrade_unformulated_cash:
        parser.error("Price scan needs --supplement-root, not --distribution-root")
    folder, result = check_references(args.event_view, args.price_view, args.supplement_root, args.out)
    print(json.dumps({"folder": str(folder), **result}, indent=2))
    return 0
