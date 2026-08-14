# Xerrameca Console + receptor Runner de referència

## Consola

Obre:

```text
/dashboard?view=xerrameca
```

La pàgina HTML és pública igual que el dashboard principal, però **no carrega cap dada** fins que s'introdueix una API key amb permisos admin. La clau es conserva només a `sessionStorage` del navegador i s'envia com `X-API-Key` a les API de Pluribus.

Des de la consola es pot:

- activar/desactivar Xerrameca;
- activar/desactivar el Runner;
- modificar `poll_interval_seconds` i `max_dispatches_per_tick`;
- executar un tick manual;
- crear i iniciar una conversa;
- veure estat, ronda, torn actual i lease;
- pausar, reprendre i cancel·lar;
- modificar `max_rounds` i timeout de torn;
- assignar o saltar el torn actual;
- consultar l'historial de missatges;
- configurar un callback Runner per agent;
- veure `last_status`, errors consecutius, últim intent/èxit i circuit breaker;
- rotar el secret Runner;
- eliminar una configuració Runner.

Els secrets Runner només es mostren en crear una configuració nova o en rotar el secret.

## Receptor de referència

Pluribus inclou un receptor ASGI genèric:

```text
pluribus.xerrameca.receiver:app
```

I un launcher:

```bash
python scripts/xerrameca_receiver.py
```

### Variables obligatòries

```bash
export XERRAMECA_RUNNER_SECRET='secret-copiat-del-dashboard'
export PLURIBUS_API_KEY='plb_clau_de_l_agent'
```

### Variables opcionals

```bash
export PLURIBUS_URL='http://127.0.0.1:8000'
export XERRAMECA_RECEIVER_HOST='0.0.0.0'
export XERRAMECA_RECEIVER_PORT='8090'
export XERRAMECA_RECEIVER_DB='/var/lib/my-agent/xerrameca_receiver.db'
export XERRAMECA_REPLY_TIMEOUT='30'
export XERRAMECA_HANDLER='my_agent.xerrameca:handle_turn'
```

El callback a configurar al dashboard seria, per exemple:

```text
https://agent.example:8090/xerrameca/turn
```

## Seguretat del receptor

Abans de processar un torn, el receptor:

1. limita el payload a 1 MiB;
2. verifica `X-Pluribus-Signature` amb HMAC-SHA256 sobre els bytes exactes del body;
3. valida l'event `xerrameca.turn.claimed`;
4. valida `turn.id` i `turn.lease_token`;
5. exigeix que `X-Pluribus-Idempotency-Key` coincideixi amb `turn.id`;
6. registra la delivery en una SQLite local abans d'acceptar-la.

Una delivery repetida no torna a executar el handler.

## Contracte del handler

El handler és una funció Python síncrona o asíncrona que rep l'envelope complet del Runner.

Exemple:

```python
async def handle_turn(payload):
    message = payload["input_message"]["content"]
    round_no = payload["turn"]["round"]

    # Aquí l'agent pot consultar el seu model, eines o runtime.
    answer = await my_agent.process(message)

    return {
        "content": answer,
        "result": "continue",
        "metadata": {"round": round_no},
    }
```

Valors per `result`:

```text
continue
complete
blocked
needs_human
error
```

En política `supervisor`, el handler del supervisor també pot retornar:

```python
{
    "content": "Passa-ho al worker",
    "result": "continue",
    "next_agent_id": "agent-worker-id"
}
```

El receptor valida el resultat i crida:

```text
POST /v1/xerrameca/turns/{turn_id}/reply
X-API-Key: <API key de l'agent>
```

amb la `lease_token` rebuda del Runner.

## ACK i recuperació

El receptor respon `202 Accepted` després de validar i persistir la delivery. El handler s'executa en background.

Això és important perquè el Runner no mantingui una connexió HTTP oberta mentre el model pensa.

Si el procés cau després de l'ACK:

- la lease queda activa fins al seu venciment;
- Xerrameca torna a fer el torn reclamable quan caduca;
- la SQLite local impedeix reexecutar accidentalment una delivery que ja s'havia processat amb la mateixa clau.

Si es necessita política de retry més sofisticada del costat receptor, es pot substituir el `BackgroundTasks` per una cua persistent mantenint el mateix contracte HMAC/idempotència.

## Handler per defecte

Si no es defineix `XERRAMECA_HANDLER`, el receptor és deliberadament conservador: respon a Pluribus amb `needs_human` en lloc d'inventar o executar cap acció.

## Flux complet

```text
Agent A -> Xerrameca -> torn Agent B
                         |
                         v
                    Runner claim
                         |
                 POST HMAC signat
                         |
                         v
              receptor Agent B (202)
                         |
                    handler/model
                         |
                         v
              POST xerrameca_reply
                         |
                         v
               següent ronda/agent
```
