"""Compatibility wrapper for the historical ``agentic_dashboard`` module."""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent / "src"))
from agentic_runner import dashboard as _implementation

_sys.modules[__name__] = _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
