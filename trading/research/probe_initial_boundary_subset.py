"""Probe raw price continuity for a bounded missing-start subset.

This consumer is diagnostic only. It downloads raw ``adjustflag=3`` history
from 1990 through a frozen cutoff, compares it with cached factor histories,
and records which codes satisfy the evidence-bound ``initial_level`` bridge.
It never rewrites source views or creates an adjusted-price panel.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket
import time
import uuid

import pandas as pd

from a_stock_committee.market_data import BAOSTOCK_FIELDS, baostock_session
from .daily_source_probe import select_sdk
from ..market_dataset import provider_frame, write_frame, write_json
from .price_boundaries import initial_level


def _patch_legacy_pandas_append() -> bool:
    """Keep the pinned BaoStock 0.9.4 resultset usable on pandas 2.x."""
    if hasattr(pd.DataFrame, "append"):
        return False

    def append(self, other, ignore_index=False, verify_integrity=False, sort=False):
        return pd.concat([self, other], ignore_index=ignore_index,
                         verify_integrity=verify_integrity, sort=sort)

    pd.DataFrame.append = append
    return True


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _read_codes(path: Path) -> list[str]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(values, dict):
        values = values.get("codes", [])
    if not values or values != sorted(set(values)):
        raise ValueError("Codes must be a nonempty sorted unique list")
    if not all(isinstance(value, str) and value.startswith(("sh.", "sz.")) for value in values):
        raise ValueError("Codes must use provider identities")
    return values


def run(codes_path: Path, factor_dir: Path, master_path: Path, sdk_wheel: Path,
        raw_start: str, end: str, destination: Path | None = None) -> Path:
    codes = _read_codes(codes_path)
    factor_dir, master_path = factor_dir.resolve(), master_path.resolve()
    master = pd.read_csv(master_path, dtype=str, keep_default_na=False).set_index("code")
    if master.index.has_duplicates or not set(codes) <= set(master.index):
        raise ValueError("Listing master does not cover the requested codes")

    folder = destination or Path("D:/QuantificationData/trading/research/price_adjustment_probes") / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_raw_boundary32_" + uuid.uuid4().hex[:8])
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(codes_path, folder / "input_codes.json")
    shutil.copy2(master_path, folder / "input_listing_master.csv")
    sdk = select_sdk(sdk_wheel)
    pandas_append_shim = _patch_legacy_pandas_append()

    receipt = {
        "schema": "price_adjustment_initial_boundary_probe_v1",
        "purpose": "Bounded raw adjustflag=3 continuity check for diagnosed missing-start codes",
        "codes": codes,
        "raw_start": raw_start,
        "end": end,
        "factor_dir": str(factor_dir),
        "factor_dir_codes": codes,
        "master": str(master_path),
        "sdk": sdk,
        "pandas_append_shim": pandas_append_shim,
        "status": "running",
        "entries": [],
        "outcomes": [],
        "execution_admitted": False,
        "promotion": False,
    }
    write_json(folder / "receipt.json", receipt)

    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(30)
        with baostock_session() as bs:
            calendar_response = bs.query_trade_dates(start_date=raw_start, end_date=end)
            calendar_frame = provider_frame(calendar_response)
            if calendar_response.error_code != "0" or calendar_frame.empty:
                raise ValueError(f"Calendar query failed: {calendar_response.error_msg}")
            calendar = calendar_frame.loc[calendar_frame.is_trading_day.eq("1"), "calendar_date"].tolist()
            if not calendar:
                raise ValueError("No trading dates returned")
            write_frame(folder / "calendar.csv", pd.DataFrame({"date": calendar}))
            receipt["calendar"] = {
                "start": calendar[0], "end": calendar[-1], "sessions": len(calendar),
                "file": "calendar.csv", "sha256": _hash(folder / "calendar.csv"),
            }
            write_json(folder / "receipt.json", receipt)

            for code in codes:
                factor_path = factor_dir / (code.replace(".", "_") + "_factors.parquet")
                if not factor_path.exists():
                    raise FileNotFoundError(factor_path)
                factors = pd.read_parquet(factor_path).sort_values("dividOperateDate").reset_index(drop=True)
                if factors.empty or not factors.code.eq(code).all():
                    raise ValueError(f"Invalid factor history for {code}")
                begin = time.monotonic()
                response = bs.query_history_k_data_plus(
                    code, BAOSTOCK_FIELDS, start_date=raw_start, end_date=end,
                    frequency="d", adjustflag="3")
                raw = provider_frame(response)
                name = code.replace(".", "_") + "_raw1990.parquet"
                write_frame(folder / name, raw)
                entry = {
                    "code": code, "kind": "raw", "start": raw_start, "end": end,
                    "method": "query_history_k_data_plus", "adjustflag": "3",
                    "file": name, "rows": len(raw), "error_code": response.error_code,
                    "error_msg": response.error_msg, "sha256": _hash(folder / name),
                    "elapsed_seconds": round(time.monotonic() - begin, 3),
                }
                receipt["entries"].append(entry)
                write_json(folder / "receipt.json", receipt)
                if response.error_code != "0" or raw.empty or not raw.code.eq(code).all():
                    raise ValueError(f"Invalid raw response for {code}: {response.error_msg}")
                if set(raw.columns) != set(BAOSTOCK_FIELDS.split(",")) or raw.date.duplicated().any():
                    raise ValueError(f"Raw schema/identity changed for {code}")
                if not raw.adjustflag.eq("3").all() or not raw.date.between(raw_start, end).all():
                    raise ValueError(f"Raw adjustment basis/date changed for {code}")
                ipo = master.at[code, "ipoDate"]
                outcome = {"code": code, "raw_rows": len(raw), "ipoDate": ipo,
                           "first_raw_date": str(raw.date.iloc[0]),
                           "first_factor_date": str(factors.dividOperateDate.iloc[0]),
                           "status": "not_evaluated", "proof": None}
                try:
                    derived, proof = initial_level(raw, factors, ipo, calendar)
                    outcome.update(status="derivable", proof=proof, derived_level=derived)
                except Exception as error:  # evidence result, not a process failure
                    outcome.update(status="not_derivable", error=f"{type(error).__name__}: {error}")
                receipt["outcomes"].append(outcome)
                write_json(folder / "receipt.json", receipt)
                time.sleep(0.25)
        receipt["status"] = "completed_probe"
    except Exception as error:
        receipt.update(status="failed_probe", error=f"{type(error).__name__}: {error}")
    finally:
        socket.setdefaulttimeout(old_timeout)
        receipt["finished_at"] = datetime.now().astimezone().isoformat()
        receipt["summary"] = {
            "codes": len(codes),
            "raw_success_count": sum(e.get("error_code") == "0" for e in receipt["entries"]),
            "derivable_count": sum(o.get("status") == "derivable" for o in receipt["outcomes"]),
            "not_derivable_count": sum(o.get("status") == "not_derivable" for o in receipt["outcomes"]),
        }
        write_json(folder / "receipt.json", receipt)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", type=Path, required=True)
    parser.add_argument("--factor-dir", type=Path, required=True)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--sdk-wheel", type=Path, required=True)
    parser.add_argument("--raw-start", default="1990-01-01")
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    folder = run(args.codes, args.factor_dir, args.master, args.sdk_wheel,
                 args.raw_start, args.end, args.out)
    result = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    print(json.dumps({"folder": str(folder), "status": result["status"],
                      "summary": result.get("summary")}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed_probe" else 1


if __name__ == "__main__":
    raise SystemExit(main())
