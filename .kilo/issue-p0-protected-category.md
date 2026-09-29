## Resum

Un agent sense permís `admin` pot esborrar memòria d'infraestructura que el sistema suposa permanent, tot i que el codi exigeix explícitament `admin` per fer-ho.

Això no és una filtració d'informació sinó un **bypass d'escalada**: la protecció existeix, s'executa, i és fàcil de saltar.

## Passos per reproduir

1. Amb un agent que tingui `read`/`write`/`delete` al scope però **no** `admin`:
2. `PUT /v1/memory/{id}` amb `{"content": "...", "category": "events"}` sobre un fet de categoria `system`, `config` o `entities` → **200 OK**.
3. `DELETE /v1/memory/{id}` sobre el mateix fet → **204**. El guard d'esborrament passa, perquè mira la categoria _actual_.

## Per què el guard és buit

`pluribus/memory.py:790-796` i `pluribus/authorization.py:202-203` exigeixen `admin` per esborrar un fet de categoria protegida. Però el `PUT` deixa canviar la categoria sense cap comprovació a:

- `pluribus/authorization.py:187-195` — valida `content` i `category` del cos, però mai no mira `_PROTECTED_CATEGORIES`
- `pluribus/memory.py:674-762` (`update_memory`) — idem, cap guard

Per tant el guard de l'esborrament es pot desactivar a voluntat retagant el fet.

## Intenció de disseny (ja decidida)

La protecció és **contra l'esborrament**, no contra l'escriptura. Això és deliberat i no s'ha de canviar:

- `pluribus/validation.py:13-23` inclou `system`, `config` i `entities` a `VALID_CATEGORIES`
- `tests/test_input_validation.py:62-66` ho fixa: `WriteRequest(content="x", category="system")` s'ha d'acceptar

Per tant el pegat **no ha de tocar** el model ni la validació d'escriptura, i el test esmentat ha de continuar passant.

## Regla a implementar

Un agent no-admin no pot **des-protegir** un fet. Denegar el canvi de categoria només quan es compleixen les quatre condicions:

- el fet actualment és de categoria protegida, **i**
- el cos suplica `category`, **i**
- el valor sol·licitat és **diferent** de l'actual, **i**
- el caller no és `admin`.

| Cas | Resultat |
|---|---|
| `PUT` sense `category` sobre fet `system` (edició de contingut/metadades) | permès |
| `PUT {"category": "system"}` sobre fet ja `system` (round-trip read-modify-write) | permès |
| `PUT {"category": "events"}` sobre fet `system`, no-admin | **403** |
| `PUT {"category": "events"}` sobre fet `system`, admin | permès |
| `PUT {"category": "system"}` sobre fet `events`, no-admin | permès |

Comparar contra el valor actual (en lloc de denegar qualsevol `category` present) evita trencar els clients que fan read-modify-write i reenvien la categoria que acaben de llegir.

## Abast tècnic

**Vector únicament REST.** MCP no exposa cap tool d'actualització — només `memory_write`, `memory_delete`, `memory_get_fact`, `memory_stats` i `memory_ls` (`pluribus/mcp.py:35-108`). Cap worker intern insereix fets en categories protegides; `pluribus/xerrameca/service.py:927` usa `x-xerrameca`. Per tant n'hi ha prou amb dos punts de control.

### 1. Helper compartit a `pluribus/authorization.py`

A prop de `_PROTECTED_CATEGORIES` (línia 22) i `_require` (línia 32):

```python
def _assert_category_change_allowed(
    agent: dict[str, Any], current_category: str, requested_category: str | None
) -> None:
    if requested_category is None or requested_category == current_category:
        return
    if current_category not in _PROTECTED_CATEGORIES:
        return
    if agent.get("permissions", {}).get("admin", False):
        return
    raise HTTPException(
        status_code=403,
        detail="Canviar la categoria d'un fet persistent requereix permís admin",
    )
```

Una sola còpia de la regla, cridada pels dos punts de control, perquè no es puguin desincronitzar.

### 2. Guard central — `pluribus/authorization.py:181-203`

Dins de la branca `PUT`/`DELETE` de `memory_authorize`:

- **Trampa:** el `body` es llegeix dins del bloc `if method == "PUT":` (línies 187-195) i no sobre viu fora del bloc. Cal inicialitzar `requested_category: str | None = None` **abans** del bloc i assignar-lo a dins.
- Cridar el helper just després de `scope, category = fact` (línia 200) i abans de `_require(...)` (línia 201).

### 3. Handler — `pluribus/memory.py:674-762`

- Ampliar la projecció del `SELECT` de la línia 692 (`id, content, metadata`) per incloure **`category`**.
- Cridar el helper després del 404 (línia 697) i abans de qualsevol `UPDATE`.
- Importar `_PROTECTED_CATEGORIES` i el helper des de `pluribus.authorization` i esborrar la còpia local de la constant (línia 26). `authorization.py` no importa `memory.py` (només `db` i `validation`), així que no hi ha risc d'importació circular.

## Tests de regressió

`tests/test_authorization.py` ja té el harness `make_request` i és la casa natural dels tests de `memory_authorize`. Cap test existent cobreix la branca `PUT`/`DELETE` (la que toca la BD), cal mocar `_fact_scope_category`; `unittest.mock.patch` és el patró predominant del suite (19 fitxers l'usen).

Amb `patch("pluribus.authorization._fact_scope_category", new=AsyncMock(return_value=("shared", "system")))`:

1. `test_non_admin_cannot_unprotect_a_persistent_fact` → 403
2. `test_non_admin_can_edit_content_of_a_persistent_fact` → permès; documenta que no es trenca l'edició normal
3. `test_non_admin_may_repeat_the_same_persistent_category` → permès
4. `test_admin_can_recategorize_a_persistent_fact` → permès
5. `test_non_admin_may_tag_a_standard_fact` → permès; fixa que la protecció és contra l'esborrament, no contra l'etiquetatge

### Test del handler — demanat per la revisió del supervisor

Cal **complementar** els tests anteriors amb un test del handler `update_memory`, perquè els dos punts de control no es desincronitzin. El supervisor ho va demanar explícitament i és el test que fa que la defensa en profunditat sigui real i no nominal.

Fes-lo amb **BD real**, no amb `get_db` mocat. El patró ja existeix al suite: `TemporaryPluribusDb` a `tests/test_third_wave_hardening.py:24-53` (patch de `settings.DB_PATH` i `settings.EMBED_DIM`, `init_db()`, helper `_insert_fact`); `tests/test_chunk_embeddings.py:14-20` fa el mateix.

El motiu: el que es vols provar és precisament que el handler **carrega `category` al SELECT**. Amb BD real, el test falla si algú oblida el `category` a la projecció de `memory.py:692`; amb BD mocat, es testeja el que vols que passi i no la causa. A més `update_memory` ja necessita `settings.EMBED_DIM` per al BLOB placeholder, de manera que el patch és el mateix.

Amb el fet inserit com a `system`:

```python
request = make_request("/v1/memory/fact-1", "PUT")
request.state.agent = standard_agent()          # read/write/delete, admin=False
body = UpdateRequest(content="x", category="events")
with self.assertRaises(HTTPException) as ctx:
    await update_memory(request, "fact-1", body, BackgroundTasks())
self.assertEqual(ctx.exception.status_code, 403)
```

### Buit detectat al criteri de tancament

El criteri de tancament del supervisor inclou `DELETE system/config/entities no-admin = 403`. Aquesta garantia **ja es compleix avui** (`memory.py:790-796`, `authorization.py:202-203`) però **no té cap test**: no apareix `delete_memory`, `_PROTECTED` ni cap cas de DELETE sobre categoria protegida a tot `tests/`. Ningú detectaria si es trenca.

Com que el helper nou serà compartit, hi ha d'afegir els casos positius del mateix protocol:

- `delete_memory` no-admin sobre fet `system` → 403
- `delete_memory` admin sobre fet `system` → 204

### Criteri de tancament validat pel supervisor

- non-admin `system -> events` = 403
- non-admin `system -> system` = permès
- non-admin edita contingut de `system` = permès
- admin `system -> events` = permès
- non-admin `events -> system` = permès
- DELETE `system/config/entities` no-admin = 403
- `compileall` = PASS
- suite completa = PASS

L'últim i el penúltim **no estan verificats**: l'entorn de revisió no tenia `pip` ni xarxa.

## Documentació

`README.md:240` diu `Categories persistents system, config i entities: eliminació admin-only.` — imprecís, cal afegir-hi que no es poden des-protegir i que l'escriptura de categories protegides sí que es permet.

## Validació

```bash
python -m compileall -q pluribus pluribus_worker.py scripts
python -m unittest discover -s tests -v
```

Segon ordre = el que executa `.github/workflows/ci.yml`.

## Nota sobre l'entorn de revisió

La revisió que ha produït aquest issue **no ha pogut executar la suite de tests**: el contenidor no tenia `pip` ni accés a xarxa i les rutes externes estaven bloquejades. Els 35 fitxers de `tests/` no s'han executat mai en aquesta sessió. Cal córrer-los abans de tancar el PR.

_Proposta signada: kilocode_
