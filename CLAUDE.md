# CLAUDE.md

## Project Overview

`aioimaplib` is a native asyncio IMAP4 client for Python 3.11+, ported from CPython's stdlib `imaplib`. It does **not** wrap `imaplib` in an executor -- all protocol handling is async-native using `asyncio.open_connection` / `StreamReader` / `StreamWriter`.

## Quick Reference

```bash
# Run all local tests (241 tests, no credentials needed)
python -m pytest tests/ -v

# Run with live Fastmail integration tests (243 tests)
set -a && source .env && set +a && python -m pytest tests/ -v
```

## Repository Layout

```
src/aioimaplib/
  __init__.py              # Public exports + __getattr__ for __version__ deprecation
  client.py                # Core implementation (~1150 lines): IMAP4, IMAP4_SSL,
                           #   IMAP4_stream, AsyncIdler, _Authenticator, all 43 commands

tests/
  conftest.py              # Custom pytest hook -- runs async tests via asyncio.run()
                           #   (no pytest-asyncio dependency)
  test_aioimaplib.py       # 25 behavior tests (auth, IDLE, enable, errors, concurrency)
  test_imap_commands_matrix.py  # 216 state matrix tests + live Fastmail command sweep
  test_fastmail_live.py    # Live Fastmail login/select/logout smoke test
  helpers/
    imap_server.py         # Thread-based TCP test IMAP servers (12 handler variants)

imaplib/
  imaplib.py               # Pinned upstream CPython snapshot (source of truth)
  test_imaplib.py          # Upstream test reference
  README.md                # Contains upstream commit SHA
```

## Upstream Parity

All behavior traces back to CPython `imaplib.py` at commit SHA `6262704b134db2a4ba12e85ecfbd968534f28b45`. The snapshot lives in `imaplib/imaplib.py`. When comparing behavior or fixing bugs, always reference this file as the source of truth.

## Architecture

### Lazy Connection
Unlike stdlib which connects in `__init__`, the async version defers TCP/TLS handshake to the first command via `_simple_command()` → `connect()`. This means:
- The constructor is synchronous and instant
- `self.capabilities` is empty until after `connect()`
- Any method that checks capabilities before calling `_simple_command` must call `await self.connect()` first (see `enable()`, `starttls()`)

### Literal Passing via `_literal` Keyword
Command methods that need to send literal data (APPEND, AUTHENTICATE) pass it to `_simple_command()` via a `_literal=` keyword argument. Inside `_simple_command`, the literal is assigned to `self.literal` only after acquiring `_command_lock`, ensuring concurrent commands cannot corrupt each other's payloads. The `_command()` method then reads and clears `self.literal` to send the data on the wire.

### Timeout Behavior
The `timeout` constructor parameter applies to both connection establishment and all subsequent I/O operations (reads and writes), matching upstream where `socket.settimeout()` persists for the socket's lifetime. The `_UNSET` sentinel distinguishes "no timeout argument" (fall back to `self.timeout`) from an explicit `None` (no timeout).

### Locking
- **`_command_lock`** (`asyncio.Lock`): Serializes command execution. Acquired in `_simple_command()` around `_command()` + `_command_complete()`.
- **`_connect_lock`** (`asyncio.Lock`): Prevents concurrent connection attempts.

### AsyncIdler (IDLE)
`AsyncIdler` is an async context manager and async iterator. On enter it acquires `_command_lock` (and holds it for the duration). On exit it sends `DONE\r\n` and completes the tagged response. OSError during exit is suppressed if there's already an active exception (matching upstream behavior).

### _Authenticator / Literator Pattern
In `_command()`, if `self.literal` is `callable()`, it's treated as a "literator" -- called with continuation response data to produce auth challenge responses (used by AUTHENTICATE/CRAM-MD5). The callable check uses `callable(literal)` (not type comparison, since coroutine functions differ from bound methods).

## Key Async Divergences from Upstream

1. All command methods are `async def` and must be awaited
2. `__aenter__`/`__aexit__` instead of `__enter__`/`__exit__`
3. `aclose()` for cleanup (closes StreamWriter)
4. `Idler` → `AsyncIdler` with `async for` iteration
5. Constructor does not connect; connection is lazy
6. Literal data passed via `_literal=` keyword to `_simple_command`, assigned inside lock (not via shared state before call)
7. Timeout applied to all I/O via `asyncio.wait_for` (upstream uses persistent `socket.settimeout`)

## Tests

### Running Tests
```bash
# All local tests
python -m pytest tests/ -v

# Single test
python -m pytest tests/test_aioimaplib.py::test_login_cram_md5_str_and_bytes -v

# With live server tests (needs .env with Fastmail credentials)
set -a && source .env && set +a && python -m pytest tests/ -v
```

### Test Infrastructure
- **No pytest-asyncio**: Tests use a custom `conftest.py` hook that detects coroutine functions and runs them with `asyncio.run()`.
- **Thread-based test servers**: `tests/helpers/imap_server.py` provides 12 handler variants (SimpleIMAPHandler, AuthHandlerCRAMMD5, IdleCmdHandler, etc.) that run in background threads. Tests spin up a server, get a port, and connect the async client to localhost.
- **State matrix tests**: `test_imap_commands_matrix.py` parametrizes all 43 commands × 5 states (NONAUTH, AUTH, SELECTED, LOGOUT, IDLING) = 216 tests, verifying each command is allowed/rejected in the correct states.

### Live Tests
Require environment variables (from `.env`):
```
AIOIMAPLIB_FASTMAIL_USER=...
AIOIMAPLIB_FASTMAIL_PASSWORD=...
AIOIMAPLIB_FASTMAIL_IMAP_HOST=imap.fastmail.com   # optional, this is the default
AIOIMAPLIB_FASTMAIL_IMAP_PORT=993                  # optional, this is the default
```

### Current Test Results
- **243 passed**, 0 skipped (with credentials), 2 warnings (expected `__version__` deprecation)
- 25 behavior tests + 216 state matrix tests + 1 command table completeness test + 1 live Fastmail sweep

## Common Pitfalls

1. **Capability checks before connection**: Any method that reads `self.capabilities` before its `_simple_command` call must `await self.connect()` first. Currently `enable()` and `starttls()` do this.

2. **Literal concurrency**: Literal data must be passed via `_literal=` keyword to `_simple_command` and assigned inside the lock. Never set `self.literal` outside the lock -- concurrent tasks would corrupt each other's payloads.

3. **`callable()` not `type()` for literator detection**: In async, `self._command` is a coroutine function while `_Authenticator.process` is a regular bound method. `type(literal) is type(self._command)` always returns False. Use `callable(literal)`.

4. **IDLE duration requires a socket**: `AsyncIdler.__init__` rejects `duration` when `socket()` is None (covers both pre-connect and `IMAP4_stream` which has no socket). This matches upstream `Idler.__init__`.

5. **Lock ordering**: `connect()` must be called **before** acquiring `_command_lock` in `_simple_command()`, otherwise `_get_capabilities()` → `capability()` → `_simple_command()` would deadlock.

6. **AsyncIdler OSError suppression**: When `__aexit__` has an active exception (`exc_type` is set), OSError from sending DONE or completing the tagged response is suppressed (matching upstream `Idler.__exit__` behavior).

## Future Work

- Port remaining upstream test cases from `imaplib/test_imaplib.py`
- Differential transcript test harness (compare sync vs async on identical server scripts)
- Hypothesis-based fuzz tests for response fragmentation
- CI matrix: Python 3.11, 3.12, 3.13, 3.14-dev on Linux + macOS
- Coverage targets: 100% branch on protocol/IDLE paths, >= 95% overall
- Cancellation safety tests (cancel during connect/login/idle)
