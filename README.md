# 🧠 Pluribus — Servei de Memòria Compartida Multi-Agent

**Pluribus** és un servei lleuger de memòria compartida central per a múltiples agents d'IA. Dissenyat per a l'ecosistema Hermes, permet que diversos agents (Hetzner, RPi local, Picoclaw) emmagatzemin i consultin informació compartida de manera eficient.

## 📋 Requisits del Sistema

- **Ubuntu 24.04** minimal (o superior)
- **Python 3.12**
- **1 GB RAM** màxim (servei < 400 MB en repòs)
- **5 GB disc**
- Accés via **Tailscale** (no exposat públicament)

## 🔧 Instal·lació Pas a Pas

### 1. Requisits previs del sistema

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip sqlite3 git
```

### 2. Crear l'estructura de directoris

```bash
sudo mkdir -p /opt/brain/data /opt/brain/cache
```

Copia tots els fitxers del projecte a `/opt/brain/`:

```bash
# Si tens els fitxers localment
cp -r brain/ scripts/ systemd/ requirements.txt README.md /opt/brain/
```

### 3. Crear l'entorn virtual i instal·lar dependències

```bash
python3 -m venv /opt/brain/venv
/opt/brain/venv/bin/pip install -r /opt/brain/requirements.txt
```

> ⚠ **Nota**: `fastembed` utilitza ONNX Runtime, **no** PyTorch. Això redueix significativament l'ús de memòria.
> La primera vegada que es generi un embedding, el model `intfloat/multilingual-e5-small` es descarregarà automàticament (aproximadament 500 MB).

### 4. Inicialitzar la base de dades

```bash
mkdir -p /opt/brain/data
sqlite3 /opt/brain/data/brain.db < /opt/brain/scripts/init_db.sql
```

Això crearà totes les taules, índexs i triggers necessaris, incloent:
- `agents` — Agents amb claus API
- `facts` — Fets emmagatzemats
- `facts_fts` — Cerca per text complet (FTS5)
- `chunks` — Fragments amb embeddings vectorials
- `embedding_cache` — Cache d'embeddings
- `audit_log` — Registre d'auditoria

### 5. Crear un agent

```bash
/opt/brain/venv/bin/python /opt/brain/scripts/create_agent.py
```

Segueix les instruccions interactives:
1. Introdueix un nom per a l'agent
2. Especifica els àmbits permesos (ex: `shared`, `shared,local`)
3. Configura els permisos (o prem Enter per als valors per defecte)

**Important**: La clau API es mostra **una sola vegada**. Guarda-la immediatament.

### 6. Configurar systemd

```bash
sudo cp /opt/brain/systemd/brain.service /etc/systemd/system/brain.service
sudo systemctl daemon-reload
sudo systemctl enable --now brain.service
```

### 7. Verificar la instal·lació

```bash
# Comprovar que el servei està actiu
sudo systemctl status brain.service

# Test ràpid
curl -s http://localhost:8790/health | python3 -m json.tool

# Exemple: escriure un fet
KEY="<la_teva_clau_api>"
curl -X POST http://localhost:8790/v1/memory/write \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"content": "Hola món des del Pluribus!", "scope": "shared"}'
```

## 📐 Arquitectura

```
┌─────────────────────────────────────────────────────┐
│                    Pluribus Server                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  FastAPI  │  │  SQLite  │  │  fastembed (lazy)│  │
│  │  (Uvicorn)│  │ (aiosql) │  │  ONNX Runtime    │  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
│  Port 8790       /opt/brain/    intfloat/e5-small   │
│                  data/brain.db                       │
└─────────────────────────────────────────────────────┘
         ▲                ▲                ▲
         │                │                │
    ┌────┴────┐    ┌──────┴──────┐    ┌───┴────┐
    │ Hermes  │    │  Hermes     │    │Picoclaw│
    │ Hetzner │    │  RPi Local  │    │ Agent  │
    └─────────┘    └─────────────┘    └────────┘
```

## 🔌 Endpoints de l'API

Tots els endpoints (excepte `/health`, `/dashboard` i `/api/stats`) requereixen la capçalera `X-API-Key`.

| Mètode | Endpoint | Descripció |
|--------|----------|------------|
| `POST` | `/v1/memory/write` | Crear un fet (amb generació d'embeddings en segon pla) |
| `GET` | `/v1/memory/query` | Cerca per text (FTS5) |
| `POST` | `/v1/memory/search/semantic` | Cerca semàntica per similitud cosinus |
| `PUT` | `/v1/memory/{fact_id}` | Actualitzar un fet |
| `DELETE` | `/v1/memory/{fact_id}` | Eliminar un fet (soft delete) |
| `GET` | `/v1/memory/audit` | Consultar registre d'auditoria (cal admin) |
| `GET` | `/health` | Estat del servei |
| `GET` | `/dashboard` | Dashboard HTML amb Chart.js |
| `GET` | `/api/stats` | Mètriques JSON |

### Exemples d'ús

**Escriure un fet:**
```bash
curl -X POST http://localhost:8790/v1/memory/write \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"content": "El servidor principal està a Frankfurt", "scope": "shared", "key": "ubicacio_servidor", "metadata": {"tipus": "infraestructura"}}'
```

**Cercar per text:**
```bash
curl "http://localhost:8790/v1/memory/query?q=servidor&scope=shared&limit=5" \
  -H "X-API-Key: $KEY"
```

**Cerca semàntica:**
```bash
curl -X POST http://localhost:8790/v1/memory/search/semantic \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "On estan els servidors?", "top_k": 3, "scope": "shared"}'
```

## 🧪 Model d'Emmagatzematge

### Text amb FTS5
- Indexació per text complet amb `unicode61 remove_diacritics 2`
- Ignora accents i majúscules/minúscules
- Consultes ràpides sense necessitat d'embeddings

### Embeddings amb fastembed
- Model: `intfloat/multilingual-e5-small` (384 dimensions)
- ONNX Runtime (sense PyTorch) — consum de RAM molt baix
- **Lazy loading**: el model només es carrega en el primer embedding
- Prefixos: `query: ` per a cerques, `passage: ` per a continguts
- Cache SHA256 per evitar recomputacions
- Si falla fastembed, fa **fallback automàtic a FTS5**

### Conversió de Text a Fragments (Chunking)
- Màxim 500 caràcters per fragment
- Solapament de 50 caràcters entre fragments consecutius
- Talls intel·ligents per espais

## 🔒 Seguretat

- **Autenticació**: API Key amb bcrypt (l'hash es compara amb `bcrypt.checkpw`)
- **Rate limiting**: 100 peticions per finestra de 60 segons per agent
- **Permisos granulars**: read/write/delete/admin per agent
- **Àmbits**: restricció per scope (shared/local)
- **Soft delete**: els fets no s'eliminen físicament (només es marquen)

## 📊 Monitoratge

El dashboard HTML a `/dashboard` inclou:
- Gràfic de barres: fets per dia (últims 7 dies)
- Gràfic de sectors: distribució per agent
- Gràfic de línia: mida de la base de dades
- Comptadors: actius, eliminats, fragments, agents
- Taula: últimes 10 accions d'auditoria

## ⚙️ Configuració

Variables d'entorn (prefix `BRAIN_`) o camps a `brain/config.py`:

| Variable | Default | Descripció |
|----------|---------|------------|
| `BRAIN_DB_PATH` | `/opt/brain/data/brain.db` | Ruta de la base de dades |
| `BRAIN_API_PORT` | `8790` | Port del servei |
| `BRAIN_EMBED_MODEL` | `intfloat/multilingual-e5-small` | Model d'embeddings |
| `BRAIN_EMBED_DIM` | `384` | Dimensions del vector |
| `BRAIN_MAX_CHUNK_SIZE` | `500` | Màxim de caràcters per fragment |
| `BRAIN_CHUNK_OVERLAP` | `50` | Solapament entre fragments |
| `BRAIN_RATE_LIMIT` | `100` | Peticions per finestra |
| `BRAIN_RATE_LIMIT_WINDOW` | `60` | Finestra de rate limit (segons) |

## 🚀 Desplegament en Producció

### LXC (recomanat)
```bash
# Dins del contenidor LXC
lxc exec brain -- bash
# Segueix la guia d'instal·lació pas a pas
```

### Tailscale
```bash
# Assegura't que Tailscale està configurat
sudo tailscale up
# El servei escolta a 0.0.0.0:8790 i és accessible via Tailscale IP
```

### Systemd
```bash
sudo systemctl enable brain.service
sudo systemctl start brain.service

# Logs
sudo journalctl -u brain.service -f
```

## 🛠 Manteniment

### Compactar base de dades
```bash
sqlite3 /opt/brain/data/brain.db "VACUUM;"
```

### Reiniciar el servei
```bash
sudo systemctl restart brain.service
```

### Netejar la cache d'embeddings
```bash
sqlite3 /opt/brain/data/brain.db "DELETE FROM embedding_cache; VACUUM;"
```

## 📝 Llicència

Ús intern per a l'ecosistema Hermes.

---

**Pluribus** — *La memòria compartida que fa intel·ligent el teu ecosistema d'agents.*
