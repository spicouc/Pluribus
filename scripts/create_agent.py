#!/usr/bin/env python3
"""Script CLI per crear un agent i generar una clau API.

Crea un agent a la base de dades amb una clau API generada aleatòriament.
La clau es mostra UNA SOLA VEGADA per stdout i no es pot recuperar després.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import sys
from pathlib import Path

import bcrypt


def main() -> None:
    """Punt d'entrada principal."""
    db_path = "/opt/pluribus/data/pluribus.db"

    if not Path(db_path).exists():
        print(f"Error: No es troba la base de dades a {db_path}", file=sys.stderr)
        print("Assegura't d'haver executat: sqlite3 {db_path} < scripts/init_db.sql", file=sys.stderr)
        sys.exit(1)

    print("=== Creació d Agent Pluribus ===")
    name = input("Nom de l'agent: ").strip()
    if not name:
        print("Error: El nom és obligatori", file=sys.stderr)
        sys.exit(1)

    scopes_input = input("Àmbits permesos (separats per comes, default: shared): ").strip()
    if not scopes_input:
        allowed_scopes = ["shared"]
    else:
        allowed_scopes = [s.strip() for s in scopes_input.split(",") if s.strip()]

    print("Permisos (deixa buit per defecte):")
    print("  read: true, write: true, delete: false, admin: false")
    perms_input = input("Permisos JSON (o Enter per defecte): ").strip()
    if perms_input:
        try:
            permissions = json.loads(perms_input)
        except json.JSONDecodeError as e:
            print(f"Error: JSON invàlid: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        permissions = {"read": True, "write": True, "delete": False, "admin": False}

    # Genera clau API
    api_key = secrets.token_urlsafe(32)
    api_key_hash = bcrypt.hashpw(api_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    # Genera UUID v4 per a l'ID
    agent_id = secrets.token_hex(16)
    agent_id = f"{agent_id[:8]}-{agent_id[8:12]}-4{agent_id[13:16]}-{agent_id[16:20]}-{agent_id[20:]}"

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")

        cursor = conn.execute(
            "INSERT INTO agents (id, name, api_key_hash, permissions, allowed_scopes) VALUES (?, ?, ?, ?, ?)",
            (
                agent_id,
                name,
                api_key_hash,
                json.dumps(permissions),
                json.dumps(allowed_scopes),
            ),
        )
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError as e:
        print(f"Error: Ja existeix un agent amb el nom '{name}': {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        print(f"Error de base de dades: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 60)
    print("✅ AGENT CREAT CORRECTAMENT")
    print("=" * 60)
    print(f"  ID:    {agent_id}")
    print(f"  Nom:   {name}")
    print(f"  Àmbits: {', '.join(allowed_scopes)}")
    print(f"  Permisos: {json.dumps(permissions)}")
    print("=" * 60)
    print("\n⚠  CLAU API (guarda-la ara - no es mostrarà mai més):")
    print(f"\n  {api_key}\n")
    print("=" * 60)

    # Desa la clau a stdout per si es redirigeix
    sys.stdout.flush()


if __name__ == "__main__":
    main()
