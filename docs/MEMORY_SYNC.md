# Memory Sync v1

Memory Sync és el camí lleuger perquè un agent mantingui la seva memòria local al dia sense repetir cerques completes.

## Cadència recomanada

Per defecte, cada agent rep aquesta política:

- `active_poll_seconds = 5`: després de trobar canvis o mentre hi ha més pàgines.
- `idle_poll_seconds = 30`: quan no hi ha cap canvi pendent.
- `write_debounce_seconds = 2`: un client pot agrupar records produïts gairebé simultàniament durant 2 segons.
- `max_write_delay_seconds = 5`: encara que el client agrupi, un record que s'ha decidit persistir no s'hauria de retenir més de 5 segons.

Aquests valors són configurables per agent. La persistència de `POST /v1/memory/write` continua sent immediata: Pluribus no reté ni bufferitza una escriptura rebuda.

## Flux

```text
agent inicia cursor=0
        │
        ▼
GET /v1/memory/sync?cursor=0
        │
        ├─ canvis → aplica upserts/tombstones → torna en 5 s
        │
        └─ buit   → conserva cursor          → torna en 30 s
```

Cada resposta conté `next_cursor`. El client l'ha de persistir i enviar a la següent consulta. El cursor avança també sobre canvis de scopes no autoritzats, però aquests canvis mai es retornen al client; això evita que un agent es quedi reescanejant activitat privada d'altres scopes.

## Canvis de scope i eliminacions

El change log guarda `upsert` i `delete`.

Quan un fact passa de `shared` a `private`, un agent que només veu `shared` rep un `delete` per retirar la còpia antiga, mentre que un agent autoritzat per `private` rep l'`upsert` nou. Les eliminacions soft-delete també produeixen tombstones.

## API

```text
GET /v1/memory/sync?cursor=<seq>&limit=100
GET /v1/memory/sync/policy
PUT /v1/memory/sync/policy/{agent_id}   # admin
```

L'endpoint de política admin accepta:

```json
{
  "active_poll_seconds": 5,
  "idle_poll_seconds": 30,
  "write_debounce_seconds": 2,
  "max_write_delay_seconds": 5
}
```

`idle_poll_seconds` no pot ser menor que `active_poll_seconds`, i `max_write_delay_seconds` no pot ser menor que `write_debounce_seconds`.

## MCP

Els clients MCP poden usar `memory_sync` amb `cursor` i `limit`. La resposta és equivalent a REST i inclou la política i `recommended_poll_seconds`.

Directives v1 també s'exposa per MCP amb `directive_inbox`, `directive_create`, `directive_get`, `directive_claim`, `directive_complete`, `directive_fail`, `directive_reject`, `directive_list_grants` i `directive_set_grant`. Aquests tools reutilitzen els mateixos handlers i comprovacions del control plane REST; Pluribus continua sense executar shell o codi local.

## Worker pesat

El worker de consolidació/relacions s'executa cada 5 minuts amb `OnUnitInactiveSec=5min`. Aquesta freqüència és independent de Memory Sync: un fact nou és visible per FTS i sync immediatament després del commit, i els embeddings es generen en background.
