"""Explicit known-after IPO metadata for REPLAY; no new-stock matching model.

First observed raw bars are not IPO dates. Official dated evidence and a saved
retrieval-time master may provide research dates, but neither clears the full
PIT/actions/live gates. First five sessions remain unsupported for execution.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import csv
import json
from pathlib import Path
from urllib.parse import urlparse

from ..contracts import TradingMode
from .leader_daily_signals import _calendar_view


SCHEMA = "research_ipo_qualification_v1"
SYMBOL, ISSUER, SHORT_NAME = "301660.SZSE", "粤芯半导体技术股份有限公司", "粤芯半导体"


def _time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("IPO availability requires an explicit timezone")
    return parsed.astimezone(timezone(timedelta(hours=8)))


def _digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _canonical_day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("IPO dates must be canonical ISO dates")
    return value


@dataclass(frozen=True)
class IpoQualification:
    records: dict
    calendar: tuple
    verification: dict

    def qualify(self, day, symbol, *, decision_as_of=None):
        """Assess a historical feature day using only a declared decision cutoff.

        Later decisions may use listing facts already available by that later
        cutoff. This never changes the Engine's same-day assessment contract.
        """
        return self._assess(symbol, day, decision_as_of=decision_as_of, historical_feature=True)

    def assess(self, symbol, day, *, decision_as_of=None):
        return self._assess(symbol, day, decision_as_of=decision_as_of, historical_feature=False)

    def _assess(self, symbol, day, *, decision_as_of, historical_feature):
        _canonical_day(day)
        cutoff = _time(decision_as_of or day + "T15:05:00+08:00")
        if cutoff.date().isoformat() < day or (not historical_feature and cutoff.date().isoformat() != day):
            raise ValueError("IPO cutoff must match the exact decision day")
        record = self.records.get(symbol)
        result = {"symbol": symbol, "day": day, "decision_as_of": cutoff.isoformat(),
                  "ipo_date": None, "listing_session_ordinal": None, "price_limit_pct": None,
                  "no_price_limit_first_five": None, "engine_block_reason": "missing_listing_date",
                  "knowledge_status": "unknown", "st_status_inferred": False,
                  "ipo_window_feature_support": False, "ipo_feature_exclusion_reason": "listing_window_unknown",
                  "pit_verified": False, "corporate_actions_verified": False,
                  "paper_admitted": False, "execution_admitted": False}
        if record is None or _time(record["available_at"]) > cutoff:
            return result
        ipo = record["ipo_date"]
        result.update(ipo_date=ipo, knowledge_status="known_after_source_availability",
                      source_basis=record["source_basis"], available_at=record["available_at"],
                      historical_publication_archive_verified=False)
        if day < ipo:
            result["engine_block_reason"] = "not_yet_listed"
            return result
        if day not in self.calendar:
            result["engine_block_reason"] = "listing_calendar_incomplete"
            return result
        if ipo in self.calendar:
            ordinal = bisect_right(self.calendar, day) - bisect_left(self.calendar, ipo)
            result["listing_session_ordinal"] = ordinal
        elif ipo < self.calendar[0] and bisect_right(self.calendar, day) >= 6:
            # Six known current-year sessions after an earlier IPO suffice to
            # rule out its first five, without inventing an exact ordinal.
            ordinal = 6
            result["listing_session_ordinal_lower_bound"] = 6
        else:
            result["engine_block_reason"] = "listing_calendar_incomplete"
            return result
        if record.get("board") != "chinext":
            # The saved provider master dates support the existing generic IPO
            # guard only. This narrow overlay does not certify all board rules.
            result["engine_block_reason"] = "unsupported_first_five_listing_sessions" if ordinal <= 5 else None
            result.update(ipo_window_feature_support=ordinal > 5,
                          ipo_feature_exclusion_reason="no_price_limit_first_five_not_limit_up_streak" if ordinal <= 5 else None)
            return result
        if day < record.get("rule_effective_from", "0001-01-01"):
            result["engine_block_reason"] = "ipo_rule_not_yet_effective"
            return result
        result.update(no_price_limit_first_five=ordinal <= 5,
                      price_limit_pct=None if ordinal <= 5 else 20,
                      ipo_window_feature_support=ordinal > 5,
                      ipo_feature_exclusion_reason="no_price_limit_first_five_not_limit_up_streak" if ordinal <= 5 else None,
                      engine_block_reason="unsupported_first_five_listing_sessions" if ordinal <= 5 else None)
        return result


def load_ipo_qualification(evidence_path, calendar_path, *, master_run=None):
    """Opt into a saved listing bundle; legacy evidence keeps its exact contract."""
    evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    if evidence.get("schema") == "research_ipo_listing_bundle_v1":
        from .ipo_listing_supplement import load_listing_bundle
        return load_listing_bundle(evidence_path, calendar_path, master_run=master_run)
    return _load_legacy_ipo_qualification(evidence_path, calendar_path, master_run=master_run)


def _load_legacy_ipo_qualification(evidence_path, calendar_path, *, master_run=None):
    """Read exact saved official bytes; optional provider master stays non-PIT."""
    evidence_path, calendar_path = Path(evidence_path).resolve(), Path(calendar_path).resolve()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if (evidence.get("schema") != SCHEMA or evidence.get("execution_admitted") is not False
            or evidence.get("pit_verified") is not False):
        raise ValueError("Expected unadmitted research IPO evidence")
    receipt_path = Path(evidence["source_receipt"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("schema") != "ipo_qualification_official_sources_v1" or receipt.get("execution_admitted") is not False:
        raise ValueError("Invalid official IPO source receipt")
    sources = {}
    for source in receipt["sources"]:
        identity = source["source_id"]
        if identity in sources or source.get("status") != "downloaded":
            raise ValueError("Duplicate or incomplete IPO source")
        host = urlparse(source["url"]).hostname or ""
        if not any(host == domain or host.endswith("." + domain) for domain in ("szse.cn", "cninfo.com.cn")):
            raise ValueError("IPO source is not official exchange/disclosure hosting")
        if _digest(source["raw_path"]) != source["sha256"] or Path(source["raw_path"]).stat().st_size != source["byte_count"]:
            raise ValueError("IPO source bytes changed")
        _time(source["retrieved_at"])
        available = source["publication_date"] + "T23:59:59+08:00"
        if source.get("available_at") != available or source.get("publication_time_known") is not False:
            raise ValueError("Unknown IPO release time must retain conservative publication-day end")
        sources[identity] = source
    record = evidence["instrument"]
    if (record.get("symbol") != SYMBOL or record.get("issuer") != ISSUER
            or record.get("name") != SHORT_NAME or record.get("board") != "chinext"
            or record.get("ipo_date") != "2026-10-13" or record.get("rule_effective_from") != "2026-07-06"
            or record.get("first_no_limit_sessions") != 5 or record.get("following_price_limit_pct") != 20):
        raise ValueError("Unsupported or contradictory curated IPO identity/rule")
    expected_sources = {"szse_listing_notice", "issuer_listing_notice", "szse_2026_trading_rules", "szse_2026_rules_effective_notice"}
    if set(record.get("source_ids", [])) != expected_sources or not expected_sources <= set(sources):
        raise ValueError("IPO identity and effective-rule sources are incomplete")
    known_at = max(sources[identity]["available_at"] for identity in record["source_ids"])
    if record.get("available_at") != known_at:
        raise ValueError("IPO availability differs from its supporting sources")
    payload = json.loads(calendar_path.read_text(encoding="utf-8"))
    year = int(payload["year"])
    sessions, _, status = _calendar_view(payload, f"{year}-01-01", f"{year}-12-31", [])
    if status != "pinned_static_exchange_calendar" or record["ipo_date"] not in sessions:
        raise ValueError("IPO overlay requires a complete pinned listing-year exchange calendar")
    records, master_verification = {}, None
    if master_run is not None:
        master_run = Path(master_run).resolve()
        manifest = json.loads(master_run.read_text(encoding="utf-8"))
        master_path = master_run.parent / manifest["master_file"]
        if (manifest.get("schema") != "instrument_master_v1" or manifest.get("execution_admitted") is not False
                or _digest(master_path) != manifest.get("master_sha256")):
            raise ValueError("Research instrument master provenance changed")
        available = manifest["created_at"]
        _time(available)
        with master_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                symbol = row["canonical_symbol"]
                if symbol in records:
                    raise ValueError("Duplicate master instrument")
                ipo = _canonical_day(row["provider_ipo_date"])
                records[symbol] = {"ipo_date": ipo, "available_at": available, "board": row["board"],
                    "source_basis": "retrieval_time_provider_master_not_exchange_PIT"}
        if len(records) != manifest["instrument_count"]:
            raise ValueError("Master instrument count changed")
        master_verification = {"run": str(master_run), "master_sha256": manifest["master_sha256"],
                               "available_at": available, "pit_verified": False}
    records[SYMBOL] = {**deepcopy(record), "source_basis": "official_listing_and_issuer_notice_known_after_conservative_publication_date"}
    return IpoQualification(records, tuple(sessions), {"evidence_file": str(evidence_path),
        "evidence_sha256": _digest(evidence_path), "source_receipt_sha256": _digest(receipt_path),
        "calendar": str(calendar_path), "calendar_sha256": _digest(calendar_path),
        "official_source_count": len(sources), "research_master": master_verification,
        "pit_verified": False, "historical_publication_archive_verified": False,
        "corporate_actions_verified": False, "execution_admitted": False})


class IpoQualifiedResearchFeed:
    """Read-only explicit feed overlay; the existing feed is never mutated.

    The Engine must opt into ``research_listing_limit_reason``. Its default
    listing guard and every source/current/next raw/ledger check remain intact.
    """
    def __init__(self, base, qualification):
        if not isinstance(qualification, IpoQualification):
            raise ValueError("Typed IPO qualification is required")
        self.base, self.ipo_qualification = base, qualification
        self.instruments = deepcopy(base.instruments)
        for symbol, values in self.instruments.items():
            record = qualification.records.get(symbol)
            values["ipoDate"] = record["ipo_date"] if record else None
            values["ipoDate_basis"] = record["source_basis"] if record else "unknown_not_first_observed"
        self.manifest = {**base.manifest, "ipo_qualification_overlay": deepcopy(qualification.verification),
                         "replay_only": True, "execution_admitted": False}

    def __getattr__(self, name):
        return getattr(self.base, name)

    def research_listing_limit_reason(self, symbol, day, context):
        if context.mode != TradingMode.REPLAY:
            raise ValueError("IPO qualification overlay is replay-only")
        return self.ipo_qualification.assess(symbol, day)["engine_block_reason"]
