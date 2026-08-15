# Xerrameca Dialogue Protocol v1

## Objectiu

Xerrameca coordina una conversa estructurada entre dos agents. Pluribus controla estat, ordre, leases, límits i monitorització; els agents continuen sent responsables del contingut i de les accions que executen.

## Inici

Una conversa nova creada per l'API Xerrameca queda marcada `protocol_version=dialogue-v1`. Les converses preexistents continuen `legacy-v0` i no canvien de semàntica a mig vol.

En `start`, Xerrameca genera un missatge `control` de kickoff amb:

- objectiu;
- participants;
- política de torns;
- màxim de rondes;
- timeout;
- regles de finalització.

El primer torn s'assigna a `first_agent_id`.

## Rondes

`xerrameca_turns.round_no` es conserva com a seqüència interna compatible amb versions anteriors.

Dialogue v1 afegeix:

- `dialogue_round`: ronda humana;
- `turn_in_round`: 1 o 2;
- `phase`: `dialogue` o `completion_confirmation`.

En `alternating`:

```text
Ronda 1: Agent A -> Agent B
Ronda 2: Agent A -> Agent B
...
```

Per tant, quan A respon el primer torn amb `continue`, la conversa continua a ronda 1 amb B. Només després de B es passa a ronda 2.

## Context d'un torn

`claim` retorna, a més de la lease:

- `round` (ronda humana);
- `turn_sequence`;
- `turn_in_round`;
- `phase`;
- `dialogue_context`.

`dialogue_context` conté objectiu, política, estat, ronda, límit, proposta de finalització i un historial recent acotat. El Runner reenvia aquest context al receptor de l'agent.

## Resultats

- `continue`: la conversa segueix.
- `complete`: proposta o confirmació de finalització.
- `blocked`: bloqueig funcional.
- `needs_human`: cal decisió humana.
- `error`: error de l'agent.

## Finalització en alternating

Un únic agent no pot finalitzar unilateralment.

```text
A -> complete
B -> complete
=> completed
```

Si B respon `continue`, la proposta queda rebutjada i la conversa segueix.

## Finalització en supervisor

El supervisor pot finalitzar amb `complete`. Si el worker envia `complete`, queda com una proposta que el supervisor ha de confirmar.

## Max rounds

El límit s'aplica a rondes humanes completes, no al número brut de torns. Una confirmació de finalització pendent sempre es pot resoldre; si es rebutja quan ja no queden rondes, la conversa passa a `blocked/max_rounds`.

## Leases

La lease continua sent el mecanisme exclusiu d'exclusió. Recuperar una lease caducada no crea una ronda nova. El mateix `turn_id` pot obtenir una nova `lease_token` després de timeout.

# Xerrameca Monitor

El Monitor s'inicialitza amb Xerrameca i està activat per observar, però les auto-accions estan desactivades per defecte.

## Detecta

- conversa activa sense torn;
- torn `ready` encallat;
- torn `claimed` encallat;
- lease caducada;
- proximitat a `max_rounds`;
- `blocked`, `needs_human` i `error`;
- proposta `complete` pendent;
- patró fort de loop A/B repetit.

Les alertes són persistents amb severitat `info`, `warning` o `critical` i estat `open`, `acknowledged` o `resolved`.

## Auto-accions

Opcionalment es pot activar:

- `auto_pause_stalled`;
- `auto_pause_loop`.

L'única auto-acció és `pause` + revocació de lease. El Monitor mai:

- respon un torn;
- inventa contingut;
- executa eines;
- finalitza una conversa.

## API

```text
GET   /v1/xerrameca/monitor/system
PATCH /v1/xerrameca/monitor/system
POST  /v1/xerrameca/monitor/tick
GET   /v1/xerrameca/monitor/snapshot
GET   /v1/xerrameca/monitor/alerts
POST  /v1/xerrameca/monitor/alerts/{id}/acknowledge
POST  /v1/xerrameca/monitor/alerts/{id}/resolve
```

Tots els endpoints del Monitor són admin-only.

## UI

```text
/dashboard?view=xerrameca-monitor
```

La vista mostra health de cada conversa, ronda/torn, phase, lease, idle time, proposta de finalització i alertes persistents. La clau admin es conserva només a `sessionStorage`.
