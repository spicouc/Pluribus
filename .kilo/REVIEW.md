# Revisió de codi — Pluribus

Quatre propostes de treball, obertes perquè un altre agent les reculli. Cadascuna és **autònoma** i es pot treballar per separat; no hi ha dependències entre elles.

| # | Prioritat | Tema |
|---|---|---|
| [#97](https://github.com/spicouc/Pluribus/issues/97) | P0 | `PUT /v1/memory/{id}` des-protegeix categories persistents i by-passa el guard d'esborrament |
| [#98](https://github.com/spicouc/Pluribus/issues/98) | P1 | `xerrameca/command.py` eleva permisos en temps d'execució per cridar serveis admin-only |
| [#99](https://github.com/spicouc/Pluribus/issues/99) | P1 | `dashboard.py` conserva rutes de configuració obsoletes que contradiuen el README |
| [#100](https://github.com/spicouc/Pluribus/issues/100) | P2 | Auditoria en silenci, docs exposats i nits menors |

**Ordre recomanat:** #97 primer (seguretat, petit i revertible), després #99 (neteja, cap canvi funcional), després #98 (refactor que necessita decisions de disseny), i #100 quan hi hagi temps.

## Advertiment sobre l'entorn de revisió

Aquesta revisió **no ha pogut executar la suite de tests**. El contenidor no tenia `pip` ni accés a xarxa, i les rutes externes estaven bloquejades per regla. Els 35 fitxers de `tests/` **no s'han executat mai**.

El que sí que s'ha verificat:

- `python -m compileall -q pluribus pluribus_worker.py scripts` → **OK**
- Lectura completa dels fitxers implicats a cada finding, amb els números de línia citats als issues

Això vol dir que **tots els findings són de lectura de codi, no d'execució**, i que l'agent que implementi ha de córrer la suite abans de tancar el PR. El README indica com montar l'entorn:

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.lock
python -m unittest discover -s tests -v
```

## Es comporta bé (verificat, sense que s'hi hagi trobat problema)

Aquestes àrees s'han revisat i no s'hi ha detectat res que calgui canviar:

- **Injecció SQL.** Tots els SQL és parametrizat. Els únics f-strings són `SET {join}` amb noms de columna d'una whitelist (`agents.py:146`, `xerrameca/service.py:182,723`, `runner.py:142`, `monitor.py:108`) i `WHERE {where_clause}` compostes de fragments literals (`memory.py:848,928`, `mcp.py:504`).
- **SSRF als webhooks.** `webhooks.py:126-185` resol el hostname una sola vegada, valida **totes** les adreces resoltes, i `_post_pinned` connecta directament a la IP validada conservant `Host` i SNI. Elimina correctament el TOCTOU de resolució.
- **Atomicitat dels torns.** `xerrameca/service.py:857-867` usa `BEGIN IMMEDIATE` + `UPDATE ... WHERE status='ready'` + comprovació de `rowcount`; `runner.py:356-369` allibera el lease només si `claimed_by` i `lease_token` coincideixen. Sense double-processing.
- **Escritura atòmica de la configuració.** `admin_config.py:159-185` fa temp-file + `fsync` + `chmod 0600` + `os.replace`, i rebutja symlinks.
- **Fail-closed.** `security.py:186-188` fa fallar l'autenticació en qualsevol excepció; `authorization.py:222-230` concedeix `False` en JSON de permisos corrupte.
- **`identity_provider.py`** no exposa hashes, fingerprints, IPs ni metadata privada.
- **Resposta del model a CPU DoS.** `security.py` combina fast path indexat per fingerprint, limitar de comprovacions de bcrypt en memòria i un límit específic per al camí legacy.

## Un matís important sobre el P0

En la revisió inicial, el comportament d'acceptar escriptures en categories `system`/`config`/`entities` es va presentar com si fos un error. **No ho és.** `validation.py:13-23` inclou aquestes categories a `VALID_CATEGORIES` i `tests/test_input_validation.py:62-66` ho fixa a propòsit.

La intenció del disseny és que la protecció sigui **contra l'esborrament**, no contra l'etiquetatge. Per això el pegat del #97 no toca el model ni la validació, i aquell test ha de continuar passant. Un cop decidit això, el bypass és clar: el `PUT` deixa canviar la categoria, i el guard de l'esborrament mira la categoria _actual_ en lloc de l'original.

---

_Revisió signada: kilocode_
