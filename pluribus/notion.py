"""Integració opcional amb Notion per enriquir fets amb context de pàgines."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import numpy as np
import requests

from pluribus.config import settings
from pluribus.db import get_db

logger = logging.getLogger(__name__)

_NOTION_HEADERS: dict[str, str] | None = None


def _get_headers() -> dict[str, str] | None:
    global _NOTION_HEADERS
    if _NOTION_HEADERS is not None:
        return _NOTION_HEADERS
    if not settings.NOTION_API_KEY:
        return None
    _NOTION_HEADERS = {
        "Authorization": f"Bearer {settings.NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": settings.NOTION_API_VERSION,
    }
    return _NOTION_HEADERS


def _notion_block_to_text(block: dict) -> str:
    block_type = block.get("type", "")
    bdata = block.get(block_type, {})
    rich_text = bdata.get("rich_text", []) if isinstance(bdata, dict) else []
    parts = []
    for rt in rich_text:
        if isinstance(rt, dict) and "plain_text" in rt:
            parts.append(rt["plain_text"])
    return " ".join(parts)


def _fetch_block_children(page_id: str, max_blocks: int = 200) -> list[dict]:
    headers = _get_headers()
    if not headers:
        return []
    blocks: list[dict] = []
    cursor: str | None = None
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    while len(blocks) < max_blocks:
        params: dict = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        except Exception as exc:
            logger.warning("Error llegint blocs Notion %s: %s", page_id, exc)
            break
    return blocks[:max_blocks]


def _page_to_markdown(page_id: str, max_blocks: int = 200) -> str:
    blocks = _fetch_block_children(page_id, max_blocks)
    lines: list[str] = []
    for block in blocks:
        text = _notion_block_to_text(block)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _search_pages_sync(headers: dict[str, str]) -> list[dict]:
    response = requests.post(
        "https://api.notion.com/v1/search",
        headers=headers,
        json={"filter": {"value": "page", "property": "object"}, "page_size": 50},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    results = data.get("results", [])
    return results if isinstance(results, list) else []


def _valid_embedding(vec: np.ndarray | None) -> bool:
    if vec is None:
        return False
    arr = np.asarray(vec, dtype=np.float32)
    return (
        arr.size == settings.EMBED_DIM
        and np.all(np.isfinite(arr))
        and float(np.linalg.norm(arr)) > 0
    )


async def sync_notion_cache() -> dict:
    """Sincronitza pàgines accessibles de Notion al cache local."""
    headers = _get_headers()
    if not headers:
        return {"synced": 0, "errors": 0, "message": "Notion API key no configurada"}

    try:
        pages = await asyncio.to_thread(_search_pages_sync, headers)
    except Exception as exc:
        logger.warning("Error cercant pàgines Notion: %s", exc)
        return {"synced": 0, "errors": 1, "message": "Error Notion API"}

    try:
        from pluribus.embedding import embedding_service
    except ImportError:
        embedding_service = None

    synced = 0
    errors = 0
    async with get_db() as db:
        for page in pages:
            if not isinstance(page, dict) or not page.get("id"):
                errors += 1
                continue
            page_id = str(page["id"])
            title = ""
            for prop in page.get("properties", {}).values():
                if isinstance(prop, dict) and prop.get("type") == "title":
                    title = "".join(
                        t.get("plain_text", "")
                        for t in prop.get("title", [])
                        if isinstance(t, dict)
                    )
                    break

            markdown = await asyncio.to_thread(_page_to_markdown, page_id)
            page_url = page.get("url", f"https://notion.so/{page_id.replace('-', '')}")

            embedding_blob = None
            if embedding_service is not None and markdown:
                try:
                    vec = await embedding_service.get_embedding_async(markdown[:2000])
                    if _valid_embedding(vec):
                        embedding_blob = np.asarray(vec, dtype=np.float32).tobytes()
                except Exception as exc:
                    logger.debug("Embedding Notion no disponible per %s: %s", page_id, exc)

            try:
                await db.execute(
                    """INSERT OR REPLACE INTO notion_cache
                       (id, title, markdown, url, embedding_blob, last_synced, parent_db)
                       VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
                    (page_id, title, markdown, page_url, embedding_blob, ""),
                )
                synced += 1
            except Exception as exc:
                logger.warning("Error caching Notion page %s: %s", page_id, exc)
                errors += 1
        await db.commit()

    return {"synced": synced, "errors": errors, "message": "Sincronització completada"}


async def search_notion(query: str, top_k: int = 5) -> list[dict]:
    """Cerca al cache de Notion per similitud semàntica o text."""
    if top_k < 1:
        return []
    top_k = min(top_k, 50)

    try:
        from pluribus.embedding import embedding_service
    except ImportError:
        embedding_service = None

    query_vec: np.ndarray | None = None
    if embedding_service is not None:
        try:
            candidate = await embedding_service.get_embedding_async(query)
            if _valid_embedding(candidate):
                arr = np.asarray(candidate, dtype=np.float32)
                query_vec = arr / float(np.linalg.norm(arr))
        except Exception:
            query_vec = None

    async with get_db() as db:
        if query_vec is not None:
            cursor = await db.execute(
                "SELECT id, title, markdown, url, embedding_blob FROM notion_cache WHERE embedding_blob IS NOT NULL"
            )
            scored: list[tuple[dict, float]] = []
            for row in await cursor.fetchall():
                blob = row["embedding_blob"]
                if blob is None:
                    continue
                vec = np.frombuffer(blob, dtype=np.float32)
                if not _valid_embedding(vec):
                    continue
                vec = vec / float(np.linalg.norm(vec))
                score = float(vec @ query_vec)
                scored.append(
                    (
                        {
                            "id": row["id"],
                            "title": row["title"],
                            "markdown": (row["markdown"] or "")[:300],
                            "url": row["url"],
                        },
                        score,
                    )
                )
            if scored:
                scored.sort(key=lambda item: -item[1])
                return [item | {"score": round(score, 4)} for item, score in scored[:top_k]]

        cursor = await db.execute(
            "SELECT id, title, markdown, url FROM notion_cache WHERE markdown LIKE ? LIMIT ?",
            (f"%{query}%", top_k),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "markdown": (row["markdown"] or "")[:300],
                "url": row["url"],
                "score": 0.0,
            }
            for row in rows
        ]


async def link_fact_to_notion(fact_id: str) -> int:
    """Busca pàgines Notion relacionades amb un fact i crea links."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT content FROM facts WHERE id = ? AND deleted_at IS NULL",
            (fact_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return 0
        fact_content = row["content"]

    related = await search_notion(fact_content, top_k=3)
    created = 0
    async with get_db() as db:
        for page in related:
            if page["score"] < 0.3:
                continue
            cursor = await db.execute(
                """INSERT OR IGNORE INTO notion_links
                   (fact_id, notion_page_id, relevance, match_type)
                   VALUES (?, ?, ?, 'auto')""",
                (fact_id, page["id"], round(page["score"], 4)),
            )
            if cursor.rowcount > 0:
                created += 1
        if created:
            await db.commit()
    return created


async def get_notion_context(fact_id: str) -> list[dict]:
    """Retorna les pàgines Notion linkades a un fact."""
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT nc.id, nc.title, nc.url, nl.relevance, nl.match_type
               FROM notion_links nl
               JOIN notion_cache nc ON nl.notion_page_id = nc.id
               WHERE nl.fact_id = ?
               ORDER BY nl.relevance DESC
               LIMIT 5""",
            (fact_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]
