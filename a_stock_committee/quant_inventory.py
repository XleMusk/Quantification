"""Read-only inventory of the local research assets, never a live-data claim."""

from datetime import datetime
from importlib import metadata
from pathlib import Path
import json
import subprocess
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def inspect_workspace(root=ROOT):
    root = Path(root)
    caches = []
    for path in sorted((root / "market_data/a_share/daily").glob("*.parquet")):
        frame = pd.read_parquet(path)
        dates = pd.to_datetime(frame["date"], errors="raise")
        caches.append({
            "path": str(path), "rows": len(frame),
            "start": str(dates.min().date()), "end": str(dates.max().date()),
            "adjustflag": sorted(frame["adjustflag"].astype(str).unique().tolist()),
            "duplicate_dates": int(dates.duplicated().sum()),
            "source": sorted(frame["source"].astype(str).unique().tolist()) if "source" in frame else [],
        })
    provider = root / "qlib_data/cn_data"
    calendar_path = provider / "calendars/day.txt"
    qlib_data = {"path": str(provider), "available": calendar_path.exists()}
    if calendar_path.exists():
        dates = pd.to_datetime(pd.read_csv(calendar_path, header=None)[0])
        members = pd.read_csv(provider / "instruments/csi300.txt", sep="\t", header=None,
                              names=["symbol", "start", "end"])
        qlib_data.update({
            "start": str(dates.min().date()), "end": str(dates.max().date()),
            "sessions": len(dates),
            "feature_directories": len(list((provider / "features").iterdir())),
            "csi300_historical_symbols": members.symbol.nunique(),
            "missing_member_features": [s for s in sorted(members.symbol.unique())
                                        if not (provider / "features" / s.lower() / "close.day.bin").exists()],
        })
    packages = {}
    for name in ("pandas", "numpy", "scipy", "pyarrow", "pyqlib", "baostock", "vnpy", "vnpy_ctastrategy"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    check = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True, timeout=60)
    components = {
        "committee_and_risk": root / "a_stock_committee",
        "qlib_vnpy_bridge": root / "bridge",
        "factor_tutorial": root / "quant_learning_starter/factor_lab.py",
        "learning_resources": root / "quant_learning_starter/learning_resources/RESOURCE_NAVIGATION.html",
        "fincept_terminal": Path.home() / "AppData/Local/Programs/FinceptTerminal/FinceptTerminal.exe",
    }
    return {
        "checked_at": datetime.now().astimezone().isoformat(), "root": str(root),
        "python": sys.executable, "packages": packages,
        "pip_check": {"exit_code": check.returncode, "output": (check.stdout + check.stderr).strip()},
        "components": {name: {"path": str(p), "exists": p.exists()} for name, p in components.items()},
        "vendor_projects": [p.name for p in sorted((root / "vendor").glob("*")) if p.is_dir()],
        "daily_caches": caches, "qlib_data": qlib_data,
        "live_ready": False,
        "warnings": [
            "Local data are historical snapshots, not verified current market data.",
            "The vn.py bridge uses adjusted Qlib bars directly; it is not admitted for cash/share execution research.",
            "Skill repositories and Fincept are research assistants, not validated alpha engines.",
        ],
    }


def save_inventory(out, root=ROOT):
    result = inspect_workspace(root)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "inventory.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    q = result["qlib_data"]
    lines = ["# Local A-share research inventory", "", f"Checked: {result['checked_at']}", "",
             f"Python: `{result['python']}`", "", "## Existing components", ""]
    lines += [f"- {name}: {'present' if item['exists'] else 'missing'}; `{item['path']}`"
              for name, item in result["components"].items()]
    lines += ["", "## Qlib historical data", "", json.dumps(q, ensure_ascii=False), "",
              "## Daily caches", "", "| File | Rows | Start | End | Adjustment |", "|---|---:|---|---|---|"]
    lines += [f"| {Path(c['path']).name} | {c['rows']} | {c['start']} | {c['end']} | {','.join(c['adjustflag'])} |"
              for c in result["daily_caches"]]
    lines += ["", "## Environment check", "", "```text", result["pip_check"]["output"], "```", "",
              "No packages were installed or removed by this command.", "", "## Boundaries", ""]
    lines += [f"- {warning}" for warning in result["warnings"]]
    (out / "INVENTORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result
