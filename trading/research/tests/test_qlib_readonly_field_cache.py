"""Small synthetic binary equivalence checks, with no real feature extraction."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
from threading import Barrier

import numpy as np
import pandas as pd
import pytest
from qlib.data.storage.file_storage import FileFeatureStorage

from trading.research.qlib_readonly_field_cache import (
    AsOfBoundaryError, FIELDS, SourceChangedError, readonly_field_cache,
)


class FixtureStorage(FileFeatureStorage):
    """Use original storage methods with a synthetic path, without Qlib init."""
    def __init__(self, root, field="close"):
        super().__init__("sh600000", field, "day", provider_uri={"day": str(root)})
        self.fixture_path = root / "features/sh600000" / (field + ".day.bin")

    @property
    def uri(self):
        return self.fixture_path


def fixture(tmp_path, name="one", *, start=2, values=(1., np.nan, 3.)):
    root = tmp_path / name / "provider"
    fields = root / "features/sh600000"
    fields.mkdir(parents=True)
    raw = fields / "close.day.bin"
    raw.write_bytes(np.array([start, *values], dtype="<f4").tobytes())
    proof = root.parent / "result.json"
    proof.write_text(json.dumps({"provider_folder": str(root), "fields": sorted(FIELDS)}), encoding="utf-8")
    reference = {"file": str(proof), "sha256": sha256(proof.read_bytes()).hexdigest()}
    storage = FixtureStorage(root)
    return root, reference, storage, raw


def capture(storage, key):
    try:
        return "value", storage[key]
    except Exception as error:
        return "error", type(error), str(error)


def same(expected, actual):
    assert expected[0] == actual[0]
    if expected[0] == "error":
        assert expected[1:] == actual[1:]
    elif isinstance(expected[1], pd.Series):
        pd.testing.assert_series_equal(expected[1], actual[1], check_exact=True)
    else:
        np.testing.assert_equal(expected[1], actual[1])


def test_native_int_slice_step_negative_missing_empty_and_truncated_semantics(tmp_path):
    original = FileFeatureStorage.__getitem__
    keys = [2, 3, 4, -1, 8, True, np.int64(2), "bad", slice(None), slice(-8, 4),
            slice(2, 5, 0), slice(2, 5, -1), slice(2, 5, 2), slice(9, 3), slice(3, -1),
            slice(2, 9), slice(2., 4), slice(None, 0)]
    variants = [("normal", 2, (1., np.nan, 3.), None), ("negative_header", -2, (1., 2., 3.), None),
                ("header_only", 2, (), None), ("missing", 2, (), "missing"),
                ("empty_file", 2, (), "empty"), ("partial_tail", 2, (1.,), "partial")]
    for name, start, values, mutation in variants:
        root, reference, storage, raw = fixture(tmp_path, name, start=start, values=values)
        if mutation == "missing":
            raw.unlink()
        elif mutation == "empty":
            raw.write_bytes(b"")
        elif mutation == "partial":
            raw.write_bytes(raw.read_bytes() + b"\x01\x02")
        before = raw.read_bytes() if raw.exists() else None
        expected = [capture(storage, key) for key in keys]
        if mutation is None and values:
            assert expected[8][0] == "value" and isinstance(expected[8][1], pd.Series)
        with readonly_field_cache(root, provider_reference=reference, max_index=20):
            for key, answer in zip(keys, expected):
                same(answer, capture(storage, key))
        assert FileFeatureStorage.__getitem__ is original
        assert (raw.read_bytes() if raw.exists() else None) == before


def test_asof_never_physically_reads_future_records(monkeypatch, tmp_path):
    root, reference, storage, raw = fixture(tmp_path, start=0, values=(10., 20., 999999., 888888.))
    native_open = Path.open
    reads = []

    class Spy:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def __getattr__(self, name): return getattr(self.stream, name)
        def read(self, count=-1):
            assert count >= 0
            end = self.stream.tell() + count
            reads.append(end)
            assert end <= 12  # header + indices 0 and 1 only
            return self.stream.read(count)

    def tracked(path, *args, **kwargs):
        stream = native_open(path, *args, **kwargs)
        return Spy(stream) if path.resolve() == raw.resolve() and (not args or args[0] == "rb") else stream

    monkeypatch.setattr(Path, "open", tracked)
    with readonly_field_cache(root, provider_reference=reference, max_index=1) as cache:
        pd.testing.assert_series_equal(storage[:2], pd.Series(np.array([10., 20.], dtype="float32"), index=pd.RangeIndex(0, 2)))
        assert storage[1] == (1, 20.)
        for key in (2, slice(None), slice(0, 3)):
            with pytest.raises(AsOfBoundaryError): storage[key]
        assert cache.stats()["hits"] >= 1
    assert reads and max(reads) == 12


def test_lru_limits_and_source_mutation_are_explicit(tmp_path):
    root, reference, storage, raw = fixture(tmp_path, start=0, values=(1., 2.))
    opening = FixtureStorage(root, "open")
    opening.uri.write_bytes(np.array([0, 3., 4.], dtype="<f4").tobytes())
    with readonly_field_cache(root, provider_reference=reference, max_index=1, max_bytes=8, max_entries=1) as cache:
        assert storage[0] == (0, 1.)
        assert opening[0] == (0, 3.)
        assert cache.stats()["payload_bytes"] <= 8 and cache.stats()["entries"] == 1
        assert storage[1] == (1, 2.)  # a valid re-read after LRU eviction
        assert opening[1] == (1, 4.)  # close is evicted again before mutation
        raw.write_bytes(np.array([0, 7., 8.], dtype="<f4").tobytes())
        with pytest.raises(SourceChangedError): storage[0]
    assert cache.stats()["closed"] and cache.stats()["payload_bytes"] == 0
    with pytest.raises(SourceChangedError):
        with readonly_field_cache(root, provider_reference={**reference, "sha256": "0" * 64}, max_index=1): pass


def test_missing_field_appearing_later_is_not_silently_refreshed(tmp_path):
    original = FileFeatureStorage.__getitem__
    root, reference, storage, raw = fixture(tmp_path, start=0, values=(1.,))
    raw.unlink()
    with readonly_field_cache(root, provider_reference=reference, max_index=0):
        assert storage[0] == (None, None)
        pd.testing.assert_series_equal(storage[:1], pd.Series(dtype="float32"))
        raw.write_bytes(np.array([0, 4.], dtype="<f4").tobytes())
        with pytest.raises(SourceChangedError): storage[0]
    assert FileFeatureStorage.__getitem__ is original


def test_nesting_concurrent_providers_and_exception_cleanup_do_not_misrestore(tmp_path):
    original = FileFeatureStorage.__getitem__
    a, ar, sa, _ = fixture(tmp_path, "a", start=0, values=(1., 2.))
    b, br, sb, _ = fixture(tmp_path, "b", start=0, values=(11., 12.))
    with readonly_field_cache(a, provider_reference=ar, max_index=1) as outer:
        with readonly_field_cache(a, provider_reference=ar, max_index=1) as inner:
            assert inner is outer and sa[0] == (0, 1.)
        assert FileFeatureStorage.__getitem__ is not original
        with pytest.raises(ValueError, match="Conflicting"):
            with readonly_field_cache(a, provider_reference=ar, max_index=0): pass
        with readonly_field_cache(b, provider_reference=br, max_index=1):
            assert sb[1] == (1, 12.) and sa[1] == (1, 2.)
        with pytest.raises(ValueError, match="no unique"):
            sb[0]
    assert FileFeatureStorage.__getitem__ is original
    barrier = Barrier(2)
    def worker(provider, reference, storage):
        with readonly_field_cache(provider, provider_reference=reference, max_index=1):
            barrier.wait(timeout=10)
            result = storage[0]
            barrier.wait(timeout=10)
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(worker, a, ar, sa)
        two = pool.submit(worker, b, br, sb)
        assert one.result() == (0, 1.) and two.result() == (0, 11.)
    assert FileFeatureStorage.__getitem__ is original
    with pytest.raises(RuntimeError, match="consumer failed"):
        with readonly_field_cache(a, provider_reference=ar, max_index=1):
            raise RuntimeError("consumer failed")
    assert FileFeatureStorage.__getitem__ is original
