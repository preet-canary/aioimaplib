from __future__ import annotations

import os
import ssl
import time
from uuid import uuid4

import pytest

from aioimaplib import IMAP4, IMAP4_SSL
from aioimaplib.client import Commands

ALL_STATES = ("NONAUTH", "AUTH", "SELECTED", "LOGOUT", "IDLING")

# Pinned to upstream CPython snapshot in repo (imaplib/imaplib.py)
EXPECTED_COMMANDS = {
    "APPEND": ("AUTH", "SELECTED"),
    "AUTHENTICATE": ("NONAUTH",),
    "CAPABILITY": ("NONAUTH", "AUTH", "SELECTED", "LOGOUT"),
    "CHECK": ("SELECTED",),
    "CLOSE": ("SELECTED",),
    "COPY": ("SELECTED",),
    "CREATE": ("AUTH", "SELECTED"),
    "DELETE": ("AUTH", "SELECTED"),
    "DELETEACL": ("AUTH", "SELECTED"),
    "ENABLE": ("AUTH",),
    "EXAMINE": ("AUTH", "SELECTED"),
    "EXPUNGE": ("SELECTED",),
    "FETCH": ("SELECTED",),
    "GETACL": ("AUTH", "SELECTED"),
    "GETANNOTATION": ("AUTH", "SELECTED"),
    "GETQUOTA": ("AUTH", "SELECTED"),
    "GETQUOTAROOT": ("AUTH", "SELECTED"),
    "IDLE": ("AUTH", "SELECTED"),
    "MYRIGHTS": ("AUTH", "SELECTED"),
    "LIST": ("AUTH", "SELECTED"),
    "LOGIN": ("NONAUTH",),
    "LOGOUT": ("NONAUTH", "AUTH", "SELECTED", "LOGOUT"),
    "LSUB": ("AUTH", "SELECTED"),
    "MOVE": ("SELECTED",),
    "NAMESPACE": ("AUTH", "SELECTED"),
    "NOOP": ("NONAUTH", "AUTH", "SELECTED", "LOGOUT"),
    "PARTIAL": ("SELECTED",),
    "PROXYAUTH": ("AUTH",),
    "RENAME": ("AUTH", "SELECTED"),
    "SEARCH": ("SELECTED",),
    "SELECT": ("AUTH", "SELECTED"),
    "SETACL": ("AUTH", "SELECTED"),
    "SETANNOTATION": ("AUTH", "SELECTED"),
    "SETQUOTA": ("AUTH", "SELECTED"),
    "SORT": ("SELECTED",),
    "STARTTLS": ("NONAUTH",),
    "STATUS": ("AUTH", "SELECTED"),
    "STORE": ("SELECTED",),
    "SUBSCRIBE": ("AUTH", "SELECTED"),
    "THREAD": ("SELECTED",),
    "UID": ("SELECTED",),
    "UNSUBSCRIBE": ("AUTH", "SELECTED"),
    "UNSELECT": ("SELECTED",),
}


def _get_live_config():
    user = os.getenv("AIOIMAPLIB_FASTMAIL_USER")
    password = os.getenv("AIOIMAPLIB_FASTMAIL_PASSWORD")
    host = os.getenv("AIOIMAPLIB_FASTMAIL_IMAP_HOST", "imap.fastmail.com")
    port = int(os.getenv("AIOIMAPLIB_FASTMAIL_IMAP_PORT", "993"))
    starttls_port = int(os.getenv("AIOIMAPLIB_FASTMAIL_IMAP_STARTTLS_PORT", "143"))
    if not user or not password:
        pytest.skip("Live Fastmail credentials are not set in environment")
    return user, password, host, port, starttls_port


def _mailbox_name(prefix: str) -> str:
    return f"AIOIMAPLIB_{prefix}_{int(time.time())}_{uuid4().hex[:8]}"


STATE_MATRIX = tuple(
    (command, state, state in EXPECTED_COMMANDS[command])
    for command in sorted(EXPECTED_COMMANDS)
    for state in ALL_STATES
)


@pytest.mark.parametrize(
    ("command", "state", "allowed"),
    STATE_MATRIX,
    ids=[f"{cmd}:{st}" for cmd, st, _ in STATE_MATRIX],
)
def test_command_state_matrix(command, state, allowed):
    assert command in Commands
    assert set(Commands[command]).issubset(set(ALL_STATES))
    assert (state in Commands[command]) is allowed


def test_command_table_complete_and_exact():
    assert set(Commands) == set(EXPECTED_COMMANDS)
    for command, states in EXPECTED_COMMANDS.items():
        assert tuple(Commands[command]) == tuple(states)


async def test_fastmail_attempt_all_commands_live():
    user, password, host, port, _starttls_port = _get_live_config()
    ssl_context = ssl.create_default_context()

    attempted = set()
    succeeded = set()
    failed: dict[str, str] = {}

    client = IMAP4_SSL(host, port, ssl_context=ssl_context, timeout=30)
    box_a = _mailbox_name("BOX_A")
    box_b = _mailbox_name("BOX_B")
    box_renamed = box_a + "_RENAMED"
    seq = "1"

    async def _attempt(name: str, coro, *, must_succeed: bool = False,
                       ok_types: set[str] = frozenset({"OK"})):
        attempted.add(name)
        try:
            result = await coro
            if isinstance(result, tuple):
                typ = result[0]
                assert typ in {"OK", "NO", "BYE", "BAD"}, f"{name}: unexpected type {typ!r}"
                if typ in ok_types:
                    succeeded.add(name)
                    return True
                # Server handled the command but denied/rejected it
                if must_succeed:
                    failed[name] = f"expected {ok_types}, got {typ}"
                return False
            succeeded.add(name)
            return True
        except (IMAP4.error, IMAP4.abort, OSError) as exc:
            if must_succeed:
                failed[name] = str(exc)
            return False

    try:
        await _attempt("CAPABILITY", client.capability(), must_succeed=True)
        await _attempt("LOGIN", client.login(user, password), must_succeed=True)
        await _attempt("CAPABILITY", client.capability(), must_succeed=True)

        await _attempt("CREATE", client.create(box_a), must_succeed=True)
        await _attempt("CREATE", client.create(box_b), must_succeed=True)
        await _attempt("LIST", client.list(), must_succeed=True)
        await _attempt("LSUB", client.lsub(), must_succeed=True)
        await _attempt("NAMESPACE", client.namespace(), must_succeed=True)
        await _attempt("STATUS", client.status("INBOX", "(MESSAGES UIDNEXT UIDVALIDITY)"), must_succeed=True)
        await _attempt("MYRIGHTS", client.myrights("INBOX"), must_succeed=True)
        await _attempt("SUBSCRIBE", client.subscribe(box_a), must_succeed=True)
        await _attempt("UNSUBSCRIBE", client.unsubscribe(box_a), must_succeed=True)
        await _attempt("GETACL", client.getacl("INBOX"))
        await _attempt("DELETEACL", client.deleteacl("INBOX", user))
        await _attempt("SETACL", client.setacl("INBOX", user, "lr"))
        await _attempt("GETQUOTA", client.getquota(""))
        await _attempt("GETQUOTAROOT", client.getquotaroot("INBOX"))
        await _attempt("SETQUOTA", client.setquota("", "(STORAGE 1000)"))
        await _attempt("GETANNOTATION", client.getannotation("INBOX", "/comment", "value"))
        await _attempt("SETANNOTATION", client.setannotation("INBOX", "/comment", "value"))
        await _attempt("PROXYAUTH", client.proxyauth(user))
        await _attempt("ENABLE", client.enable("UTF8=ACCEPT"))

        await _attempt("SELECT", client.select(box_a), must_succeed=True)
        await _attempt("APPEND", client.append(box_a, None, None, b"Subject: one\r\n\r\nbody"), must_succeed=True)
        await _attempt("CHECK", client.check(), must_succeed=True)
        await _attempt("SEARCH", client.search(None, "ALL"), must_succeed=True)

        typ, data = await client.search(None, "ALL")
        if typ == "OK" and data and data[0]:
            seq = data[0].split()[-1].decode()

        await _attempt("FETCH", client.fetch(seq, "(UID FLAGS RFC822.SIZE)"), must_succeed=True)
        await _attempt("STORE", client.store(seq, "+FLAGS", "\\Seen"), must_succeed=True)
        await _attempt("COPY", client.copy(seq, box_b), must_succeed=True)
        await _attempt("PARTIAL", client.partial(seq, "BODY[TEXT]", "0", "16"))
        await _attempt("SORT", client.sort("DATE", "UTF-8", "ALL"))
        await _attempt("THREAD", client.thread("REFERENCES", "UTF-8", "ALL"))
        await _attempt("UID", client.uid("FETCH", seq, "(UID FLAGS)"), must_succeed=True)
        await _attempt("UID", client.uid("SEARCH", None, "ALL"), must_succeed=True)
        await _attempt("UID", client.uid("SORT", "(DATE)", "UTF-8", "ALL"))
        await _attempt("UID", client.uid("THREAD", "REFERENCES", "UTF-8", "ALL"))
        await _attempt("MOVE", client.xatom("MOVE", seq, box_b))

        # IDLE has custom flow: count as attempted even if capability/policy denies it.
        attempted.add("IDLE")
        try:
            async with client.idle(0.3) as idler:
                try:
                    await anext(idler)
                except StopAsyncIteration:
                    pass
            succeeded.add("IDLE")
        except (IMAP4.error, IMAP4.abort, OSError):
            pass

        await _attempt("NOOP", client.noop(), must_succeed=True)
        await _attempt("EXPUNGE", client.expunge(), must_succeed=True)
        await _attempt("UNSELECT", client.unselect(), must_succeed=True)

        # Use select(readonly=True) so is_readonly is set properly,
        # preventing the READ-ONLY untagged response from raising readonly.
        await _attempt("EXAMINE", client.select(box_a, readonly=True), must_succeed=True)
        await _attempt("CLOSE", client.close(), must_succeed=True)

        await _attempt("RENAME", client.rename(box_a, box_renamed), must_succeed=True)
        await _attempt("RENAME", client.rename(box_renamed, box_a), must_succeed=True)

        await _attempt("DELETE", client.delete(box_b), must_succeed=True)
        await _attempt("DELETE", client.delete(box_a), must_succeed=True)

        await _attempt("LOGOUT", client.logout(), must_succeed=True, ok_types={"BYE"})
    finally:
        await client.aclose()

    # AUTHENTICATE and STARTTLS require NONAUTH on dedicated fresh sessions.
    auth_client = IMAP4_SSL(host, port, ssl_context=ssl_context, timeout=30)
    try:
        attempted.add("AUTHENTICATE")
        try:
            await auth_client.authenticate("PLAIN", lambda _r: f"\0{user}\0{password}".encode())
            succeeded.add("AUTHENTICATE")
        except (IMAP4.error, IMAP4.abort, OSError):
            pass
    finally:
        await auth_client.aclose()

    # STARTTLS should be attempted from a non-SSL connection on port 143.
    # On some networks/servers it may be unavailable; attempt is still recorded.
    starttls_client = IMAP4(host, _starttls_port, timeout=30)
    try:
        attempted.add("STARTTLS")
        try:
            await starttls_client.starttls()
            succeeded.add("STARTTLS")
        except (IMAP4.error, IMAP4.abort, OSError):
            pass
    except (IMAP4.error, IMAP4.abort, OSError):
        pass
    finally:
        await starttls_client.aclose()

    # Every command in the table must have been attempted.
    missing = set(EXPECTED_COMMANDS) - attempted
    assert not missing, f"Commands not attempted on live server: {sorted(missing)}"

    # Core commands that should have succeeded must not have failed.
    assert not failed, f"Core commands failed on live server: {failed}"
