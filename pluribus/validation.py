"""Shared validation primitives for public Pluribus request inputs."""

from __future__ import annotations

import json
import re
from typing import Any

_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_EXTENSION_CATEGORY_RE = re.compile(r"^x-[a-z0-9][a-z0-9._-]{0,47}$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}$")

VALID_CATEGORIES = {
    "",
    "profile",
    "preferences",
    "entities",
    "events",
    "cases",
    "patterns",
    "system",
    "config",
}
PERMISSION_KEYS = {"read", "write", "delete", "admin"}

MAX_CONTENT_LENGTH = 100_000
MAX_QUERY_LENGTH = 4_096
MAX_METADATA_BYTES = 65_536


def _reject_controls(value: str, field: str, allow_layout: bool = False) -> str:
    allowed = {"\t", "\n", "\r"} if allow_layout else set()
    if any(ord(ch) < 32 and ch not in allowed for ch in value):
        raise ValueError(f"{field} conté caràcters de control no permesos")
    if not allow_layout and any(ch in value for ch in ("\t", "\n", "\r")):
        raise ValueError(f"{field} conté caràcters de control no permesos")
    if "\x00" in value:
        raise ValueError(f"{field} conté NUL")
    return value


def validate_scope(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("scope ha de ser text")
    value = value.strip()
    if not _SCOPE_RE.fullmatch(value):
        raise ValueError("scope invàlid")
    return value


def validate_category(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("category ha de ser text")
    value = value.strip().lower()
    if value in VALID_CATEGORIES or _EXTENSION_CATEGORY_RE.fullmatch(value):
        return value
    raise ValueError("category invàlida; usa una categoria coneguda o prefix x-")


def validate_content(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("content ha de ser text")
    _reject_controls(value, "content", allow_layout=True)
    if not value.strip():
        raise ValueError("content no pot ser buit")
    if len(value) > MAX_CONTENT_LENGTH:
        raise ValueError(f"content supera {MAX_CONTENT_LENGTH} caràcters")
    return value


def validate_query(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("query ha de ser text")
    value = value.strip()
    _reject_controls(value, "query")
    if not value:
        raise ValueError("query no pot ser buida")
    if len(value) > MAX_QUERY_LENGTH:
        raise ValueError(f"query supera {MAX_QUERY_LENGTH} caràcters")
    if '"' in value:
        raise ValueError('query no pot contenir el caràcter "')
    return value


def validate_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("key ha de ser text")
    value = value.strip()
    _reject_controls(value, "key")
    if not value:
        return None
    if len(value) > 256:
        raise ValueError("key supera 256 caràcters")
    return value


def validate_metadata(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("metadata ha de ser un objecte JSON")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata no és serialitzable a JSON") from exc
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata supera {MAX_METADATA_BYTES} bytes")
    return value


def validate_ttl(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("ttl_days ha de ser enter")
    if not 1 <= value <= 3650:
        raise ValueError("ttl_days ha d'estar entre 1 i 3650")
    return value


def validate_agent_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("name ha de ser text")
    value = value.strip()
    _reject_controls(value, "name")
    if not 1 <= len(value) <= 128:
        raise ValueError("name ha de tenir entre 1 i 128 caràcters")
    return value


def validate_permissions(value: dict[str, Any]) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise ValueError("permissions ha de ser un objecte")
    unknown = set(value) - PERMISSION_KEYS
    if unknown:
        raise ValueError(f"permissions desconeguts: {', '.join(sorted(unknown))}")
    result = {name: False for name in PERMISSION_KEYS}
    for name, enabled in value.items():
        if not isinstance(enabled, bool):
            raise ValueError(f"permission {name} ha de ser booleà")
        result[name] = enabled
    return result


def validate_scopes(value: list[str]) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError("allowed_scopes ha de tenir entre 1 i 32 elements")
    normalized = [validate_scope(item) for item in value]
    if len(set(normalized)) != len(normalized):
        raise ValueError("allowed_scopes conté duplicats")
    return normalized


def validate_identifier(value: str, field: str = "id") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} ha de ser text")
    value = value.strip()
    if not _SAFE_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} invàlid")
    return value
