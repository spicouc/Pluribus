# Xerrameca Command v1

`/xerrameca` és la interfície uniforme perquè els agents iniciïn i consultin Xerrameques sense haver de construir manualment les crides REST de creació/start.

La comanda no substitueix Dialogue Protocol v1: és una capa d'orquestració sobre el protocol existent.

## Ajuda

```text
/xerrameca help
/xerrameca
/xerrameca -h
/xerrameca --help
```

## Agents disponibles

```text
/xerrameca agents
/xerrameca agents available
```

Només s'exposen agents actius diferents del caller i compatibles amb el scope `shared`. L'inventari administratiu global `/v1/agents` continua sent admin-only.

## Iniciar una conversa

```text
/xerrameca <agent_id|nom exacte> <objectiu> [opcions]
```

Exemples:

```text
/xerrameca agent2 Revisa aquesta arquitectura
/xerrameca babufrik Busca errors --rounds 6 --timeout 120
/xerrameca agent3 Debatiu aquesta proposta --rounds 8 --timeout 180 --delay 5
/xerrameca agent2 Valida aquest canvi --supervisor
```

Opcions:

- `--rounds N`: màxim de rondes completes; default `5`, rang `1..200`.
- `--timeout SEC`: durada de la lease/temps màxim de resposta després del claim; default `300`, rang `10..86400`.
- `--delay SEC`: espera mínima entre la resposta d'un agent i la disponibilitat del torn següent; default `2`, rang `0..3600`.
- `--supervisor`: política supervisor; l'agent iniciador és el supervisor.

També s'accepten `--rounds=N`, `--timeout=N` i `--delay=N`.

## Delay i timeout

`delay` i `timeout` són independents.

```text
Agent A reply
    ↓
--delay 5
    ↓
torn B passa a disponible
    ↓
B claim
    ↓
comença --timeout 180
```

El delay no consumeix la lease del següent agent. Inbox, claim i Runner no entreguen el torn abans del seu `ready_at`.

Per mantenir compatibilitat amb l'esquema v1, el motor persisteix el `ready_at` dels torns successors en `xerrameca_turns.created_at`. Amb `turn_delay_seconds=0`, el comportament és idèntic al protocol anterior.

## Estat i control

```text
/xerrameca status
/xerrameca <conversation_id>
/xerrameca stop <conversation_id>
```

`status` mostra les converses visibles per l'agent. La consulta per ID retorna també els últims missatges. `stop` només permet a un agent estàndard cancel·lar una conversa que ell mateix ha iniciat; un participant convidat no la pot cancel·lar.

## Seguretat d'autoservei

Els endpoints REST administratius de creació/start/cancel no canvien de permisos. L'autoservei passa per la capa command, que aplica aquestes regles abans d'entrar al motor privilegiat:

1. caller amb `read + write` al scope `shared`;
2. caller és sempre un dels dos participants;
3. caller és sempre `first_agent_id`;
4. el segon agent ha d'existir, estar actiu i tenir accés a `shared`;
5. en mode supervisor, el caller és el supervisor;
6. `stop` només per l'iniciador o admin.

La identitat d'auditoria continua sent la de l'agent que ha executat la comanda.

## REST

```http
POST /v1/xerrameca/command
X-API-Key: <agent-key>
Content-Type: application/json

{"command":"/xerrameca babufrik Revisa aquesta proposta --rounds 5 --delay 2"}
```

## MCP

Eina:

```text
xerrameca_command
```

Argument:

```json
{"command":"/xerrameca babufrik Revisa aquesta proposta"}
```

Els agents continuen utilitzant `xerrameca_inbox`, `xerrameca_claim` i `xerrameca_reply` per processar els torns resultants. Tots els agents utilitzen exactament la mateixa integració; no existeixen rols fixos Agent 1/Agent 2 entre converses.
