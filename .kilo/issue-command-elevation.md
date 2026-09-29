## Resum

`pluribus/xerrameca/command.py:102-111` fabrica un dict d'agent amb `permissions["admin"] = True` i el passa a serveis que són **admin-only**. És una obtenció d'elevació de privilegi en temps d'execució, no una autorització real: qualsevol validació admin que s'afegeixi a aquests serveis quedarà silenciosament by-passada.

## Codi

```python
def _internal_admin(agent: dict[str, Any]) -> dict[str, Any]:
    """Elevate only after command-specific self-service checks have passed. ..."""
    permissions = dict(agent.get("permissions") or {})
    permissions["admin"] = True
    return {**agent, "permissions": permissions}
```

## Qui el crida

- `command.py:337` i `command.py:343` — `create_conversation` i `start_conversation`
- `command.py:430` — `cancel_conversation` (després de `:427`, on sí que es comprova `created_by_agent_id`)

Els serveis cridats exigeixen `admin` de veritat:

- `pluribus/xerrameca/service.py:328` — `create_conversation`
- `pluribus/xerrameca/service.py:466` — `start_conversation`
- `pluribus/xerrameca/service.py:1286` — `cancel_conversation`

## Per què importa

Els checks de la comanda són correctes **avui**:

- `_require_command_access` (`command.py:86-99`) exigeix `read` + `write` i accés al scope `shared`
- `_resolve_target` (`command.py:171`) només resol agents actius del mateix scope que poden participar
- `_stop` (`command.py:425-430`) comprova que el caller hagi creat la conversa

El problema no és un exploit actual sinó la **fragilitat**: el servei i la comanda tenen dues politiques d'autorització diferents per a la mateixa operació, i la comanda la reescriu en temps d'execució. Si demà s'afegeix, per exemple, un límit de rondes per a no-admins o una restricció de participants, el `_internal_admin` la desactiva sense deixar rastre.

## Direccions possibles

1. **Parametre explícit al servei.** Els serveis admin-only prenen un argument tipat (p. ex. `elevated: bool = False` o un `AuthorizationContext`) en lloc de rebre un dict d'agent. El codi de la comanda passa l'elevació com a dada explícita i els guards del servei es decideixen una sola vegada.
2. **Duplicar els serveis "de comanda"** amb la seva pròpia política, deixant els admin-only intactes.
3. **Retirar l'auto-servei** i tornar `/xerrameca <agent>` admin-only, igual que ho era abans.

La opció 1 és la que recomano: manté el comportament actual, converteix l'elevació en un contracte explícit i fa que les futures comprovacions del servei s'apliquin de manera visible.

## Notes

- `command.py` no és l'únic lloc amb duplicació de lògica d'autorització: `pluribus/memory.py:50` té el seu propi `_check_permission` i `pluribus/authorization.py:32` un altre. No cal tractar-ho aquí, però és el mateix patró.
- La revisió d'on surt aquest issue **no ha pogut executar la suite de tests** (sense `pip` ni xarxa a l'entorn). Els 35 fitxers de `tests/` no s'han executat.

_Proposta signada: kilocode_
