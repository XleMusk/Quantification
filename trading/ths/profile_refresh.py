"""Refresh a THS session profile from one strict read-only process probe."""

from copy import deepcopy
import json
import os
from pathlib import Path

from .windows import ControlWindow, probe


def _same_path(left, right):
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _unique_process(processes, expected, *, label):
    matches = [
        item for item in processes
        if _same_path(item.get("exe"), expected.get("exe"))
        and item.get("version") == expected.get("version")
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"{label} identity is ambiguous: expected one {expected.get('exe')} "
            f"version {expected.get('version')}, found {len(matches)}"
        )
    return matches[0]


def _unique_window(windows, processes, expected):
    process_by_pid = {int(item["pid"]): item for item in processes}
    matches = []
    for window in windows:
        process = process_by_pid.get(int(window["pid"]))
        if not process:
            continue
        if not _same_path(process.get("exe"), expected.get("exe")):
            continue
        if process.get("version") != expected.get("version"):
            continue
        if window.get("class_name") != expected.get("class_name"):
            continue
        if not window.get("enabled") or window.get("minimized"):
            continue
        matches.append((window, process))
    if len(matches) != 1:
        raise RuntimeError(
            f"THS client identity is ambiguous: expected one visible {expected.get('exe')} "
            f"version {expected.get('version')}, found {len(matches)}"
        )
    return matches[0]


def refresh_profile(profile):
    """Return a refreshed copy only after the mapped account summary reads successfully.

    This updates volatile HWND/PID values, but never invents control IDs or GUI
    field mappings. A failed summary read leaves the caller's profile untouched.
    """
    if profile.get("summary_verified") is not True:
        raise ValueError("Profile refresh requires the already inspected summary mapping; HWND discovery cannot verify it")
    expected_alias = profile.get("expected_account_alias")
    if not isinstance(expected_alias, str) or not expected_alias.strip() or expected_alias != expected_alias.strip():
        raise ValueError("Profile refresh requires an explicit expected_account_alias")
    if not isinstance(profile.get("target", {}).get("class_name"), str) or not profile["target"]["class_name"]:
        raise ValueError("Profile refresh requires the inspected exact root window class")
    current = probe()
    window, process = _unique_window(
        current.get("windows", []), current.get("processes", []), profile["target"]
    )
    helper = _unique_process(
        current.get("processes", []), profile.get("summary_process", {}), label="THS summary helper"
    )
    refreshed = deepcopy(profile)
    refreshed["target"] = {
        **window,
        "exe": process["exe"],
        "version": process["version"],
        "backend": profile["target"].get("backend", "win32"),
    }
    refreshed["summary_process"] = {
        **profile["summary_process"],
        "pid": int(helper["pid"]),
        "exe": helper["exe"],
        "version": helper["version"],
    }
    for section in ("summary", "grids"):
        for selector in (refreshed.get(section) or {}).values():
            if isinstance(selector, dict) and "process_id" in selector:
                selector["process_id"] = int(helper["pid"])

    values = ControlWindow(refreshed["target"]).read_summary_background(
        refreshed["summary"], process=refreshed["summary_process"], expected_account_alias=expected_alias
    )
    # A backend returning text is not a new field-map verification. Keep the
    # prior verified status only after the same account/class checks pass.
    if values.get("account_alias") != expected_alias:
        raise RuntimeError("Refreshed summary belongs to another account; original profile remains unchanged")
    refreshed["summary_verified"] = profile["summary_verified"]
    refreshed["session_refresh"] = {
        "status": "verified_read_only",
        "target_pid": int(process["pid"]),
        "summary_helper_pid": int(helper["pid"]),
        "account_alias": values["account_alias"],
        "expected_account_alias": expected_alias,
        "root_class_verified": True,
        "account_alias_verified": True,
        "field_mapping_reverified": False,
        "cash_balance_fields_read": {"cash_total", "cash_frozen"} <= set(values),
        "metrics_raw": {key: value for key, value in values.items() if key != "account_alias"},
    }
    return refreshed


def write_refreshed_profile(profile_path, output_path):
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8-sig"))
    refreshed = refresh_profile(profile)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return refreshed
