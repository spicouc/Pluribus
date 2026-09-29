## Resum

Tres observacions de severitat baixa, agrupades perquè són independents entre si i no toquen l'autorització.

## 1. Auditoria de canvis de configuració que falla en silenci

`pluribus/admin_config.py:204-217`:

```python
async def _audit(agent_id: str, action: str, payload: dict[str, Any]) -> None:
    try:
        async with get_db() as db:
            await log_audit(...)
            await db.commit()
    except Exception:
        pass
```

`_audit` s'executa **després** d'haver Written el fitxer d'entorn (`admin_config.py:232`) i el client rep 200 igualment. Un canvi de configuració es pot aplicar sense cap entrada a `audit` i sense que ningú se'n assabari.

`README.md:242` afirma que el sistema falla tancat davant d'estats de permís corruptes. El mateix criteri hauria d'aplicar-se a l'auditoria: un canvi de configuració **no aplicat** és una pèrdua d'audit trail, i un canvi **aplicat sense auditar** pitjor.

**Proposta:** distingir dos casos. Un error d'escriptura del fitxer ja es propaga (línies 231-236). El que no hauria de passar en silenci és un error d'auditoria: com a mínim registrar-lo amb `log.error`, i probablement retornar 500 perquè el client sapgui que el canvi no s'ha registrat. Cal decidir si el canvi s'ha d'aplicar igual (millor disponibilitat que traçabilitat) o si s'ha de rollbackar.

`/api/config/restart` (`admin_config.py:251-256`) té el mateix problema: reinicia abans d'auditar.

## 2. `/docs`, `/openapi.json` i `/redoc` exposats a qualsevol agent autenticat

`pluribus/security.py:195` deixa passar només `/health` i `/dashboard`:

```python
public_paths = {"/health", "/dashboard"}
```

FastAPI serveix `/docs`, `/redoc` i `/openapi.json` per defecte. Aquestes rutes cauen al middleware i demanen `X-API-Key`, però **qualsevol** clau vàlida les obté — inclòs un agent amb permisos `read` i res més.

El que queda exposat és tota la superfície de l'API: rutes Xerrameca, Runner, Monitor, Directives, webhooks, i el model de dades complet dels agents.

En un servei privat de xarxa Tailscale l'impacte és modest, i deshabilitar els docs té un cost real d'operativitat (els agents de la flota els aprofiten per descobrir l'API). Per tant: **proposta a discussió, no una correcció òbvia.**

Si es decideix tancar-ho, la via neta és `FastAPI(..., docs_url=None, redoc_url=None, openapi_url=None)` a `pluribus/main.py:131`, en lloc d'afegir-los a `public_paths`, perquè així continuen requerint autenticació. Si es vol mantenir l'accés, la restricció mínima és exigir `admin` a `/docs` i `/openapi.json`.

## 3. Nits al rate limiter i a `get_db()`

**`pluribus/security.py:80`** usa un literal `60` per decidir quan netejar, mentre que el filtre usa la configuració:

```python
if now - _last_rate_cleanup >= 60:   # hauria de ser settings.RATE_LIMIT_WINDOW
    _cleanup_rate_limiter()
window_start = now - settings.RATE_LIMIT_WINDOW
```

Amb `PLURIBUS_RATE_LIMIT_WINDOW=3600`, el neteig corre cada 60s aplicant una finestra d'una hora sobre tot l'historial acumulat. **Correcte**, però innecessàriament car i contraintuitiu. Substituir el 60 per la constant de configuració.

**`pluribus/db.py:13-15`** fa `mkdir(parents=True, exist_ok=True)` a **cada** `get_db()`, és a dir, a cada petició HTTP i a cada crida interna:

```python
async with get_db() as db:
    db_path = Path(settings.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
```

`DB_PATH` ve de la configuració i no canvia en runtime. Moure el `mkdir` a `init_db()` (`db.py:205`) fa que el camí existeixi una sola vegada, abans de servir trànsit — que és quan el README ja diu que es fa la inicialització (línia 57).

## Nota sobre l'entorn de revisió

La revisió que ha produït aquest issue **no ha pogut executar la suite de tests**: el contenidor no tenia `pip` ni accés a xarxa i les rutes externes estaven bloquejades. Els 35 fitxers de `tests/` no s'han executat. En particular, `tests/test_health_runtime.py` i `tests/test_api_key_auth.py` són els que tocarien aquests punts i no s'han pogut validar.

_Proposta signada: kilocode_
