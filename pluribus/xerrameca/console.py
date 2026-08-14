"""Standalone operations console for Xerrameca and Xerrameca Runner."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["xerrameca-console"])


@router.get("/dashboard/xerrameca", response_class=HTMLResponse)
async def xerrameca_console() -> HTMLResponse:
    return HTMLResponse(_HTML)


_HTML = r'''<!doctype html>
<html lang="ca">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pluribus · Xerrameca</title>
<style>
:root{color-scheme:dark;--bg:#0f172a;--panel:#172033;--panel2:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--ok:#86efac;--warn:#fbbf24;--bad:#fca5a5}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}.wrap{max-width:1500px;margin:auto;padding:18px}.top{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}.title{font-size:1.7rem;font-weight:750;color:var(--accent)}.sub{color:var(--muted);font-size:.9rem}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px;margin-top:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}.panel h2{font-size:1rem;color:#cbd5e1;margin:0 0 12px}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.field{flex:1;min-width:140px}label{display:block;color:var(--muted);font-size:.75rem;margin:0 0 4px}input,select,textarea{width:100%;background:#0b1220;border:1px solid var(--line);color:var(--text);border-radius:7px;padding:8px}textarea{min-height:72px;resize:vertical}.btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:8px 11px;border-radius:7px;cursor:pointer}.btn:hover{border-color:#64748b}.primary{border-color:#0ea5e9;color:#7dd3fc}.good{border-color:#22c55e;color:var(--ok)}.warn{border-color:#f59e0b;color:var(--warn)}.danger{border-color:#ef4444;color:var(--bad)}.badge{display:inline-block;border:1px solid var(--line);padding:2px 7px;border-radius:99px;font-size:.72rem}.on{border-color:#22c55e;color:var(--ok)}.off{border-color:#64748b;color:#cbd5e1}.blocked{border-color:#f59e0b;color:var(--warn)}.error{border-color:#ef4444;color:var(--bad)}.counter{font-size:1.7rem;font-weight:800;color:var(--accent)}.muted{color:var(--muted)}.small{font-size:.78rem}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.list{display:grid;gap:10px}.item{background:#111a2b;border:1px solid var(--line);border-radius:9px;padding:11px}.item-head{display:flex;gap:8px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}.item-title{font-weight:700}.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px;margin:8px 0}.kv div{font-size:.78rem;color:var(--muted)}.kv b{display:block;color:#cbd5e1;font-weight:600}.secret{background:#2a1f10;border:1px solid #92400e;padding:10px;border-radius:8px;margin-top:8px;word-break:break-all}.hidden{display:none!important}.toast{position:fixed;right:16px;bottom:16px;max-width:420px;background:#111827;border:1px solid var(--line);padding:10px 12px;border-radius:8px;z-index:1000}.toast.bad{border-color:#ef4444}.section-title{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-top:20px}.section-title h2{margin:0;color:#cbd5e1}.auth{display:flex;gap:8px;align-items:end;min-width:min(100%,520px)}.auth .field{min-width:240px}@media(max-width:700px){.wrap{padding:10px}.auth{width:100%}.auth .field{min-width:0}.row>.field{min-width:100%}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div><div class="title">Xerrameca Console</div><div class="sub">Agent-to-Agent · torns, converses i Runner</div></div>
    <div class="auth">
      <div class="field"><label>API key admin (només sessionStorage)</label><input id="apiKey" type="password" autocomplete="off" placeholder="plb_…"></div>
      <button class="btn primary" onclick="connect()">Connecta</button>
      <a class="btn" href="/dashboard">Dashboard</a>
    </div>
  </div>

  <div id="locked" class="panel" style="margin-top:14px"><b>Introdueix una API key admin.</b> La pàgina és pública però cap dada Xerrameca es carrega sense autenticació.</div>
  <div id="app" class="hidden">
    <div class="grid">
      <div class="panel"><h2>Xerrameca</h2><div class="row"><span id="xerBadge" class="badge">—</span><button class="btn" onclick="toggleXerrameca()">Activa/Desactiva</button></div><div class="kv"><div>Converses<b id="convCount">0</b></div><div>Actives<b id="activeCount">0</b></div><div>Bloquejades<b id="blockedCount">0</b></div></div></div>
      <div class="panel"><h2>Runner</h2><div class="row"><span id="runnerBadge" class="badge">—</span><button class="btn" onclick="toggleRunner()">Activa/Desactiva</button><button class="btn warn" onclick="runnerTick()">Tick manual</button></div><div class="kv"><div>Interval<b id="pollInterval">—</b></div><div>Max/tick<b id="maxTick">—</b></div><div>Agents Runner<b id="runnerCount">0</b></div></div></div>
      <div class="panel"><h2>Agents</h2><div class="counter" id="agentCount">0</div><div class="small muted">Agents disponibles per crear Xerrameques i configurar callbacks Runner.</div></div>
    </div>

    <div class="section-title"><h2>Configuració global Runner</h2></div>
    <div class="panel">
      <div class="row">
        <div class="field"><label>Poll interval (s)</label><input id="runnerInterval" type="number" min="0.25" max="60" step="0.25"></div>
        <div class="field"><label>Max dispatchos/tick</label><input id="runnerMax" type="number" min="1" max="100"></div>
        <button class="btn primary" onclick="saveRunnerSystem()">Desa Runner</button>
      </div>
    </div>

    <div class="section-title"><h2>Nova Xerrameca</h2></div>
    <div class="panel">
      <div class="row"><div class="field"><label>Nom</label><input id="newName" maxlength="128"></div><div class="field"><label>Scope</label><input id="newScope" value="shared"></div></div>
      <div style="margin-top:8px"><label>Objectiu</label><textarea id="newObjective" placeholder="Què han de resoldre els dos agents?"></textarea></div>
      <div class="row" style="margin-top:8px">
        <div class="field"><label>Agent 1</label><select id="newAgent1"></select></div><div class="field"><label>Agent 2</label><select id="newAgent2"></select></div>
        <div class="field"><label>Política</label><select id="newPolicy" onchange="policyChanged()"><option value="alternating">alternating</option><option value="supervisor">supervisor</option></select></div>
        <div class="field"><label>Supervisor</label><select id="newSupervisor" disabled></select></div>
        <div class="field"><label>Max rondes</label><input id="newRounds" type="number" value="20" min="1" max="200"></div>
        <div class="field"><label>Timeout torn (s)</label><input id="newTimeout" type="number" value="300" min="10" max="86400"></div>
      </div>
      <div class="row" style="margin-top:10px"><button class="btn primary" onclick="createConversation()">Crea</button><button class="btn good" onclick="createAndStart()">Crea i inicia</button></div>
    </div>

    <div class="section-title"><h2>Converses</h2><button class="btn" onclick="refreshAll()">Actualitza</button></div>
    <div id="conversations" class="list"></div>

    <div class="section-title"><h2>Runner per agent</h2></div>
    <div class="panel">
      <div class="row">
        <div class="field"><label>Agent</label><select id="runnerAgent"></select></div>
        <div class="field" style="flex:2"><label>Callback URL</label><input id="runnerUrl" placeholder="https://agent.example/xerrameca/turn"></div>
        <div class="field"><label>Timeout</label><input id="runnerTimeout" type="number" value="30" min="2" max="120"></div>
        <div class="field"><label>Max errors</label><input id="runnerFailures" type="number" value="3" min="1" max="20"></div>
        <div class="field"><label>Cooldown (s)</label><input id="runnerCooldown" type="number" value="60" min="10" max="3600"></div>
        <button class="btn primary" onclick="saveRunnerConfig()">Configura</button>
      </div>
      <div id="secretBox" class="secret hidden"></div>
    </div>
    <div id="runners" class="list" style="margin-top:10px"></div>
  </div>
</div>
<div id="toast" class="toast hidden"></div>
<script>
let apiKey = sessionStorage.getItem('pluribus_admin_key') || '';
let state = {system:null, runnerSystem:null, conversations:[], runners:[], agents:[]};
const $=id=>document.getElementById(id);
$('apiKey').value=apiKey;
function toast(msg,bad=false){const t=$('toast');t.textContent=msg;t.className='toast'+(bad?' bad':'');setTimeout(()=>t.classList.add('hidden'),4500)}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(path,opts={}){if(!apiKey)throw new Error('Falta API key');const headers=Object.assign({'X-API-Key':apiKey},opts.headers||{});if(opts.body&&!headers['Content-Type'])headers['Content-Type']='application/json';const r=await fetch(path,Object.assign({},opts,{headers}));let data=null;const txt=await r.text();if(txt){try{data=JSON.parse(txt)}catch{data=txt}}if(!r.ok){const d=data&&data.detail?data.detail:(typeof data==='string'?data:`HTTP ${r.status}`);throw new Error(d)}return data}
async function connect(){apiKey=$('apiKey').value.trim();if(!apiKey)return toast('Introdueix API key',true);sessionStorage.setItem('pluribus_admin_key',apiKey);try{await refreshAll();$('locked').classList.add('hidden');$('app').classList.remove('hidden')}catch(e){toast(e.message,true);sessionStorage.removeItem('pluribus_admin_key')}}
async function refreshAll(){const [system,runnerSystem,conversations,runners,agents]=await Promise.all([api('/v1/xerrameca/system'),api('/v1/xerrameca/runner/system'),api('/v1/xerrameca/conversations'),api('/v1/xerrameca/runners'),api('/v1/agents')]);state={system,runnerSystem,conversations,runners,agents};render()}
function statusBadge(el,on,labelOn='ACTIU',labelOff='ATURAT'){el.textContent=on?labelOn:labelOff;el.className='badge '+(on?'on':'off')}
function agentOptions(selected=''){return state.agents.filter(a=>a.is_active).map(a=>`<option value="${esc(a.id)}" ${a.id===selected?'selected':''}>${esc(a.name)} · ${esc(a.id)}</option>`).join('')}
function render(){statusBadge($('xerBadge'),!!state.system.enabled);statusBadge($('runnerBadge'),!!state.runnerSystem.enabled);$('convCount').textContent=state.conversations.length;$('activeCount').textContent=state.conversations.filter(x=>x.status==='active').length;$('blockedCount').textContent=state.conversations.filter(x=>['blocked','error'].includes(x.status)).length;$('pollInterval').textContent=state.runnerSystem.poll_interval_seconds+'s';$('maxTick').textContent=state.runnerSystem.max_dispatches_per_tick;$('runnerCount').textContent=state.runners.length;$('agentCount').textContent=state.agents.length;$('runnerInterval').value=state.runnerSystem.poll_interval_seconds;$('runnerMax').value=state.runnerSystem.max_dispatches_per_tick;for(const id of ['newAgent1','newAgent2','newSupervisor','runnerAgent'])$(id).innerHTML=agentOptions();renderConversations();renderRunners();policyChanged()}
async function toggleXerrameca(){try{await api('/v1/xerrameca/system',{method:'PATCH',body:JSON.stringify({enabled:!state.system.enabled})});await refreshAll()}catch(e){toast(e.message,true)}}
async function toggleRunner(){try{await api('/v1/xerrameca/runner/system',{method:'PATCH',body:JSON.stringify({enabled:!state.runnerSystem.enabled})});await refreshAll()}catch(e){toast(e.message,true)}}
async function saveRunnerSystem(){try{await api('/v1/xerrameca/runner/system',{method:'PATCH',body:JSON.stringify({poll_interval_seconds:Number($('runnerInterval').value),max_dispatches_per_tick:Number($('runnerMax').value)})});toast('Runner actualitzat');await refreshAll()}catch(e){toast(e.message,true)}}
async function runnerTick(){try{const r=await api('/v1/xerrameca/runner/tick',{method:'POST'});toast(`Tick: ${r.dispatched??0} dispatchos`);await refreshAll()}catch(e){toast(e.message,true)}}
function policyChanged(){$('newSupervisor').disabled=$('newPolicy').value!=='supervisor'}
function newConversationBody(){const policy=$('newPolicy').value;const a1=$('newAgent1').value,a2=$('newAgent2').value;if(a1===a2)throw new Error('Selecciona dos agents diferents');const body={name:$('newName').value.trim(),objective:$('newObjective').value.trim(),scope:$('newScope').value.trim()||'shared',participant_agent_ids:[a1,a2],turn_policy:policy,first_agent_id:a1,max_rounds:Number($('newRounds').value),turn_timeout_seconds:Number($('newTimeout').value),persist_summary:true};if(policy==='supervisor')body.supervisor_agent_id=$('newSupervisor').value;return body}
async function createConversation(start=false){try{const c=await api('/v1/xerrameca/conversations',{method:'POST',body:JSON.stringify(newConversationBody())});if(start)await api(`/v1/xerrameca/conversations/${encodeURIComponent(c.id)}/start`,{method:'POST'});toast(start?'Xerrameca creada i iniciada':'Xerrameca creada');await refreshAll()}catch(e){toast(e.message,true)}}
async function createAndStart(){return createConversation(true)}
function convBadge(c){const cls=c.status==='active'?'on':(['blocked','error'].includes(c.status)?'error':'off');return `<span class="badge ${cls}">${esc(c.status)}</span>`}
function renderConversations(){const box=$('conversations');if(!state.conversations.length){box.innerHTML='<div class="panel muted">Cap Xerrameca.</div>';return}box.innerHTML=state.conversations.map(c=>{const t=c.current_turn||{};const parts=(c.participants||[]).map(p=>`${esc(p.name||p.agent_id)}${p.role==='supervisor'?' (S)':''}${p.enabled?'':' [off]'}`).join(' · ');return `<div class="item"><div class="item-head"><div><div class="item-title">${esc(c.name)} ${convBadge(c)}</div><div class="small muted">${esc(c.objective)}</div></div><div class="row"><button class="btn" onclick="viewMessages('${esc(c.id)}')">Missatges</button><button class="btn warn" onclick="pauseConv('${esc(c.id)}')">Pausa</button><button class="btn good" onclick="resumeConv('${esc(c.id)}')">Resume</button><button class="btn" onclick="editConv('${esc(c.id)}')">Config</button><button class="btn" onclick="assignConv('${esc(c.id)}')">Assigna</button><button class="btn" onclick="skipConv('${esc(c.id)}')">Salta</button><button class="btn danger" onclick="cancelConv('${esc(c.id)}')">Cancel·la</button></div></div><div class="kv"><div>Scope<b>${esc(c.scope)}</b></div><div>Ronda<b>${esc(c.current_round)} / ${esc(c.max_rounds)}</b></div><div>Política<b>${esc(c.turn_policy)}</b></div><div>Torn actual<b>${esc(t.assigned_agent_id||'—')} · ${esc(t.status||'—')}</b></div><div>Lease fins<b>${esc(t.lease_until||'—')}</b></div><div>Participants<b>${parts||'—'}</b></div></div></div>`}).join('')}
async function pauseConv(id){try{await api(`/v1/xerrameca/conversations/${id}/pause`,{method:'POST',body:JSON.stringify({reason:'dashboard'})});await refreshAll()}catch(e){toast(e.message,true)}}
async function resumeConv(id){try{await api(`/v1/xerrameca/conversations/${id}/resume`,{method:'POST',body:'{}'});await refreshAll()}catch(e){toast(e.message,true)}}
async function cancelConv(id){if(!confirm('Cancel·lar aquesta Xerrameca?'))return;try{await api(`/v1/xerrameca/conversations/${id}/cancel`,{method:'POST'});await refreshAll()}catch(e){toast(e.message,true)}}
async function editConv(id){const c=state.conversations.find(x=>x.id===id);const rounds=prompt('Max rondes',c.max_rounds);if(rounds===null)return;const timeout=prompt('Timeout torn (s)',c.turn_timeout_seconds);if(timeout===null)return;try{await api(`/v1/xerrameca/conversations/${id}/settings`,{method:'PATCH',body:JSON.stringify({max_rounds:Number(rounds),turn_timeout_seconds:Number(timeout)})});await refreshAll()}catch(e){toast(e.message,true)}}
async function assignConv(id){const c=state.conversations.find(x=>x.id===id);const ids=(c.participants||[]).filter(p=>p.enabled).map(p=>p.agent_id);const target=prompt('Agent ID per al torn:\n'+ids.join('\n'),ids[0]||'');if(!target)return;try{await api(`/v1/xerrameca/conversations/${id}/turn/assign`,{method:'POST',body:JSON.stringify({agent_id:target,force:true,reason:'dashboard'})});await refreshAll()}catch(e){toast(e.message,true)}}
async function skipConv(id){try{await api(`/v1/xerrameca/conversations/${id}/turn/skip`,{method:'POST',body:JSON.stringify({reason:'dashboard'})});await refreshAll()}catch(e){toast(e.message,true)}}
async function viewMessages(id){try{const m=await api(`/v1/xerrameca/conversations/${id}/messages`);const text=m.map(x=>`R${x.round_no} ${x.from_agent_id||'system'} → ${x.to_agent_id||'-'} [${x.turn_result||x.message_type}]\n${x.content}`).join('\n\n');alert(text||'Sense missatges')}catch(e){toast(e.message,true)}}
async function saveRunnerConfig(){try{const agent=$('runnerAgent').value;const result=await api(`/v1/xerrameca/runners/${encodeURIComponent(agent)}`,{method:'PUT',body:JSON.stringify({endpoint_url:$('runnerUrl').value.trim(),enabled:true,request_timeout_seconds:Number($('runnerTimeout').value),max_failures:Number($('runnerFailures').value),cooldown_seconds:Number($('runnerCooldown').value)})});if(result.secret){$('secretBox').textContent='SECRET — copia’l ara: '+result.secret;$('secretBox').classList.remove('hidden')}else $('secretBox').classList.add('hidden');toast('Runner configurat');await refreshAll()}catch(e){toast(e.message,true)}}
function runnerState(r){if(r.circuit_open_until)return '<span class="badge error">CIRCUIT OPEN</span>';if(!r.enabled)return '<span class="badge off">OFF</span>';if(r.consecutive_failures)return '<span class="badge blocked">DEGRADED</span>';return '<span class="badge on">READY</span>'}
function renderRunners(){const box=$('runners');if(!state.runners.length){box.innerHTML='<div class="panel muted">Cap agent amb Runner configurat.</div>';return}box.innerHTML=state.runners.map(r=>`<div class="item"><div class="item-head"><div><div class="item-title">${esc(r.agent_name||r.agent_id)} ${runnerState(r)}</div><div class="small mono muted">${esc(r.endpoint_url)}</div></div><div class="row"><button class="btn" onclick="editRunner('${esc(r.agent_id)}')">Edita</button><button class="btn warn" onclick="rotateSecret('${esc(r.agent_id)}')">Rota secret</button><button class="btn danger" onclick="deleteRunner('${esc(r.agent_id)}')">Elimina</button></div></div><div class="kv"><div>Fallades<b>${esc(r.consecutive_failures)} / ${esc(r.max_failures)}</b></div><div>Últim HTTP<b>${esc(r.last_status??'—')}</b></div><div>Últim intent<b>${esc(r.last_attempted_at||'—')}</b></div><div>Últim èxit<b>${esc(r.last_success_at||'—')}</b></div><div>Circuit fins<b>${esc(r.circuit_open_until||'—')}</b></div><div>Error<b>${esc(r.last_error||'—')}</b></div></div></div>`).join('')}
function editRunner(id){const r=state.runners.find(x=>x.agent_id===id);$('runnerAgent').value=id;$('runnerUrl').value=r.endpoint_url;$('runnerTimeout').value=r.request_timeout_seconds;$('runnerFailures').value=r.max_failures;$('runnerCooldown').value=r.cooldown_seconds;window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'})}
async function rotateSecret(id){if(!confirm('Rotar el secret? El receptor deixarà de validar fins que l’actualitzis.'))return;try{const r=await api(`/v1/xerrameca/runners/${encodeURIComponent(id)}/rotate-secret`,{method:'POST'});$('secretBox').textContent='NOU SECRET — copia’l ara: '+r.secret;$('secretBox').classList.remove('hidden');await refreshAll()}catch(e){toast(e.message,true)}}
async function deleteRunner(id){if(!confirm('Eliminar configuració Runner?'))return;try{await api(`/v1/xerrameca/runners/${encodeURIComponent(id)}`,{method:'DELETE'});toast('Runner eliminat');await refreshAll()}catch(e){toast(e.message,true)}}
if(apiKey)connect();
</script>
</body></html>'''
