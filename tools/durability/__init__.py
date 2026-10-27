"""Fault-injection durability harness for the two capture paths (#51, docs/durability.md).

Stdlib only. Nothing here is imported by the app: the server-side shim (shim/sitecustomize.py) is injected
from outside via PYTHONPATH and is inert unless DURABILITY_SHIM=1.
"""
