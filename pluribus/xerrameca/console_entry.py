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

_DASHBOARD_LINK = """
<a href="/dashboard?view=xerrameca"
   style="position:fixed;right:16px;top:16px;z-index:9999;padding:9px 12px;
          border:1px solid #0ea5e9;border-radius:8px;background:#172033;
          color:#7dd3fc;text-decoration:none;font:600 13px system-ui">
  Xerrameca
</a>
"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_entry(view: str = Query(default="", max_length=32)) -> HTMLResponse:
    if view.strip().lower() == "xerrameca":
        return HTMLResponse(_HTML)

    legacy = await legacy_dashboard()
    body = legacy.body.decode("utf-8", errors="replace")
    if "</body>" in body:
        body = body.replace("</body>", _DASHBOARD_LINK + "</body>", 1)
    else:
        body += _DASHBOARD_LINK
    return HTMLResponse(body, status_code=legacy.status_code)
