"""Saved inputs for actual close-publication consumers, separate from accounts."""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from ..contracts import MARKET_TZ
from ..market_dataset import write_json
from .experiment import clean
from .leader_forward_run import _check_feature_binding, _check_source_binding, _ipo_proof, _protocol, _validate_bundle
from .provider_close_batches import build_provider_close_bundle

SCHEMA = "leader_published_close_saved_input_bundle_v1"


def _now():
    return datetime.now(MARKET_TZ)


def _digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def save_bundle(bundle, out_dir, *, origin):
    """Save all consumer inputs; the unused producer table is identified."""
    raw, days = _validate_bundle(bundle)
    if not origin or not isinstance(origin, str):
        raise ValueError("Saved bundle needs an explicit actual origin")
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)
    raw_path = out / "raw_bars.parquet"
    # Legacy/provider object columns can mix numbers and numeric text. Encode
    # those cells explicitly so Arrow cannot silently infer/coerce one type.
    stored = raw.copy(deep=False)
    encoded = []
    for column in raw.select_dtypes(include=["object"]).columns:
        kinds = raw[column].dropna().map(type).unique()
        if len(kinds) > 1:
            encoded.append(column)
            stored[column] = raw[column].map(lambda v: json.dumps(
                None if pd.isna(v) else clean(v), ensure_ascii=False, allow_nan=False))
    stored.to_parquet(raw_path, index=False)
    payload = {k: clean(v) for k, v in bundle.items() if k not in {"raw_bars", "candidate_table"}}
    value = {"schema": SCHEMA, "recorded_at": _now().isoformat(), "origin": origin,
        "raw_bars_file": str(raw_path), "raw_bars_sha256": _digest(raw_path),
        "raw_rows": len(raw), "source_days_complete": days, "bundle": payload,
        "json_encoded_object_columns": encoded,
        "omitted_unused_producer_fields": ["candidate_table"],
        "candidate_panel_only": True, "strategy_validated": False, "selection_enabled": False,
        "execution_admitted": False, "account_created": False, "orders_generated": False,
        "provider_contacted": False}
    path = out / "bundle.json"
    write_json(path, value)
    return path


def load_bundle(path):
    path = Path(path).resolve()
    before = _digest(path)
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    stamp = datetime.fromisoformat(value["recorded_at"].replace("Z", "+00:00"))
    raw_path = Path(value["raw_bars_file"]).resolve()
    raw_path.relative_to(path.parent)
    if (value.get("schema") != SCHEMA or stamp.utcoffset() is None or stamp > _now()
            or not value.get("origin") or value.get("candidate_panel_only") is not True
            or any(value.get(k) is not False for k in
                ("strategy_validated", "selection_enabled", "execution_admitted",
                 "account_created", "orders_generated", "provider_contacted"))
            or _digest(raw_path) != value["raw_bars_sha256"]):
        raise ValueError("Saved publication input bytes, actual clock or boundaries changed")
    bundle = value["bundle"]
    bundle["raw_bars"] = pd.read_parquet(raw_path)
    encoded = value.get("json_encoded_object_columns")
    if not isinstance(encoded, list) or len(encoded) != len(set(encoded)) or not set(encoded) <= set(bundle["raw_bars"]):
        raise ValueError("Saved mixed-column encoding metadata changed")
    for column in encoded:
        bundle["raw_bars"][column] = bundle["raw_bars"][column].map(json.loads)
    raw, days = _validate_bundle(bundle)
    if len(raw) != value["raw_rows"] or days != value["source_days_complete"] or _digest(path) != before:
        raise ValueError("Saved publication input shape or manifest changed")
    return bundle


def input_proof(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return {"file": str(path), "sha256": _digest(path),
        "recorded_at": value["recorded_at"], "origin": value["origin"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", action="append", type=Path, required=True)
    parser.add_argument("--history-snapshot", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    args = parser.parse_args(argv)
    binding, calendar, _ = _protocol(args.protocol)
    options = {"ipo_qualification": _ipo_proof(binding)} if binding.get("feature_qualification_method_id") else {}
    if binding.get("history_source_view") is not None:
        options["recent_source_view"] = binding["history_source_view"]["view_file"]
    bundle = build_provider_close_bundle(args.package, args.history_snapshot, Path(binding["calendar"]), **options)
    _check_feature_binding(bundle, binding)
    _check_source_binding(bundle, binding)
    path = save_bundle(bundle, args.out, origin=args.origin)
    print(json.dumps({"status": "saved_local_publication_input_bundle",
        "input": input_proof(path), "provider_contacted": False, "execution_admitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
