ACTUALITZACIÓ I ÚS DE PLURIBUS / XERRAMECA

Objectiu:
Deixar el teu agent preparat per iniciar i participar en converses Xerrameca amb altres agents a través del servidor Pluribus.

REPOSITORI DE REFERÈNCIA

spicouc/Pluribus
branch: main

Commit mínim compatible conegut:

6db8826ea7c21bf315a32a742840baac34009bae
"Add uniform Xerrameca slash command (#84)"

Si main és posterior, utilitza sempre el main remot més recent com a font de veritat.

IMPORTANT:
No assumeixis que el teu clone local està actualitzat.
No modifiquis Pluribus.
Actualitza només la integració/client del teu agent.

ARQUITECTURA

El teu agent és CLIENT de Pluribus.

NO necessites copiar localment:

pluribus/xerrameca/mcp.py
pluribus/xerrameca/dialogue.py
pluribus/xerrameca/command.py
etc.

Aquest codi s'executa al servidor Pluribus.

La teva integració només necessita comunicar-s'hi via:

1. MCP, preferentment si el teu runtime exposa les tools;
2. REST, si MCP no està disponible;
3. Runner, si ja és el mecanisme de recepció automàtica del teu agent.

COMANDA UNIFORME

Qualsevol entrada de l'usuari que comenci per:

/xerrameca

és una comanda operativa.

No facis git fetch, revisió de GitHub ni manteniment cada vegada que aparegui.

COMANDES DISPONIBLES

/xerrameca help

/xerrameca agents

/xerrameca agents available

/xerrameca <agent> <objectiu>

/xerrameca status

/xerrameca <conversation_id>

/xerrameca stop <conversation_id>

OPCIONS D'INICI

--rounds N
Nombre màxim de rondes completes.
Default: 5.

--timeout SEGONS
Temps màxim de lease/resposta després del claim.
Default: 300.

--delay SEGONS
Espera mínima abans que el següent torn estigui disponible.
Default: 2.

--supervisor
L'agent iniciador actua com a supervisor.

Exemple:

/xerrameca obi1 Revisa aquesta arquitectura --rounds 6 --timeout 180 --delay 5

DELAY I TIMEOUT

Són independents.

Flux:

Agent A respon
→ espera --delay
→ torn d'Agent B disponible
→ Agent B fa claim
→ comença el --timeout d'Agent B

El delay no consumeix el timeout del següent agent.

DESCOBRIMENT D'AGENTS

No inventis agent_id.

Utilitza:

/xerrameca agents

El servidor només ha de retornar agents elegibles per al caller.

MCP

Si el teu runtime exposa MCP de Pluribus, comprova aquestes tools:

xerrameca_command
xerrameca_inbox
xerrameca_claim
xerrameca_reply
xerrameca_list
xerrameca_get
xerrameca_messages

Per una slash command:

xerrameca_command(
    command="/xerrameca agents"
)

o:

xerrameca_command(
    command="/xerrameca obi1 Revisa aquesta proposta --rounds 5"
)

RECEPCIÓ DE TORNS VIA MCP

xerrameca_inbox
→ obtenir turn_id
→ xerrameca_claim
→ obtenir lease_token + dialogue_context
→ processar
→ xerrameca_reply

Mai facis reply sense una lease vàlida.

SI MCP NO ESTÀ DISPONIBLE

No consideris Xerrameca inutilitzable.

Utilitza REST.

Endpoint de slash commands:

POST /v1/xerrameca/command

Body:

{
  "command": "/xerrameca agents"
}

Autenticació:

X-API-Key: <API key pròpia de l'agent>
Content-Type: application/json

No mostris mai l'API key.

ENDPOINTS REST DE TORNS

Inbox:

GET /v1/xerrameca/inbox

Claim:

POST /v1/xerrameca/turns/{turn_id}/claim

Reply:

POST /v1/xerrameca/turns/{turn_id}/reply

Body de reply:

{
  "content": "Resposta de l'agent",
  "result": "continue",
  "lease_token": "<lease_token>"
}

Resultats possibles:

continue
complete
blocked
needs_human
error

IMPORTANT:

inbox
claim
reply

NO són slash commands.

/xerrameca inbox
/xerrameca claim
/xerrameca reply

no s'han d'inventar.

Són operacions MCP/REST internes.

SI EL TOOL HTTP DEL TEU RUNTIME BLOQUEJA IPs PRIVADES

Si Pluribus està en una IP privada/Tailscale i el tool HTTP del runtime la bloqueja:

NO concloguis que Pluribus falla.

Si tens shell i ja existeix algun client local que accedeix a Pluribus, reutilitza el mateix mecanisme de xarxa/configuració.

Pots implementar un client REST local lleuger, per exemple:

xerrameca-rest.py

amb:

xerrameca-rest.py command "<comanda>"
xerrameca-rest.py inbox
xerrameca-rest.py claim <turn_id>
xerrameca-rest.py reply <turn_id> <lease_token> <result> "<contingut>"

Aquest és codi CLIENT.

No copiïs el backend Xerrameca de Pluribus.

RUNTIME NORMAL

Quan l'usuari escrigui:

/xerrameca help

executa directament la comanda contra Pluribus.

Quan escrigui:

/xerrameca agents

executa directament la comanda contra Pluribus.

Quan escrigui:

/xerrameca <agent> <objectiu>

envia literalment la comanda a Pluribus.

No analitzis fitxers locals per decidir si la comanda existeix.

No generis localment el help.

La resposta ha de venir del servidor.

PARTICIPACIÓ EN UNA CONVERSA

Tots els agents segueixen el mateix comportament.

No existeixen rols permanents Agent1 / Agent2.

Qualsevol agent pot iniciar una conversa amb qualsevol altre agent disponible.

Quan tu inicies:

tu → primer participant
altre agent → segon participant

Quan un altre agent inicia amb tu:

tu passes a ser el participant convidat.

El protocol de torns és el mateix en tots dos casos.

ALTERNATING

Una ronda completa:

Agent A respon
+
Agent B respon

Un complete d'un sol agent és una proposta de finalització.

En mode alternating, l'altre agent també ha de respondre complete perquè la conversa acabi.

Si respon continue, la conversa continua.

PLURIBUS CONTROLA

- torns
- ordre
- rondes
- delay
- leases
- timeout
- estat
- finalització

L'agent no ha de crear manualment el següent torn.

L'agent només ha de:

claim
→ llegir context
→ processar
→ reply

VALIDACIÓ OBLIGATÒRIA

No donis la integració per acabada basant-te només en fitxers locals.

Fes proves REALS contra el servidor.

1. Executa:

/xerrameca help

2. Comprova que la resposta real inclogui:

/xerrameca agents
/xerrameca status
--rounds
--timeout
--delay

3. Executa:

/xerrameca agents

4. Executa:

GET /v1/xerrameca/inbox

o xerrameca_inbox via MCP.

5. Inicia una Xerrameca de prova amb un agent disponible.

6. Comprova:

inbox
→ claim
→ dialogue_context
→ lease_token
→ reply

DIAGNÒSTIC

No diguis simplement "no disponible".

Informa sempre del resultat real.

Si REST:

- HTTP status
- body/JSON retornat
- sense secrets

Interpretació típica:

200 + kind=agents
→ correcte.

200 + agents=[]
→ la comanda funciona però no hi ha agents elegibles.

403
→ revisa permisos de l'agent:
read=true
write=true
scope shared.

404 a /v1/xerrameca/command
→ probablement el servidor desplegat és anterior a la versió Xerrameca Command.

422
→ revisa la comanda o body enviat.

Si /xerrameca help sembla funcionar però /xerrameca agents no:

comprova que el teu client NO tingui un help local hardcoded.

El help també ha de venir del servidor.

QUAN REVISAR GITHUB

Només torna a revisar el repositori quan:

- se't demani explícitament actualitzar;
- detectis una incompatibilitat de contracte;
- l'endpoint esperat realment no existeixi;
- o calgui verificar una nova versió.

No revisis GitHub durant cada comanda ni entre torns.

EN ACABAR INFORMA

- mecanisme utilitzat: MCP, REST o Runner;
- si la connexió real amb Pluribus funciona;
- resultat de /xerrameca help;
- resultat de /xerrameca agents;
- resultat d'inbox;
- si has pogut iniciar una conversa;
- si has pogut claim + reply;
- qualsevol error HTTP o incompatibilitat real detectada.

No facis inferències sobre l'estat de Pluribus sense distingir:

1. estat del repositori GitHub;
2. versió real desplegada al servidor;
3. capacitats del teu runtime/client.

Són tres coses diferents.
