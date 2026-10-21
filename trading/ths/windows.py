"""Exact-window control access; no clicks, hotkeys, login or popup dismissal."""
import os
import re


def version(path):
    import win32api
    value = win32api.GetFileVersionInfo(str(path), "\\")
    return ".".join(str(x) for x in (value["FileVersionMS"] >> 16, value["FileVersionMS"] & 65535,
                                   value["FileVersionLS"] >> 16, value["FileVersionLS"] & 65535))


def redact(text):
    return re.sub(r"\d{7,}", "[redacted]", text)


def probe():
    if os.name != "nt":
        return {"status": "unsupported_platform", "windows": [], "processes": []}
    import psutil
    import win32gui
    import win32process
    candidates, windows, errors = {}, [], []
    names = {"hexin.exe", "hexinlauncher.exe", "xiadan.exe", "happ.exe"}
    for process in psutil.process_iter(["pid", "name", "exe"]):
        info = process.info
        if (info["name"] or "").lower() in names:
            item = dict(info)
            try:
                item["version"] = version(info["exe"]) if info["exe"] else None
            except Exception as error:
                item["version"] = None
                errors.append({"pid": info["pid"], "error": str(error)})
            candidates[info["pid"]] = item
    def found(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in candidates and win32gui.IsWindowVisible(hwnd):
            windows.append({"hwnd": hwnd, "pid": pid, "title": redact(win32gui.GetWindowText(hwnd)),
                            "class_name": win32gui.GetClassName(hwnd), "rect": win32gui.GetWindowRect(hwnd),
                            "enabled": bool(win32gui.IsWindowEnabled(hwnd)), "minimized": bool(win32gui.IsIconic(hwnd))})
    win32gui.EnumWindows(found, None)
    return {"status": "candidates_found" if windows else "no_candidate_window", "windows": windows,
            "processes": list(candidates.values()), "errors": errors, "read_only": True}


class ControlWindow:
    def __init__(self, target):
        from pywinauto import Desktop
        self.target = target
        if target["backend"] not in {"win32", "uia"}:
            raise ValueError("Unsupported control backend")
        self.desktop = Desktop(backend=target["backend"])
        self.root = self.desktop.window(handle=int(target["hwnd"]))
        self.check_identity()

    def check_identity(self):
        import psutil
        import win32gui
        import win32process
        hwnd = int(self.target["hwnd"])
        if not win32gui.IsWindow(hwnd):
            raise RuntimeError("Window no longer exists; probe again")
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid != self.target["pid"]:
            raise RuntimeError("Window process changed")
        actual = psutil.Process(pid).exe()
        if os.path.normcase(os.path.abspath(actual)) != os.path.normcase(os.path.abspath(self.target["exe"])):
            raise RuntimeError("Unexpected client executable")
        if version(actual) != self.target["version"]:
            raise RuntimeError("Client version changed; control mapping must be rechecked")
        if win32gui.GetClassName(hwnd) != self.target["class_name"]:
            raise RuntimeError("Unexpected root window class")

    def ready(self, *, require_foreground=True):
        import ctypes
        from ctypes import wintypes
        import win32gui
        self.check_identity()
        hwnd = int(self.target["hwnd"])
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.IsWindowEnabled(hwnd) or win32gui.IsIconic(hwnd):
            raise RuntimeError("Client must be visible, enabled and not minimized")
        if require_foreground and win32gui.GetForegroundWindow() != hwnd:
            raise RuntimeError("User must put the exact trade window in foreground; no focus stealing")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        handle = user32.OpenInputDesktop(0, False, 0x0001)
        if not handle:
            raise RuntimeError("Interactive desktop unavailable")
        user32.CloseDesktop(handle)
        blockers = []
        def owned(other, _):
            if other != hwnd and win32gui.IsWindowVisible(other) and win32gui.GetWindow(other, 4) == hwnd:
                blockers.append(other)
        win32gui.EnumWindows(owned, None)
        if blockers:
            raise RuntimeError("Owned popup present; user must handle it without automatic confirmation")

    def control(self, selector):
        allowed = {"control_id", "auto_id", "class_name", "control_type", "title"}
        if not selector or set(selector) - allowed or not ({"control_id", "auto_id"} & set(selector)):
            raise ValueError("Use an inspected control/automation ID; no guessed index or fuzzy match")
        result = self.root.child_window(**selector).wrapper_object()
        if not result.is_visible() or not result.is_enabled():
            raise RuntimeError("Mapped control is not visible/enabled")
        if result.element_info.process_id != self.target["pid"]:
            raise RuntimeError("Control belongs to another process")
        return result

    def text(self, selector):
        return self.control(selector).window_text().strip()

    def summary_text(self, selector, process=None):
        """Read one explicitly mapped summary control, optionally owned by a verified helper process."""
        allowed = {"control_id", "auto_id", "class_name", "control_type", "title", "process_id"}
        if not selector or set(selector) - allowed or not ({"control_id", "auto_id"} & set(selector)):
            raise ValueError("Use an inspected summary control/automation ID")
        query = {key: value for key, value in selector.items() if key != "process_id"}
        result = self.root.child_window(**query).wrapper_object()
        if not result.is_visible() or not result.is_enabled():
            raise RuntimeError("Mapped summary control is not visible/enabled")
        actual_pid = int(result.element_info.process_id)
        expected_pid = int(selector.get("process_id", (process or {}).get("pid", self.target["pid"])))
        if actual_pid != expected_pid:
            raise RuntimeError("Summary control belongs to an unexpected process")
        if process:
            import psutil
            executable = psutil.Process(actual_pid).exe()
            if os.path.normcase(os.path.abspath(executable)) != os.path.normcase(os.path.abspath(process["exe"])):
                raise RuntimeError("Summary helper executable changed")
            if version(executable) != process["version"]:
                raise RuntimeError("Summary helper version changed; mapping must be rechecked")
        return result.window_text().strip()

    def check_fields(self, selectors):
        controls = [self.control(selector) for selector in selectors]
        if any(c.friendly_class_name() != "Edit" for c in controls):
            raise ValueError("All three mapped fields must be Edit controls")
        identities = [(c.element_info.handle or getattr(c.element_info, "runtime_id", None)) for c in controls]
        if not all(identities) or len({str(i) for i in identities}) != 3:
            raise ValueError("Mapped fields resolve to overlapping or unknown controls")

    def write(self, selector, value):
        self.ready()
        control = self.control(selector)
        if control.friendly_class_name() != "Edit":
            raise ValueError("Preview can write only an inspected Edit control")
        # No type_keys/Enter/Tab fallback: those can submit a financial order.
        control.set_edit_text(value)

    def inspect(self):
        self.check_identity()
        result = []
        for item in self.root.descendants():
            info = item.element_info
            kind = item.friendly_class_name()
            # Never collect password/edit field values in a discovery report.
            label = item.window_text() if kind in {"Static", "Button", "GroupBox", "Header"} else ""
            rect = item.rectangle()
            result.append({"handle": info.handle, "control_id": info.control_id,
                           "auto_id": getattr(info, "automation_id", ""), "class_name": info.class_name,
                           "type": kind, "label": redact(label), "visible": item.is_visible(),
                           "rect": [rect.left, rect.top, rect.right, rect.bottom]})
        return result

    def read_grid(self, selector):
        self.ready()
        grid = self.control(selector)
        if grid.friendly_class_name() != "ListView" or self.target["backend"] != "win32":
            raise RuntimeError("Grid not proven readable; use user-exported CSV, never OCR/clipboard CAPTCHA bypass")
        columns = [grid.get_column(i)["text"] for i in range(grid.column_count())]
        if not columns or len(set(columns)) != len(columns):
            raise ValueError("Missing/duplicate grid headers")
        return [dict(zip(columns, [grid.get_item(row, col).text() for col in range(len(columns))]))
                for row in range(grid.item_count())]

    def _read_summary(self, selectors, *, process, require_foreground, expected_account_alias):
        """Read mapped static fields between two exact account identity reads."""
        required = {
            "account_alias", "cash_available", "cash_withdrawable", "stock_market_value",
            "total_assets", "position_pnl", "day_pnl", "day_pnl_pct",
        }
        balances = {"cash_total", "cash_frozen"}
        if set(selectors) not in (required, required | balances):
            raise ValueError("Summary mapping requires the original inspected fields and an optional total/frozen cash pair")
        if any(not isinstance(selector, dict) or not selector for selector in selectors.values()):
            raise ValueError("Summary fields require explicitly inspected selectors")
        if balances <= set(selectors):
            # Explicitly mapped balance fields cannot borrow the available,
            # withdrawable, market-value or asset field (nor one another).
            # This checks selector identity, never guesses IDs or derives cash.
            identities = [{key: value for key, value in selector.items() if key != "process_id"}
                          for selector in selectors.values()]
            if len({repr(sorted(value.items())) for value in identities}) != len(identities):
                raise ValueError("Total/frozen cash cannot reuse another summary field selector")
        if expected_account_alias is not None and (not isinstance(expected_account_alias, str)
                or not expected_account_alias.strip() or expected_account_alias != expected_account_alias.strip()):
            raise ValueError("Expected account alias must be an explicit nonempty exact label")
        self.ready(require_foreground=require_foreground)
        before = self.summary_text(selectors["account_alias"], process)
        if not before or (expected_account_alias is not None and before != expected_account_alias):
            raise RuntimeError("Account alias differs from the explicitly expected account")
        values = {key: self.summary_text(selector, process) for key, selector in selectors.items()
                  if key != "account_alias"}
        after = self.summary_text(selectors["account_alias"], process)
        self.ready(require_foreground=require_foreground)
        if before != after:
            raise RuntimeError("Account alias changed during summary read; no account observation returned")
        return {"account_alias": before, **values}

    def read_summary(self, selectors, *, process=None, expected_account_alias=None):
        """Read explicitly inspected static account fields without touching a grid."""
        return self._read_summary(selectors, process=process, require_foreground=True,
                                  expected_account_alias=expected_account_alias)

    def read_summary_background(self, selectors, *, process=None, expected_account_alias=None):
        """Read inspected static fields from the exact visible client without focusing it."""
        return self._read_summary(selectors, process=process, require_foreground=False,
                                  expected_account_alias=expected_account_alias)

    def capture_grid_image(self, selector, output_path, *, process=None):
        """Capture an inspected virtual grid without focusing or editing the client."""
        self.ready(require_foreground=False)
        allowed = {"control_id", "auto_id", "class_name", "control_type", "title", "process_id"}
        if not selector or set(selector) - allowed or not ({"control_id", "auto_id"} & set(selector)):
            raise ValueError("Use an inspected grid control/automation ID")
        query = {key: value for key, value in selector.items() if key != "process_id"}
        grid = self.root.child_window(**query).wrapper_object()
        if grid.friendly_class_name() != "CVirtualGridCtrl":
            raise RuntimeError("Grid is not the inspected CVirtualGridCtrl")
        handle = int(grid.element_info.handle)
        actual_pid = int(grid.element_info.process_id)
        expected_pid = int(selector.get("process_id", (process or {}).get("pid", self.target["pid"])))
        if actual_pid != expected_pid:
            raise RuntimeError("Grid belongs to an unexpected process")
        if process:
            import psutil
            executable = psutil.Process(actual_pid).exe()
            if os.path.normcase(os.path.abspath(executable)) != os.path.normcase(os.path.abspath(process["exe"])):
                raise RuntimeError("Grid helper executable changed")
            if version(executable) != process["version"]:
                raise RuntimeError("Grid helper version changed; mapping must be rechecked")
        rect = win32_rect = grid.rectangle()
        width, height = rect.width(), rect.height()
        if width <= 0 or height <= 0:
            raise RuntimeError("Grid has no visible area")
        import ctypes
        import win32gui
        import win32ui
        destination = os.fspath(output_path)
        os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
        window_dc = win32gui.GetWindowDC(handle)
        if not window_dc:
            raise RuntimeError("Cannot obtain grid window DC")
        source = win32ui.CreateDCFromHandle(window_dc)
        memory = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source, width, height)
        memory.SelectObject(bitmap)
        try:
            if not ctypes.windll.user32.PrintWindow(handle, memory.GetSafeHdc(), 2):
                raise RuntimeError("PrintWindow could not capture the grid")
            bitmap.SaveBitmapFile(memory, destination)
        finally:
            memory.DeleteDC()
            source.DeleteDC()
            win32gui.ReleaseDC(handle, window_dc)
        return {
            "image_path": destination,
            "grid_handle": handle,
            "grid_pid": actual_pid,
            "class_name": "CVirtualGridCtrl",
            "width": width,
            "height": height,
            "structured_rows": False,
            "read_only": True,
            "focused": False,
        }
