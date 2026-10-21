"""Check supplied press examples against independently fetched public raw listings."""
from pathlib import Path

import pandas as pd

from .seats import DEFAULT_HOME, collect, normalize, read_raw

BRANCH_TERMS = ("上海江苏路", "南京太平南路", "浙江分公司", "大连黄河路", "西安朱雀大街")


def main():
    source = DEFAULT_HOME / "inputs" / "ashare_strategies_seats_20260926" / "public_event_examples.csv"
    examples = pd.read_csv(source)
    out = DEFAULT_HOME / "example_verification"
    out.mkdir(exist_ok=True)
    results = []
    for day, sample in examples.groupby("disclosure_date"):
        folder = out / day
        collect(folder, day, day, max_pages=10)
        stock_rows = pd.DataFrame(read_raw(folder, "summary"))
        events = normalize(read_raw(folder, "buy"), read_raw(folder, "sell"))
        events.to_parquet(folder / "events.parquet", index=False)
        names = stock_rows.SECURITY_NAME_ABBR.str.replace(r"^\*?ST", "", regex=True)
        for example in sample.to_dict("records"):
            # Provider short names may be current rather than point-in-time names.
            known_code = {"天际股份": "002759"}.get(example["stock_name"])
            match = stock_rows.SECURITY_CODE.eq(known_code) if known_code else names.eq(example["stock_name"])
            codes = stock_rows.loc[match, "SECUCODE"]
            symbols = codes.str.replace(".SH", ".SSE", regex=False).str.replace(".SZ", ".SZSE", regex=False)
            term = next((term for term in BRANCH_TERMS if term in example["seat_name_as_reported"]), None)
            rounded = str(example["amount_is_rounded"]).lower() == "true"
            tolerance = 500000. if rounded else 50.
            amount_field = example["reported_side"] + "_cny"
            candidates = events.loc[events.symbol.isin(symbols)]
            if term:
                candidates = candidates.loc[candidates.seat_name.str.contains(term, regex=False)]
            else:
                candidates = candidates.iloc[:0]
            branch_candidates = candidates.drop_duplicates(["symbol", "seat_name", "window_kind", amount_field])
            candidates = branch_candidates.loc[(branch_candidates[amount_field] - example["reported_amount_cny"]).abs().le(tolerance)]
            row = {**example, "checked_against": "Eastmoney raw listings; not an independent exchange audit",
                   "status": "unique_amount_branch_match" if len(candidates) == 1 else "ambiguous_or_unmatched",
                   "matching_candidates": len(candidates), "tolerance_cny": tolerance,
                   "person_identity_verified": False, "private_account_pnl_observable": False,
                   "branch_candidate_amounts": "|".join(str(x) for x in branch_candidates[amount_field].dropna()),
                   "stock_name_time_consistency_verified": False}
            if len(candidates) == 1:
                matched = candidates.iloc[0]
                row.update(symbol=matched.symbol, observed_full_seat_name=matched.seat_name,
                           actual_disclosed_amount_cny=matched[amount_field], observed_window_kind=matched.window_kind,
                           observed_reason=matched.reason, raw_id=matched.raw_id)
            results.append(row)
    target = out / "verified_public_examples.csv"
    frame = pd.DataFrame(results)
    frame.to_csv(target, index=False, encoding="utf-8-sig")
    print(frame[["stock_name", "status", "matching_candidates"]].to_string(index=False))
    print(target)


if __name__ == "__main__":
    main()
