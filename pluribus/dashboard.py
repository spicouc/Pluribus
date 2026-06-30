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
        db_path = "/opt/pluribus/data/brain.db"
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
    """Retorna la configuració actual del Brain (lectura del .env + runtime)."""
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
    
    # Actualitzar només les claus enviades (que comencin per BRAIN_)
    updated_keys = []
    for key, value in body.items():
        if key.startswith("BRAIN_") and value is not None:
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
            subprocess.Popen(["systemctl", "restart", "brain"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            result["message"] = "Configuració guardada. Reiniciant Pluribus..."
            result["restarting"] = True
        except Exception as e:
            result["error"] = f"Error al reiniciar: {e}"
    
    return JSONResponse(result)


@router.get("/api/config/restart")
async def restart_brain() -> JSONResponse:
    """Reinicia el servei Pluribus."""
    try:
        subprocess.Popen(["systemctl", "restart", "brain"],
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
    """Retorna un dashboard HTML complet amb Chart.js."""
    html = """<!DOCTYPE html>
<html lang="ca">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pluribus - Dashboard</title>
<script defer src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: rgba(56, 189, 248, 0.2); }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; padding: 16px; }
h1 { font-size: 1.6rem; margin-bottom: 20px; color: #38bdf8; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; }
h2 { font-size: 1.1rem; margin-bottom: 12px; color: #94a3b8; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-bottom: 20px; }
.card { background: #1e293b; border-radius: 12px; padding: 16px; border: 1px solid #334155; }
.card canvas { max-height: 220px; width: 100% !important; }
.counter-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
.counter { background: #1e293b; border-radius: 8px; padding: 14px 10px; text-align: center; border: 1px solid #334155; }
.counter .value { font-size: 1.6rem; font-weight: 700; color: #38bdf8; }
.counter .label { font-size: 0.75rem; color: #64748b; margin-top: 4px; }
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; min-width: 500px; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #334155; white-space: nowrap; }
th { color: #94a3b8; font-weight: 600; }
td { color: #cbd5e1; }
tr:hover { background: #334155; }
.status-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; }
.status-ok { background: #166534; color: #86efac; }
.status-deleted { background: #7f1d1d; color: #fca5a5; }
.status-created { background: #1e3a5f; color: #93c5fd; }
.btn { padding: 8px 14px; border-radius: 8px; font-size: 13px; cursor: pointer; border: 1px solid #334155; background: #1e293b; color: #e2e8f0; }
.btn-primary { background: #1e3a5f; border-color: #3b82f6; color: #93c5fd; }
.btn-danger { background: #5b1e1e; border-color: #ef4444; color: #fca5a5; }
.btn-warn { background: #1e293b; border-color: #f59e0b; color: #fbbf24; }
.btn-config { min-height: 44px; touch-action: manipulation; }
input, select { background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 8px 10px; border-radius: 6px; font-size: 13px; width: 100%; }
label { display: block; color: #94a3b8; font-size: 12px; margin-bottom: 4px; }
.config-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 14px; }
.model-dropdown { display: none; position: absolute; top: 100%; left: 0; right: 0; z-index: 100; background: #1e293b; border: 1px solid #334155; border-radius: 0 0 8px 8px; max-height: 200px; overflow-y: auto; }
.model-dropdown.show { display: block; }
.model-dropdown .item { padding: 8px 12px; cursor: pointer; color: #cbd5e1; font-size: 13px; border-bottom: 1px solid #334155; }
.model-dropdown .item:hover { background: #334155; color: #e2e8f0; }
.model-dropdown .item:last-child { border-bottom: none; }
.model-dropdown .loading, .model-dropdown .error { padding: 12px; text-align: center; color: #64748b; font-size: 13px; }
.model-dropdown .error { color: #f87171; }
.search-row { display: flex; gap: 8px; flex-wrap: wrap; }
@media (max-width: 640px) {
  body { padding: 10px; }
  h1 { font-size: 1.3rem; gap: 8px; text-align: center; }
  h1 .btn { padding: 10px 16px; font-size: 14px; min-height: 44px; }
  .counter-grid { grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .counter { padding: 12px 8px; }
  .counter .value { font-size: 1.3rem; }
  .grid { grid-template-columns: 1fr; gap: 12px; }
  .config-grid { grid-template-columns: 1fr; }
  .card { padding: 12px; }
  .card canvas { max-height: 180px; }
  table { font-size: 0.75rem; min-width: auto; }
  th, td { padding: 8px 6px; }
  .btn { padding: 12px 16px; font-size: 14px; }
  .search-row { flex-direction: column; }
  .search-row input { min-width: 100%; }
}
@media (max-width: 480px) {
  .counter-grid { grid-template-columns: repeat(2, 1fr); gap: 6px; }
  .counter { padding: 10px 6px; }
  .counter .value { font-size: 1.1rem; }
  .counter .label { font-size: 0.65rem; }
  .card canvas { max-height: 150px; }
}
@media (max-width: 380px) {
  .counter-grid { gap: 6px; }
  h1 { font-size: 1.1rem; flex-direction: column; text-align: center; }
  h1 .btn { font-size: 14px; padding: 10px 16px; width: 100%; }
  th, td { padding: 5px 4px; font-size: 0.7rem; }
  .counter { padding: 8px 4px; }
  .counter .value { font-size: 1rem; }
  .btn { font-size: 14px; padding: 10px 14px; }
}
@media (hover: none) and (pointer: coarse) {
  .btn, input, select, button { min-height: 44px; }
  .btn { padding: 12px 20px !important; font-size: 16px !important; }
  input, select { font-size: 16px !important; }
  .counter-grid { gap: 10px; }
  .counter { padding: 14px 10px; }
  .model-dropdown .item { padding: 12px 14px; font-size: 15px; }
  h1 .btn { padding: 12px 20px !important; font-size: 16px !important; min-width: 120px; }
}
</style>
</head>
<body>
<h1>🧠 Pluribus Dashboard
  <button onclick="toggleSettings()" class="btn btn-config" style="padding:8px 16px;font-size:14px;">⚙️ Configuració</button>
</h1>

  <div class="counter-grid" id="counters">
    <div class="counter"><div class="value" id="active-count">--</div><div class="label">Fets Actius</div></div>
    <div class="counter"><div class="value" id="deleted-count">--</div><div class="label">Fets Eliminats</div></div>
    <div class="counter"><div class="value" id="chunks-count">--</div><div class="label">Fragments</div></div>
    <div class="counter"><div class="value" id="agents-count">--</div><div class="label">Agents</div></div>
    <div class="counter"><div class="value" id="consolidated-count">--</div><div class="label">Consolidats</div></div>
    <div class="counter"><div class="value" id="notion-count">--</div><div class="label">Notion Cache</div></div>
    <div class="counter"><div class="value" id="ollama-status">--</div><div class="label">Ollama</div></div>
  </div>

<div class="grid">
  <div class="card"><h2>Fets per dia (7 dies)</h2><canvas id="chart-daily"></canvas></div>
  <div class="card"><h2>Distribució per Agent</h2><canvas id="chart-agent"></canvas></div>
  <div class="card"><h2>Per Categoria</h2><canvas id="chart-category"></canvas></div>
  <div class="card" style="grid-column: 1 / -1;"><h2>Mida de la Base de Dades</h2><canvas id="chart-db-size" style="max-height: 150px;"></canvas></div>
</div>

<div class="card" style="margin-bottom: 20px;">
  <h2>🔍 Cerca de Fets</h2>
  <div style="display:flex;gap:8px;flex-wrap:wrap;">
    <input type="text" id="search-q" placeholder="Cerca per text..." style="flex:1;min-width:150px;">
    <select id="search-cat" style="width:auto;min-width:120px;">
      <option value="">Totes les categories</option>
      <option value="sense">sense</option>
      <option value="preferences">preferences</option>
      <option value="profile">profile</option>
      <option value="entities">entities</option>
      <option value="events">events</option>
      <option value="cases">cases</option>
      <option value="patterns">patterns</option>
    </select>
    <button onclick="doSearch()" class="btn btn-primary">🔍 Cercar</button>
  </div>
  <div id="search-results" style="margin-top:12px;font-size:0.85rem;"></div>
</div>

<!-- Settings Panel -->
<div id="settings-panel" class="card" style="margin-top: 20px; display: none;">
  <h2 style="display:flex;justify-content:space-between;align-items:center;">
    ⚙️ Configuració
    <span onclick="toggleSettings()" style="cursor:pointer;color:#64748b;font-size:20px;">✕</span>
  </h2>
  <div id="config-loading" style="padding:20px;text-align:center;color:#64748b;">Carregant configuració...</div>
  <div id="config-form" style="display:none;">
    <div class="config-grid">
      <div>
        <label for="cfg-BRAIN_OLLAMA_BASE_URL">Ollama Base URL</label>
        <input type="text" id="cfg-BRAIN_OLLAMA_BASE_URL" placeholder="http://localhost:11434">
      </div>
      <div>
        <label for="cfg-BRAIN_CONSOLIDATION_MODEL">Model de consolidació
          <button onclick="loadOllamaModels('cfg-BRAIN_CONSOLIDATION_MODEL')" class="btn" style="padding:2px 8px;font-size:11px;float:right;" title="Carregar models d'Ollama">🔄</button>
        </label>
        <div style="position:relative;">
          <input type="text" id="cfg-BRAIN_CONSOLIDATION_MODEL" placeholder="llama3.2:3b" autocomplete="off" onfocus="showModelDropdown('cfg-BRAIN_CONSOLIDATION_MODEL')" onblur="setTimeout(()=>hideModelDropdown(),200)">
          <div id="dropdown-cfg-BRAIN_CONSOLIDATION_MODEL" class="model-dropdown"></div>
        </div>
      </div>
      <div>
        <label for="cfg-BRAIN_OLLAMA_MODEL">Model d'embedding
          <button onclick="loadOllamaModels('cfg-BRAIN_OLLAMA_MODEL')" class="btn" style="padding:2px 8px;font-size:11px;float:right;" title="Carregar models d'Ollama">🔄</button>
        </label>
        <div style="position:relative;">
          <input type="text" id="cfg-BRAIN_OLLAMA_MODEL" placeholder="nomic-embed-text-v2-moe" autocomplete="off" onfocus="showModelDropdown('cfg-BRAIN_OLLAMA_MODEL')" onblur="setTimeout(()=>hideModelDropdown(),200)">
          <div id="dropdown-cfg-BRAIN_OLLAMA_MODEL" class="model-dropdown"></div>
        </div>
      </div>
      <div>
        <label>Max Chunk Size / Overlap</label>
        <div style="display:flex;gap:8px;">
          <input type="number" id="cfg-MAX_CHUNK_SIZE" placeholder="500">
          <input type="number" id="cfg-CHUNK_OVERLAP" placeholder="50">
        </div>
      </div>
    </div>
    <div id="config-status" style="color:#86efac;font-size:13px;margin-bottom:12px;display:none;"></div>
    <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;">
      <button onclick="saveConfig(false)" class="btn btn-primary">💾 Guardar</button>
      <button onclick="saveConfig(true)" class="btn btn-danger">💾 Guardar & Reiniciar</button>
      <button onclick="restartPluribus()" class="btn btn-warn">🔄 Reiniciar Pluribus</button>
      <span style="color:#64748b;font-size:12px;" id="config-info">Els canvis requereixen reinici</span>
    </div>
  </div>
</div>

<div class="card" style="margin-top: 20px;">
  <h2>Últimes Accions d'Auditoria</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>ID</th><th>Agent</th><th>Acció</th><th>Tipus</th><th>Recurs</th><th>Timestamp</th></tr></thead>
      <tbody id="audit-table"></tbody>
    </table>
  </div>
</div>

<script>
// ========== ESTADÍSTIQUES ==========
async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();

    document.getElementById('active-count').textContent = data.total_active;
    document.getElementById('deleted-count').textContent = data.total_deleted;
    document.getElementById('chunks-count').textContent = data.total_chunks;
    document.getElementById('agents-count').textContent = data.total_agents;
    document.getElementById('consolidated-count').textContent = data.total_consolidated || 0;
    document.getElementById('notion-count').textContent = data.total_notion_cached || 0;
    document.getElementById('ollama-status').textContent = data.ollama_connected ? '✓' : '✗';
    document.getElementById('ollama-status').style.color = data.ollama_connected ? '#86efac' : '#fca5a5';

    if (typeof Chart === 'undefined') {
      document.querySelectorAll('canvas').forEach(c => c.style.display = 'none');
    } else {
    new Chart(document.getElementById('chart-daily'), {
      type: 'bar',
      data: {
        labels: data.facts_last_7_days.map(d => d.date),
        datasets: [{ label: 'Fets', data: data.facts_last_7_days.map(d => d.count), backgroundColor: '#38bdf8', borderRadius: 4 }]
      },
      options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { stepSize: 1, color: '#94a3b8' } }, x: { ticks: { color: '#94a3b8' } } } }
    });

    const agentColors = ['#38bdf8','#f472b6','#a78bfa','#34d399','#fbbf24','#fb923c','#f87171','#818cf8'];
    new Chart(document.getElementById('chart-agent'), {
      type: 'pie',
      data: {
        labels: data.facts_by_agent.map(a => a.agent_name),
        datasets: [{ data: data.facts_by_agent.map(a => a.count), backgroundColor: agentColors.slice(0, data.facts_by_agent.length) }]
      },
      options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { position: 'bottom', labels: { color: '#94a3b8' } } } }
    });

    const catColors = ['#fbbf24','#34d399','#818cf8','#f472b6','#fb923c','#f87171','#a78bfa'];
    new Chart(document.getElementById('chart-category'), {
      type: 'doughnut',
      data: {
        labels: data.facts_by_category.map(c => c.category),
        datasets: [{ data: data.facts_by_category.map(c => c.count), backgroundColor: catColors.slice(0, data.facts_by_category.length) }]
      },
      options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { position: 'bottom', labels: { color: '#94a3b8' } } } }
    });

    if (data.db_size_history.length > 0) {
      new Chart(document.getElementById('chart-db-size'), {
        type: 'line',
        data: {
          labels: data.db_size_history.map(d => d.date.substring(0, 10)),
          datasets: [{ label: 'Mida (KB)', data: data.db_size_history.map(d => (d.size_bytes / 1024).toFixed(1)), borderColor: '#34d399', backgroundColor: 'rgba(52,211,153,0.1)', fill: true, tension: 0.3 }]
        },
        options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { labels: { color: '#94a3b8' } } },
          scales: { y: { beginAtZero: true, ticks: { color: '#94a3b8' } }, x: { ticks: { color: '#94a3b8' } } } }
      });
    }
    }

    const tbody = document.getElementById('audit-table');
    data.last_10_audit.forEach(entry => {
      const tr = document.createElement('tr');
      const actionClass = entry.action === 'DELETE' ? 'status-deleted' : 'status-created';
      tr.innerHTML = `<td>${entry.id}</td><td>${entry.agent_id ? entry.agent_id.substring(0,8)+'...' : '-'}</td>
        <td><span class="status-badge ${actionClass}">${entry.action}</span></td>
        <td>${entry.resource_type}</td><td>${entry.resource_id ? entry.resource_id.substring(0,8)+'...' : '-'}</td>
        <td>${entry.timestamp}</td>`;
      tbody.appendChild(tr);
    });

  } catch (err) {
    document.querySelector('.counter-grid').innerHTML = '<p style="color:#f87171;">Error carregant estadístiques</p>';
  }
}

// ========== CONFIGURACIÓ ==========
function toggleSettings() {
  const panel = document.getElementById('settings-panel');
  const isHidden = panel.style.display === 'none' || panel.style.display === '';
  panel.style.display = isHidden ? 'block' : 'none';
  if (isHidden) loadConfig();
}

async function loadConfig() {
  document.getElementById('config-loading').style.display = 'block';
  document.getElementById('config-form').style.display = 'none';
  document.getElementById('config-status').style.display = 'none';
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    document.getElementById('cfg-BRAIN_OLLAMA_BASE_URL').value = data.BRAIN_OLLAMA_BASE_URL || data._OLLAMA_BASE_URL || '';
    document.getElementById('cfg-BRAIN_CONSOLIDATION_MODEL').value = data.BRAIN_CONSOLIDATION_MODEL || data._CONSOLIDATION_MODEL || '';
    document.getElementById('cfg-BRAIN_OLLAMA_MODEL').value = data.BRAIN_OLLAMA_MODEL || data._OLLAMA_MODEL || '';
    document.getElementById('cfg-MAX_CHUNK_SIZE').value = data.MAX_CHUNK_SIZE || data._MAX_CHUNK_SIZE || '500';
    document.getElementById('cfg-CHUNK_OVERLAP').value = data.CHUNK_OVERLAP || data._CHUNK_OVERLAP || '50';
    document.getElementById('config-loading').style.display = 'none';
    document.getElementById('config-form').style.display = 'block';
  } catch (err) {
    document.getElementById('config-loading').textContent = 'Error carregant configuració: ' + err.message;
  }
}

async function saveConfig(restart) {
  const body = {};
  const keys = ['BRAIN_OLLAMA_BASE_URL','BRAIN_CONSOLIDATION_MODEL','BRAIN_OLLAMA_MODEL','MAX_CHUNK_SIZE','CHUNK_OVERLAP'];
  keys.forEach(k => {
    const el = document.getElementById('cfg-'+k);
    if (el) body[k] = el.value.trim();
  });
  if (restart) body._restart = true;

  const status = document.getElementById('config-status');
  status.style.display = 'block';
  status.style.color = '#fbbf24';
  status.textContent = 'Guardant...';

  try {
    const res = await fetch('/api/config/save', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const data = await res.json();
    status.textContent = data.message || 'Configuració guardada';
    status.style.color = data.error ? '#f87171' : '#86efac';
  } catch (err) {
    status.textContent = 'Error: ' + err.message;
    status.style.color = '#f87171';
  }
}

async function restartPluribus() {
  const status = document.getElementById('config-status');
  status.style.display = 'block';
  status.style.color = '#fbbf24';
  status.textContent = 'Reiniciant...';

  try {
    const res = await fetch('/api/config/restart');
    const data = await res.json();
    status.textContent = data.message || 'Reiniciant Pluribus...';
    status.style.color = '#86efac';
  } catch (err) {
    status.textContent = 'Error: ' + err.message;
    status.style.color = '#f87171';
  }
}

// ========== MODELS D'OLLAMA ==========
let _modelsCache = [];

async function loadOllamaModels(focusId) {
  const dd = document.getElementById('dropdown-' + focusId);
  if (!dd) return;
  dd.innerHTML = '<div class="loading">⏳ Carregant...</div>';
  dd.classList.add('show');
  try {
    const res = await fetch('/api/ollama/models');
    const data = await res.json();
    dd.innerHTML = '';
    if (data.models && data.models.length) {
      _modelsCache = data.models;
      // Ordenar: els que comencin pel valor actual primer
      let current = document.getElementById(focusId)?.value || '';
      let sorted = [...data.models].sort((a, b) => {
        if (current && a.startsWith(current) && !b.startsWith(current)) return -1;
        if (current && b.startsWith(current) && !a.startsWith(current)) return 1;
        return a.localeCompare(b);
      });
      sorted.forEach(m => {
        const div = document.createElement('div');
        div.className = 'item';
        div.textContent = m;
        div.onclick = () => selectModel(focusId, m);
        dd.appendChild(div);
      });
    } else {
      dd.innerHTML = '<div class="error">❌ Cap model trobat</div>';
    }
  } catch (err) {
    dd.innerHTML = '<div class="error">❌ Error: ' + err.message + '</div>';
  }
}

function showModelDropdown(fieldId) {
  const dd = document.getElementById('dropdown-' + fieldId);
  if (!dd) return;
  // Si ja té models, mostra'ls; si no, carrega'ls
  if (dd.children.length === 0) {
    loadOllamaModels(fieldId);
  } else {
    dd.classList.add('show');
  }
}

function hideModelDropdown() {
  document.querySelectorAll('.model-dropdown').forEach(d => d.classList.remove('show'));
}

function selectModel(fieldId, model) {
  document.getElementById(fieldId).value = model;
  hideModelDropdown();
}

// ========== CERCA ==========
async function doSearch() {
  const q = document.getElementById('search-q').value.trim();
  const cat = document.getElementById('search-cat').value;
  const results = document.getElementById('search-results');
  if (!q) { results.innerHTML = '<span style="color:#64748b;">Escriu un text per cercar</span>'; return; }
  results.innerHTML = '<span style="color:#64748b;">Cercant...</span>';
  try {
    const res = await fetch('/api/search?q='+encodeURIComponent(q)+'&category='+encodeURIComponent(cat)+'&limit=20');
    const data = await res.json();
    if (data.error) { results.innerHTML = '<span style="color:#f87171;">Error: '+data.error+'</span>'; return; }
    if (data.total === 0) { results.innerHTML = '<span style="color:#64748b;">Cap resultat per &quot;'+q+'&quot;</span>'; return; }
    let html = '<div style="color:#94a3b8;margin-bottom:8px;">'+data.total+' resultats per &quot;'+q+'&quot;</div><div class="table-wrap"><table><thead><tr><th>ID</th><th>Categoria</th><th>Contingut</th><th>Data</th></tr></thead><tbody>';
    data.results.forEach(r => {
      const catBadge = r.category ? '<span class="status-badge" style="background:#1e3a5f;color:#93c5fd;">'+r.category+'</span>' : '-';
      html += '<tr><td style="font-family:monospace;font-size:0.7rem;">'+r.id.substring(0,8)+'</td><td>'+catBadge+'</td><td style="white-space:normal;max-width:300px;">'+r.content_preview+'</td><td style="white-space:nowrap;">'+r.created_at.substring(0,10)+'</td></tr>';
    });
    html += '</tbody></table></div>';
    results.innerHTML = html;
  } catch (err) {
    results.innerHTML = '<span style="color:#f87171;">Error: '+err.message+'</span>';
  }
}

// Enter key triggers search
document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('search-q').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') doSearch();
  });
});

// ========== AUTO-REFRESH ==========
setInterval(loadStats, 30000);

loadStats();
</script>
</body>
</html>"""
    return html
