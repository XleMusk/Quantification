"""Legacy symbol-format parsing without loading market providers or the committee."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolInfo:
    raw: str
    market: str
    code: str
    security_type: str
    baostock_code: str
    vt_symbol: str
    qlib_code: str


def normalize_symbol(symbol: str) -> SymbolInfo:
    """Preserve the existing SH/SZ aliases; this is not the D01 master."""
    raw = symbol.strip()
    if not raw:
        raise ValueError("symbol is required")
    text = raw.upper().replace(" ", "")
    if text.startswith("SH.") or text.startswith("SZ."):
        market, code = text[:2], text[3:]
    elif text.startswith("SH") and len(text) >= 8:
        market, code = "SH", text[2:]
    elif text.startswith("SZ") and len(text) >= 8:
        market, code = "SZ", text[2:]
    elif "." in text:
        code, suffix = text.split(".", 1)
        if suffix in {"SSE", "SH"}:
            market = "SH"
        elif suffix in {"SZSE", "SZ"}:
            market = "SZ"
        else:
            raise ValueError(f"unsupported exchange suffix: {suffix}")
    else:
        code, market = text, infer_market(text)
    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"unsupported A-share code: {symbol}")
    exchange = "SSE" if market == "SH" else "SZSE"
    return SymbolInfo(raw, market, code, infer_security_type(code), f"{market.lower()}.{code}",
                      f"{code}.{exchange}", f"{market}{code}")


def infer_market(code: str) -> str:
    return "SH" if code.startswith(("5", "6", "9")) else "SZ"


def infer_security_type(code: str) -> str:
    """Retain legacy prefix inference; never treat this as verified ETF identity."""
    return "fund" if code.startswith(("15", "16", "18", "51", "56", "58")) else "stock"
