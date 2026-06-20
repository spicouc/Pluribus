"""Funcions d'auditoria per registrar accions a la base de dades."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import aiosqlite


async def log_audit(
    db: "aiosqlite.Connection",
    agent_id: str,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    payload: Optional[str] = None,
) -> None:
    """Registra una acció d'auditoria a la taula audit_log."""
    await db.execute(
        """
        INSERT INTO audit_log (agent_id, action, resource_type, resource_id, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (agent_id, action, resource_type, resource_id, payload),
    )
