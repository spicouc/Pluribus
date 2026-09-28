"""Regression tests for pluribus._numpy_fallback graceful degradation."""
from __future__ import annotations

import builtins
import sys
import unittest


def _numpy_runtime_error() -> RuntimeError:
    return RuntimeError(
        "NumPy was built with baseline optimizations: (X86_V2) but your "
        "machine doesn't support: (X86_V2)."
    )


class NumpyFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Pluribus must start and core CRUD/auth paths must remain functional
    even when NumPy is completely unavailable at import time."""

    def setUp(self) -> None:
        self._original_import = builtins.__import__
        for mod in list(sys.modules):
            if mod.startswith("pluribus"):
                del sys.modules[mod]

    def tearDown(self) -> None:
        builtins.__import__ = self._original_import
        for mod in list(sys.modules):
            if mod.startswith("pluribus"):
                del sys.modules[mod]

    def _mock_numpy_import(self, error: Exception) -> None:
        original = builtins.__import__
        def _fake_import(name, *args, **kwargs):
            if name == "numpy":
                raise error
            return original(name, *args, **kwargs)
        builtins.__import__ = _fake_import

    def test_fallback_module_loads_without_numpy(self) -> None:
        """_numpy_fallback.py must import even when numpy raises RuntimeError."""
        self._mock_numpy_import(_numpy_runtime_error())
        try:
            import pluribus._numpy_fallback as nf
            self.assertFalse(nf.NUMPY_AVAILABLE)
            self.assertIsNotNone(nf.np)
        finally:
            builtins.__import__ = self._original_import

    def test_dummy_numpy_raises_import_error_on_use(self) -> None:
        """Accessing any attribute on _DummyNumpy must raise ImportError
        with a clear message — never AttributeError or silent None."""
        self._mock_numpy_import(_numpy_runtime_error())
        try:
            import pluribus._numpy_fallback as nf
            with self.assertRaises(ImportError) as ctx:
                _ = nf.np.zeros(1)
            self.assertIn("NumPy not available", str(ctx.exception))
        finally:
            builtins.__import__ = self._original_import

    def test_try_import_numpy_returns_false_on_runtime_error(self) -> None:
        """try_import_numpy() must return False when numpy raises RuntimeError."""
        self._mock_numpy_import(_numpy_runtime_error())
        try:
            from pluribus._numpy_fallback import try_import_numpy
            result = try_import_numpy()
            self.assertFalse(result)
        finally:
            builtins.__import__ = self._original_import

    def test_pluribus_main_imports_without_numpy(self) -> None:
        """pluribus.main must import successfully even when numpy is unavailable."""
        self._mock_numpy_import(_numpy_runtime_error())
        try:
            import pluribus.main
        finally:
            builtins.__import__ = self._original_import

    def test_core_modules_import_without_numpy(self) -> None:
        """Core modules that depend on numpy must import without crashing."""
        self._mock_numpy_import(_numpy_runtime_error())
        try:
            import pluribus.config
            import pluribus.db
            import pluribus.embedding
            import pluribus.memory
            import pluribus.worker
            import pluribus.recall
            import pluribus.semantic_async
            import pluribus.notion
            import pluribus.contradiction
            import pluribus.query_save
            import pluribus.library_indexer
            import pluribus.document_vector_index
            import pluribus.vector_index
            import pluribus.mcp
        finally:
            builtins.__import__ = self._original_import


if __name__ == "__main__":
    unittest.main()