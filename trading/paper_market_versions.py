"""Immutable market inputs for the standard end-of-day paper worker.

Daily Parquet objects are content addressed. A session adds only changed data
partitions and metadata; retries use the already captured session instead of
following a provider which may have changed after an interruption.
"""

from copy import deepcopy
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import inspect
import marshal
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import settings
from .feed import HistoricalFeed


SCHEMA = "paper_market_version_v1"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT = re.compile(r"^[0-9a-f]{32}$")
_IMPLEMENTATION_FILES = ("feed.py", "engine.py", "ledger.py", "rules.py", "config.py")


def current_implementation():
    """Identity of deployed source and the functions loaded in this process.

    Continuation requires the same deployed files and loaded implementation;
    upgrading or relocating the runtime needs an explicit new account path.
    Reading old market observations does not require executing that old code.
    """
    from . import config, engine, feed, ledger, rules
    result = {}
    for module in (feed, engine, ledger, rules, config):
        functions = {}
        for name, member in vars(module).items():
            if inspect.isfunction(member) and member.__module__ == module.__name__:
                functions[name] = sha256(marshal.dumps(member.__code__)).hexdigest()
            elif inspect.isclass(member) and member.__module__ == module.__name__:
                for method, function in vars(member).items():
                    if isinstance(function, (staticmethod, classmethod)):
                        function = function.__func__
                    if hasattr(function, "__code__"):
                        functions[name + "." + method] = sha256(marshal.dumps(function.__code__)).hexdigest()
        name = module.__name__.rsplit(".", 1)[-1] + ".py"
        result[name] = {"source_sha256": sha256(Path(module.__file__).read_bytes()).hexdigest(),
                        "runtime_sha256": sha256(_json_bytes({"python": sys.implementation.cache_tag,
                                                              "functions": functions})).hexdigest()}
    return result


def _require_same_implementation(version, current):
    expected = version.get("implementation", {})
    runtime = version.get("implementation_runtime", {})
    for name in _IMPLEMENTATION_FILES:
        if (expected.get(name, {}).get("sha256") != current[name]["source_sha256"]
                or runtime.get(name) != current[name]["runtime_sha256"]):
            raise ValueError("Paper execution implementation changed; saved inputs remain read-only, continuation requires an explicit new account")


def _root(identifier):
    if not _ACCOUNT.fullmatch(identifier):
        raise ValueError("Invalid paper account identity")
    return settings.DATA_ROOT / "paper_market_versions" / identifier


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def _atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _put(root, content, suffix):
    digest = sha256(content).hexdigest()
    path = root / "objects" / (digest + suffix)
    if path.exists():
        if sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Saved paper market object changed")
    else:
        _atomic(path, content)
    return {"sha256": digest, "suffix": suffix}


def _object(root, reference):
    digest, suffix = reference.get("sha256", ""), reference.get("suffix")
    if not _DIGEST.fullmatch(digest) or suffix not in {".parquet", ".csv", ".bin", ".py"}:
        raise ValueError("Invalid paper market object reference")
    path = root / "objects" / (digest + suffix)
    content = path.read_bytes()
    if sha256(content).hexdigest() != digest:
        raise ValueError("Saved paper market object changed")
    return path, content


def _partition(root, frame, previous):
    frame = frame.sort_values(["date", "symbol"]).reset_index(drop=True)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    buffer = pa.BufferOutputStream()
    with pa.ipc.new_stream(buffer, table.schema) as stream:
        stream.write_table(table)
    semantic = sha256(buffer.getvalue().to_pybytes()).hexdigest()
    if previous and previous.get("semantic_sha256") == semantic:
        return previous
    output = BytesIO()
    pq.write_table(table, output, compression="zstd")
    return {**_put(root, output.getvalue(), ".parquet"), "semantic_sha256": semantic,
            "rows": len(frame)}


def _link(root, reference, destination):
    source, _ = _object(root, reference)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)


def _day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Invalid paper session date")
    return value


def load_version(identifier, reference):
    root = _root(identifier)
    if reference.get("schema") != SCHEMA or not _DIGEST.fullmatch(reference.get("sha256", "")):
        raise ValueError("Paper session has no immutable market version")
    day = _day(reference["day"])
    folder = root / "versions" / day / reference["sha256"]
    content = (folder / "manifest.json").read_bytes()
    if sha256(content).hexdigest() != reference["sha256"]:
        raise ValueError("Saved paper market manifest changed")
    manifest = json.loads(content)
    version = manifest.get("paper_market_version", {})
    if version.get("schema") != SCHEMA or version.get("account_id") != identifier or version.get("day") != day:
        raise ValueError("Paper market version belongs to a different account/session")
    for name, artifact in {**version["artifacts"], **version["documents"]}.items():
        _object(root, artifact)
        path = (folder / name).resolve()
        if not path.is_relative_to(folder.resolve()) or sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
            raise ValueError("Saved paper market metadata changed")
    for artifact in version.get("implementation", {}).values():
        _object(root, artifact)
    return folder, manifest


class FrozenPaperFeed(HistoricalFeed):
    """HistoricalFeed behavior using verified partition objects, never a live path."""

    def _bars(self, name):
        version = self.manifest["paper_market_version"]
        root = _root(version["account_id"])
        key = {"raw.parquet": "raw_parts", "adjusted.parquet": "adjusted_parts"}[name]
        frames = [pd.read_parquet(BytesIO(_object(root, reference)[1]))
                  for _, reference in sorted(version[key].items())]
        if not frames:
            raise ValueError("Paper market version contains no observations")
        frame = pd.concat(frames, ignore_index=True)
        frame["date"] = pd.to_datetime(frame.date).dt.strftime("%Y-%m-%d")
        for field in ("open", "high", "low", "close", "preclose", "volume", "amount", "tradestatus", "isST"):
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        if frame.duplicated(["date", "symbol"]).any():
            raise ValueError("Duplicate paper market observations")
        return frame.sort_values(["date", "symbol"])


def capture(identifier, feed, day, previous=None):
    """Return an immutable feed and the reference to store with this daily row.

    Custom feeds can implement different scoring semantics. They keep their
    existing simulation behavior and remain explicitly unbound until a matching
    immutable adapter exists; this adapter never replaces their signal method.
    """
    if type(feed) is not HistoricalFeed:
        return feed, None
    day = _day(day)
    root = _root(identifier)
    session = root / "sessions" / (day + ".json")
    implementation = current_implementation()
    if session.is_file():
        reference = json.loads(session.read_bytes())
        folder, saved = load_version(identifier, reference)
        _require_same_implementation(saved["paper_market_version"], implementation)
        return FrozenPaperFeed(folder), reference
    if day not in feed.calendar:
        raise ValueError("Paper session is outside its saved calendar")
    prior = load_version(identifier, previous)[1]["paper_market_version"] if previous else {}
    if previous:
        _require_same_implementation(prior, implementation)
        prior_calendar = pd.read_csv(BytesIO(_object(root, prior["artifacts"]["calendar.csv"])[1])).date.tolist()
        previous_day = previous["day"]
        old_prefix = [value for value in prior_calendar if value <= previous_day]
        new_prefix = [value for value in feed.calendar if value <= previous_day]
        position = feed.calendar.index(previous_day) if previous_day in feed.calendar else -1
        following = feed.calendar[position + 1] if 0 <= position < len(feed.calendar) - 1 else None
        old_position = prior_calendar.index(previous_day)
        known_following = prior_calendar[old_position + 1] if old_position < len(prior_calendar) - 1 else None
        if old_prefix != new_prefix or following != day or (known_following and known_following != day):
            raise ValueError("Paper exchange-calendar history or next session changed")
    manifest = deepcopy(feed.manifest)
    manifest.pop("folder", None)
    manifest["end"] = day
    manifest["historical_price_adjustments"] = [item for item in manifest.get("historical_price_adjustments", [])
                                                 if item["date"] <= day]
    version = {"schema": SCHEMA, "account_id": identifier, "day": day,
               "captured_at": datetime.now(timezone.utc).isoformat(),
               "source_folder": str(feed.folder), "source_version": feed.manifest.get("version"),
               "source_kind": feed.manifest.get("kind"), "previous": previous,
               "raw_parts": {}, "adjusted_parts": {}, "artifacts": {}, "documents": {}}
    for attribute, key in (("raw", "raw_parts"), ("adjusted", "adjusted_parts")):
        frame = getattr(feed, attribute)
        for session_day, rows in frame.loc[frame.date.le(day)].groupby("date", sort=True):
            version[key][session_day] = _partition(root, rows, prior.get(key, {}).get(session_day))
    # Capture the loaded metadata used by the engine, rather than rereading a
    # provider CSV which may have changed since HistoricalFeed was constructed.
    instruments = pd.DataFrame.from_dict(feed.instruments, orient="index").rename_axis("symbol").reset_index()
    actions = deepcopy(feed.actions)
    for owner in [*actions, *manifest["historical_price_adjustments"]]:
        for field, hash_field in (("source_file", "source_sha256"), ("holder_source_file", "holder_source_sha256")):
            if not owner.get(field):
                continue
            source = (feed.folder / owner[field]).resolve()
            if not source.is_relative_to(feed.folder.resolve()):
                raise ValueError("Corporate-action source leaves the captured feed")
            content = source.read_bytes()
            reference = _put(root, content, ".bin")
            if owner.get(hash_field) and reference["sha256"] != owner[hash_field]:
                raise ValueError("Corporate-action source changed before paper capture")
            relative = "documents/" + reference["sha256"] + ".bin"
            owner[field] = relative
            version["documents"][relative] = reference
    action_frame = pd.DataFrame(actions) if actions else pd.DataFrame(columns=["symbol", "dividOperateDate"])
    for name, frame in (("calendar.csv", pd.DataFrame({"date": feed.calendar})),
                        ("instruments.csv", instruments), ("actions.csv", action_frame)):
        version["artifacts"][name] = _put(root, frame.to_csv(index=False).encode("utf-8"), ".csv")
    version["implementation"] = {name: _put(root, (Path(__file__).parent / name).read_bytes(), ".py")
                                  for name in _IMPLEMENTATION_FILES}
    version["implementation_runtime"] = {name: implementation[name]["runtime_sha256"] for name in _IMPLEMENTATION_FILES}
    # A deployment changed during capture must not mix two implementations.
    _require_same_implementation(version, current_implementation())
    manifest["paper_market_version"] = version
    content = _json_bytes(manifest)
    digest = sha256(content).hexdigest()
    folder = root / "versions" / day / digest
    staging = folder.with_name(digest + "." + uuid.uuid4().hex + ".tmp")
    staging.mkdir(parents=True)
    try:
        for name, reference in {**version["artifacts"], **version["documents"]}.items():
            _link(root, reference, staging / name)
        (staging / "manifest.json").write_bytes(content)
        # Validate the reconstructed immutable view before it can drive Ledger.
        FrozenPaperFeed(staging)
        folder.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, folder)
        reference = {"schema": SCHEMA, "day": day, "sha256": digest,
                     "captured_at": version["captured_at"]}
        _atomic(session, _json_bytes(reference))
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return FrozenPaperFeed(folder), reference


def validate_completion(run, feed, daily_rows):
    """A shortened provider calendar cannot turn a partial book into completion."""
    if type(feed) is not HistoricalFeed or not daily_rows or len(daily_rows) != run["cursor"]:
        raise ValueError("Paper completion requires bound daily market versions")
    rows = sorted(daily_rows, key=lambda row: row["date"])
    if rows[-1]["date"] != run["state"].get("day") or any(not row.get("market_version") for row in rows):
        raise ValueError("Paper completion has unmatched account sessions")
    manifests = [load_version(run["id"], row["market_version"])[1]["paper_market_version"] for row in rows]
    for index, (row, version) in enumerate(zip(rows, manifests)):
        if version["day"] != row["date"] or (index and version.get("previous") != rows[index - 1]["market_version"]):
            raise ValueError("Paper completion has an inconsistent market-version chain")
    latest = manifests[-1]
    _require_same_implementation(latest, current_implementation())
    saved_calendar = pd.read_csv(BytesIO(_object(_root(run["id"]), latest["artifacts"]["calendar.csv"])[1])).date.tolist()
    current_calendar = list(feed.calendar)
    if not current_calendar or not saved_calendar or max(current_calendar[-1], saved_calendar[-1]) < run["end"]:
        raise ValueError("Paper source calendar does not cover the requested completion horizon")
    expected = [day for day in saved_calendar if run["start"] <= day <= run["end"]]
    current = [day for day in current_calendar if run["start"] <= day <= run["end"]]
    observed = [row["date"] for row in rows]
    if expected != current or observed != current:
        raise ValueError("Paper calendar changed or expected account sessions are still missing")


def chart_observations(run, daily_rows, end, symbol=None):
    """Stitch each consumed day's raw candle from the version used that day.

    Later provider revisions cannot rewrite an earlier execution candle. The
    initial captured history supplies warmup, with all rows clipped to `end`.
    """
    if not daily_rows or len(daily_rows) != run["cursor"]:
        raise ValueError("日终纸面账户缺少完整逐日行情版本")
    rows = sorted(daily_rows, key=lambda row: row["date"])
    if rows[-1]["date"] != run["state"].get("day") or rows[0]["date"] < run["start"]:
        raise ValueError("纸面日终记录与账户进度不一致")
    relevant = [row for row in rows if row["date"] <= end]
    selected = relevant or rows[:1]  # Initial warmup is available before start.
    if any(not row.get("market_version") for row in selected):
        raise ValueError("旧日终记录没有逐日行情版本，不补猜历史绑定")
    versions = [(row, load_version(run["id"], row["market_version"])[1]) for row in selected]
    first_row, first_manifest = versions[0]
    initial = first_manifest["paper_market_version"]
    symbols = sorted({value for _, manifest in versions for value in pd.read_csv(BytesIO(
        _object(_root(run["id"]), manifest["paper_market_version"]["artifacts"]["instruments.csv"])[1])).symbol})
    if symbol is not None and symbol not in symbols:
        raise ValueError("证券代码不在此账户行情中")
    frames, references = [], []
    calendar = set()
    for index, (row, manifest) in enumerate(versions):
        version = manifest["paper_market_version"]
        if row["market_version"]["day"] != row["date"]:
            raise ValueError("纸面日终日期与行情版本不一致")
        if index and version.get("previous") != versions[index - 1][0]["market_version"]:
            raise ValueError("纸面日终行情版本的连续关系不一致")
        calendar.update(pd.read_csv(BytesIO(_object(_root(run["id"]), version["artifacts"]["calendar.csv"])[1])).date)
        parts = version["raw_parts"] if index == 0 else {row["date"]: version["raw_parts"].get(row["date"])}
        for day, reference in sorted(parts.items()):
            if day > end:
                continue
            if reference is None:
                raise ValueError("纸面日终缺少对应原始行情分片")
            frame = pd.read_parquet(BytesIO(_object(_root(run["id"]), reference)[1]))
            if symbol is not None:
                frame = frame.loc[frame.symbol.eq(symbol)]
            frames.append(frame)
        references.append(row["market_version"])
    raw = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "symbol", "tradestatus", "open", "high", "low", "close", "volume", "amount"])
    raw["date"] = pd.to_datetime(raw.date).dt.strftime("%Y-%m-%d")
    if raw.duplicated(["date", "symbol"]).any():
        raise ValueError("纸面行情分片出现重复日期与证券")
    return {"raw": raw, "symbols": symbols, "calendar": sorted(day for day in calendar if day <= end),
            "manifest": first_manifest, "versions": references}
