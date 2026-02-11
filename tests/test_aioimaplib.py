from __future__ import annotations

import asyncio
import calendar
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

import aioimaplib
from aioimaplib import IMAP4
import aioimaplib.client as client_mod
from tests.helpers.imap_server import (
    AuthHandlerCRAMMD5,
    BadTerminationHandler,
    EnableHandler,
    IdleCmdDelayedPacketHandler,
    IdleCmdDenyHandler,
    IdleCmdHandler,
    LsubHandler,
    NoWelcomeHandler,
    SimpleIMAPHandler,
    TooLongLineHandler,
    TruncatedLiteralHandler,
    get_unused_port,
    run_server,
)


@asynccontextmanager
async def open_client(*address):
    client = IMAP4(*address)
    try:
        yield client
    finally:
        await client.aclose()


def test_internaldate2tuple_parity():
    t0 = calendar.timegm((2000, 1, 1, 0, 0, 0, -1, -1, -1))
    tt = aioimaplib.Internaldate2tuple(b'25 (INTERNALDATE "01-Jan-2000 00:00:00 +0000")')
    assert time.mktime(tt) == t0
    tt = aioimaplib.Internaldate2tuple(b'25 (INTERNALDATE "01-Jan-2000 11:30:00 +1130")')
    assert time.mktime(tt) == t0
    tt = aioimaplib.Internaldate2tuple(b'25 (INTERNALDATE "31-Dec-1999 12:30:00 -1130")')
    assert time.mktime(tt) == t0


def test_time2internaldate_parity():
    dt = datetime.fromtimestamp(2_000_000_000, timezone(timedelta(hours=2)))
    assert aioimaplib.Time2Internaldate(dt) == '"18-May-2033 05:33:20 +0200"'


async def test_login_and_logout():
    with run_server(SimpleIMAPHandler) as server:
        async with open_client(*server.server_address) as client:
            typ, data = await client.login("user", "pass")
            assert typ == "OK"
            assert data[0] == b"LOGIN completed"
            assert server.logged == "user"

            typ, data = await client.logout()
            assert typ == "BYE"
            assert data[0] == b"IMAP4ref1 Server logging out"
            assert client.state == "LOGOUT"


async def test_async_context_manager_logs_out():
    with run_server(SimpleIMAPHandler) as server:
        async with IMAP4(*server.server_address) as client:
            typ, _ = await client.login("user", "pass")
            assert typ == "OK"
            assert server.logged == "user"
        assert server.logged is None


async def test_lsub_and_unselect():
    with run_server(LsubHandler) as server:
        async with open_client(*server.server_address) as client:
            await client.login("user", "pass")
            typ, data = await client.lsub()
            assert typ == "OK"
            assert data[0] == b'() "." directoryA'

            typ, data = await client.select()
            assert typ == "OK"
            assert data[0] == b"2"

            typ, data = await client.unselect()
            assert typ == "OK"
            assert data[0] == b"Returned to authenticated state. (Success)"


async def test_enable_not_supported():
    with run_server(SimpleIMAPHandler) as server:
        async with open_client(*server.server_address) as client:
            await client.login("user", "pass")
            with pytest.raises(IMAP4.error, match="Server does not support ENABLE"):
                await client.enable("UTF8=ACCEPT")


async def test_enable_disallowed_if_not_auth_state():
    with run_server(EnableHandler) as server:
        async with open_client(*server.server_address) as client:
            with pytest.raises(IMAP4.error, match="illegal in state NONAUTH"):
                await client.enable("UTF8=ACCEPT")


async def test_enable_utf8_blocks_search_charset():
    with run_server(EnableHandler) as server:
        async with open_client(*server.server_address) as client:
            await client.login("user", "pass")
            typ, _ = await client.enable("UTF8=ACCEPT")
            assert typ == "OK"
            with pytest.raises(IMAP4.error, match="Non-None charset not valid in UTF8 mode"):
                await client.search("UTF-8", "ALL")


async def test_login_cram_md5_str_and_bytes():
    with run_server(AuthHandlerCRAMMD5) as server:
        async with open_client(*server.server_address) as client:
            typ, _ = await client.login_cram_md5("tim", "tanstaaftanstaaf")
            assert typ == "OK"

    with run_server(AuthHandlerCRAMMD5) as server:
        async with open_client(*server.server_address) as client:
            typ, _ = await client.login_cram_md5("tim", b"tanstaaftanstaaf")
            assert typ == "OK"


async def test_idle_capability_required():
    with run_server(SimpleIMAPHandler) as server:
        async with open_client(*server.server_address) as client:
            with pytest.raises(IMAP4.error, match="does not support IMAP4 IDLE"):
                async with client.idle():
                    pass


async def test_idle_denied():
    with run_server(IdleCmdDenyHandler) as server:
        async with open_client(*server.server_address) as client:
            await client.login("user", "pass")
            with pytest.raises(IMAP4.error, match="idle denied"):
                async with client.idle():
                    pass


async def test_idle_iteration_and_response_visibility():
    with run_server(IdleCmdHandler) as server:
        async with open_client(*server.server_address) as client:
            await client.login("user", "pass")
            async with client.idle() as idler:
                response = await anext(idler)
                assert response == ("EXISTS", [b"0"])
                response = await anext(idler)
                assert response == ("EXISTS", [b"2"])

                typ, data = await anext(idler)
                assert typ == "FETCH"
                assert data == [
                    (b"1 (BODY[HEADER.FIELDS (DATE)] {41}", b"Date: Fri, 06 Dec 2024 06:00:00 +0000\r\n\r\n"),
                    b")",
                ]

                response = await anext(idler)
                assert response == ("EXISTS", [b"3"])

            _, exists = await client.response("EXISTS")
            assert exists == [None]
            _, recent = await client.response("RECENT")
            assert recent[0] == b"1"
            assert recent[1] == b"9"


async def test_idle_burst_batches_immediate_responses():
    with run_server(IdleCmdHandler) as server:
        async with open_client(*server.server_address) as client:
            await client.login("user", "pass")
            async with client.idle() as idler:
                batch = [item async for item in idler.burst()]
                assert len(batch) == 4
            _, recent = await client.response("RECENT")
            assert recent == [b"1", b"9"]


async def test_idle_delayed_packet_not_corrupted_by_timeout():
    with run_server(IdleCmdDelayedPacketHandler) as server:
        async with open_client(*server.server_address) as client:
            await client.login("user", "pass")
            async with client.idle(0.1) as idler:
                with pytest.raises(StopAsyncIteration):
                    await anext(idler)


async def test_control_characters_not_allowed_in_commands():
    with run_server(SimpleIMAPHandler) as server:
        async with open_client(*server.server_address) as client:
            await client.login("user", "pass")
            with pytest.raises(ValueError, match="Control characters not allowed"):
                await client.list("\x00", "*")


async def test_literal_not_leaked_after_command_error():
    """A literal set before _command() raises must not leak to the next command."""
    with run_server(SimpleIMAPHandler) as server:
        async with open_client(*server.server_address) as client:
            await client.login("user", "pass")
            await client.select("INBOX")
            # append sets a literal, but the control-char mailbox name
            # causes ValueError inside _command() before the literal is consumed.
            with pytest.raises(ValueError, match="Control characters not allowed"):
                await client.append("\x00bad", None, None, b"LIT")
            # self.literal must be None now; noop must succeed cleanly.
            assert client.literal is None
            typ, _ = await client.noop()
            assert typ == "OK"


async def test_connect_failure_refused_port():
    port = get_unused_port()
    client = IMAP4("127.0.0.1", port)
    try:
        with pytest.raises(OSError):
            await client.connect()
    finally:
        await client.aclose()


async def test_eof_without_welcome_message():
    with run_server(NoWelcomeHandler) as server:
        async with open_client(*server.server_address) as client:
            with pytest.raises(IMAP4.abort, match="EOF"):
                await client.connect()


async def test_line_termination_error():
    with run_server(BadTerminationHandler) as server:
        async with open_client(*server.server_address) as client:
            with pytest.raises(IMAP4.abort, match="unterminated line"):
                await client.connect()


async def test_line_too_long(monkeypatch):
    monkeypatch.setattr(client_mod, "_MAXLINE", 1024)

    class Handler(TooLongLineHandler):
        line_size = 2048

    with run_server(Handler) as server:
        async with open_client(*server.server_address) as client:
            with pytest.raises(IMAP4.error, match="got more than"):
                await client.connect()


async def test_truncated_large_literal_aborts():
    class Handler(TruncatedLiteralHandler):
        literal_size = 1 << 20

    with run_server(Handler) as server:
        async with open_client(*server.server_address) as client:
            with pytest.raises(IMAP4.abort):
                await client.connect()


async def test_uppercase_command_aliases():
    with run_server(SimpleIMAPHandler) as server:
        async with open_client(*server.server_address) as client:
            typ, _ = await client.LOGIN("user", "pass")
            assert typ == "OK"
            typ, _ = await client.LOGOUT()
            assert typ == "BYE"


async def test_unknown_imap_attribute_raises():
    with run_server(SimpleIMAPHandler) as server:
        async with open_client(*server.server_address) as client:
            with pytest.raises(AttributeError, match="Unknown IMAP4 command"):
                _ = client.SOMETHING_UNKNOWN


async def test_parallel_commands_are_serialized_and_safe():
    with run_server(SimpleIMAPHandler) as server:
        async with open_client(*server.server_address) as client:
            await client.login("user", "pass")
            results = await asyncio.gather(client.noop(), client.noop(), client.noop())
            assert [typ for typ, _ in results] == ["OK", "OK", "OK"]


def test_module_getattr_version():
    assert aioimaplib.client.__getattr__("__version__") == "2.60"
    assert aioimaplib.__getattr__("__version__") == "2.60"


def test_module_getattr_unknown():
    with pytest.raises(AttributeError):
        aioimaplib.client.__getattr__("bogus")
