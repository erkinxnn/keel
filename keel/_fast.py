# -*- coding: utf-8 -*-
"""Cheap primitives for the adapters' hot path.

Measured on the author's machine (`python -S -E -X importtime`, median of 3):
`import hashlib` costs 17 ms (13.5 ms of it is `_hashlib`, i.e. loading OpenSSL),
`import statistics` costs 30 ms (it pulls in fractions, decimal, random). The adapters need
each module for a single function: sha256 for the ledger chain and a median for one detector.
This module provides the same two functions without that cost:

    * sha256 comes from the built-in module (`_sha256` in CPython 3.11, `_sha2` in 3.12+),
      which is compiled into the interpreter and costs nothing; hashlib stays as the fallback;
    * the median is computed in place, six lines are enough.

There will be no other logic here: this is not a "utilities layer", just two measured wins.
"""
from __future__ import annotations

try:  # CPython 3.11: built-in sha256 module without OpenSSL
    from _sha256 import sha256 as _sha256  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on the interpreter build
    try:  # CPython 3.12+: the two modules were merged into _sha2
        from _sha2 import sha256 as _sha256  # type: ignore[import-not-found]
    except ImportError:
        from hashlib import sha256 as _sha256  # type: ignore[assignment]


def sha256_hex(data: bytes) -> str:
    return _sha256(data).hexdigest()


def median(values) -> float:
    """Median of a non-empty sequence of numbers (like statistics.median, without the import)."""
    xs = sorted(float(v) for v in values)
    if not xs:
        raise ValueError("median of an empty sequence")
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0
