"""Read saved full-close progress and its exact read-only preview, never a job runner."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re


def _process_start(pid):
    # The workbench's small Python runtime has no psutil. Query only this PID
    # with the Windows standard library; no process launch or UI interaction.
    if os.name != "nt":
        import psutil
        return psutil.Process(pid).create_time()
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            raise ProcessLookupError(pid)
        raise ctypes.WinError(error)
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if exit_code.value != 259:
            raise ProcessLookupError(pid)
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return ticks / 10_000_000 - 11_644_473_600
    finally:
        kernel.CloseHandle(handle)


def _read(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("Expected an object receipt")
    return payload


def _inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("Close evidence path is outside its declared directory")
    return path


def process_identity(pid, expected_start):
    """PID alone cannot prove the detached collector is still the same process."""
    try:
        # .NET emits seven fractional digits; Python 3.10 accepts at most six.
        text = re.sub(r"(\.\d{6})\d+(?=Z|[+-]\d\d:\d\d$)", r"\1", str(expected_start))
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Process identity timestamp needs a timezone")
        actual = _process_start(int(pid))
        matched = abs(actual - stamp.timestamp()) < .001
        return {"status": "verified_alive" if matched else "pid_reused",
                "pid": int(pid), "expected_start": expected_start,
                "observed_start": datetime.fromtimestamp(actual, timezone.utc).isoformat()}
    except ProcessLookupError:
        return {"status": "not_running", "pid": pid}
    except (OSError, ImportError, TypeError, ValueError, OverflowError) as error:
        return {"status": "identity_unverified", "pid": pid, "reason": str(error)}


def close_workbench(data_root, workspace, *, inspect_process=process_identity):
    root = Path(data_root) / "research" / "full_close"
    locator_path = root / "active_job.json"
    boundary = {"research_only": True, "execution_admitted": False,
                "selection_enabled": False, "strategy_validated": False,
                "ledger_mutated": False, "provider_contacted": False,
                "job_started": False, "orders_generated": False}
    result = {"schema": "full_close_workbench_v1", "status": "missing",
              "observed_at": datetime.now().astimezone().isoformat(),
              "source_file": str(locator_path.resolve()), "preview": None, **boundary}
    if not locator_path.is_file():
        return result
    locator = _read(locator_path)
    if locator.get("schema") != "full_close_active_job_locator_v1" or locator.get("execution_admitted") is not False:
        raise ValueError("Full-close locator boundary changed")
    folder = _inside(locator["folder"], root)
    receipt_path = folder / "receipt.json"
    receipt = _read(receipt_path)
    if receipt.get("schema") != "baostock_full_dated_close_inventory_v1" or receipt.get("execution_admitted") is not False:
        raise ValueError("Full-close source receipt boundary changed")
    for key in ("selection_enabled", "strategy_validated", "stockdb_modified", "ledger_mutated"):
        if receipt.get(key) is not False:
            raise ValueError(f"Full-close source flag changed: {key}")
    process = inspect_process(locator.get("collector_pid"), locator.get("collector_process_started_at"))
    status = receipt.get("status")
    # No freshness timeout stops a collector. A live collector can be waiting
    # on a provider; a dead/reused PID can never be displayed as still running.
    display_status = "collector_not_verified" if status == "running" and process["status"] != "verified_alive" else status
    fields = ("day", "acquisition_mode", "ordinary_symbols", "completed_symbols", "logical_price_queries",
              "new_generation_price_queries", "reused_completed_symbols", "parent_generation_price_attempts",
              "nullable_suspension_rows", "execution_bar_rows", "updated_at",
              "full_dated_roster_inventory_complete", "execution_bar_coverage_complete")
    result.update(status=display_status, source_status=status,
                  source_receipt=str(receipt_path), folder=str(folder),
                  collector=process, progress={key: receipt.get(key) for key in fields},
                  source_bytes_revalidated_this_call=False,
                  completion_basis="saved receipt; exact completed preview is a separate artifact")
    watcher_path = _inside(locator["watcher_receipt"], root)
    if not watcher_path.is_file():
        return result
    watcher = _read(watcher_path)
    if watcher.get("execution_admitted") is not False or watcher.get("ledger_mutated") is not False:
        raise ValueError("Full-close watcher boundary changed")
    result["followup"] = {key: watcher.get(key) for key in
                          ("status", "updated_at", "preview_called", "preview_exit_code", "preview_output")}
    if status != "complete_full_dated_close_inventory_not_admitted" or watcher.get("preview_exit_code") != 0:
        return result
    preview_path = _inside(watcher["preview_output"], Path(workspace) / "outputs" / "leader_close_preview") / "result.json"
    preview = _read(preview_path)
    source = preview.get("source") or {}
    if (preview.get("schema") != "leader_complete_close_preview_v1"
            or preview.get("day") != receipt.get("day")
            or receipt.get("full_dated_roster_inventory_complete") is not True
            or receipt.get("completed_symbols") != receipt.get("ordinary_symbols")
            or source.get("source_folder") is None
            or Path(source["source_folder"]).resolve() != folder
            or source.get("receipt", {}).get("raw_daily_sha256") != receipt.get("raw_daily_sha256")
            or source.get("read_only_verified") is not True
            or preview.get("source_inventory_complete") is not True):
        raise ValueError("Preview does not refer to this exact completed source receipt")
    for key, required in {"candidate_panel_only": True, "execution_admitted": False,
                          "selection_enabled": False, "strategy_validated": False,
                          "orders_generated": False, "ledger_mutated": False}.items():
        if preview.get(key) is not required:
            raise ValueError(f"Preview boundary changed: {key}")
    result["preview"] = {key: preview.get(key) for key in
                         ("day", "created_at", "status", "tape", "families", "limitations")}
    result["preview"]["source_file"] = str(preview_path)
    return result
