"""Vendored fastcdc - content-defined chunking (MIT License).

Tries the Cython-compiled extension first for speed; falls back to pure Python.
"""

try:
    from kg._vendor.fastcdc.fastcdc_cy import fastcdc_cy as fastcdc_py
except ImportError:
    from kg._vendor.fastcdc.fastcdc_py import fastcdc_py  # type: ignore[no-redef]

__all__ = ["fastcdc_py"]

__version__ = "1.7.0"
