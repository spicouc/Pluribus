"""Dashboard HTML i endpoint JSON d'estadístiques per al monitoratge."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from pluribus.config import settings
from pluribus.db import get_db
from pluribus.embedding import embedding_service
from pluribus.models import AuditEntry

router = APIRouter(tags=["dashboard"])


@router.get("/api/stats")
async def get_stats() -> JSONResponse:
    """Retorna mètriques JSON per al dashboard."""
    async with get_db() as db:
        # Fets dels últims 7 dies
        seven_days_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await db.execute(
            """
            SELECT date(created_at) as day, COUNT(*) as count
            FROM facts
            WHERE created_at >= ?
            GROUP BY date(created_at)
            ORDER BY day ASC
            """,
            (seven_days_ago,),
        )
        facts_last_7_days = [{"date": row["day"], "count": row["count"]} for row in await cursor.fetchall()]

        # Fets per agent
        cursor = await db.execute(
            """
            SELECT COALESCE(a.name, 'unknown') as agent_name, COUNT(f.id) as count
            FROM facts f
            LEFT JOIN agents a ON f.agent_id = a.id
            WHERE f.deleted_at IS NULL
            GROUP BY f.agent_id
            ORDER BY count DESC
            """
        )
        facts_by_agent = [{"agent_name": row["agent_name"], "count": row["count"]} for row in await cursor.fetchall()]

        # Fets per categoria (Fase 1 OpenViking)
        cursor = await db.execute(
            "SELECT COALESCE(category, '') as category, COUNT(id) as count FROM facts WHERE deleted_at IS NULL GROUP BY category ORDER BY count DESC"
        )
        facts_by_category = [{"category": row["category"] or "sense", "count": row["count"]} for row in await cursor.fetchall()]

        # Mida de la base de dades
        db_path = "/opt/pluribus/data/pluribus.db"
        try:
            db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        except OSError:
            db_size = 0
        db_size_history = [{"date": datetime.utcnow().isoformat(), "size_bytes": db_size}]

        # Total actius i eliminats
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM facts WHERE deleted_at IS NULL")
        row = await cursor.fetchone()
        total_active = row["cnt"] if row else 0

        cursor = await db.execute("SELECT COUNT(*) as cnt FROM facts WHERE deleted_at IS NOT NULL")
        row = await cursor.fetchone()
        total_deleted = row["cnt"] if row else 0

        # Total fragments
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM chunks")
        row = await cursor.fetchone()
        total_chunks = row["cnt"] if row else 0

        # Total agents
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM agents")
        row = await cursor.fetchone()
        total_agents = row["cnt"] if row else 0

        # Total consolidated
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM consolidated")
        row = await cursor.fetchone()
        total_consolidated = row["cnt"] if row else 0

        # Total notion cached
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM notion_cache")
        row = await cursor.fetchone()
        total_notion_cached = row["cnt"] if row else 0

        # Ollama connected
        ollama_connected = embedding_service.is_ready

        # Últimes 10 entrades d'auditoria
        cursor = await db.execute(
            "SELECT id, agent_id, action, resource_type, resource_id, payload, timestamp FROM audit_log ORDER BY id DESC LIMIT 10"
        )
        last_10_audit = [dict(row) for row in await cursor.fetchall()]

    return JSONResponse({
        "facts_last_7_days": facts_last_7_days,
        "facts_by_agent": facts_by_agent,
        "facts_by_category": facts_by_category,
        "db_size_history": db_size_history,
        "total_active": total_active,
        "total_deleted": total_deleted,
        "last_10_audit": last_10_audit,
        "total_chunks": total_chunks,
        "total_agents": total_agents,
        "total_consolidated": total_consolidated,
        "total_notion_cached": total_notion_cached,
        "ollama_connected": ollama_connected,
    })

@router.get("/api/search")
async def search_facts(q: str = "", category: str = "", limit: int = 20) -> JSONResponse:
    """Cerca fets per text complet (FTS5). Públic com /api/stats."""
    if not q.strip():
        return JSONResponse({"results": [], "total": 0})
    try:
        from pluribus.db import get_db
        
        async with get_db() as db:
            terms = q.strip().split()
            fts_query = " OR ".join(f'"{t}"*' for t in terms if t)
            if not fts_query:
                return JSONResponse({"results": [], "total": 0})
            sql = """SELECT f.id, f.scope, f.category, f.agent_id, f.key,
                            substr(f.content, 1, 200) as content_preview,
                            f.created_at
                     FROM facts f
                     JOIN facts_fts fts ON f.id = fts.fact_id
                     WHERE facts_fts MATCH ?
                       AND f.deleted_at IS NULL"""
            bind = [fts_query]
            if category:
                sql += " AND f.category = ?"
                bind.append(category)
            sql += " ORDER BY f.updated_at DESC LIMIT ?"
            bind.append(limit)
            cursor = await db.execute(sql, bind)
            rows = await cursor.fetchall()
        return JSONResponse({"results": [dict(r) for r in rows], "total": len(rows), "query": q})
    except Exception as e:
        return JSONResponse({"error": str(e), "results": []})


@router.get("/api/config")
async def get_config() -> JSONResponse:
    """Retorna la configuració actual de Pluribus (lectura del .env + runtime)."""
    env_path = "/opt/pluribus/.env"
    config = {}
    
    # Llegir .env
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    except FileNotFoundError:
        config["_error"] = ".env no trobat"

    # Filtrar secrets (no exposar claus API)
    SENSITIVE_SUFFIXES = ["_KEY", "_SECRET", "_TOKEN", "_PASSWORD"]
    config = {k: v for k, v in config.items() if not any(suf in k.upper() for suf in SENSITIVE_SUFFIXES)}
    
    # Afegir settings de runtime
    try:
        from pluribus.config import settings as s
        config["_OLLAMA_BASE_URL"] = s.OLLAMA_BASE_URL
        config["_OLLAMA_MODEL"] = s.OLLAMA_MODEL
        config["_CONSOLIDATION_MODEL"] = s.CONSOLIDATION_MODEL
        config["_EMBED_DIM"] = str(s.EMBED_DIM)
        config["_MAX_CHUNK_SIZE"] = str(s.MAX_CHUNK_SIZE)
        config["_CHUNK_OVERLAP"] = str(s.CHUNK_OVERLAP)
        config["_RATE_LIMIT"] = str(s.RATE_LIMIT)
        config["_RATE_LIMIT_WINDOW"] = str(s.RATE_LIMIT_WINDOW)
        config["_API_PORT"] = str(s.API_PORT)
    except ImportError:
        config["_settings_error"] = "No s'ha pogut carregar config"
    
    # Estat del servei
    config["_embedding_ready"] = str(embedding_service.is_ready)
    config["_version"] = "2.0.0"
    
    return JSONResponse(config)


@router.post("/api/config/save")
async def save_config(request: Request) -> JSONResponse:
    """Guarda canvis de configuració al .env i reinicia el servei."""
    body = await request.json()
    env_path = "/opt/pluribus/.env"
    restart = body.pop("_restart", False)
    
    # Llegir .env actual
    lines = []
    try:
        with open(env_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    
    # Actualitzar només les claus enviades (que comencin per PLURIBUS_)
    updated_keys = []
    for key, value in body.items():
        if key.startswith("PLURIBUS_") and value is not None:
            found = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith(key + "="):
                    lines[i] = f"{key}={value}\n"
                    found = True
                    updated_keys.append(key)
                    break
            if not found:
                lines.append(f"{key}={value}\n")
                updated_keys.append(key)
    
    # Escriure .env
    with open(env_path, "w") as f:
        f.writelines(lines)
    
    result = {
        "message": "Configuració guardada",
        "updated_keys": updated_keys,
        "restart": restart,
    }
    
    if restart:
        try:
            subprocess.Popen(["systemctl", "restart", "pluribus"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            result["message"] = "Configuració guardada. Reiniciant Pluribus..."
            result["restarting"] = True
        except Exception as e:
            result["error"] = f"Error al reiniciar: {e}"
    
    return JSONResponse(result)


@router.get("/api/config/restart")
async def restart_pluribus() -> JSONResponse:
    """Reinicia el servei Pluribus."""
    try:
        subprocess.Popen(["systemctl", "restart", "pluribus"],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return JSONResponse({"message": "Reiniciant Pluribus..."})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/ollama/models")
async def get_ollama_models() -> JSONResponse:
    """Retorna els models disponibles al servidor Ollama."""
    import httpx
    try:
        base_url = settings.OLLAMA_BASE_URL.rstrip("/")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base_url}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                models = [m["name"] for m in data.get("models", [])]
                return JSONResponse({"models": models, "count": len(models)})
            return JSONResponse({"error": f"Ollama retornà {resp.status_code}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": f"Error connectant amb Ollama: {str(e)}"}, status_code=502)

@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard() -> HTMLResponse:
    """D1 unified observability dashboard (HOME / AGENTS / MEMORY / SYSTEM).

    READ-ONLY. No API key in HTML. The browser fetches the four
    /v1/dashboard/* read-only endpoints (no admin required, just any
    agent with `read` permission) and renders the data. Auto-refresh
    every 20 seconds. Unknown fields are explicitly shown as
    `UNKNOWN` — we never fabricate online/busy/task/project/blocker.
    """
    html = """<!DOCTYPE html>
<html lang="ca">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width-fit, initial-scale=1.0">
<title>Pluribus — Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: rgba(56, 189, 248, 0.2); }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; padding: 16px; }
h1 { font-size: 1.4rem; margin-bottom: 16px; color: #38bdf8; }
h2 { font-size: 1rem; margin: 0 0 10px 0; color: #94a3b8; }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid #334155; margin-bottom: 16px; flex-wrap: wrap; }
.tab { padding: 10px 16px; cursor: pointer; color: #94a3b8; border: 1px solid transparent; border-radius: 6px 6px 0 0; user-select: none; min-height: 40px; }
.tab:hover { color: #cbd5e1; background: #1e293b; }
.tab.active { color: #38bdf8; background: #1e293b; border-color: #334155; border-bottom-color: #1e293b; }
.tab .count { display: inline-block; margin-left: 6px; padding: 0 6px; background: #334155; color: #cbd5e1; border-radius: 10px; font-size: 0.75rem; }
.tab.active .count { background: #0f172a; }
.panel { display: none; }
.panel.active { display: block; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin-bottom: 14px; }
.card { background: #1e293b; border-radius: 10px; padding: 14px; border: 1px solid #334155; }
.card h3 { font-size: 0.85rem; color: #94a3b8; margin-bottom: 6px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
.status { display: inline-block; padding: 3px 10px; border-radius: 4px; font-size: 0.85rem; font-weight: 600; }
.status-HEALTHY { background: #166534; color: #86efac; }
.status-DEGRADED { background: #92400e; color: #fcd34d; }
.status-DOWN { background: #7f1d1d; color: #fca5a5; }
.status-UNKNOWN, .status-NOT_CONFIGURED { background: #334155; color: #94a3b8; }
.status-OK { background: #166534; color: #86efac; }
.status-NONE, .status-PASS { background: #334155; color: #cbd5e1; }
.status-FAIL, .status-BLOCKED { background: #7f1d1d; color: #fca5a5; }
.kpi { font-size: 1.6rem; font-weight: 700; color: #38bdf8; }
.kpi-sub { font-size: 0.75rem; color: #64748b; }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #334155; vertical-align: top; }
th { color: #94a3b8; font-weight: 600; background: #0f172a; position: sticky; top: 0; }
td { color: #cbd5e1; }
tr:hover td { background: #334155; }
pre { background: #0f172a; padding: 8px; border-radius: 6px; overflow-x: auto; font-size: 0.78rem; color: #cbd5e1; margin: 0; }
.muted { color: #64748b; }
.search-row { display: flex; gap: 6px; margin-bottom: 10px; }
.search-row input { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 6px 10px; border-radius: 6px; font-size: 13px; flex: 1; }
.search-row button { padding: 6px 12px; background: #1e3a5f; border: 1px solid #3b82f6; color: #93c5fd; border-radius: 6px; cursor: pointer; font-size: 13px; }
footer { color: #64748b; font-size: 0.75rem; margin-top: 20px; text-align: center; }
.loading { color: #64748b; padding: 20px; text-align: center; font-style: italic; }
@media (max-width: 640px) {
  body { padding: 10px; }
  h1 { font-size: 1.1rem; }
  .tab { padding: 8px 10px; font-size: 0.85rem; }
  .grid { grid-template-columns: 1fr; }
  th, td { padding: 6px 4px; font-size: 0.75rem; }
}
</style>
</head>
<body>

<h1>Pluribus — Dashboard (D1 read-only)</h1>

<nav class="tabs" id="tabs">
  <div class="tab active" data-panel="home">HOME</div>
  <div class="tab" data-panel="agents">AGENTS</div>
  <div class="tab" data-panel="memory">MEMORY</div>
  <div class="tab" data-panel="system">SYSTEM</div>
</nav>

<section class="panel active" id="panel-home">
  <div class="grid" id="home-grid"><div class="loading">Carregant...</div></div>
</section>

<section class="panel" id="panel-agents">
  <div id="agents-content"><div class="loading">Carregant...</div></div>
</section>

<section class="panel" id="panel-memory">
  <div class="search-row">
    <input type="text" id="memory-q" placeholder="Cerca a Pluribus memory...">
    <button id="memory-search-btn">Cerca</button>
  </div>
  <div id="memory-content"><div class="loading">Carregant...</div></div>
</section>

<section class="panel" id="panel-system">
  <div class="grid" id="system-grid"><div class="loading">Carregant...</div></div>
</section>

<footer id="footer">D1 · última actualització: <span id="last-update">—</span></footer>

<script>
// ========== D1 UNIFIED OBSERVABILITY ==========
// Read-only. The browser never receives an API key. The four endpoints
// under /v1/dashboard/* accept any agent with `read` permission. We
// do not need admin because the endpoints do not require it.

const API = {
  summary: '/v1/dashboard/summary',
  agents:  '/v1/dashboard/agents',
  memory:  '/v1/dashboard/memory',
  system:  '/v1/dashboard/system',
};

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function statusBadge(s) {
  const t = (s || 'UNKNOWN').toUpperCase();
  return `<span class="status status-${esc(t)}">${esc(t)}</span>`;
}

function statusFor(v) {
  return statusBadge(v);
}

function panel(name) { return document.getElementById('panel-' + name); }

async function fetchJson(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return await r.json();
}

// ========== HOME ==========
async function loadHome() {
  const el = document.getElementById('home-grid');
  el.innerHTML = '<div class="loading">Carregant...</div>';
  try {
    const s = await fetchJson(API.summary);
    const services = ['pluribus','xerrameca','hermes','ollama'];
    let cards = '';
    for (const svc of services) {
      const info = s[svc] || {};
      cards += `
        <div class="card">
          <h3>${esc(svc.toUpperCase())}</h3>
          <p>${statusFor(info.status)}</p>
          <p class="muted">${esc(info.version || 'UNKNOWN')} · ${esc(info.detail || '')}</p>
        </div>`;
    }
    cards += `
      <div class="card"><h3>AGENTS</h3><p class="kpi">${esc(s.agents_known ?? 'UNKNOWN')}</p><p class="kpi-sub">registrats a Pluribus</p></div>
      <div class="card"><h3>RECENT MEMORIES</h3><p class="kpi">${esc(s.recent_memories ?? 'UNKNOWN')}</p><p class="kpi-sub">últims al memory</p></div>
      <div class="card"><h3>WARNINGS</h3><p class="kpi">${esc(s.warnings ?? 0)}</p><p class="kpi-sub">facts amb BLOCKED/FAIL</p></div>`;
    el.innerHTML = cards;
  } catch (e) {
    el.innerHTML = `<div class="card">Error: ${esc(e.message)}</div>`;
  }
}

// ========== AGENTS ==========
async function loadAgents() {
  const el = document.getElementById('agents-content');
  el.innerHTML = '<div class="loading">Carregant...</div>';
  try {
    const j = await fetchJson(API.agents);
    const rows = (j.agents || []).map(a => {
      const active = a.active_flag ? 'YES' : 'NO';
      return `<tr>
        <td><strong>${esc(a.name || '?')}</strong><br><span class="muted">${esc(a.identity || '')}</span></td>
        <td>${statusBadge(active)}<br><span class="muted">registered</span></td>
        <td>${statusBadge(a.online_now || 'UNKNOWN')}</td>
        <td>${statusBadge(a.last_known_activity || 'UNKNOWN')}</td>
        <td>${esc(a.current_task || 'UNKNOWN')}</td>
        <td>${esc(a.project || 'UNKNOWN')}</td>
        <td>${statusBadge(a.blocker || 'NONE')}</td>
        <td>${statusBadge(a.last_result || 'UNKNOWN')}</td>
      </tr>`;
    }).join('');
    el.innerHTML = `<table>
      <thead><tr>
        <th>NAME / IDENTITY</th><th>REGISTERED</th><th>ONLINE NOW</th>
        <th>LAST ACTIVITY</th><th>CURRENT TASK</th><th>PROJECT</th>
        <th>BLOCKER</th><th>LAST RESULT</th>
      </tr></thead>
      <tbody>${rows || '<tr><td colspan="8" class="muted">No agents</td></tr>'}</tbody>
    </table>
    <p class="muted" style="margin-top:10px;">${esc(j.count || 0)} agent(s) known. Active = Pluribus registered. Online = real-time presence (UNKNOWN unless a heartbeat source is available).</p>`;
  } catch (e) {
    el.innerHTML = `<div class="card">Error: ${esc(e.message)}</div>`;
  }
}

// ========== MEMORY ==========
async function loadMemory() {
  const el = document.getElementById('memory-content');
  el.innerHTML = '<div class="loading">Carregant...</div>';
  const q = document.getElementById('memory-q').value.trim();
  const url = API.memory + '?limit=20' + (q ? '&q=' + encodeURIComponent(q) : '');
  try {
    const j = await fetchJson(url);
    const rows = (j.items || []).map(it => `<tr>
      <td><code>${esc(it.id ? it.id.slice(0, 8) : '?')}</code><br><span class="muted">${esc(it.created_at || '?')}</span></td>
      <td>${esc(it.key || '(no-key)')}</td>
      <td>${esc(it.category || '?')}</td>
      <td>${esc(it.project || 'UNKNOWN')}</td>
      <td>${esc(it.scope || '?')}</td>
      <td><pre>${esc((it.content_preview || '').slice(0, 200))}</pre></td>
    </tr>`).join('');
    el.innerHTML = `<table>
      <thead><tr><th>ID / TIME</th><th>KEY</th><th>CATEGORY</th><th>PROJECT</th><th>SCOPE</th><th>PREVIEW</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="muted">No facts</td></tr>'}</tbody>
    </table>
    <p class="muted" style="margin-top:10px;">${esc(j.total ?? '?')} total · showing ${esc(j.items ? j.items.length : 0)}${q ? ' · search: ' + esc(q) : ''}</p>`;
  } catch (e) {
    el.innerHTML = `<div class="card">Error: ${esc(e.message)}</div>`;
  }
}

// ========== SYSTEM ==========
async function loadSystem() {
  const el = document.getElementById('system-grid');
  el.innerHTML = '<div class="loading">Carlegant...</div>';
  try {
    const j = await fetchJson(API.system);
    const cards = (j.services || []).map(s => `
      <div class="card">
        <h3>${esc(s.name || '?')}</h3>
        <p>${statusBadge(s.status || 'UNKNOWN')}</p>
        <p class="muted">${esc(s.version || 'UNKNOWN version')}</p>
        <p class="muted">${esc(s.endpoint || '')}</p>
        <p class="muted">last check: ${esc(s.last_check || '?')}</p>
      </div>`).join('');
    el.innerHTML = cards || '<div class="card muted">No services discovered</div>';
  } catch (e) {
    el.innerHTML = `<div class="card">Error: ${esc(e.message)}</div>`;
  }
}

// ========== TABS ==========
document.getElementById('tabs').addEventListener('click', e => {
  const tab = e.target.closest('.tab');
  if (!tab) return;
  const name = tab.dataset.panel;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t === tab));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + name));
  if (name === 'agents') loadAgents();
  else if (name === 'memory') loadMemory();
  else if (name === 'system') loadSystem();
  else loadHome();
});

// ========== MEMORY SEARCH ==========
document.getElementById('memory-search-btn').addEventListener('click', loadMemory);
document.getElementById('memory-q').addEventListener('keydown', e => { if (e.key === 'Enter') loadMemory(); });

// ========== AUTO-REFRESH (20s) ==========
async function refreshAll() {
  await Promise.all([loadHome(), loadSystem()]);
  // Update footer timestamp
  const lu = document.getElementById('last-update');
  if (lu) lu.textContent = new Date().toISOString();
}

refreshAll();
setInterval(refreshAll, 20000);

// Refresh MEMORY / AGENTS when their tab is opened (no global interval
// to avoid hammering the server when nobody is looking).
</script>
</body>
</html>"""
    return html
