"""Unified entrypoint for shortline touchability diagnostics.

Both subcommands remain research-only.  They consume the frozen corrected
shortline panel, never change the canonical feed, and never create a target or
THS payload.
"""

from __future__ import annotations

import argparse
import runpy
from pathlib import Path


ROOT = Path(r"D:\Quantification\outputs\shortline_research\20261014_limitup_continuation")
PROXY_SCRIPT = ROOT / "build_daily_touchability_proxy.py"
ADJUSTED_SCRIPT = ROOT / "run_touchability_adjusted_shortline.py"


def _run(path: Path) -> int:
    if not path.is_file():
        raise FileNotFoundError(path)
    namespace = runpy.run_path(str(path), run_name="shortline_touchability_run")
    namespace["main"]()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("proxy", "adjusted-model"),
                        help="daily OHLC proxy or fixed-fold adjusted-label diagnostic")
    args = parser.parse_args(argv)
    return _run(PROXY_SCRIPT if args.mode == "proxy" else ADJUSTED_SCRIPT)


if __name__ == "__main__":
    raise SystemExit(main())
