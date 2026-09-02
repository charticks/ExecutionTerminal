"""Explicit strategy plugin registration.

One import per plugin, on purpose — see registry.py's docstring. Importing
this package is what populates the registry; nothing scans a directory.
"""
from . import quant_preset  # noqa: F401
