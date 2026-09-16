"""
Pluribus graceful NumPy degradation module.

Provides a safe numpy import that prevents startup crashes when NumPy
is incompatible (e.g., built with X86_V2 baseline on older CPUs).

Usage: Import this module early in the application lifecycle (before
any other pluribus modules that unconditionally import numpy).

Typical usage in main.py or app startup:
    from pluribus._numpy_fallback import try_import_numpy, NUMPY_AVAILABLE

If NUMPY_AVAILABLE is False, all numpy-dependent features degrade
gracefully (semantic search disabled, embeddings return zeros, etc.)
without crashing the entire app at import time.
"""

import sys
from typing import Any


# ── Module-level state ────────────────────────────────────────────────

NUMPY_AVAILABLE: bool = False
np: Any = None  # type: ignore


def try_import_numpy() -> bool:
    """Attempt to import numpy; set module-level state and return success.

    Returns True if numpy was successfully imported, False otherwise.
    When False, subsequent numpy-dependent operations will degrade
    gracefully instead of crashing at import time.
    """
    global NUMPY_AVAILABLE, np
    try:
        import numpy as _np  # noqa: F811
        # Verify it actually works with a minimal operation
        _np.zeros(1)
        NUMPY_AVAILABLE = True
        np = _np
        return True
    except (ImportError, RuntimeError, ValueError):
        NUMPY_AVAILABLE = False
        np = _DummyNumpy()
        return False


class _DummyNumpy:
    """Minimal stub that raises clear errors when numpy features are used.

    Any attempt to use actual numpy functionality will raise
    ImportError with a clear message, preventing silent failures.
    """

    def __getattr__(self, name: str) -> Any:
        raise ImportError(
            "NumPy not available (incompatible build for this CPU). "
            "NumPy-dependent features are disabled. "
            "Install a compatible numpy or update CPU drivers."
        )

    def __dir__(self) -> list[str]:
        return []


# Attempt the import immediately
_ = try_import_numpy()

# Re-export numpy name for backwards compatibility when available
if NUMPY_AVAILABLE:
    sys.modules[__name__].np = np  # noqa: F811