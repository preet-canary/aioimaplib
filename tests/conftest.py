"""Async test runner hook for pytest.

We use asyncio.run() directly instead of pytest-asyncio for two reasons:

1. Zero external test dependencies — pytest is all that's needed.
2. Each test gets a fresh event loop via asyncio.run(), which matches the
   typical user-facing usage pattern and prevents cross-test loop leakage.

Trade-off: this approach does not support async fixtures.  If async fixtures
become necessary, switch to pytest-asyncio with ``asyncio_mode = "auto"``.
"""

import pathlib
import sys
import inspect
import asyncio

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_pyfunc_call(pyfuncitem):
    test_func = pyfuncitem.obj
    if inspect.iscoroutinefunction(test_func):
        kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(test_func(**kwargs))
        return True
    return None
