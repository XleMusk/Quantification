"""Read-only OCR helpers for verified THS virtual-grid screenshots."""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


VENDOR_ROOT = Path(r"D:\QuantificationData\trading\ths\vendor\rapidocr")
DEFAULT_FIELDS = (
    "symbol", "name", "quantity", "sellable", "frozen", "cost_price",
    "market_price", "pnl", "pnl_pct", "day_pnl", "day_pnl_pct",
    "market_value", "holding_pct", "day_buy", "day_sell", "market",
)
NUMERIC_FIELDS = {
    "quantity", "sellable", "frozen", "cost_price", "market_price", "pnl",
    "pnl_pct", "day_pnl", "day_pnl_pct", "market_value", "holding_pct",
    "day_buy", "day_sell",
}

# OCR rows are never broker evidence, but their page header is still useful
# for detecting a common desktop failure: the holdings grid being captured
# while the operator thinks the orders/trades tab is active.  Keep these hints
# conservative and configurable through the profile; a missing/ambiguous
# header must remain a candidate-only blocker.
DEFAULT_HEADER_TOKENS = {
    "holdings": ("\u80a1\u7968\u4f59\u989d", "\u53ef\u7528\u4f59\u989d", "\u51bb\u7ed3\u6570\u91cf", "\u5e02\u4ef7", "\u5e02\u503c"),
    "orders": ("\u59d4\u6258", "\u59d4\u6258\u72b6\u6001", "\u59d4\u6258\u6570\u91cf", "\u59d4\u6258\u4ef7\u683c", "\u59d4\u6258\u65f6\u95f4"),
    "trades": ("\u6210\u4ea4", "\u6210\u4ea4\u6570\u91cf", "\u6210\u4ea4\u4ef7\u683c", "\u6210\u4ea4\u65f6\u95f4", "\u6210\u4ea4\u91d1\u989d"),
    "activity": ("\u59d4\u6258", "\u6210\u4ea4", "\u72b6\u6001", "\u4ef7\u683c", "\u6570\u91cf", "\u65f6\u95f4"),
}


def _header_semantics(detections, mapping, expected_kind):
    texts = [str(item.get("text") or "").strip() for item in detections if str(item.get("text") or "").strip()]
    observed = " ".join(texts)
    configured = mapping.get("header_tokens")
    tokens = tuple(str(token) for token in configured) if configured else DEFAULT_HEADER_TOKENS.get(
        expected_kind, DEFAULT_HEADER_TOKENS.get(mapping.get("kind"), ())
    )
    matched = tuple(token for token in tokens if token in observed)
    # Holdings needs several distinctive balance columns.  Activity pages
    # need one domain token and at least one table-shape token; this avoids
    # rejecting minor THS label variants while still rejecting a holdings page.
    if expected_kind == "holdings":
        verified = len(matched) >= 3 and any(token in matched for token in ("股票余额", "可用余额", "冻结数量"))
    elif expected_kind in {"orders", "trades", "activity"}:
        domain = ("\u59d4\u6258", "\u59d4\u6258\u72b6\u6001", "\u6210\u4ea4", "\u6210\u4ea4\u6570\u91cf", "\u6210\u4ea4\u4ef7\u683c", "\u6210\u4ea4\u65f6\u95f4", "\u6210\u4ea4\u91d1\u989d")
        shape = ("\u72b6\u6001", "\u4ef7\u683c", "\u6570\u91cf", "\u65f6\u95f4", "\u91d1\u989d")
        matched_text = " ".join(matched)
        verified = bool(set(matched) & set(domain)) and any(token in matched_text for token in shape)
    else:
        verified = False
    return {
        "expected_kind": expected_kind,
        "expected_tokens": list(tokens),
        "matched_tokens": list(matched),
        "observed_header_text": observed,
        "header_present": bool(texts),
        "header_verified": bool(verified),
        "status": "header_verified" if verified else "page_header_mismatch" if texts else "header_missing",
    }


def _ocr_engine():
    if VENDOR_ROOT.is_dir() and str(VENDOR_ROOT) not in sys.path:
        sys.path.insert(0, str(VENDOR_ROOT))
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def _detections(image_path):
    result, elapsed = _ocr_engine()(str(image_path))
    detections = []
    for item in result or []:
        box, text, score = item
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        detections.append({
            "text": str(text),
            "score": float(score),
            "x": sum(xs) / len(xs),
            "y": sum(ys) / len(ys),
            "box": [[float(point[0]), float(point[1])] for point in box],
        })
    return detections, [float(value) for value in elapsed]


def _row_groups(detections, y_min, y_max, tolerance):
    rows = []
    for item in sorted((d for d in detections if y_min <= d["y"] <= y_max), key=lambda d: d["y"]):
        group = next((row for row in rows if abs(row["y"] - item["y"]) <= tolerance), None)
        if group is None:
            group = {"y": item["y"], "items": []}
            rows.append(group)
        group["items"].append(item)
        group["y"] = sum(value["y"] for value in group["items"]) / len(group["items"])
    return sorted(rows, key=lambda row: row["y"])


def _numeric(text):
    return re.findall(r"[-+]?\d+(?:\.\d+)?", text or "")


def _as_number(text):
    values = _numeric(text)
    if not values:
        return None
    try:
        return float(values[-1])
    except ValueError:
        return None


def _as_int(text):
    value = _as_number(text)
    return int(value) if value is not None and value.is_integer() else None


def _row_from_items(items, centers, fields):
    grouped = defaultdict(list)
    for item in items:
        index = min(range(len(centers)), key=lambda value: abs(item["x"] - centers[value]))
        grouped[index].append(item)
    raw = {
        fields[index]: " ".join(item["text"] for item in sorted(grouped[index], key=lambda d: d["x"]))
        for index in grouped if index < len(fields)
    }
    row = {"raw": raw}
    for field in fields:
        text = raw.get(field, "")
        if field in {"quantity", "sellable", "frozen", "day_buy", "day_sell"}:
            row[field] = _as_int(text)
        elif field in NUMERIC_FIELDS:
            row[field] = _as_number(text)
        else:
            row[field] = text or None
    if row.get("market"):
        market = re.search(r"(上海|深圳|北京)A股", row["market"])
        row["market"] = f"{market.group(1)}A股" if market else None
    if row.get("quantity") is not None and row.get("market_price") is not None:
        row["market_value"] = round(row["quantity"] * row["market_price"], 6)
        row.setdefault("derived_fields", []).append("market_value")
    return row


def read_grid_candidate(image_path, mapping, *, summary=None, kind=None):
    image_path = Path(image_path)
    if not image_path.is_file():
        raise FileNotFoundError(image_path)
    detections, elapsed = _detections(image_path)
    header_detections = [
        item for item in detections
        if item["y"] <= float(mapping.get("header_y_max", 34))
    ]
    centers = [float(value) for value in mapping["x_centers"]]
    fields = tuple(mapping.get("fields") or DEFAULT_FIELDS)
    rows = []
    for group in _row_groups(
        detections, float(mapping.get("row_y_min", 35)), float(mapping.get("row_y_max", 180)),
        float(mapping.get("row_tolerance", 12)),
    ):
        row = _row_from_items(group["items"], centers, fields)
        if re.search(r"\b\d{6}\b", row.get("symbol") or ""):
            row["symbol"] = re.search(r"\b\d{6}\b", row["symbol"]).group(0)
            row["row_y"] = group["y"]
            rows.append(row)
    expected_kind = str(kind or mapping.get("kind") or "unknown")
    header_semantics = _header_semantics(header_detections, mapping, expected_kind)
    checks = {
        "row_count": len(rows),
        "derived_market_value": round(sum(row.get("market_value") or 0 for row in rows), 6),
        "derived_pnl": round(sum(row.get("pnl") or 0 for row in rows), 6),
        "derived_day_pnl": round(sum(row.get("day_pnl") or 0 for row in rows), 6),
        "header_verified": header_semantics["header_verified"],
        "header_status": header_semantics["status"],
    }
    if summary:
        metrics = summary.get("metrics_raw") or {}
        expected = {}
        for key, check_key in (
            ("stock_market_value", "derived_market_value"),
            ("position_pnl", "derived_pnl"),
            ("day_pnl", "derived_day_pnl"),
        ):
            value = _as_number(str(metrics.get(key, "")))
            expected[key] = value
            checks[f"{key}_matches_summary"] = value is not None and abs(value - checks[check_key]) < 0.02
        checks["summary_cross_checks_pass"] = all(
            value for key, value in checks.items() if key.endswith("_matches_summary")
        )
        checks["summary_expected"] = expected
    if not header_semantics["header_verified"]:
        candidate_state = header_semantics["status"]
    elif rows:
        candidate_state = "symbol_rows_detected"
    elif header_detections:
        candidate_state = "header_only_no_symbol_rows"
    else:
        candidate_state = "no_symbol_rows_detected"
    return {
        "schema": "ths_virtual_grid_ocr_candidate_v1",
        "status": "candidate_only",
        "source": "ths_virtual_grid_printwindow_ocr",
        "kind": expected_kind,
        "mapping_kind": mapping.get("kind") or expected_kind,
        "image_path": str(image_path),
        "ocr_elapsed": elapsed,
        "detections": detections,
        "header_detections": header_detections,
        "header_semantics": header_semantics,
        "rows": rows,
        "candidate_state": candidate_state,
        "checks": checks,
        "structured_rows": False,
        "semantic_verified": False,
        "header_semantic_verified": bool(header_semantics["header_verified"]),
        "execution_admitted": False,
        "ledger_mutated": False,
        "submitted": False,
        "next_action": (
            "The captured page header does not match the requested grid; do not use rows. Foreground the exact THS orders/trades page and recapture."
            if not header_semantics["header_verified"] else
            "Review OCR rows against the native THS export and import only the explicit structured export; never size orders from this candidate alone."
            if rows else
            "No structured security rows were detected; do not interpret this image as zero orders or zero trades. Verify the exact page or import an explicit THS export."
        ),
    }
