"""Partial positive issuer-notice evidence, never a complete safe-stock list."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from .rules import stock_board


PARSER_ID = "explicit_issuer_major_illegal_delisting_risk_v1"
NOTICE_BLOCK_REASON = "published_major_illegal_delisting_risk_buy_blocked"
COVERAGE = {"kind": "partial_positive_notices_only", "complete": False,
            "no_event_means": "unknown_not_safe", "execution_admitted": False}
TIME_BASIS = "Dated primary archive route agrees with issuer footer; exact historical posting clock unverified. Conservative next-calendar-day upper-bound proxy, applied at the first later exchange session"


def known_at_proxy(publication_day):
    day = date.fromisoformat(publication_day)
    if day.isoformat() != publication_day:
        raise ValueError("Notice publication date must be canonical")
    return datetime.combine(day + timedelta(days=1), time.min,
                            timezone(timedelta(hours=8))).isoformat()


def checked_notice_url(symbol, publication_day, url):
    stock_board(symbol)
    known_at_proxy(publication_day)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.username or parsed.port:
        raise ValueError("Expected an HTTPS dated primary issuer PDF")
    code = symbol.split(".")[0]
    if parsed.hostname == "static.cninfo.com.cn":
        match = re.fullmatch(r"/finalpage/(\d{4}-\d{2}-\d{2})/[0-9]+\.[Pp][Dd][Ff]", parsed.path)
    elif parsed.hostname in {"static.sse.com.cn", "www.sse.com.cn", "big5.sse.com.cn"}:
        match = re.fullmatch(r"(?:/site/cht/www\.sse\.com\.cn)?/disclosure/listedinfo/announcement/c/new/"
                            r"(\d{4}-\d{2}-\d{2})/" + code + r"_(\d{8})_[A-Za-z0-9]+\.pdf", parsed.path)
        if match and match[2] != publication_day.replace("-", ""):
            raise ValueError("Issuer PDF filename date differs from publication date")
    else:
        match = None
    if not match or match[1] != publication_day:
        raise ValueError("Unverified primary publisher, issuer route or date")
    return url


class NoticeRiskView:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        manifest = json.loads((self.folder / "manifest.json").read_text(encoding="utf-8"))
        raw = (self.folder / "events.json").read_bytes()
        if (manifest.get("status") != "primary_notice_risk_diagnostic_v1"
                or manifest.get("parser") != PARSER_ID or manifest.get("coverage") != COVERAGE
                or manifest.get("time_basis") != TIME_BASIS
                or manifest.get("events_sha256") != sha256(raw).hexdigest()):
            raise ValueError("Require the pinned partial primary-notice event view")
        events = json.loads(raw)
        if not isinstance(events, list) or manifest.get("events") != len(events):
            raise ValueError("Notice event count mismatch")
        seen, self.by_symbol = set(), {}
        for event in events:
            checked_notice_url(event["symbol"], event["publication_on"], event["source_url"])
            if (event["id"] in seen or event["parser"] != PARSER_ID
                    or event["category"] != "major_illegal_delisting_risk"
                    or event["known_at_proxy"] != known_at_proxy(event["publication_on"])
                    or not re.fullmatch(r"[0-9a-f]{64}", event["source_sha256"])
                    or not event["evidence_pages"]):
                raise ValueError("Invalid primary-notice risk event")
            seen.add(event["id"])
            self.by_symbol.setdefault(event["symbol"], []).append(deepcopy(event))
        for rows in self.by_symbol.values():
            rows.sort(key=lambda row: (row["publication_on"], row["id"]))
        self._binding = {"id": PARSER_ID, "snapshot": str(self.folder),
            "events_sha256": manifest["events_sha256"], "events": len(events),
            "coverage": COVERAGE, "time_basis": TIME_BASIS, "source_selection_basis": manifest.get("basis"),
            "release_rule": "No automatic expiry or clearance inference; no held-share removal or forced settlement",
            "mode": "historical_replay_only"}

    @property
    def binding(self):
        return deepcopy(self._binding)

    def active_event(self, symbol, day):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("Notice lookup needs a canonical session date")
        # Date-only notices never affect a trade on their dated publication day.
        return next((deepcopy(row) for row in self.by_symbol.get(symbol, [])
                     if row["publication_on"] < day), None)
