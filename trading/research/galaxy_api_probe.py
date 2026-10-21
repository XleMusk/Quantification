"""Prepare a non-connecting, read-only discovery probe for Galaxy broker APIs.

The probe only inspects local package metadata, bounded install paths, process
names, and the *names* of relevant environment variables.  It never imports a
broker SDK, opens a socket, logs in, reads an account, submits/cancels an order,
or changes the execution gate.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from .experiment import write_json


BROKER = "中国银河证券"
DOCS = {
    "galaxy_pb_services": "https://www.chinastock.com.cn/newsite/cgs-services/private/privateFeature.html",
    "galaxy_tfast": "https://www.chinastock.com.cn/newsite/cgs-services/strategyTrade/TFast.html",
    "galaxy_xingyao_data": "https://www.chinastock.com.cn/newsite/cgs-services/strategyTrade/geWuInstitution.html",
}

API_CANDIDATES = {
    "qmt": {"packages": ("xtquant",), "patterns": ("qmt", "xtqmt", "迅投")},
    "miniqmt": {"packages": ("xtquant",), "patterns": ("miniqmt", "xtminiqmt", "xtquant")},
    "ptrade": {"packages": ("ptrade", "ptrade_capitalized"), "patterns": ("ptrade", "恒生ptrade")},
    "tfast": {"packages": (), "patterns": ("tfast", "银河策略", "策略云")},
}

PACKAGE_IMPORTS = {
    "xtquant": "xtquant",
    "ptrade": "ptrade",
    "ptrade_capitalized": "PTrade",
}

def _default_roots() -> list[Path]:
    roots = [
        Path("C:/迅投"), Path("C:/迅投极速交易终端"), Path("C:/QMT"),
        Path("C:/MiniQMT"), Path("C:/TFast"), Path("C:/PTrade"),
        Path("D:/迅投"), Path("D:/迅投极速交易终端"), Path("D:/QMT"),
        Path("D:/MiniQMT"), Path("D:/TFast"), Path("D:/PTrade"),
        Path("C:/银河证券"), Path("D:/银河证券"),
    ]
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
        value = os.environ.get(name)
        if value:
            roots.extend([Path(value) / "迅投", Path(value) / "QMT", Path(value) / "TFast", Path(value) / "PTrade"])
    unique = []
    seen = set()
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _bounded_files(roots: list[Path], *, max_depth: int = 2, max_entries: int = 5000) -> list[Path]:
    """Enumerate a small set of local paths without scanning whole drives."""
    found = []
    visited = 0
    for root in roots:
        if not root.is_dir():
            continue
        queue = [(root, 0)]
        while queue and visited < max_entries:
            current, depth = queue.pop(0)
            try:
                children = list(current.iterdir())
            except (OSError, PermissionError):
                continue
            for child in children:
                visited += 1
                if visited > max_entries:
                    break
                if child.is_file() and child.name.lower().endswith((".exe", ".dll", ".pyd", ".whl")):
                    found.append(child)
                elif child.is_dir() and depth < max_depth:
                    queue.append((child, depth + 1))
    return found


def _package_probe() -> dict:
    result = {}
    for label, package in PACKAGE_IMPORTS.items():
        try:
            spec = importlib.util.find_spec(package)
            result[label] = {"module": package, "present": spec is not None, "origin": str(spec.origin) if spec and spec.origin else None}
        except (ImportError, AttributeError, ValueError) as exc:
            result[label] = {"module": package, "present": False, "origin": None, "probe_error": type(exc).__name__}
    return result


def _process_probe() -> dict:
    try:
        completed = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unavailable", "matched": [], "error": type(exc).__name__}
    matched = []
    try:
        rows = csv.reader(completed.stdout.splitlines())
        for row in rows:
            if len(row) < 2:
                continue
            name = row[0]
            if any(pattern in name.lower() for pattern in ("qmt", "xtmini", "ptrade", "tfast", "迅投")):
                matched.append({"image_name": name, "pid": row[1]})
    except csv.Error:
        return {"status": "unparseable", "matched": []}
    return {"status": "completed", "matched": matched}


def _environment_presence() -> dict:
    tokens = ("QMT", "PTRADE", "TFAST", "GALAXY", "XTQUANT")
    names = sorted(name for name in os.environ if any(token in name.upper() for token in tokens))
    return {"relevant_names_present": names, "values_read": False}


def build_probe(*, search_roots: list[Path] | None = None) -> dict:
    roots = search_roots or _default_roots()
    files = _bounded_files(roots)
    packages = _package_probe()
    processes = _process_probe()
    env = _environment_presence()
    lower_files = [(path, path.name.lower()) for path in files]

    matrix = {}
    for candidate, spec in API_CANDIDATES.items():
        package_hits = [name for name in spec["packages"] if packages.get(name, {}).get("present")]
        path_hits = [str(path) for path, name in lower_files
                     if any(pattern in name for pattern in spec["patterns"])]
        matrix[candidate] = {
            "local_package_hits": package_hits,
            "local_path_hits": sorted(set(path_hits)),
            "account_permission_verified": False,
            "connection_attempted": False,
            "login_attempted": False,
            "account_read_attempted": False,
            "order_submission_attempted": False,
            "cancellation_attempted": False,
        }

    return {
        "schema": "galaxy_api_readonly_probe_v1",
        "status": "prepared_no_connection",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "broker": BROKER,
        "execution_admitted": False,
        "login_attempted": False,
        "broker_io_attempted": False,
        "client_interacted": False,
        "submitted": False,
        "cancelled": False,
        "ledger_mutated": False,
        "read_contract": {
            "required_account_reads": ["asset", "positions", "orders", "trades"],
            "required_order_methods_to_verify_later": ["order_stock", "cancel_order_stock"],
            "current_probe_action": "local_discovery_only",
        },
        "api_candidates": matrix,
        "local_discovery": {
            "python_executable": sys.executable,
            "python_version": sys.version.split()[0],
            "package_probe": packages,
            "process_probe": processes,
            "environment_presence": env,
            "search_roots": [str(root) for root in roots],
            "bounded_file_hits": sorted(set(str(path) for path in files)),
        },
        "official_reference_pages": DOCS,
        "account_permission_status": "unverified",
        "next_step": "After official Galaxy entitlement and SDK are supplied, run a separate read-only connection probe with no order methods enabled.",
        "limitations": [
            "Local discovery cannot prove account permissions or柜台 connectivity.",
            "No SDK import, socket, login, account query, order or cancellation was attempted.",
            "Process and path absence is not proof that Galaxy does not support a product.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="Optional JSON receipt path")
    parser.add_argument("--search-root", action="append", type=Path, help="Additional bounded local root; repeatable")
    args = parser.parse_args(argv)
    result = build_probe(search_roots=args.search_root or None)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
