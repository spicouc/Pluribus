#!/usr/bin/env python3
"""Script CLI per crear un agent i generar una clau API."""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path

import bcrypt

from pluribus.api_keys import fingerprint_api_key, generate_api_key
from pluribus.config import settings


def main() -> None:
    """Punt d'entrada principal."""
    db_path = settings.DB_PATH

    if not Path(db_path).exists():
        print(f"Error: No es troba la base de dades a {db_path}", file=sys.stderr)
        print("Arrenca Pluribus una vegada perquè executi init_db().", file=sys.stderr)
        sys.exit(1)

    print("=== Creació d Agent Pluribus ===")
    name = input("Nom de l'agent: ").strip()
    if not name:
        print("Error: El nom és obligatori", file=sys.stderr)
        sys.exit(1)

    scopes_input = input("Àmbits permesos (separats per comes, default: shared): ").strip()
    allowed_scopes = (
        ["shared"]
        if not scopes_input
        else [s.strip() for s in scopes_input.split(",") if s.strip()]
    )

    print("Permisos (deixa buit per defecte):")
    print("  read: true, write: true, delete: false, admin: false")
    perms_input = input("Permisos JSON (o Enter per defecte): ").strip()
    if perms_input:
        try:
            permissions = json.loads(perms_input)
        except json.JSONDecodeError as exc:
            print(f"Error: JSON invàlid: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        permissions = {"read": True, "write": True, "delete": False, "admin": False}

    api_key = generate_api_key()
    api_key_hash = bcrypt.hashpw(api_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    api_key_fingerprint = fingerprint_api_key(api_key)
    agent_id = str(uuid.uuid4())

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT INTO agents
               (id, name, api_key_hash, api_key_fingerprint, permissions, allowed_scopes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                agent_id,
                name,
                api_key_hash,
                api_key_fingerprint,
                json.dumps(permissions),
                json.dumps(allowed_scopes),
            ),
        )
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError as exc:
        print(f"Error d'integritat creant l'agent: {exc}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as exc:
        print(f"Error de base de dades: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 60)
    print("AGENT CREAT CORRECTAMENT")
    print("=" * 60)
    print(f"  ID:    {agent_id}")
    print(f"  Nom:   {name}")
    print(f"  Àmbits: {', '.join(allowed_scopes)}")
    print(f"  Permisos: {json.dumps(permissions)}")
    print("=" * 60)
    print("\nCLAU API (guarda-la ara - no es mostrarà mai més):")
    print(f"\n  {api_key}\n")
    print("=" * 60)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
