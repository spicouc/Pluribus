## Resum

`pluribus/dashboard.py` conté rutes de configuració que contradiuen explícitament el README i que només estan neutralitzades per l'ordre de registre dels routers a `pluribus/main.py`.

## Els dos problemes

### 1. `systemctl restart` malgrat que el README diu que no passa

`pluribus/dashboard.py:238` i `pluribus/dashboard.py:253`:

```python
subprocess.Popen(["systemctl", "restart", "pluribus"],
                 stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL)
```

`README.md:139` afirma:

> El restart administratiu de l'API **no executa `systemctl`**. ... Un `systemctl stop pluribus` explícit continua aturant la unitat normalment.

I `pluribus/admin_config.py:188-201` (`_restart_service`) fa exactament això, amb `SIGTERM` al mateix procés i `Restart=always`. La intenció del disseny és clara i ben implementada a `admin_config.py`; les rutes de `dashboard.py` en són la versió anterior, que va quedar morta però no esborrada.

### 2. Escriptura no atòmica del fitxer d'entorn

`pluribus/dashboard.py:227`:

```python
with open(env_path, "w") as f:
    f.writelines(lines)
```

Sense `fsync`, sense `chmod 0600`, sense `os.replace()` atòmic. Compareix amb `pluribus/admin_config.py:159-185`, que fa temp-file + `fsync` + `chmod 0600` + `os.replace` i rebutja symlinks — i que està documentat al README com a requisit (línies 49-55).

Un truncament en aquest punt deixa el servei sense configuració vàlida en el reinici següent.

## Per què estan dormides

`pluribus/main.py` registra els routers en aquest ordre:

```
:158  admin_config_view_router   (GET  /api/config)
:159  admin_config_router        (POST /api/config/save, POST|GET /api/config/restart)
:160  dashboard_router           (les duplicades)
```

FastAPI resol per ordre d'inserció, de manera que `admin_config.py` sempre atalla el dashboard. Però són rutes **registrades**, dependents d'un detall de registre que pot canviar en qualsevol refactor, i que mai retornen. El README les descriu com a inexistents.

## Proposta

Eliminar de `pluribus/dashboard.py`:

- `POST /api/config/save` (línies 195-246) — versió no atòmica de `admin_config.py:220-248`
- `GET /api/config/restart` (línies 249-258) — `admin_config.py:251-262` ja el rebutja amb 405 al mateix path i mètode

I el `import subprocess` (línia 7), que es queda sense ús.

Deixar intactes `/api/stats`, `/api/search`, `/api/ollama/models` i el HTML de `/dashboard`, que no tenen equivalent modern.

**No** cal cap canvi funcional: les rutes que hi quedaran són les que ja responen avui.

## Notes

- `pluribus/admin_config_view.py:41` (`GET /api/config`) ja atalla el `GET /api/config` de `dashboard.py:152` pel mateix motiu.
- Si es prefereix no esborrar, l'alternativa és_DELETE rutes i deixar-ne un comentari_, però esborrar és preferible: el codi mort que sembla segur és pitjor que el codi absent.
- La revisió d'on surt aquest issue **no ha pogut executar la suite de tests** (sense `pip` ni xarxa a l'entorn). Els 35 fitxers de `tests/` no s'han executat. En particular, cal comprovar que cap test existent depengui d'aquestes rutes.

_Proposta signada: kilocode_
