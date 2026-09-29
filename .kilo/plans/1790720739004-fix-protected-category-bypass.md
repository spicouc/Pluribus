# Corregir el bypass de categories persistents al `PUT /v1/memory/{fact_id}`

## Problema

`system`, `config` i `entities` són categories **vàlides per escriure** (`pluribus/validation.py:13-23`, i
`tests/test_input_validation.py:62-66` ho fixa explícitament). La protecció d'aquestes categories és, per disseny,
**només contra l'esborrament**: `pluribus/memory.py:790-796` i `pluribus/authorization.py:202-203` exigeixen `admin`
per esborrar un fet que hi pertany.

Però `PUT /v1/memory/{fact_id}` deixa canviar la categoria sense cap comprovació. Un agent no-admin amb `write`+`delete`
al scope pot fer:

1. `PUT /v1/memory/{id}` amb `{"category": "events"}` sobre un fet de categoria `system` → el fet deixa de estar protegit.
2. `DELETE /v1/memory/{id}` → el guard passa, perquè mira la categoria **actual**.

El guard d'esborrament queda buit. És un bypass d'escalada complet: un agent sense `admin` pot esborrar memòria d'infraestructura que el sistema
presuposa permanent.

**Vector únicament REST.** MCP no exposa cap tool d'actualització, només `memory_write` / `memory_delete` /
`memory_get_fact` / `memory_stats` / `memory_ls` (`pluribus/mcp.py:35-108`), i cap worker intern insereix fets en
categories protegides. `pluribus/xerrameca/service.py:927` usa `x-xerrameca`. Per tant el pegat és acotat.

## Decisió de disseny

**Regla:** en un `PUT`, un agent no-admin no pot **des-protegir** un fet. Es denega el canvi només quan es compleixen
les quatre condicions:

- el fet actualment és de categoria protegida, **i**
- el cos suplica `category`, **i**
- el valor sol·licitat és ** diferent** de l'actual, **i**
- el caller no és `admin`.

Conseqüències intencionades:

| Cas | Resultat |
|---|---|
| `PUT` sense `category` sobre fet `system` (edició de contingut/metadades) | **Permès** — no es toca la protecció |
| `PUT {"category": "system"}` sobre fet ja `system` (round-trip read-modify-write) | **Permès** — no hi ha cannet |
| `PUT {"category": "events"}` sobre fet `system`, no-admin | **403** |
| `PUT {"category": "events"}` sobre fet `system`, admin | **Permès** |
| `PUT {"category": "system"}` sobre fet `events`, no-admin | **Permès** — no facilita cap esborrament, i el `POST /write` ja permet crear fets `system` |

Comparar contra el valor actual (en lloc de denegar qualsevol `category` present) evita de trencar els clients que
fan read-modify-write i reenvien la categoria que acaben de llegir.

## Passos d'implementació

### 1. Helper compartit — `pluribus/authorization.py`

A prop de `_PROTECTED_CATEGORIES` (línia 22) i `_require` (línia 32), afegir una funció de mòdul:

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

És l'única còpia de la regla. Els dos punts de control la criden, de manera que no es poden desincronitzar.

### 2. Punt de control al guard central — `pluribus/authorization.py:181-203`

Dins de la branca `PUT`/`DELETE` de `memory_authorize`:

- **Trampa d'implementació:** el `body` es llegeix dins del bloc `if method == "PUT":` (línies 187-195) i no sobre viu
  fora. Cal inicialitzar `requested_category: str | None = None` **abans** del bloc i assignar-lo a dins
  (`requested_category = body.get("category")`), després de passar `_validated(validate_category, ...)`.
- Inserir la crida a `_assert_category_change_allowed(agent, category, requested_category)` just després de
  `scope, category = fact` (línia 200) i abans de `_require(...)` (línia 201).

El fet inexistent ja surt aviat via `if fact is None: return` (línia 198) i el handler respon 404; no cal tocar-ho.

### 3. Punt de control al handler — `pluribus/memory.py:674-762`

`update_memory` és el segon control, per simetria amb com `DELETE` ja està defensat dues vegades:

- Ampliar la projecció del `SELECT` de la línia 692 (`id, content, metadata`) per incloure **`category`**; si no,
  no hi ha res a comprovar.
- Cridar `_assert_category_change_allowed(agent, existing_dict["category"], body.category)` després del 404
  (línia 697) i abans de qualsevol `UPDATE`.
- Importar `_PROTECTED_CATEGORIES` i `_assert_category_change_allowed` des de `pluribus.authorization` i **esborrar
  la còpia local** de la constant (línia 26), reutilitzant la del helper. `authorization.py` no importa `memory.py`
  (només `db` i `validation`), així que no hi ha risc d'importació circular.

### 4. Test de regressió — `tests/test_authorization.py`

El fitxer ja té el harness `make_request` i és la casa natural dels tests de `memory_authorize`. Cap test existent
cobreix la branca `PUT`/`DELETE` (la que toca la BD), cal mocar `_fact_scope_category`; `unittest.mock.patch` és el
patró predominant del suite (19 fitxers l'usen).

Amb `patch("pluribus.authorization._fact_scope_category", new=AsyncMock(return_value=("shared", "system")))`
i `standard_agent()` / una variant admin:

1. `test_non_admin_cannot_unprotect_a_persistent_fact` — `PUT` amb `{"content": "x", "category": "events"}` → 403.
2. `test_non_admin_can_edit_content_of_a_persistent_fact` — `PUT` sense `category` → no Raises. **És el que documenta
   que no es trenca l'edició normal.**
3. `test_non_admin_may_repeat_the_same_persistent_category` — `{"category": "system"}` sobre fet `system` → no Raises.
4. `test_admin_can_recategorize_a_persistent_fact` → no Raises.
5. `test_non_admin_may_tag_a_standard_fact` — amb el mock retornant `("shared", "events")`, `{"category": "system"}`
   → no Raises. Fixa que la protecció és contra l'esborrament, no contra l'etiquetatge.

### 5. Documentació — `README.md:240`

La línia actual és imprecisa: `Categories persistents system, config i entities: eliminació admin-only.` Substituir per
una formulació que descrigui la regla completa, incloent-hi que no es poden des-protegir, i que l'escriptura de
categories protegides sí que es permet.

## Validació

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.lock
python -m compileall -q pluribus pluribus_worker.py scripts
python -m unittest discover -s tests -v
```

(`python -m unittest discover -s tests -v` és el que executa `.github/workflows/ci.yml`.)

**Nota:** en la sessió on es va escriure aquest pla no hi havia `pip` ni accés a xarxa, i la suite **no s'ha pogut
executar**. L'agent que implementi ha de correr els 35 fitxers de `tests/` abans de donar-ho per bo. El test
`test_write_accepts_protected_and_explicit_extension_categories` ha de continuar passant: aquest canvi no toca el
model ni la validació d'escriptura.

## Riscos

- **Baix.** Un `PUT` de contingut sobre fets protegides continua funcionant sense `admin`; és el cas més
 Freqüent i és el que cobreix el test 2.
- **Baix.** Un agent que avui reconfiguri categòries de memòria d'infraestructura haurà de demanar-ho a un admin.
  Aquest és el canvi de política demanat, no una regressió.
- **Notes de normalització:** `UpdateRequest.category` passa per `validate_category`, que aplica `strip().lower()`
  (`pluribus/models.py:161-164`). Si una BD legacy tingui categories amb majúscules (`System`), la comparació les
  tractarà com a diferents i denegarà el canvi. És la direcció segura, però es deixa documentat perquè un
  `lint` que normalitzi les categories antigues seria una millora separada.

## Fora d'abast

Aquest pla cobreix **només** el bypass de categories. Els altres findings de la revisió queden oberts i sense
plans, pendents de la teva decisió en un pla separat:

- Auditoria de configuració que falla en silenci (`admin_config.py:216-217`).
- Rutes obsoletes de `dashboard.py` amb `systemctl restart` i escriptura no atòmica de `.env` (`:227-258`).
- Elevació de permisos forjada a `xerrameca/command.py:102-111`.
- `/docs`, `/openapi.json` i `/redoc` accessibles a qualsevol agent autenticat.
- Nits del rate limiter i del `mkdir` per connexió.
