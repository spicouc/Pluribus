"""Worker d'expiració automàtica de facts amb TTL.

S'encarrega de marcar com a deleted_at els facts on
created_at + ttl_days < now. Es crida periòdicament des
del lifespan de FastAPI cada 5 minuts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from pluribus.db import get_db

logger = logging.getLogger("pluribus.expiry")


async def expire_old_facts() -> int:
    """Marca com a deleted_at els facts amb TTL expirat.

    Retorna el nombre de facts expirats.
    """
    expired_count = 0
    try:
        async with get_db() as db:
            # Facts amb ttl_days definit i no eliminats encara
            cursor = await db.execute(
                """
                SELECT id, created_at, ttl_days
                FROM facts
                WHERE ttl_days IS NOT NULL
                  AND deleted_at IS NULL
                  AND expires_at IS NOT NULL
                  AND expires_at < datetime('now')
                """
            )
            rows = await cursor.fetchall()

            for row in rows:
                await db.execute(
                    "UPDATE facts SET deleted_at = datetime('now') WHERE id = ?",
                    (row["id"],),
                )
                expired_count += 1

            if expired_count > 0:
                await db.commit()
                logger.info(
                    "Expirats %d facts amb TTL vencut", expired_count
                )
    except Exception as exc:
        logger.error("Error en expire_old_facts: %s", exc)

    return expired_count


async def expiry_worker_loop() -> None:
    """Bucle infinit que executa expire_old_facts cada 5 minuts."""
    while True:
        try:
            count = await expire_old_facts()
            if count > 0:
                logger.info("Worker d'expiració: %d facts expirats", count)
        except Exception as exc:
            logger.error("Error en expiry_worker_loop: %s", exc)
        await asyncio.sleep(300)  # 5 minuts
