"""Code mapping: Qlib SH600000 <-> vnpy 600000.SSE."""


def qlib_to_vnpy(qlib_code: str) -> str:
    """SH600000 -> 600000.SSE,  SZ000001 -> 000001.SZSE"""
    market = qlib_code[:2].upper()
    code = qlib_code[2:]
    exchange = "SSE" if market == "SH" else "SZSE"
    return f"{code}.{exchange}"


def vnpy_to_qlib(vt_symbol: str) -> str:
    """600000.SSE -> SH600000,  000001.SZSE -> SZ000001"""
    code, exchange = vt_symbol.split(".")
    prefix = "SH" if exchange == "SSE" else "SZ"
    return f"{prefix}{code}"


# Reverse lookups
EXCHANGE_TO_PREFIX = {"SSE": "SH", "SZSE": "SZ"}
PREFIX_TO_EXCHANGE = {"SH": "SSE", "SZ": "SZSE"}
