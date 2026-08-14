<p align="center">
  <img src="assets/pluribus-logo.jpg" alt="Pluribus Logo" width="400">
</p>

# Pluribus

*e pluribus unum — «de molts, un»*

Pluribus és un servei lleuger de memòria compartida per a múltiples agents d'IA. Combina FastAPI, SQLite/FTS5, embeddings via Ollama i un índex vectorial TurboVec.

## Requisits

- Ubuntu 24.04 o equivalent
- Python 3.12+
- SQLite amb FTS5
- Ollama accessible des del servidor per a embeddings i consolidació
- Tailscale o una xarxa privada recomanada

## Instal·lació

El procés de servei **no s'ha d'executar com root**. Crea primer un usuari de sistema dedicat i un directori d'estat writable separat del codi:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip sqlite3 git
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin pluribus 2>/dev/null || true
sudo mkdir -p /opt/pluribus
sudo install -d -o pluribus -g pluribus -m 0750 /opt/pluribus/data
```

Copia el codi com a root i mantén-lo no modificable per l'usuari del servei:

```bash
sudo cp -r pluribus/ scripts/ systemd/ requirements.txt README.md .env.example pluribus_worker.py /opt/pluribus/
sudo chown -R root:root /opt/pluribus/pluribus /opt/pluribus/scripts /opt/pluribus/systemd
sudo chown root:root /opt/pluribus/requirements.txt /opt/pluribus/pluribus_worker.py /opt/pluribus/.env.example
sudo chmod -R u=rwX,go=rX /opt/pluribus/pluribus /opt/pluribus/scripts /opt/pluribus/systemd
```

Crea el virtualenv com a part del desplegament, no des del procés web:

```bash
sudo python3 -m venv /opt/pluribus/venv
sudo /opt/pluribus/venv/bin/pip install -r /opt/pluribus/requirements.txt
sudo chown -R root:root /opt/pluribus/venv
```

La configuració mutable del dashboard viu dins del directori d'estat, no al costat del codi:

```bash
sudo install -o pluribus -g pluribus -m 0600 \
  /opt/pluribus/.env.example /opt/pluribus/data/pluribus.env
sudoedit /opt/pluribus/data/pluribus.env
```

La inicialització de l'esquema es fa automàticament durant la startup. Si el bootstrap o una migració fallen, el servei falla abans de servir trànsit.

### Migració d'una instal·lació existent

Si ja existia `/opt/pluribus/.env`, mou-ne el contingut al nou EnvironmentFile abans d'activar les unitats hardened:

```bash
sudo cp /opt/pluribus/.env /opt/pluribus/data/pluribus.env
sudo chown pluribus:pluribus /opt/pluribus/data/pluribus.env /opt/pluribus/data
sudo chmod 0600 /opt/pluribus/data/pluribus.env
sudo chmod 0750 /opt/pluribus/data
```

La BD i els seus fitxers WAL/SHM també han de pertànyer a `pluribus`:

```bash
sudo chown -R pluribus:pluribus /opt/pluribus/data
```

### Crear el primer agent

El bootstrap inicial d'agents es fa amb l'script local **com el mateix usuari del servei**, perquè no deixi la BD propietat de root:

```bash
sudo -u pluribus /opt/pluribus/venv/bin/python /opt/pluribus/scripts/create_agent.py
```

Després, l'endpoint `/v1/agents/register` només és accessible per agents administradors.

## Configuració

Totes les variables utilitzen el prefix `PLURIBUS_`:

| Variable | Default / exemple | Ús |
|---|---|---|
| `PLURIBUS_DB_PATH` | `/opt/pluribus/data/pluribus.db` | SQLite |
| `PLURIBUS_API_PORT` | `8790` | Port HTTP |
| `PLURIBUS_OLLAMA_BASE_URL` | `http://localhost:11434` | API Ollama |
| `PLURIBUS_OLLAMA_MODEL` | `nomic-embed-text-v2-moe:latest` | Embeddings |
| `PLURIBUS_EMBED_DIM` | `768` | Dimensions del vector |
| `PLURIBUS_CONSOLIDATION_MODEL` | `qwen3.5-2b:latest` | Resums del worker |
| `PLURIBUS_MAX_CHUNK_SIZE` | `500` | Mida de chunk |
| `PLURIBUS_CHUNK_OVERLAP` | `50` | Solapament |
| `PLURIBUS_RATE_LIMIT` | `100` | Requests per finestra |
| `PLURIBUS_RATE_LIMIT_WINDOW` | `60` | Finestra en segons |

En desplegament systemd, les variables es carreguen de `/opt/pluribus/data/pluribus.env`. `settings.ENV_PATH` apunta al mateix fitxer perquè les modificacions admin del dashboard siguin atòmiques dins d'un directori writable sense donar escriptura sobre el codi.

## Systemd

Instal·la el servei principal i el worker periòdic:

```bash
sudo cp /opt/pluribus/systemd/pluribus.service /etc/systemd/system/
sudo cp /opt/pluribus/systemd/pluribus-worker.service /etc/systemd/system/
sudo cp /opt/pluribus/systemd/pluribus-worker.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pluribus.service
sudo systemctl enable --now pluribus-worker.timer
```

Les unitats s'executen com `pluribus:pluribus`, sense capabilities, amb `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, proteccions de kernel/control groups i només `/opt/pluribus/data` writable. La xarxa queda limitada a `AF_UNIX`, `AF_INET` i `AF_INET6`.

Comprovacions:

```bash
systemctl status pluribus.service
systemctl status pluribus-worker.timer
journalctl -u pluribus.service -f
journalctl -u pluribus-worker.service -f
systemd-analyze security pluribus.service
```

El restart administratiu de l'API **no executa `systemctl`**. Després d'escriure i fsync el nou `pluribus.env`, programa un `SIGTERM` al mateix procés; `Restart=always` fa que systemd iniciï una instància nova que rellegeix l'EnvironmentFile. Un `systemctl stop pluribus` explícit continua aturant la unitat normalment.

## API

`/health` i el shell HTML `/dashboard` són públics. La resta requereix `X-API-Key`. Les APIs del dashboard (`/api/*`) són admin-only perquè actualment exposen dades agregades globals o configuració del procés.

| Mètode | Endpoint | Descripció |
|---|---|---|
| `POST` | `/v1/memory/write` | Crear un fet |
| `GET` | `/v1/memory/query` | Cerca FTS5 |
| `POST` | `/v1/memory/search/semantic` | Cerca semàntica |
| `GET` | `/v1/memory/search` | Cerca combinada |
| `GET` | `/v1/memory/ls` | Llista per scope/categoria |
| `GET` | `/v1/memory` | Llista paginada |
| `PUT` | `/v1/memory/{fact_id}` | Actualitzar |
| `DELETE` | `/v1/memory/{fact_id}` | Soft-delete |
| `POST` | `/v1/memory/query-save` | Guardar un insight i relacions origen |
| `POST` | `/v1/memory/lint` | Report global de salut; admin-only |
| `POST` | `/v1/memory/expire` | Forçar expiració TTL; admin-only |
| `GET` | `/v1/memory/audit` | Auditoria; admin-only |
| `POST` | `/mcp/` | MCP JSON-RPC |

Els `allowed_scopes` s'apliquen abans d'entrar als handlers REST i MCP. Els endpoints globals que encara no poden filtrar per scope queden restringits a admin.

### Exemple

```bash
KEY="<api-key>"
curl -X POST http://localhost:8790/v1/memory/write \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"content":"El servidor principal és a Frankfurt","scope":"shared","category":"events"}'

curl "http://localhost:8790/v1/memory/query?q=servidor&scope=shared" \
  -H "X-API-Key: $KEY"
```

## Embeddings i cerca

- Els embeddings es generen amb Ollama `/api/embed`.
- Model per defecte: `nomic-embed-text-v2-moe:latest`.
- Dimensió per defecte: 768.
- TurboVec proporciona l'índex vectorial ràpid.
- FTS5 continua disponible sense embeddings i actua com a fallback funcional.
- Els chunks es creen una sola vegada; el background actualitza el BLOB placeholder quan l'embedding està llest.

## Worker

`pluribus_worker.py` és un wrapper de `pluribus.worker`.

Cada ronda:

1. inicialitza/migra la DB;
2. consolida facts encara no processats;
3. crea relacions semàntiques noves;
4. elimina chunks orfes i cache antiga;
5. sincronitza Notion si està configurat.

La consolidació no usa `MAX(created_at)` com a checkpoint. La taula `consolidated_facts` guarda el mapping exacte `fact_id → consolidated_id`, de manera que un backlog o un error puntual no pot saltar fets antics.

El timer inclòs executa el worker cada 15 minuts. Un error fatal o errors de consolidació produeixen codi de sortida no-zero.

## Seguretat

- API keys hasheades amb bcrypt.
- Agents amb `is_active=0` no poden autenticar-se.
- Permisos `read/write/delete/admin`.
- `allowed_scopes` aplicat a REST i MCP.
- Registre i eliminació d'agents: admin-only.
- Categories persistents `system`, `config` i `entities`: eliminació admin-only.
- Configuració, restart i dades globals del dashboard: admin-only.
- JSON de permisos corrupte falla tancat, sense concedir accessos per defecte.
- Servei i worker sense privilegis root i amb sandboxing systemd.
- El directori de codi és read-only per al procés; només el directori d'estat és writable.

## Arquitectura resumida

```text
Agents
  │ X-API-Key
  ▼
FastAPI ── authorization guards ── SQLite + FTS5
  │                                  │
  ├── Ollama embeddings              └── facts/chunks/audit/graph
  └── TurboVec

systemd timer ──> pluribus.worker ──> consolidació + relacions + manteniment
```

## Llicència

Ús intern per a l'ecosistema Hermes. El repositori no declara actualment una llicència pública de redistribució.
