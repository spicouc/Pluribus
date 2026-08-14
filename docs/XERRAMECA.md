# Xerrameca v1

Xerrameca és el motor de conversa Agent-to-Agent de Pluribus. Coordina torns i missatges; no executa ordres arbitràries en nom dels agents.

## Model v1

- Exactament 2 agents per conversa.
- Cada conversa té un `scope`; tots dos agents hi han de tenir accés.
- Polítiques: `alternating` i `supervisor`.
- Estats: `draft`, `active`, `paused`, `blocked`, `completed`, `cancelled`, `error`.
- Els torns utilitzen `claim/lease` per impedir doble execució.
- Resultats estructurats: `continue`, `complete`, `blocked`, `needs_human`, `error`.
- `max_rounds` i `turn_timeout_seconds` es poden canviar en calent.
- El resum final opcional es desa com a fact `x-xerrameca` dins del mateix scope.

## Control global

```http
GET /v1/xerrameca/system
PATCH /v1/xerrameca/system
```

Exemple per aturar temporalment totes les converses sense perdre estat:

```json
{"enabled": false}
```

Es poden canviar també els defaults:

```json
{
  "enabled": true,
  "default_max_rounds": 20,
  "default_turn_timeout_seconds": 300
}
```

## Crear i iniciar una conversa

```http
POST /v1/xerrameca/conversations
```

```json
{
  "name": "Revisió arquitectura",
  "objective": "Agent A proposa i Agent B revisa fins arribar a una decisió.",
  "scope": "shared",
  "participant_agent_ids": ["agent-a", "agent-b"],
  "turn_policy": "supervisor",
  "supervisor_agent_id": "agent-a",
  "first_agent_id": "agent-a",
  "max_rounds": 10,
  "turn_timeout_seconds": 300,
  "persist_summary": true
}
```

Després:

```http
POST /v1/xerrameca/conversations/{id}/start
```

## Flux d'un agent

1. `GET /v1/xerrameca/inbox`
2. `POST /v1/xerrameca/turns/{turn_id}/claim`
3. Processar el missatge retornat.
4. `POST /v1/xerrameca/turns/{turn_id}/reply`

Exemple de resposta:

```json
{
  "lease_token": "...",
  "content": "He revisat la proposta. Recomano separar l'autenticació.",
  "result": "continue",
  "metadata": {"confidence": 0.91}
}
```

En política `supervisor`, només el supervisor pot incloure `next_agent_id` per decidir explícitament el següent torn.

## Controls d'administració

```http
POST  /v1/xerrameca/conversations/{id}/pause
POST  /v1/xerrameca/conversations/{id}/resume
PATCH /v1/xerrameca/conversations/{id}/settings
PATCH /v1/xerrameca/conversations/{id}/participants/{agent_id}
POST  /v1/xerrameca/conversations/{id}/turn/assign
POST  /v1/xerrameca/conversations/{id}/turn/skip
POST  /v1/xerrameca/conversations/{id}/finish
POST  /v1/xerrameca/conversations/{id}/cancel
```

Desactivar una conversa activa la deixa pausada i revoca una lease activa. Rehabilitar-la no la reprèn automàticament: cal `resume` explícit.

Si s'arriba a `max_rounds`, la conversa queda `blocked` amb `block_reason=max_rounds`. Un administrador pot ampliar `max_rounds` i reprendre-la sense perdre l'historial.

## MCP

Xerrameca s'exposa també al catàleg MCP de Pluribus:

- `xerrameca_inbox`
- `xerrameca_claim`
- `xerrameca_reply`
- `xerrameca_list`
- `xerrameca_get`
- `xerrameca_messages`

Això permet que un agent que ja parla MCP amb Pluribus participi en una Xerrameca sense una integració addicional.

## Seguretat

- Crear/configurar/pausar/reprendre/reassignar/finalitzar és admin-only.
- Només un participant pot veure la conversa i l'historial (excepte admin).
- Només l'agent assignat pot reclamar un torn.
- Una resposta necessita una lease vigent i el `lease_token` correcte.
- La desactivació global bloqueja `start`, `claim`, `reply`, `resume` i `skip` sense esborrar dades.
