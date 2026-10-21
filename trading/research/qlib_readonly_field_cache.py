"""Explicit, bounded FileFeatureStorage read cache for an immutable daily provider.

No Qlib init/configuration, expression, model, or production caller is changed.
The bound limits physical field reads, not arbitrary pre-existing Qlib expression
caches. A caller must still request only its declared decision date.
"""
from collections import OrderedDict
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import struct
from threading import RLock

import numpy as np
import pandas as pd


FIELDS = frozenset(("open", "high", "low", "close", "volume", "vwap", "factor"))
_guard = RLock()
_bindings = {}
_original = None


class AsOfBoundaryError(ValueError):
    """A requested field read would pass the declared maximum calendar index."""


class SourceChangedError(ValueError):
    """A pinned manifest or field stat changed; no implicit cache refresh."""


def _stat(path):
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class _Binding:
    def __init__(self, provider, reference, max_index, max_bytes, max_entries):
        self.provider = provider
        self.reference = reference
        self.max_index, self.max_bytes, self.max_entries = max_index, max_bytes, max_entries
        self.lock = RLock()
        self.refs = 1
        self.closed = False
        self.cache = OrderedDict()
        self.fingerprints = {}
        self.payload_bytes = self.bytes_read = self.hits = self.misses = 0
        proof = Path(reference["file"]).resolve()
        before = _stat(proof)
        raw = proof.read_bytes()
        self.proof, self.proof_stat = proof, _stat(proof)
        if before != self.proof_stat or sha256(raw).hexdigest() != reference["sha256"]:
            raise SourceChangedError("Immutable provider reference hash/stat differs")
        record = json.loads(raw)
        if (Path(record["provider_folder"]).resolve() != provider
                or set(record["fields"]) != FIELDS or len(record["fields"]) != len(FIELDS)):
            raise ValueError("Provider reference must bind this exact seven-field daily provider")

    def stats(self):
        with self.lock:
            return {"hits": self.hits, "misses": self.misses, "bytes_read": self.bytes_read,
                    "payload_bytes": self.payload_bytes, "entries": len(self.cache),
                    "max_bytes": self.max_bytes, "max_entries": self.max_entries,
                    "max_index": self.max_index, "closed": self.closed}

    def _check(self, path):
        if self.closed or _stat(self.proof) != self.proof_stat:
            raise SourceChangedError("Provider reference changed or context closed")
        current = _stat(path)
        if path in self.fingerprints and self.fingerprints[path] != current:
            raise SourceChangedError("Immutable field stat changed: " + str(path))
        self.fingerprints.setdefault(path, current)
        return current

    def _load(self, path, fingerprint):
        if path in self.cache:
            self.hits += 1
            self.cache.move_to_end(path)
            return self.cache[path]
        self.misses += 1
        # The header is metadata. Never prefetch bytes belonging to an index
        # after max_index, even when the physical immutable file has a tail.
        with path.open("rb") as stream:
            header = stream.read(4)
            self.bytes_read += len(header)
            start = int(np.frombuffer(header, dtype="<f")[0])
            physical_end = start + fingerprint[2] // 4 - 2
            allowed = max(0, min(physical_end, self.max_index) - start + 1)
            payload = stream.read(allowed * 4)
            self.bytes_read += len(payload)
        self._check(path)
        array = np.frombuffer(payload, dtype="<f")
        value = (start, physical_end, array)
        if array.nbytes <= self.max_bytes:
            while self.cache and (self.payload_bytes + array.nbytes > self.max_bytes
                                  or len(self.cache) >= self.max_entries):
                _, old = self.cache.popitem(last=False)
                self.payload_bytes -= old[2].nbytes
            self.cache[path] = value
            self.payload_bytes += array.nbytes
        return value

    def read(self, storage, item, original):
        path = storage.uri.resolve()
        path.relative_to(self.provider / "features")
        if storage.freq != "day" or str(storage.field).lower() not in FIELDS:
            raise ValueError("Only the bound seven original daily fields are supported")
        fingerprint = self._check(path)
        if isinstance(item, int) and item > self.max_index:
            raise AsOfBoundaryError("Integer field read passes the declared as-of index")
        if fingerprint is None:
            if not isinstance(item, (int, slice)):
                raise TypeError(f"type(i) = {type(item)}")
            if isinstance(item, slice) and item.stop is not None and item.stop - 1 > self.max_index:
                raise AsOfBoundaryError("Missing-field request passes the declared as-of index")
            self._check(path)
            return (None, None) if isinstance(item, int) else pd.Series(dtype=np.float32)
        # For malformed/truncated payloads, retain original error behavior after
        # checking the same header and explicit read bound, without prefetch.
        if fingerprint[2] % 4:
            with path.open("rb") as stream:
                start = int(np.frombuffer(stream.read(4), dtype="<f")[0])
            physical_end = start + fingerprint[2] // 4 - 2
            array = None
        else:
            start, physical_end, array = self._load(path, fingerprint)
        # Existing-file native storage reads its header before reporting an
        # unsupported index type. Preserve that error precedence for empties.
        if not isinstance(item, (int, slice)):
            raise TypeError(f"type(i) = {type(item)}")
        if isinstance(item, int):
            if start > item:
                raise IndexError(f"{item}: start index is {start}")
            if array is None:
                result = original(storage, item)
            elif item > physical_end:
                result = (item, struct.unpack("f", b"")[0])  # original short-read error
            else:
                result = (item, float(array[item - start]))
        else:
            # Qlib's original storage ignores slice.step and treats negative
            # indices as calendar indices, not Python relative offsets.
            left = start if item.start is None else item.start
            right = physical_end if item.stop is None else item.stop - 1
            first = max(left, start)
            if first > right:
                result = pd.Series(dtype=np.float32)
            else:
                if right > self.max_index:
                    raise AsOfBoundaryError("Slice field read passes the declared as-of index")
                if array is None or not isinstance(first, int) or not isinstance(right, int):
                    result = original(storage, item)
                else:
                    part = array[first - start:right - start + 1]
                    result = pd.Series(part, index=pd.RangeIndex(first, first + len(part)))
        self._check(path)
        return result


def _dispatch(storage, item):
    with _guard:
        path = storage.uri.resolve()
        matches = [state for root, state in _bindings.items() if path.is_relative_to(root)]
        if len(matches) != 1:
            raise ValueError("Storage path has no unique active immutable provider binding")
        state, original = matches[0], _original
        state.lock.acquire()
    try:
        return state.read(storage, item, original)
    finally:
        state.lock.release()


@contextmanager
def readonly_field_cache(provider, *, provider_reference, max_index, max_bytes=8 * 1024 * 1024, max_entries=2048):
    """Opt in for one immutable provider and an explicit inclusive as-of index.

    The reference is ``{file, sha256}`` for a JSON binding provider_folder and
    the seven original fields. Same-policy nesting shares a lease; conflicting
    policies or overlapping provider roots are rejected. Different providers
    have separate thread-safe LRUs. Join worker tasks before leaving context.
    """
    global _original
    from qlib.data.storage.file_storage import FileFeatureStorage

    if (type(max_index) is not int or max_index < 0 or type(max_bytes) is not int or max_bytes < 1
            or type(max_entries) is not int or max_entries < 1):
        raise ValueError("Explicit nonnegative as-of index and positive bounded cache limits required")
    root = Path(provider).resolve()
    reference = {"file": str(Path(provider_reference["file"]).resolve()), "sha256": provider_reference["sha256"]}
    policy = (reference, max_index, max_bytes, max_entries)
    with _guard:
        if root in _bindings:
            state = _bindings[root]
            if (state.reference, state.max_index, state.max_bytes, state.max_entries) != policy:
                raise ValueError("Conflicting nested policy for the same immutable provider")
            with state.lock:
                state._check(state.proof)
                state.refs += 1
        else:
            if any(root.is_relative_to(p) or p.is_relative_to(root) for p in _bindings):
                raise ValueError("Overlapping provider roots are ambiguous")
            state = _Binding(root, reference, max_index, max_bytes, max_entries)
            if not _bindings:
                _original = FileFeatureStorage.__getitem__
                FileFeatureStorage.__getitem__ = _dispatch
            elif FileFeatureStorage.__getitem__ is not _dispatch:
                raise RuntimeError("Storage method changed during another cache context")
            _bindings[root] = state
    try:
        yield state
    finally:
        with _guard:
            with state.lock:
                state.refs -= 1
                if not state.refs:
                    del _bindings[root]
                    state.closed = True
                    state.cache.clear()
                    state.payload_bytes = 0
                if not _bindings:
                    original, _original = _original, None
                    if FileFeatureStorage.__getitem__ is not _dispatch:
                        raise RuntimeError("Storage method changed externally; refusing an incorrect restoration")
                    FileFeatureStorage.__getitem__ = original
