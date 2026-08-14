"""API-key generation and lookup helpers."""

from __future__ import annotations

import hashlib
import secrets

_API_KEY_PREFIX = "plb_"


def generate_api_key() -> str:
    """Generate a high-entropy API key with a versionable prefix."""
    return _API_KEY_PREFIX + secrets.token_urlsafe(32)


def fingerprint_api_key(api_key: str) -> str:
    """Return a deterministic lookup fingerprint for a high-entropy API key.

    The bcrypt hash remains the credential verifier. This SHA-256 fingerprint is
    only an indexed selector so authentication performs one bcrypt operation
    instead of scanning every agent.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def is_current_api_key(api_key: str) -> bool:
    return api_key.startswith(_API_KEY_PREFIX)
