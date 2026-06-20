"""Wrapper per cridar la compactació des del codi Pluribus."""

import sys
from pathlib import Path

# Afegir /opt/brain/scripts al path
_scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from compact import compact_database  # noqa: E402

__all__ = ["compact_database"]
