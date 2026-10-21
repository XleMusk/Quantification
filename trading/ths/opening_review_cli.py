"""Read a registered research case and optionally compare explicit THS evidence."""
import argparse
import json
from pathlib import Path

from ..settings import DATA_ROOT


def main(argv=None):
    from .opening_review import build_review
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--account-observation", type=Path)
    parser.add_argument("--broker-quotes", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    destination = args.out.resolve()
    if destination.exists():
        raise FileExistsError("Use a new review output; existing observations are not replaced")
    inputs = [path.resolve() for path in (args.account_observation, args.broker_quotes) if path]
    if destination in inputs:
        raise ValueError("Review output must remain separate from supplied evidence")
    def read(path):
        return json.loads(path.read_text(encoding="utf-8-sig")) if path else None
    result = build_review(DATA_ROOT, args.route, args.case,
        account_observation=read(args.account_observation), broker_quotes=read(args.broker_quotes))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({key: result.get(key) for key in
        ("schema", "status", "route_id", "case_id", "source_day", "execution_day", "missing",
         "prefill_allowed", "execution_admitted")}, ensure_ascii=False))
    return 0
