"""Minimal utils for fastcdc vendored in kg."""
from __future__ import annotations

import mmap
from io import BufferedReader
from pathlib import Path

Data = str | Path | BufferedReader | bytes | bytearray | mmap.mmap | memoryview


def get_memoryview(data: Data) -> memoryview:
    if isinstance(data, (str, Path)):
        with Path(data).open("rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            return memoryview(mm)
    if hasattr(data, "fileno"):
        mm = mmap.mmap(data.fileno(), 0, access=mmap.ACCESS_READ)
        return memoryview(mm)
    if isinstance(data, BufferedReader):
        mm = mmap.mmap(data.raw.fileno(), 0, access=mmap.ACCESS_READ)
        return memoryview(mm)
    if isinstance(data, (bytes, bytearray, mmap.mmap, memoryview)):
        return memoryview(data)
    raise TypeError(f"Unsupported data type: {type(data)}")
