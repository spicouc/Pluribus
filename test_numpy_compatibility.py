import sys
import builtins
import importlib

# Mock numpy to raise RuntimeError on import
_real_import = builtins.__import__
def _mock_import(name, *args, **kwargs):
    if name == 'numpy':
        raise RuntimeError("NumPy was built with baseline optimizations: (X86_V2) but your machine doesn't support: (X86_V2).")
    return _real_import(name, *args, **kwargs)

builtins.__import__ = _mock_import

# Load pluribus modules after numpy mock is in place
sys.path.insert(0, '/root/Pluribus')
try:
    import pluribus._numpy_fallback as nf
    import pluribus.main as main
    import pluribus._numpy_fallback as nf
    print("NUMPY_AVAILABLE:", nf.NUMPY_AVAILABLE)
    print("np type:", type(nf.np).__name__)
    
    # Test that core functionality works
    import pluribus.main as main
    print("pluribus.main imported successfully")
    
    # Test that vector_index can be imported
    import pluribus.vector_index as vec_index
    print("vector_index imported successfully")
    
    # Test that turbovec imports (this was failing)
    import turbovec
    print("turbovec imported successfully")
    
    print("✅ All critical paths verified - NumPy graceful degradation is working")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")