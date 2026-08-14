"""Public dashboard switch: legacy dashboard or Xerrameca console.

Only the HTML shell is public. Xerrameca data and mutations still require the
admin API key entered by the operator and sent as X-API-Key by the console.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from pluribus.dashboard import get_dashboard as legacy_dashboard
from .console import _HTML

router = APIRouter(tags=["xerrameca-console"])


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_entry(view: str = Query(default="", max_length=32)) -> HTMLResponse:
    if view.strip().lower() == "xerrameca":
        return HTMLResponse(_HTML)
    return await legacy_dashboard()
