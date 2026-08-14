<p align="center">
  <img src="assets/pluribus-logo.jpg" alt="Pluribus Logo" width="400">
</p>

# Pluribus

*e pluribus unum — «de molts, un»*

Pluribus és un servei lleuger de memòria compartida per a múltiples agents d'IA. Combina FastAPI, SQLite/FTS5, embeddings via Ollama i un índex vectorial TurboVec.

## Requisits

- Ubuntu 24.04 o equivalent
- Python 3.12.13 per reproduir exactament el lock verificat
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
sudo cp -r pluribus/ scripts/ systemd/ requirements.txt requirements.lock README.md .env.example pluribus_worker.py /opt/pluribus/
sudo chown -R root:root /opt/pluribus/pluribus /opt/pluribus/scripts /opt/pluribus/systemd
sudo chown root:root /opt/pluribus/requirements.txt /opt/pluribus/requirements.lock /opt/pluribus/pluribus_worker.py /opt/pluribus/.env.example
sudo chmod -R u=rwX,go=rX /opt/pluribus/pluribus /opt/pluribus/scripts /opt/pluribus/systemd
```

`requirements.txt` descriu les dependències directes compatibles. **Producció i CI instal·len `requirements.lock`**, que fixa també les transitives a la combinació validada per la suite.

```bash
sudo python3 -m venv /opt/pluribus/venv
sudo /opt/pluribus/venv/bin/pip install --disable-pip-version-check -r /opt/pluribus/requirements.lock
sudo /opt/pluribus/venv/bin/pip check
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
| `PLURIBUS_BACKUP_DIR` | `/opt/pluribus/data/backups` | Destí dels backups verificats |
| `PLURIBUS_BACKUP_RETENTION_DAYS` | `14` | Retenció de snapshots |

En desplegament systemd, les variables es carreguen de `/opt/pluribus/data/pluribus.env`. `settings.ENV_PATH` apunta al mateix fitxer perquè les modificacions admin del dashboard siguin atòmiques dins d'un directori writable sense donar escriptura sobre el codi.

## Systemd

Instal·la el servei principal, el worker periòdic i el backup verificat:

```bash
sudo cp /opt/pluribus/systemd/pluribus.service /etc/systemd/system/
sudo cp /opt/pluribus/systemd/pluribus-worker.service /etc/systemd/system/
sudo cp /opt/pluribus/systemd/pluribus-worker.timer /etc/systemd/system/
sudo cp /opt/pluribus/systemd/pluribus-backup.service /etc/systemd/system/
sudo cp /opt/pluribus/systemd/pluribus-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pluribus.service
sudo systemctl enable --now pluribus-worker.timer
sudo systemctl enable --now pluribus-backup.timer
```

Les unitats s'executen com `pluribus:pluribus`, sense capabilities, amb `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, proteccions de kernel/control groups i només `/opt/pluribus/data` writable. El servei de backup és local-only (`AF_UNIX`); API i worker poden usar `AF_UNIX`, `AF_INET` i `AF_INET6`.

Comprovacions:

```bash
systemctl status pluribus.service
systemctl status pluribus-worker.timer
systemctl status pluribus-backup.timer
systemctl list-timers 'pluribus-*'
journalctl -u pluribus.service -f
journalctl -u pluribus-worker.service -f
journalctl -u pluribus-backup.service -f
systemd-analyze security pluribus.service
systemd-analyze security pluribus-backup.service
```

El restart administratiu de l'API **no executa `systemctl`**. Després d'escriure i fsync el nou `pluribus.env`, programa un `SIGTERM` al mateix procés; `Restart=always` fa que systemd iniciï una instància nova que rellegeix l'EnvironmentFile. Un `systemctl stop pluribus` explícit continua aturant la unitat normalment.

## Backups i recuperació

`pluribus-backup.timer` executa una còpia diària aproximadament a les 03:30, amb un retard aleatori de fins a 30 minuts. És `Persistent=true`, de manera que una execució perduda durant una aturada es recupera després de tornar a arrencar.

Cada backup:

1. usa `sqlite3.Connection.backup()` i obté una snapshot coherent encara que la BD estigui en WAL;
2. executa `PRAGMA quick_check` sobre la snapshot;
3. comprimeix a gzip amb permisos `0600`;
4. **descomprimeix l'artefacte temporal i torna a executar `quick_check`**;
5. només llavors publica el `.db.gz` amb `os.replace()` atòmic;
6. elimina backups més antics que la retenció configurada.

Execució manual:

```bash
sudo -u pluribus /opt/pluribus/venv/bin/python -m pluribus.backup
```

Restauració controlada d'un backup (amb el servei aturat):

```bash
sudo systemctl stop pluribus.service pluribus-worker.timer
sudo -u pluribus gzip -cd /opt/pluribus/data/backups/pluribus_YYYYMMDD_HHMMSS_xxxxxx.db.gz \
  > /opt/pluribus/data/pluribus.restore.db
sudo -u pluribus sqlite3 /opt/pluribus/data/pluribus.restore.db 'PRAGMA quick_check;'
# Després de verificar "ok", substitueix la BD segons el teu procediment operatiu.
```

No cal restaurar cap fitxer TurboVec: l'índex és derivat de SQLite i es reconstrueix automàticament.

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
3. crea relacions semàntiques noves **només dins del mateix scope**;
4. elimina chunks orfes i cache antiga;
5. sincronitza Notion si està configurat.

La consolidació no usa `MAX(created_at)` com a checkpoint. La taula `consolidated_facts` guarda el mapping exacte `fact_id → consolidated_id`, de manera que un backlog o un error puntual no pot saltar fets antics.

El timer inclòs executa el worker cada 15 minuts. Un error fatal o errors de consolidació produeixen codi de sortida no-zero.

## Seguretat

- API keys hasheades amb bcrypt.
- Agents amb `is_active=0` no poden autenticar-se.
- Permisos `read/write/delete/admin`.
- `allowed_scopes` aplicat a REST, MCP i generació automàtica de relacions.
- Inventari global d'agents: admin-only; un agent estàndard només pot consultar-se a si mateix.
- Registre i eliminació d'agents: admin-only.
- Categories persistents `system`, `config` i `entities`: eliminació admin-only.
- Configuració, restart i dades globals del dashboard: admin-only.
- JSON de permisos corrupte falla tancat, sense concedir accessos per defecte.
- Webhooks signats i amb destinació/IP validada i fixada per entrega.
- Servei, worker i backup sense privilegis root i amb sandboxing systemd.
- El directori de codi és read-only per als processos; només el directori d'estat és writable.
- CI amb `GITHUB_TOKEN` read-only, Actions pinnejades per SHA i dependències instal·lades des de `requirements.lock`.

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
systemd timer ──> pluribus.backup ──> snapshot + restore-check + retention
```

## Dependències

`requirements.txt` és la declaració de dependències directes. `requirements.lock` és el conjunt exacte validat per CI per a Python 3.12.13/Ubuntu 24.04. Dependabot obre PRs setmanals per canvis de pip i GitHub Actions; qualsevol actualització ha de passar la mateixa suite abans d'entrar a `main`.

## Llicència

Ús intern per a l'ecosistema Hermes. El repositori no declara actualment una llicència pública de redistribució.
