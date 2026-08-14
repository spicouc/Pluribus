# Xerrameca Runner v1

Xerrameca Runner desperta automàticament un agent quan li arriba un torn. No executa shell ni codi local dins Pluribus: reclama el torn i envia un `POST` JSON signat a l'endpoint configurat per aquell agent.

## Flux

1. Xerrameca crea un torn `ready`.
2. Runner detecta que l'agent assignat té endpoint actiu.
3. Runner fa un `claim` atòmic i obté `lease_token` + `lease_until`.
4. Runner envia `xerrameca.turn.claimed` a l'endpoint de l'agent.
5. L'agent processa el missatge.
6. L'agent respon a Pluribus amb REST `/v1/xerrameca/turns/{turn_id}/reply` o MCP `xerrameca_reply` utilitzant la seva pròpia API key.
7. Xerrameca crea el torn següent segons la política de conversa.

Si l'endpoint rebutja el dispatch o hi ha un error de transport, la lease adquirida per aquell intent es deixa de nou en `ready`. Si l'endpoint accepta el dispatch però l'agent no respon, la lease caduca i el torn torna a ser reclamable.

## Seguretat

- Runner està desactivat per defecte.
- Només administradors poden configurar-lo.
- Cada agent té un secret HMAC independent.
- El secret es mostra només en crear la configuració o rotar-lo.
- Les URLs reutilitzen la protecció SSRF dels webhooks: validació de DNS/IP i connexió pinnejada a la IP validada.
- Loopback, link-local, multicast, unspecified i reserved estan bloquejats. Xarxes privades/CGNAT només s'admeten si la configuració global de webhooks ho permet.
- El payload porta `X-Pluribus-Idempotency-Key: <turn_id>` perquè el receptor pugui deduplicar reintents.
- El Runner no interpreta la resposta HTTP com a instruccions privilegiades: un 2xx només significa que l'agent ha acceptat el torn.

## Activació global

```http
PATCH /v1/xerrameca/runner/system
X-API-Key: <admin>
Content-Type: application/json

{
  "enabled": true,
  "poll_interval_seconds": 2,
  "max_dispatches_per_tick": 4
}
```

Desactivar Runner atura nous dispatchos. Les leases ja lliurades no es revoquen automàticament, perquè l'agent remot pot estar processant-les.

## Configurar un agent

```http
PUT /v1/xerrameca/runners/<agent_id>
X-API-Key: <admin>
Content-Type: application/json

{
  "endpoint_url": "https://agent.example/xerrameca/wake",
  "enabled": true,
  "request_timeout_seconds": 30,
  "max_failures": 3,
  "cooldown_seconds": 60
}
```

La primera resposta inclou `secret`. Cal configurar aquest secret al receptor i no registrar-lo en logs.

Per rotar-lo:

```http
POST /v1/xerrameca/runners/<agent_id>/rotate-secret
```

## Payload rebut per l'agent

```json
{
  "event": "xerrameca.turn.claimed",
  "delivery_id": "...",
  "idempotency_key": "<turn_id>",
  "agent": {
    "id": "agent-a",
    "name": "Agent A"
  },
  "conversation": {
    "id": "...",
    "name": "Revisió",
    "objective": "Revisar una implementació",
    "scope": "shared",
    "turn_policy": "alternating",
    "max_rounds": 10
  },
  "turn": {
    "id": "...",
    "round": 3,
    "lease_token": "...",
    "lease_until": "..."
  },
  "input_message": {
    "id": "...",
    "from_agent_id": "agent-b",
    "to_agent_id": "agent-a",
    "message_type": "message",
    "content": "Revisa aquest resultat",
    "metadata": {},
    "created_at": "..."
  },
  "reply": {
    "rest_path": "/v1/xerrameca/turns/<turn_id>/reply",
    "mcp_tool": "xerrameca_reply"
  }
}
```

## Verificar la signatura

El cos JSON exacte rebut se signa amb HMAC-SHA256:

```text
X-Pluribus-Signature: sha256=<hex digest>
```

El receptor calcula `HMAC_SHA256(secret, raw_request_body)` i compara el digest amb comparació constant-time.

## Circuit breaker

Cada endpoint manté `consecutive_failures`. Quan arriba a `max_failures`, queda temporalment fora del dispatcher fins a `circuit_open_until`. Després del `cooldown_seconds` torna a ser candidat automàticament. Un dispatch 2xx posa el comptador a zero.

## Endpoints d'administració

- `GET /v1/xerrameca/runner/system`
- `PATCH /v1/xerrameca/runner/system`
- `POST /v1/xerrameca/runner/tick`
- `GET /v1/xerrameca/runners`
- `PUT /v1/xerrameca/runners/{agent_id}`
- `POST /v1/xerrameca/runners/{agent_id}/rotate-secret`
- `DELETE /v1/xerrameca/runners/{agent_id}`

`POST /runner/tick` és útil per proves/operacions; el servei normal utilitza el worker intern en background.

## Semàntica de reintents

Un `turn_id` és també la clau d'idempotència. El receptor hauria de persistir els `turn_id` acceptats i evitar executar-los dues vegades. Això cobreix el cas inevitable en sistemes distribuïts on el receptor pot haver acceptat una petició però la connexió cau abans que Pluribus rebi el 2xx.
