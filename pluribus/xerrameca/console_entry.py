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


def _console_html() -> str:
    """Add small operational controls without forking the whole console template."""
    html = _HTML
    cancel_button = "<button class=\"btn danger\" onclick=\"cancelConv('${esc(c.id)}')\">Cancel·la</button>"
    finish_button = (
        "<button class=\"btn good\" onclick=\"finishConv('${esc(c.id)}')\">Finalitza</button>"
        + cancel_button
    )
    html = html.replace(cancel_button, finish_button, 1)

    marker = "async function cancelConv(id){"
    finish_js = """async function finishConv(id){const summary=prompt('Resum final (opcional)','');if(summary===null)return;try{await api(`/v1/xerrameca/conversations/${id}/finish`,{method:'POST',body:JSON.stringify({summary:summary||null})});toast('Xerrameca finalitzada');await refreshAll()}catch(e){toast(e.message,true)}}
"""
    html = html.replace(marker, finish_js + marker, 1)
    return html


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_entry(view: str = Query(default="", max_length=32)) -> HTMLResponse:
    if view.strip().lower() == "xerrameca":
        return HTMLResponse(_console_html())

    legacy = await legacy_dashboard()
    body = legacy.body.decode("utf-8", errors="replace")
    if "</body>" in body:
        body = body.replace("</body>", _DASHBOARD_LINK + "</body>", 1)
    else:
        body += _DASHBOARD_LINK
    return HTMLResponse(body, status_code=legacy.status_code)
