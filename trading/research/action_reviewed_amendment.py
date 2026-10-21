"""Apply supported reviewed distribution corrections to frozen research views."""
import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import uuid

import pandas as pd

from ..market_dataset import normalize_symbol, write_frame, write_json
from ..market_snapshot import (load_action_resolutions, official_distribution_notice_date,
                               reconcile_provider_actions, supplement_actions)
from ..settings import DATA_ROOT
from .action_events import project_actions, reference_concordance
from .adjusted_prices import checked_file
from .action_confirmations import KIND as CONFIRMATION_KIND, LIMITATION as CONFIRMATION_LIMITATION, load_confirmations

KINDS = {"issuer_confirmed_cash_only_terms", "issuer_cash_only_precision_correction",
         "issuer_confirmed_reserve_zero_term", "issuer_reserve_bonus_correction"}


def corrected_actions(actions, selected, source_path):
    keys = [(row["symbol"], row["dividOperateDate"]) for row in selected]
    mask = pd.Series([(s, d) in keys for s, d in zip(actions.symbol, actions.dividOperateDate)], index=actions.index)
    rows = actions.loc[mask].copy()
    if len(rows) != len(keys) or rows.duplicated(["symbol", "dividOperateDate"]).any():
        raise ValueError("Selected canonical action identity is not unique")
    rows["_cache_file"] = str(source_path)
    if any(row.get("kind") == CONFIRMATION_KIND for row in selected):
        if not all(row.get("kind") == CONFIRMATION_KIND for row in selected):
            raise ValueError("Post-event confirmations and pre-event reviews need separate amendments")
        corrected, audit = rows.copy(), []
        for terms in selected:
            selected_mask = corrected.symbol.eq(terms["symbol"]) & corrected.dividOperateDate.eq(terms["dividOperateDate"])
            original = corrected.loc[selected_mask].iloc[0]
            if any(original[field] != value for field, value in terms["expected_provider_terms"].items()):
                raise ValueError("Post-event confirmation source terms changed")
            for field in ("dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"):
                if original[field] == "":
                    corrected.loc[selected_mask, field] = terms[field]
            audit.append({"symbol": terms["symbol"], "dividOperateDate": terms["dividOperateDate"],
                          "status": CONFIRMATION_KIND, "confirmation_known_after_event": True,
                          "source_date": terms["source_date"], "execution_admitted": False})
    else:
        corrected, audit = reconcile_provider_actions(rows, selected)
    indexed = corrected.set_index(["symbol", "dividOperateDate"])
    if not indexed.index.is_unique or set(indexed.index) != set(keys):
        raise ValueError("Reviewed correction must preserve selected event identities")
    fields = ["dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"]
    result = actions.copy(deep=True)
    if "bonus_taxable_cash_ps" in corrected:
        fields.append("bonus_taxable_cash_ps")
        if "bonus_taxable_cash_ps" not in result:
            result["bonus_taxable_cash_ps"] = ""
    # The reconciler resets row numbers; match economic identities, not its index.
    for index, row in actions.loc[mask].iterrows():
        result.loc[index, fields] = indexed.loc[(row.symbol, row.dividOperateDate), fields].to_numpy()
    if (not result.loc[~mask, actions.columns].equals(actions.loc[~mask])
            or ("bonus_taxable_cash_ps" not in actions and "bonus_taxable_cash_ps" in result
                and result.loc[~mask, "bonus_taxable_cash_ps"].ne("").any())):
        raise ValueError("Unselected corporate actions changed")
    return result, audit


def patch_reference_rows(gaps, mask, refreshed):
    original = gaps.loc[mask]
    identities = ["code", "date"]
    if (len(refreshed) != len(original)
            or refreshed[identities].values.tolist() != original[identities].values.tolist()):
        raise ValueError("Reference amendment changed event identities")
    refreshed = refreshed.copy()
    refreshed.index = original.index
    result = gaps.copy(deep=True)
    for column in gaps.columns:
        values = refreshed[column]
        if pd.api.types.is_numeric_dtype(gaps[column].dtype):
            values = pd.to_numeric(values, errors="raise")
        result.loc[original.index, column] = values.to_numpy()
    allowed = {"dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs",
               "reference_formula_status", "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb"}
    untouched = [column for column in gaps.columns if column not in allowed]
    if not result[untouched].equals(gaps[untouched]) or not result.loc[~mask].equals(gaps.loc[~mask]):
        raise ValueError("Raw or unselected reference facts changed")
    return result


def amend(event_view, reference_view, reviewed_root, events, *, postevent_confirmation=False):
    event_view, reference_view, reviewed_root = (Path(p).resolve() for p in (event_view, reference_view, reviewed_root))
    before = json.loads((event_view / "result.json").read_text(encoding="utf-8"))
    references = json.loads((reference_view / "result.json").read_text(encoding="utf-8"))
    keys = [tuple(event) for event in events]
    if (not keys or len(set(keys)) != len(keys)
            or before["status"] != "research_action_events_projected"
            or references["status"] != "research_reference_presence_diagnostic"
            or Path(references["event_view"]).resolve() != event_view
            or before["execution_admitted"] or references["execution_admitted"]):
        raise ValueError("Unique explicit events and aligned unadmitted research inputs required")
    actions = pd.read_parquet(checked_file(event_view, "canonical_actions.parquet", before["files"]["canonical_actions.parquet"])).fillna("")
    loader = load_confirmations if postevent_confirmation else load_action_resolutions
    terms = loader(reviewed_root, set(actions.symbol))
    selected = [row for row in terms if (row["symbol"], row["dividOperateDate"]) in keys]
    if (sorted((row["symbol"], row["dividOperateDate"]) for row in selected) != sorted(keys)
            or any(row["kind"] not in ({CONFIRMATION_KIND} if postevent_confirmation else KINDS) for row in selected)):
        raise ValueError("Every requested event needs one supported reviewed resolution; no new amount tolerance")
    actions, audit = corrected_actions(actions, selected, event_view / "canonical_actions.parquet")
    fields = ["dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"]
    gaps = pd.read_parquet(checked_file(reference_view, "raw_reference_changes.parquet", references["files"]["raw_reference_changes.parquet"]))
    gap_mask = pd.Series([(normalize_symbol(c).vt_symbol, d) in keys for c, d in zip(gaps.code, gaps.date)], index=gaps.index)
    if gap_mask.any():
        formula = ["reference_formula_status", "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb"]
        inputs = gaps.loc[gap_mask].drop(columns=formula).copy()
        for index, row in inputs.iterrows():
            action = actions.loc[actions.code.eq(row.code) & actions.dividOperateDate.eq(row.date)]
            if len(action) != 1:
                raise ValueError("One corrected canonical action required for each reference")
            inputs.loc[index, fields] = action.iloc[0][fields]
        def evidence(name):
            return json.loads(checked_file(reference_view, name, references["files"][name]).read_text(encoding="utf-8"))
        refreshed = reference_concordance(inputs, evidence("reviewed_special_references.json"),
                                          evidence("cash_reference_evidence.json") + evidence("distribution_reference_evidence.json"))
        gaps = patch_reference_rows(gaps, gap_mask, refreshed)
    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    folder, refs = DATA_ROOT / "research/stock_action_events" / identifier, DATA_ROOT / "research/stock_action_references" / identifier
    shutil.copytree(event_view, folder)
    shutil.copytree(reference_view, refs)
    write_frame(folder / "canonical_actions.parquet", actions)
    write_frame(refs / "raw_reference_changes.parquet", gaps)
    stored = deepcopy(selected)
    for row in stored:
        source_fields = ["source_file"]
        if "plan_source_file" in row:
            source_fields.append("plan_source_file")
        if row.get("source_channel") == "issuer_linked_ir_valueonline":
            source_fields.extend(["issuer_page_file", "issuer_ir_file"])
        if postevent_confirmation:
            source_fields.extend(["final_source_file", "source_text_file", "final_text_file"])
            source_manifest = json.loads((reviewed_root / "result.json").read_text(encoding="utf-8"))
        for field in source_fields:
            old_name = row[field]
            row[field] = (f"reviewed_amendments/{identifier}/{row['symbol']}/{row['dividOperateDate']}/"
                          f"{field.removesuffix('_file')}_{Path(old_name).name}")
            dest = folder / row[field]
            dest.parent.mkdir(parents=True, exist_ok=True)
            digest = source_manifest["files"][old_name] if field in {"source_text_file", "final_text_file"} else row[field.replace("_file", "_sha256")]
            shutil.copy2(checked_file(reviewed_root, old_name, digest), dest)
    audit_name = f"reviewed_amendments/{identifier}/audit.json"
    write_json(folder / audit_name, {"resolutions": stored, "audit": audit})
    implementation = f"reviewed_amendments/{identifier}/implementation.py"
    shutil.copy2(Path(__file__), folder / implementation)
    receipt = {"parent_event_view": str(event_view), "parent_reference_view": str(reference_view),
               "reviewed_root": str(reviewed_root), "events": keys, "raw_prices_rescanned": False,
               "reference_rows_refreshed": int(gap_mask.sum()), "other_events_changed": False,
               "execution_admitted": False}
    if postevent_confirmation:
        receipt.update(confirmation_known_after_event=True, basis=CONFIRMATION_LIMITATION)
    result = {**before, "reviewed_cash_amendment": receipt, "files": dict(before["files"])}
    if postevent_confirmation:
        result["postevent_cash_confirmations"] = before.get("postevent_cash_confirmations", 0) + len(selected)
        result["postevent_cash_confirmation_basis"] = CONFIRMATION_LIMITATION
    archived_sources = [row[field] for row in stored for field in
                        ("source_file", "plan_source_file", "issuer_page_file", "issuer_ir_file") if field in row]
    for name in ["canonical_actions.parquet", audit_name, implementation, *archived_sources]:
        result["files"][name] = sha256((folder / name).read_bytes()).hexdigest()
    if postevent_confirmation:
        for row in stored:
            for field in ("final_source_file", "source_text_file", "final_text_file"):
                name = row[field]
                result["files"][name] = sha256((folder / name).read_bytes()).hexdigest()
    write_json(folder / "result.json", result)
    shutil.copy2(folder / "result.json", refs / "input_events.json")
    reference_result = {**references, "event_view": str(folder), "reviewed_cash_amendment": receipt,
        "reference_formula_status_counts": gaps.reference_formula_status.value_counts().to_dict(),
        "reference_formula_basis_counts": gaps.reference_formula_basis.value_counts().to_dict(), "files": dict(references["files"])}
    for name in ("raw_reference_changes.parquet", "input_events.json"):
        reference_result["files"][name] = sha256((refs / name).read_bytes()).hexdigest()
    write_json(refs / "result.json", reference_result)
    return folder, refs, receipt


def amend_missing_cash(event_view, reference_view, supplement_root, events):
    """Append explicitly evidenced missing cash events to an existing frozen pair."""
    event_view, reference_view, supplement_root = (
        Path(path).resolve() for path in (event_view, reference_view, supplement_root))
    before = json.loads((event_view / "result.json").read_text(encoding="utf-8"))
    references = json.loads((reference_view / "result.json").read_text(encoding="utf-8"))
    keys = [tuple(event) for event in events]
    if (not keys or any(len(key) != 2 for key in keys) or len(set(keys)) != len(keys)
            or before["status"] != "research_action_events_projected"
            or references["status"] != "research_reference_presence_diagnostic"
            or Path(references["event_view"]).resolve() != event_view
            or before["execution_admitted"] or references["execution_admitted"]):
        raise ValueError("Unique explicit missing cash events and aligned research pair required")

    def event_file(name):
        return checked_file(event_view, name, before["files"][name])

    def reference_evidence(name):
        return json.loads(checked_file(reference_view, name, references["files"][name]).read_text(encoding="utf-8"))

    actions = pd.read_parquet(event_file("canonical_actions.parquet"))
    universe = pd.read_csv(event_file("source_input_universe.csv"), dtype=str, keep_default_na=False)
    identity = json.loads(event_file("price_identity.json").read_text(encoding="utf-8"))
    if (len(actions) != before["canonical_events"]
            or actions.duplicated(["symbol", "dividOperateDate"]).any()):
        raise ValueError("Frozen canonical event identities/count changed")
    for symbol, day in keys:
        if (normalize_symbol(symbol).vt_symbol != symbol or symbol not in set(universe.symbol)
                or not before["start"] <= day <= before["end"]):
            raise ValueError("Missing cash event is outside the original universe/window")
        if (actions.symbol.eq(symbol) & actions.dividOperateDate.eq(day)).any():
            raise ValueError("Missing cash amendment cannot replace an existing event")

    source_csv = supplement_root / "supplements/corporate_actions.csv"
    supplied = pd.read_csv(source_csv, dtype=str, keep_default_na=False)
    required = {"symbol", "dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate",
                "dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs", "source_url",
                "source_file", "source_date", "source_sha256"}
    raw_reference_fields = {"code", "date", "close", "preclose", "tradestatus", "previous_observed_close",
                            "reference_delta", "source_year_queried", "has_reviewed_special_reference"}
    if (not required <= set(supplied)
            or supplied.duplicated(["symbol", "dividOperateDate"]).any()
            or set(zip(supplied.symbol, supplied.dividOperateDate)) != set(keys)):
        raise ValueError("Cash supplement must contain exactly the unique requested events")
    for row in supplied.to_dict("records"):
        source = (supplement_root / row["source_file"]).resolve()
        source.relative_to(supplement_root)
        if (official_distribution_notice_date(row["source_url"]) != row["source_date"]
                or row["dividStockMarketDate"] != ""
                or sha256(source.read_bytes()).hexdigest() != row["source_sha256"]):
            raise ValueError("Missing cash supplement requires matching official dated PDF/SHA")
    supplemented, evidence = supplement_actions(supplement_root, actions, set(universe.symbol))
    if len(evidence) != len(keys) or any(row["status"] != "added_missing_cash_dividend" for row in evidence):
        raise ValueError("Every requested cash supplement must add one missing event")
    # Project additions only: re-projecting the old canonical table would rewrite provenance.
    extra = supplemented.iloc[len(actions):].copy()
    _, extra, outside, _ = project_actions(extra, before["start"], before["end"], identity["rename"])
    if not outside.empty or set(zip(extra.symbol, extra.dividOperateDate)) != set(keys):
        raise ValueError("Missing cash projection changed the explicit event identities")

    old_cash = json.loads(event_file("cash_supplements.json").read_text(encoding="utf-8"))
    cash = reference_evidence("cash_reference_evidence.json")
    distribution = reference_evidence("distribution_reference_evidence.json")
    special = reference_evidence("reviewed_special_references.json")
    requested_codes = {(normalize_symbol(symbol).baostock_code, day) for symbol, day in keys}
    for group in (old_cash, cash, distribution, special):
        if any((row.get("code", normalize_symbol(row["symbol"]).baostock_code),
                row.get("dividOperateDate", row.get("date"))) in requested_codes for row in group):
            raise ValueError("Missing cash event conflicts with existing reference evidence")
    gaps = pd.read_parquet(checked_file(reference_view, "raw_reference_changes.parquet",
                                      references["files"]["raw_reference_changes.parquet"]))
    mask = pd.Series([(code, day) in requested_codes for code, day in zip(gaps.code, gaps.date)], index=gaps.index)
    selected = gaps.loc[mask]
    if (len(selected) != len(keys) or selected.duplicated(["code", "date"]).any()
            or selected.has_reported_distribution.any() or selected.has_reviewed_special_reference.any()
            or not selected.reference_formula_status.eq("missing_source_event").all()):
        raise ValueError("Each missing cash event needs one exact frozen missing-event gap")
    formula_fields = {"reference_formula_status", "reference_formula_basis",
                      "expected_reference_rmb", "reference_error_rmb"}
    frozen_fields = raw_reference_fields | formula_fields | {"has_reported_distribution"}
    # Curation may carry review metadata named like quote/formula columns. It remains
    # evidence only, never a source of frozen quotes, action facts or formula output.
    action_fields = [field for field in actions if field in extra and field in gaps and field not in frozen_fields]
    if any(selected[field].notna().any() and selected[field].fillna("").ne("").any()
           for field in action_fields):
        raise ValueError("Frozen missing-event gap already contains conflicting action facts")

    identifier = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    stored = deepcopy(evidence)
    sources = []
    for row in stored:
        old_name = row["source_file"]
        row["source_file"] = (f"cash_supplements/{identifier}/{row['symbol']}/{row['dividOperateDate']}/"
                              f"{Path(old_name).name}")
        row.pop("_cache_file", None)
        sources.append((supplement_root / old_name, row["source_file"], row["source_sha256"]))
        match = extra.symbol.eq(row["symbol"]) & extra.dividOperateDate.eq(row["dividOperateDate"])
        extra.loc[match, "source_file"] = row["source_file"]
    updated_actions = pd.concat([actions, extra.reindex(columns=actions.columns)], ignore_index=True)
    if not updated_actions.iloc[:len(actions)][actions.columns].equals(actions):
        raise ValueError("Missing cash addition changed an existing canonical row")
    inputs = selected.drop(columns=["reference_formula_status", "reference_formula_basis",
                                    "expected_reference_rmb", "reference_error_rmb"]).copy()
    indexed = extra.set_index(["code", "dividOperateDate"], drop=False)
    for index, row in inputs.iterrows():
        inputs.loc[index, action_fields] = indexed.loc[(row.code, row.date), action_fields].to_numpy()
    inputs["has_reported_distribution"] = True
    refreshed = reference_concordance(inputs, special, cash + distribution + stored)
    updated_gaps = gaps.copy()
    mutable = [*action_fields, "has_reported_distribution", "reference_formula_status",
               "reference_formula_basis", "expected_reference_rmb", "reference_error_rmb"]
    for column in mutable:
        values = refreshed[column]
        if pd.api.types.is_numeric_dtype(gaps[column].dtype):
            values = pd.to_numeric(values, errors="raise")
        updated_gaps.loc[selected.index, column] = values.to_numpy()
    immutable = [column for column in gaps if column not in mutable]
    if not updated_gaps[immutable].equals(gaps[immutable]) or not updated_gaps.loc[~mask].equals(gaps.loc[~mask]):
        raise ValueError("Missing cash addition changed raw references or unrelated rows")

    folder = DATA_ROOT / "research/stock_action_events" / identifier
    refs = DATA_ROOT / "research/stock_action_references" / identifier
    # Do not leave a copied success manifest if publication fails partway through.
    for source, target in ((event_view, folder), (reference_view, refs)):
        shutil.copytree(source, target, ignore=lambda path, names: ["result.json"] if Path(path) == source else [])
    for source, name, digest in sources:
        for target_root in (folder, refs):
            target = target_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("Missing cash source changed during publication")
    write_frame(folder / "canonical_actions.parquet", updated_actions)
    write_json(folder / "cash_supplements.json", old_cash + stored)
    write_json(refs / "cash_reference_evidence.json", cash + stored)
    write_frame(refs / "raw_reference_changes.parquet", updated_gaps)
    receipt = {"parent_event_view": str(event_view), "parent_reference_view": str(reference_view),
               "supplement_root": str(supplement_root), "events": keys, "added_events": len(stored),
               "reference_rows_refreshed": len(selected), "raw_prices_rescanned": False,
               "old_canonical_rows_changed": False, "source_coverage_changed": False,
               "pit_verified": False, "execution_admitted": False}
    audit = f"cash_supplements/{identifier}/audit.json"
    implementation = f"cash_supplements/{identifier}/implementation.py"
    for target_root in (folder, refs):
        write_json(target_root / audit, {"receipt": receipt, "evidence": stored})
        shutil.copy2(Path(__file__), target_root / implementation)
    result = {**before, "files": dict(before["files"]), "missing_cash_amendment": receipt,
              "canonical_events": len(updated_actions), "supplements_added": before["supplements_added"] + len(stored)}
    archived = [name for _, name, _ in sources] + [audit, implementation]
    for name in ("canonical_actions.parquet", "cash_supplements.json", *archived):
        result["files"][name] = sha256((folder / name).read_bytes()).hexdigest()
    write_json(folder / "result.json", result)
    shutil.copy2(folder / "result.json", refs / "input_events.json")
    unexplained = ~updated_gaps.has_reported_distribution & ~updated_gaps.has_reviewed_special_reference
    reference_result = {**references, "files": dict(references["files"]), "event_view": str(folder),
        "missing_cash_amendment": receipt, "with_reported_distribution": int(updated_gaps.has_reported_distribution.sum()),
        "without_event_pending_source_query": int((unexplained & ~updated_gaps.source_year_queried).sum()),
        "without_event_queried_source_year": int((unexplained & updated_gaps.source_year_queried).sum()),
        "reference_formula_status_counts": updated_gaps.reference_formula_status.value_counts().to_dict(),
        "reference_formula_basis_counts": updated_gaps.reference_formula_basis.value_counts().to_dict()}
    for name in ("raw_reference_changes.parquet", "cash_reference_evidence.json", "input_events.json", *archived):
        reference_result["files"][name] = sha256((refs / name).read_bytes()).hexdigest()
    write_json(refs / "result.json", reference_result)
    return folder, refs, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("event-view", "reference-view"):
        parser.add_argument("--" + name, type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--reviewed-root", type=Path)
    source.add_argument("--confirmation-root", type=Path, help="Post-event issuer evidence; historical research accounting only")
    source.add_argument("--cash-supplement-root", type=Path, help="Official evidenced missing cash events only; no price rescan")
    parser.add_argument("--only-event", action="append", required=True)
    args = parser.parse_args(argv)
    keys = [value.rsplit(":", 1) for value in args.only_event]
    if any(len(key) != 2 for key in keys):
        parser.error("Expected SYMBOL:YYYY-MM-DD")
    if args.cash_supplement_root is not None:
        folder, refs, receipt = amend_missing_cash(args.event_view, args.reference_view, args.cash_supplement_root, keys)
    else:
        folder, refs, receipt = amend(args.event_view, args.reference_view, args.reviewed_root or args.confirmation_root,
                                     keys, postevent_confirmation=args.confirmation_root is not None)
    print(json.dumps({"event_view": str(folder), "reference_view": str(refs), "amendment": receipt}))
    return 0
