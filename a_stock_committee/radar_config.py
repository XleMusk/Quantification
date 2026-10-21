"""Decision radar profiles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json


CONFIG_DIR = Path("committee_config")
DEFAULT_CONFIG_PATH = CONFIG_DIR / "radar_small_account.json"


@dataclass
class RadarConfig:
    profile_name: str = "small-account-10k"
    account_equity: float = 10_000
    style: str = "mixed-trend-first"
    holding_period: str = "1-3 months"
    max_positions: int = 2
    max_single_position_pct: float = 40
    max_total_stock_pct: float = 70
    cash_reserve_pct: float = 30
    risk_per_trade_pct: float = 0.5
    min_trade_lot: int = 100
    min_price: float = 3
    max_price: float = 60
    min_20d_amount: float = 100_000_000
    exclude_st: bool = True
    exclude_suspended: bool = True
    exclude_bj: bool = True
    exclude_star_if_no_permission: bool = True
    exclude_chinext_if_no_permission: bool = False
    has_star_permission: bool = False
    has_chinext_permission: bool = True
    top_n: int = 30


def small_account_config(account_equity: float = 10_000) -> RadarConfig:
    return RadarConfig(account_equity=account_equity)


def save_config(config: RadarConfig, path: str | Path = DEFAULT_CONFIG_PATH) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> RadarConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RadarConfig(**data)


def ensure_default_config() -> Path:
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return save_config(small_account_config(), DEFAULT_CONFIG_PATH)

