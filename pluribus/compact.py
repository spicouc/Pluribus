"""Wrapper per cridar la compactacio des del codi Pluribus."""

import sys
from pathlib import Path

_scripts_dir = str(Path(__file__).resolve().parent.parent / 'scripts')
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from compact import compact_database

__all__ = ['compact_database']
