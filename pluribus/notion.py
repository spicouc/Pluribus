"""Integració amb Notion per enriquir fets amb context de pàgines.

Si NOTION_API_KEY no està configurat, totes les funcions fallen
silenciosament (no trenquen el servei principal).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import aiosqlite
import requests

from pluribus.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOTION_HEADERS: dict[str, str] | None = None


def _get_headers() -> dict[str, str] | None:
    """Retorna headers de Notion o None si no hi ha API key."""
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
    """Extreu text pla d'un block de Notion."""
    block_type = block.get("type", "")
    bdata = block.get(block_type, {})
    rich_text = bdata.get("rich_text", []) if isinstance(bdata, dict) else []
    parts = []
    for rt in rich_text:
        if isinstance(rt, dict) and "plain_text" in rt:
            parts.append(rt["plain_text"])
    return " ".join(parts)


def _fetch_block_children(page_id: str, max_blocks: int = 200) -> list[dict]:
    """Recupera recursivament els blocks d'una pàgina Notion."""
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
            if resp.status_code != 200:
                break
            data = resp.json()
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        except Exception:
            break
    return blocks[:max_blocks]


def _page_to_markdown(page_id: str, max_blocks: int = 200) -> str:
    """Converteix una pàgina Notion a markdown pla."""
    blocks = _fetch_block_children(page_id, max_blocks)
    lines: list[str] = []
    for block in blocks:
        text = _notion_block_to_text(block)
        if text:
            lines.append(text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


async def sync_notion_cache() -> dict:
    """Sincronitza pàgines de Notion al cache local (taula notion_cache).

    Retorna un resum: {"synced": N, "errors": M, "message": "..."}
    """
    headers = _get_headers()
    if not headers:
        return {"synced": 0, "errors": 0, "message": "Notion API key no configurada"}

    synced = 0
    errors = 0

    # Cerca les bases de dades accessibles
    search_url = "https://api.notion.com/v1/search"
    try:
        resp = requests.post(
            search_url,
            headers=headers,
            json={"filter": {"value": "page", "property": "object"}, "page_size": 50},
            timeout=20,
        )
        if resp.status_code != 200:
            return {"synced": 0, "errors": 0, "message": f"Error Notion API: {resp.status_code}"}

        pages = resp.json().get("results", [])
    except Exception as e:
        return {"synced": 0, "errors": 1, "message": str(e)}

    embed_service = None
    try:
        from pluribus.embedding import embedding_service as es
        embed_service = es
    except ImportError:
        pass

    async with aiosqlite.connect(settings.DB_PATH) as db:
        for page in pages:
            page_id = page["id"]
            title = ""
            for prop in page.get("properties", {}).values():
                ptype = prop.get("type", "")
                if ptype == "title":
                    parts = [t.get("plain_text", "") for t in prop.get("title", [])]
                    title = "".join(parts)
                    break

            markdown = _page_to_markdown(page_id)
            page_url = page.get("url", f"https://notion.so/{page_id.replace('-', '')}")

            # Embed amb Ollama si disponible
            embedding_blob = None
            if embed_service and markdown:
                try:
                    vec = embed_service.get_embedding(markdown[:2000])
                    if vec is not None and not all(v == 0 for v in vec):
                        embedding_blob = vec.tobytes()
                except Exception:
                    pass

            try:
                await db.execute(
                    """INSERT OR REPLACE INTO notion_cache
                       (id, title, markdown, url, embedding_blob, last_synced, parent_db)
                       VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
                    (page_id, title, markdown, page_url, embedding_blob, ""),
                )
                synced += 1
            except Exception as e:
                logger.warning("Error caching Notion page %s: %s", page_id, e)
                errors += 1

        await db.commit()

    return {"synced": synced, "errors": errors, "message": "Sincronització completada"}


async def search_notion(query: str, top_k: int = 5) -> list[dict]:
    """Cerca a notion_cache per similitud semàntica o per text.

    Retorna llistat de dicts: [{id, title, markdown, url, score}]
    """
    embed_service = None
    try:
        from pluribus.embedding import embedding_service as es
        embed_service = es
    except ImportError:
        pass

    async with aiosqlite.connect(settings.DB_PATH) as db:
        if embed_service:
            try:
                query_vec = embed_service.get_embedding(query)
                cursor = await db.execute(
                    "SELECT id, title, markdown, url, embedding_blob FROM notion_cache WHERE embedding_blob IS NOT NULL",
                )
                rows = await cursor.fetchall()

                scored: list[tuple[dict, float]] = []
                for row in rows:
                    blob = row["embedding_blob"]
                    if blob is None:
                        continue
                    vec = __import__("numpy", fromlist=[""]).frombuffer(blob, dtype="float32")
                    from pluribus.embedding import EmbeddingService
                    norm_func = EmbeddingService._normalize
                    vec = norm_func(vec)
                    score = float(vec @ query_vec)
                    scored.append(({"id": row["id"], "title": row["title"], "markdown": row["markdown"][:300], "url": row["url"]}, score))

                scored.sort(key=lambda x: -x[1])
                return [s[0] | {"score": round(s[1], 4)} for s in scored[:top_k]]

            except Exception:
                pass

        # FTS5 fallback
        cursor = await db.execute(
            "SELECT id, title, markdown, url FROM notion_cache WHERE markdown LIKE ? LIMIT ?",
            (f"%{query}%", top_k),
        )
        rows = await cursor.fetchall()
        return [{"id": r["id"], "title": r["title"], "markdown": r["markdown"][:300], "url": r["url"], "score": 0.0} for r in rows]


async def link_fact_to_notion(fact_id: str) -> int:
    """Busca pàgines Notion relacionades amb un fact i crea links.

    Retorna quants links s'han creat.
    """
    async with aiosqlite.connect(settings.DB_PATH) as db:
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
        for page in related:
            if page["score"] < 0.3:
                continue
            try:
                await db.execute(
                    """INSERT OR IGNORE INTO notion_links
                       (fact_id, notion_page_id, relevance, match_type)
                       VALUES (?, ?, ?, 'auto')""",
                    (fact_id, page["id"], round(page["score"], 4)),
                )
                created += 1
            except Exception:
                pass

        if created:
            await db.commit()
    return created


async def get_notion_context(fact_id: str) -> list[dict]:
    """Retorna les pàgines Notion linkades a un fact."""
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor = await db.execute(
            """SELECT nc.id, nc.title, nc.url, nl.relevance, nl.match_type
               FROM notion_links nl
               JOIN notion_cache nc ON nl.notion_page_id = nc.id
               WHERE nl.fact_id = ?
               ORDER BY nl.relevance DESC
               LIMIT 5""",
            (fact_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
