"""Wrapper de compactació integrat amb la configuració de Pluribus."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pluribus.config import settings

_scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from compact import compact_database as _compact_database


def compact_database(
    db_path: str | None = None,
    archive_path: str | None = None,
    retention_days: int = 30,
) -> dict[str, Any]:
    """Compacta la BD configurada, tret que el caller indiqui una altra ruta."""
    return _compact_database(
        db_path=db_path or settings.DB_PATH,
        archive_path=archive_path,
        retention_days=retention_days,
    )


__all__ = ["compact_database"]
