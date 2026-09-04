"""D2-B: presence classification + work state freshness helpers.

Architectural invariant (D2-A): telemetry != memory. These
helpers turn an agent row (with last_active_at, work_state,
etc.) into a truthful dashboard response. They MUST NOT infer
state from historical memory, facts, or text matches.

Presence thresholds (matches the recommended 30-second heartbeat):

  no last_active_at        -> UNKNOWN
  age <= 60s               -> ONLINE
  60s < age <= 300s        -> STALE
  age > 300s               -> OFFLINE

Work state freshness rule:

  ONLINE  + reported WORKING -> work_state = WORKING
  STALE   + reported WORKING -> work_state = WORKING,
                                telemetry_freshness = STALE
  OFFLINE + reported WORKING -> work_state = UNKNOWN,
                                last_reported_work_state = WORKING

Last result is NOT computed here. It comes from Directives in
D2-C; until then, callers return UNKNOWN.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


WORK_STATES = ("IDLE", "WORKING", "BLOCKED", "WAITING", "ERROR", "UNKNOWN")
_PRESENCE_ONLINE_MAX_S = 60
_PRESENCE_STALE_MAX_S = 300


def _parse_iso(ts: str | None) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp as written by SQLite's
    datetime('now'). Returns None if missing or unparseable. The
    parser is intentionally narrow to avoid timezone ambiguity."""
    if not ts:
        return None
    # SQLite writes "YYYY-MM-DD HH:MM:SS" without a timezone suffix
    # when using datetime('now'). We accept both forms.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def presence_for(last_active_at: str | None,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    """Classify presence for an agent.

    Returns a dict with:
      presence:  ONLINE | STALE | OFFLINE | UNKNOWN
      age_seconds: int (None if UNKNOWN)
      threshold_online_max_s: 60
      threshold_stale_max_s:  300
    """
    t = _parse_iso(last_active_at)
    if t is None:
        return {
            "presence": "UNKNOWN",
            "age_seconds": None,
            "threshold_online_max_s": _PRESENCE_ONLINE_MAX_S,
            "threshold_stale_max_s": _PRESENCE_STALE_MAX_S,
        }
    if now is None:
        now = datetime.now(timezone.utc)
    age = int((now - t).total_seconds())
    if age <= _PRESENCE_ONLINE_MAX_S:
        p = "ONLINE"
    elif age <= _PRESENCE_STALE_MAX_S:
        p = "STALE"
    else:
        p = "OFFLINE"
    return {
        "presence": p,
        "age_seconds": age,
        "threshold_online_max_s": _PRESENCE_ONLINE_MAX_S,
        "threshold_stale_max_s": _PRESENCE_STALE_MAX_S,
    }


def work_state_truthful(reported: str | None,
                        presence: str) -> dict[str, Any]:
    """Compute the truthful work_state from a reported work_state
    and a presence classification.

    Rules:
      presence == OFFLINE: never claim a current WORKING/BLOCKED.
      presence == UNKNOWN: same — no telemetry at all.
      presence == STALE:  preserve reported value but mark
        telemetry_freshness=STALE.
      presence == ONLINE:  pass through.
    """
    rep = reported if reported in WORK_STATES else "UNKNOWN"
    if presence in ("OFFLINE", "UNKNOWN"):
        return {
            "work_state":            "UNKNOWN",
            "last_reported_work_state": rep,
            "telemetry_freshness":   presence,
        }
    # STALE or ONLINE
    return {
        "work_state":            rep,
        "last_reported_work_state": rep,
        "telemetry_freshness":   presence,
    }


def normalize_work_state(value: Any) -> Optional[str]:
    """Validate a client-supplied work_state. Returns the canonical
    value or None if invalid. Empty string and None both normalize
    to None (caller decides whether to skip or clear)."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    if v == "":
        return None
    if v in WORK_STATES:
        return v
    return None


# Heartbeat field length caps. Tuned for copy/paste from an agent
# supervisor process; no need to support paragraphs.
_FIELD_MAX_LEN = {
    "current_task_id": 128,
    "current_project": 256,
    "current_blocker": 512,
}


def validate_heartbeat_field(name: str, value: Any) -> tuple[bool, str | None]:
    """Returns (ok, normalized). normalized is the value to write
    to the DB, or None if the caller should skip / clear the field.
    Validation rules:
      - missing or None: ok=True, normalized=None (caller skips)
      - non-string: ok=False
      - empty string: ok=True, normalized=None (semantic: clear)
      - too long: ok=False
    """
    if name not in _FIELD_MAX_LEN:
        return False, None
    if value is None:
        return True, None
    if not isinstance(value, str):
        return False, None
    if len(value) > _FIELD_MAX_LEN[name]:
        return False, None
    if value == "":
        # Empty string = explicit clear. We treat it the same as
        # None (caller writes NULL). This is the documented null
        # / clear behaviour: an empty string means "no value".
        return True, None
    return True, value
