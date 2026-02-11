# aioimaplib

A native **asyncio** IMAP4 client for Python 3.11+, ported from CPython's stdlib `imaplib`.

Unlike wrapper libraries that run blocking code in an executor, `aioimaplib` is built from the ground up on `asyncio.open_connection` / `StreamReader` / `StreamWriter`. Every byte of protocol handling is async-native, giving you true non-blocking IMAP with no hidden thread pools.

## Features

- **Full protocol parity** with CPython `imaplib` (pinned to upstream SHA `6262704`), including all 43 IMAP commands, state machine, response parsing, literal handling, and untagged response semantics.
- **`IMAP4`**, **`IMAP4_SSL`**, and **`IMAP4_stream`** classes mirroring their stdlib counterparts.
- **IDLE support** via `AsyncIdler` -- an async context manager and async iterator for real-time mailbox notifications.
- **CRAM-MD5 authentication** with the same `_Authenticator` challenge-response flow as stdlib.
- **STARTTLS** upgrade with automatic capability refresh.
- **ENABLE / UTF-8** mode support with charset enforcement.
- **Lazy connection** -- the TCP/TLS handshake is deferred until the first command, so constructing a client is instant and non-blocking.
- **Command serialization** via `asyncio.Lock`, ensuring protocol correctness without manual synchronization.
- **Zero dependencies** -- only the Python standard library.

## Quick Start

```python
import asyncio
import ssl
from aioimaplib import IMAP4_SSL

async def main():
    ctx = ssl.create_default_context()
    client = IMAP4_SSL("imap.fastmail.com", 993, ssl_context=ctx)

    await client.login("user@example.com", "password")

    typ, data = await client.select("INBOX")
    print(f"INBOX has {data[0]} messages")

    typ, msgs = await client.search(None, "UNSEEN")
    print(f"Unseen: {msgs[0]}")

    await client.logout()
    await client.aclose()

asyncio.run(main())
```

### Context Manager

```python
async with IMAP4_SSL("imap.example.com", 993, ssl_context=ctx) as client:
    await client.login("user", "password")
    await client.select("INBOX")
    # ... work with mail ...
# logout + aclose handled automatically
```

### IDLE (Real-Time Notifications)

```python
await client.select("INBOX")

async with client.idle(duration=300) as idler:
    async for typ, data in idler:
        print(f"Server pushed: {typ} {data}")
        if some_condition:
            break  # sends DONE automatically on exit
```

### CRAM-MD5 Authentication

```python
await client.login_cram_md5("user", "secret")
```

## Installation

```bash
pip install aioimaplib
```

Or install from source:

```bash
git clone https://github.com/your-org/aioimaplib.git
cd aioimaplib
pip install -e .
```

Requires **Python 3.11+**. No third-party dependencies.

## Supported Commands

All 43 IMAP commands from the upstream `imaplib` command table are supported, each respecting the correct protocol state (`NONAUTH`, `AUTH`, `SELECTED`, `LOGOUT`):

| Command | States | Command | States |
|---------|--------|---------|--------|
| `append` | AUTH, SELECTED | `namespace` | AUTH, SELECTED |
| `authenticate` | NONAUTH | `noop` | * |
| `capability` | * | `partial` | SELECTED |
| `check` | SELECTED | `proxyauth` | AUTH |
| `close` | SELECTED | `rename` | AUTH, SELECTED |
| `copy` | SELECTED | `search` | SELECTED |
| `create` | AUTH, SELECTED | `select` | AUTH, SELECTED |
| `delete` | AUTH, SELECTED | `setacl` | AUTH, SELECTED |
| `deleteacl` | AUTH, SELECTED | `setannotation` | AUTH, SELECTED |
| `enable` | AUTH | `setquota` | AUTH, SELECTED |
| `examine` | AUTH, SELECTED | `sort` | SELECTED |
| `expunge` | SELECTED | `starttls` | NONAUTH |
| `fetch` | SELECTED | `status` | AUTH, SELECTED |
| `getacl` | AUTH, SELECTED | `store` | SELECTED |
| `getannotation` | AUTH, SELECTED | `subscribe` | AUTH, SELECTED |
| `getquota` | AUTH, SELECTED | `thread` | SELECTED |
| `getquotaroot` | AUTH, SELECTED | `uid` | SELECTED |
| `idle` | AUTH, SELECTED | `unselect` | SELECTED |
| `list` | AUTH, SELECTED | `unsubscribe` | AUTH, SELECTED |
| `login` | NONAUTH | `xatom` | *(custom)* |
| `login_cram_md5` | NONAUTH | `lsub` | AUTH, SELECTED |
| `logout` | * | `move` | SELECTED |
| `myrights` | AUTH, SELECTED | | |

## API Reference

### Classes

| Class | Description |
|-------|-------------|
| `IMAP4(host, port, timeout)` | Plain IMAP4 client (port 143) |
| `IMAP4_SSL(host, port, ssl_context, timeout)` | IMAP4 over TLS (port 993) |
| `IMAP4_stream(command, timeout)` | IMAP4 over a subprocess pipe |
| `AsyncIdler` | Async context manager / iterator for IDLE |

### Return Values

All command methods return the familiar `(typ, [data, ...])` tuple, exactly as stdlib `imaplib`:

```python
typ, data = await client.select("INBOX")
# typ = "OK"
# data = [b"42"]  (message count)
```

### Exception Hierarchy

```
IMAP4.error             -- command-level errors
  IMAP4.abort           -- connection/protocol failures
    IMAP4.readonly      -- mailbox is read-only
```

## Testing

### Run the full local test suite

```bash
python -m pytest tests/ -v
```

This runs **242 tests** using built-in TCP test servers (no external services needed):
- 26 behavior tests (auth, IDLE, enable, error handling, concurrency, etc.)
- 216 command-state matrix tests (every command x every protocol state)

### Run with live Fastmail tests

Create a `.env` file:

```bash
AIOIMAPLIB_FASTMAIL_USER=you@fastmail.com
AIOIMAPLIB_FASTMAIL_PASSWORD=your-app-password
```

Then:

```bash
set -a && source .env && set +a
python -m pytest tests/ -v
```

This adds 2 live integration tests that exercise login/select/logout and all 43 commands against a real Fastmail server.

**Full result: 244 passed.**

## Project Structure

```
aioimaplib/
  src/aioimaplib/
    __init__.py          # Public exports
    client.py            # Core implementation (~1150 lines)
  tests/
    conftest.py          # pytest-asyncio config + sys.path setup
    test_aioimaplib.py   # 25 behavior tests
    test_imap_commands_matrix.py  # 216 state matrix + live sweep
    test_fastmail_live.py         # Live Fastmail smoke test
    helpers/
      imap_server.py     # Thread-based TCP test IMAP servers
  imaplib/
    imaplib.py           # Pinned upstream CPython snapshot
    test_imaplib.py      # Upstream test reference
    README.md            # Upstream commit SHA
  pyproject.toml
  CLAUDE.md
```

## Design Decisions

**Why not wrap `imaplib` in an executor?** Executor-based wrappers add thread overhead, prevent true cancellation, and make IDLE polling awkward. A native asyncio implementation gives you proper `async for` on IDLE, real cancellation semantics, and zero thread-pool contention.

**Why pin to a CPython commit?** The upstream `imaplib.py` snapshot (SHA `6262704`) serves as the single source of truth for protocol behavior. Every regex, state transition, and response-parsing rule traces back to this snapshot, making behavioral parity verifiable.

**Lazy connection:** Unlike stdlib which connects in `__init__`, the async version defers the TCP/TLS handshake to the first command call. This avoids forcing users to `await` construction and keeps the constructor signature compatible.

## License

PSF-2.0 (Python Software Foundation License)
