"""Consistent, fail-safe backups for the Pluribus SQLite database."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from pluribus.config import settings


@dataclass(frozen=True)
class BackupResult:
    path: str
    bytes: int
    removed_old_backups: int


def _validate_retention_days(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3650:
        raise ValueError("retention_days must be an integer between 1 and 3650")
    return value


def _quick_check(path: Path) -> None:
    with sqlite3.connect(str(path), timeout=30) as db:
        row = db.execute("PRAGMA quick_check").fetchone()
    if not row or row[0] != "ok":
        raise RuntimeError("SQLite backup quick_check failed")


def _prune_old_backups(backup_dir: Path, retention_days: int, now_ts: float) -> int:
    cutoff = now_ts - retention_days * 86400
    removed = 0
    for candidate in backup_dir.glob("pluribus_*.db.gz"):
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def backup_database(
    db_path: str | None = None,
    backup_dir: str | None = None,
    retention_days: int = 14,
) -> BackupResult:
    """Create an atomic gzip-compressed snapshot using SQLite's backup API.

    The SQLite backup API copies a transactionally consistent view even when the
    source database is in WAL mode and remains online. The source database is
    never VACUUMed, copied with `cp`, or mutated by this function.
    """
    retention_days = _validate_retention_days(retention_days)
    source = Path(db_path or settings.DB_PATH).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source}")

    target_dir = Path(
        backup_dir
        or os.environ.get("PLURIBUS_BACKUP_DIR", "")
        or str(source.parent / "backups")
    ).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(target_dir, 0o700)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    final_path = target_dir / f"pluribus_{timestamp}.db.gz"

    raw_fd, raw_name = tempfile.mkstemp(
        prefix=".pluribus-backup-",
        suffix=".db.tmp",
        dir=target_dir,
    )
    os.close(raw_fd)
    raw_path = Path(raw_name)
    gzip_path = target_dir / f".{final_path.name}.tmp"

    try:
        with sqlite3.connect(str(source), timeout=30) as src, sqlite3.connect(
            str(raw_path), timeout=30
        ) as dst:
            src.backup(dst)

        _quick_check(raw_path)
        os.chmod(raw_path, 0o600)

        with raw_path.open("rb") as source_file, gzip.open(gzip_path, "wb", compresslevel=6) as out:
            shutil.copyfileobj(source_file, out, length=1024 * 1024)
        os.chmod(gzip_path, 0o600)
        os.replace(gzip_path, final_path)

        removed = _prune_old_backups(
            target_dir,
            retention_days,
            datetime.now(timezone.utc).timestamp(),
        )
        return BackupResult(
            path=str(final_path),
            bytes=final_path.stat().st_size,
            removed_old_backups=removed,
        )
    finally:
        raw_path.unlink(missing_ok=True)
        gzip_path.unlink(missing_ok=True)


def main() -> None:
    retention_raw = os.environ.get("PLURIBUS_BACKUP_RETENTION_DAYS", "14")
    try:
        retention_days = int(retention_raw, 10)
    except ValueError as exc:
        raise SystemExit("PLURIBUS_BACKUP_RETENTION_DAYS must be an integer") from exc

    result = backup_database(retention_days=retention_days)
    print(
        f"backup={result.path} bytes={result.bytes} "
        f"removed_old={result.removed_old_backups}"
    )


if __name__ == "__main__":
    main()
