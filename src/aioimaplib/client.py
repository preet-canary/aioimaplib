from __future__ import annotations

import asyncio
import binascii
import calendar
import errno
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from io import DEFAULT_BUFFER_SIZE
from warnings import warn

try:
    import ssl

    HAVE_SSL = True
except ImportError:  # pragma: no cover
    ssl = None
    HAVE_SSL = False


__all__ = [
    "IMAP4",
    "IMAP4_stream",
    "Internaldate2tuple",
    "Int2AP",
    "ParseFlags",
    "Time2Internaldate",
]


CRLF = b"\r\n"
_UNSET = object()  # Sentinel to distinguish "no argument" from None
Debug = 0
IMAP4_PORT = 143
IMAP4_SSL_PORT = 993
AllowedVersions = ("IMAP4REV1", "IMAP4")
_MAXLINE = 1_000_000

Commands = {
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

Continuation = re.compile(br"\+( (?P<data>.*))?")
Flags = re.compile(br".*FLAGS \((?P<flags>[^\)]*)\)")
InternalDate = re.compile(
    br".*INTERNALDATE \""
    br"(?P<day>[ 0123][0-9])-(?P<mon>[A-Z][a-z][a-z])-(?P<year>[0-9][0-9][0-9][0-9])"
    br" (?P<hour>[0-9][0-9]):(?P<min>[0-9][0-9]):(?P<sec>[0-9][0-9])"
    br" (?P<zonen>[-+])(?P<zoneh>[0-9][0-9])(?P<zonem>[0-9][0-9])"
    br"\""
)
Literal = re.compile(br".*{(?P<size>\d+)}$", re.ASCII)
MapCRLF = re.compile(br"\r\n|\r|\n")
Response_code = re.compile(br"\[(?P<type>[A-Z-]+)( (?P<data>.*))?\]")
Untagged_response = re.compile(br"\* (?P<type>[A-Z-]+)( (?P<data>.*))?")
Untagged_status = re.compile(br"\* (?P<data>\d+) (?P<type>[A-Z-]+)( (?P<data2>.*))?", re.ASCII)
_Literal = br".*{(?P<size>\d+)}$"
_Untagged_status = br"\* (?P<data>\d+) (?P<type>[A-Z-]+)( (?P<data2>.*))?"
_control_chars = re.compile(b"[\x00-\x1F\x7F]")


class _Authenticator:
    def __init__(self, mechinst):
        self.mech = mechinst

    def process(self, data):
        ret = self.mech(self.decode(data))
        if ret is None:
            return b"*"
        return self.encode(ret)

    def encode(self, inp):
        oup = b""
        if isinstance(inp, str):
            inp = inp.encode("utf-8")
        while inp:
            if len(inp) > 48:
                t = inp[:48]
                inp = inp[48:]
            else:
                t = inp
                inp = b""
            e = binascii.b2a_base64(t)
            if e:
                oup = oup + e[:-1]
        return oup

    def decode(self, inp):
        if not inp:
            return b""
        return binascii.a2b_base64(inp)


class AsyncIdler:
    def __init__(self, imap: "IMAP4", duration: float | None = None):
        if "IDLE" not in imap.capabilities:
            raise imap.error("Server does not support IMAP4 IDLE")
        if duration is not None and imap.socket() is None:
            # IMAP4_stream pipes don't support timeouts
            raise imap.error("duration requires a socket connection")
        self._imap = imap
        self._duration = duration
        self._deadline: float | None = None
        self._saved_state: str | None = None
        self._tag: bytes | None = None

    async def __aenter__(self):
        imap = self._imap
        await imap._command_lock.acquire()
        try:
            if imap._idle_responses or imap._idle_capture:
                raise imap.error("IDLE already active")
            imap._idle_capture = True
            self._tag = await imap._command("IDLE")
            while await imap._get_response():
                if imap.tagged_commands[self._tag]:
                    typ, data = imap.tagged_commands.pop(self._tag)
                    if typ == "NO":
                        raise imap.error(f"idle denied: {data}")
                    raise imap.abort("unexpected status response while entering IDLE")

            self._saved_state = imap.state
            imap.state = "IDLING"
            if self._duration is not None:
                self._deadline = time.monotonic() + self._duration
            return self
        except BaseException:
            imap._idle_capture = False
            imap._command_lock.release()
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        imap = self._imap
        imap.state = self._saved_state or imap.state
        imap._idle_capture = False

        if imap._idle_responses:
            while imap._idle_responses:
                typ, data = imap._idle_responses.pop(0)
                for datum in data:
                    imap._append_untagged(typ, datum)

        try:
            await imap.send(b"DONE" + CRLF)
            if self._tag is not None:
                await imap._command_complete("IDLE", self._tag)
        except OSError:
            if not exc_type:
                raise
        finally:
            imap._command_lock.release()

        return False

    def __aiter__(self):
        return self

    async def _pop(self, timeout, default=("", None)):
        imap = self._imap
        if imap.state != "IDLING":
            raise imap.error("_pop() only works during IDLE")

        if imap._idle_responses:
            return imap._idle_responses.pop(0)

        if timeout is not None:
            if timeout <= 0:
                return default
            timeout = float(timeout)

        try:
            await imap._get_response(timeout)
        except IMAP4._responsetimeout:
            return default

        if not imap._idle_responses:
            return default
        return imap._idle_responses.pop(0)

    async def __anext__(self):
        if self._duration is None:
            timeout = None
        else:
            timeout = (self._deadline or time.monotonic()) - time.monotonic()

        typ, data = await self._pop(timeout)
        if not typ:
            raise StopAsyncIteration
        return typ, data

    async def burst(self, interval: float = 0.1):
        if self._imap._is_stream:
            raise self._imap.error("burst() requires a socket connection")

        try:
            yield await self.__anext__()
        except StopAsyncIteration:
            return

        while True:
            response = await self._pop(interval, None)
            if response is None:
                break
            yield response


class IMAP4:
    class error(Exception):
        pass

    class abort(error):
        pass

    class readonly(abort):
        pass

    class _responsetimeout(TimeoutError):
        pass

    def __init__(self, host: str = "", port: int = IMAP4_PORT, timeout: float | None = None):
        self.debug = Debug
        self.state = "LOGOUT"
        self.literal = None
        self.tagged_commands: dict[bytes, tuple[str, list[bytes | tuple[bytes, bytes]]] | None] = {}
        self.untagged_responses: dict[str, list] = {}
        self.continuation_response: bytes = b""
        self._idle_responses: list[tuple[str, list]] = []
        self._idle_capture = False
        self.is_readonly = False
        self.tagnum = 0
        self._tls_established = False
        self._mode_ascii()

        self._readbuf = bytearray()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._proc_reader: asyncio.StreamReader | None = None
        self._proc_writer: asyncio.StreamWriter | None = None

        self._is_stream = False
        self.host = host
        self.port = port
        self.timeout = timeout
        self._connect_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()

        self.welcome: bytes | None = None
        self.capabilities: tuple[str, ...] = ()
        self.PROTOCOL_VERSION = ""
        self.mo = None

    def _mode_ascii(self):
        self.utf8_enabled = False
        self._encoding = "ascii"
        self.Literal = re.compile(_Literal, re.ASCII)
        self.Untagged_status = re.compile(_Untagged_status, re.ASCII)

    def _mode_utf8(self):
        self.utf8_enabled = True
        self._encoding = "utf-8"
        self.Literal = re.compile(_Literal)
        self.Untagged_status = re.compile(_Untagged_status)

    async def connect(self) -> "IMAP4":
        if self._reader is not None or self._proc_reader is not None:
            return self

        async with self._connect_lock:
            if self._reader is not None or self._proc_reader is not None:
                return self
            await self.open(self.host, self.port, self.timeout)
            try:
                await self._connect()
            except Exception:
                try:
                    await self.shutdown()
                except OSError:
                    pass
                raise
        return self

    async def _connect(self):
        self.tagpre = Int2AP(random.randint(4096, 65535))
        self.tagre = re.compile(
            br"(?P<tag>" + self.tagpre + br"\d+) (?P<type>[A-Z]+) (?P<data>.*)",
            re.ASCII,
        )

        self.welcome = await self._get_response()
        if "PREAUTH" in self.untagged_responses:
            self.state = "AUTH"
        elif "OK" in self.untagged_responses:
            self.state = "NONAUTH"
        else:
            raise self.error(self.welcome)

        await self._get_capabilities()
        for version in AllowedVersions:
            if version in self.capabilities:
                self.PROTOCOL_VERSION = version
                return
        raise self.error("server not IMAP4 compliant")

    async def open(self, host: str = "", port: int = IMAP4_PORT, timeout: float | None = None):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")

        self.host = host
        self.port = port
        self.timeout = timeout

        connect_host = host or "localhost"
        opener = asyncio.open_connection(connect_host, port)
        if timeout is None:
            self._reader, self._writer = await opener
        else:
            self._reader, self._writer = await asyncio.wait_for(opener, timeout)

        self._readbuf = bytearray()
        self._is_stream = False

    @property
    def file(self):
        warn(
            "IMAP4.file is unsupported, can cause errors, and may be removed.",
            RuntimeWarning,
            stacklevel=2,
        )
        return self._reader

    async def _source_read(self, n: int, timeout: float | None = None) -> bytes:
        if self._reader is not None:
            coro = self._reader.read(n)
        elif self._proc_reader is not None:
            coro = self._proc_reader.read(n)
        else:
            return b""

        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout)

    async def read(self, size: int):
        parts: list[bytes] = []

        while size > 0:
            if self._readbuf:
                if len(self._readbuf) >= size:
                    parts.append(bytes(self._readbuf[:size]))
                    del self._readbuf[:size]
                    break
                parts.append(bytes(self._readbuf))
                size -= len(self._readbuf)
                self._readbuf.clear()

            chunk = await self._source_read(DEFAULT_BUFFER_SIZE, timeout=self.timeout)
            if not chunk:
                break
            if len(chunk) >= size:
                parts.append(chunk[:size])
                self._readbuf.extend(chunk[size:])
                break
            parts.append(chunk)
            size -= len(chunk)

        return b"".join(parts)

    async def readline(self, timeout: float | None = _UNSET):
        # When no explicit timeout is given, fall back to the instance
        # timeout (matching upstream where socket.settimeout persists).
        if timeout is _UNSET:
            timeout = self.timeout

        LF = b"\n"

        while True:
            pos = self._readbuf.find(LF)
            if pos != -1:
                pos += 1
                line = bytes(self._readbuf[:pos])
                del self._readbuf[:pos]
                if len(line) > _MAXLINE:
                    raise self.error(f"got more than {_MAXLINE} bytes")
                return line

            if len(self._readbuf) > _MAXLINE:
                raise self.error(f"got more than {_MAXLINE} bytes")

            chunk = await self._source_read(DEFAULT_BUFFER_SIZE, timeout=timeout)
            if not chunk:
                line = bytes(self._readbuf)
                self._readbuf.clear()
                if len(line) > _MAXLINE:
                    raise self.error(f"got more than {_MAXLINE} bytes")
                return line
            self._readbuf.extend(chunk)

    async def send(self, data):
        if self._writer is not None:
            self._writer.write(data)
            coro = self._writer.drain()
        elif self._proc_writer is not None:
            self._proc_writer.write(data)
            coro = self._proc_writer.drain()
        else:
            raise self.abort("socket error: not connected")
        if self.timeout is not None:
            await asyncio.wait_for(coro, self.timeout)
        else:
            await coro

    async def shutdown(self):
        if self._reader is not None or self._writer is not None:
            if self._writer is not None:
                try:
                    self._writer.close()
                    await self._writer.wait_closed()
                except Exception:
                    pass
            self._reader = None
            self._writer = None

        if self._proc is not None:
            if self._proc_writer is not None:
                try:
                    self._proc_writer.close()
                    await self._proc_writer.wait_closed()
                except Exception:
                    pass
            if self._proc_reader is not None:
                self._proc_reader = None
            try:
                await self._proc.wait()
            except Exception:
                pass
            self._proc = None
            self._proc_reader = None
            self._proc_writer = None

        self._readbuf.clear()

    def socket(self):
        if self._writer is None:
            return None
        return self._writer.get_extra_info("socket")

    async def recent(self):
        name = "RECENT"
        typ, dat = self._untagged_response("OK", [None], name)
        if dat[-1]:
            return typ, dat
        typ, dat = await self.noop()
        return self._untagged_response(typ, dat, name)

    async def response(self, code):
        return self._untagged_response(code, [None], code.upper())

    async def append(self, mailbox, flags, date_time, message):
        name = "APPEND"
        if not mailbox:
            mailbox = "INBOX"
        if flags:
            if (flags[0], flags[-1]) != ("(", ")"):
                flags = f"({flags})"
        else:
            flags = None
        if date_time:
            date_time = Time2Internaldate(date_time)
        else:
            date_time = None
        literal = MapCRLF.sub(CRLF, message)
        return await self._simple_command(name, mailbox, flags, date_time, _literal=literal)

    async def authenticate(self, mechanism, authobject):
        mech = mechanism.upper()
        literal = _Authenticator(authobject).process
        typ, dat = await self._simple_command("AUTHENTICATE", mech, _literal=literal)
        if typ != "OK":
            raise self.error(dat[-1].decode("utf-8", "replace"))
        self.state = "AUTH"
        return typ, dat

    async def capability(self):
        name = "CAPABILITY"
        typ, dat = await self._simple_command(name)
        return self._untagged_response(typ, dat, name)

    async def check(self):
        return await self._simple_command("CHECK")

    async def close(self):
        try:
            typ, dat = await self._simple_command("CLOSE")
        finally:
            self.state = "AUTH"
        return typ, dat

    async def copy(self, message_set, new_mailbox):
        return await self._simple_command("COPY", message_set, new_mailbox)

    async def create(self, mailbox):
        return await self._simple_command("CREATE", mailbox)

    async def delete(self, mailbox):
        return await self._simple_command("DELETE", mailbox)

    async def deleteacl(self, mailbox, who):
        return await self._simple_command("DELETEACL", mailbox, who)

    async def enable(self, capability):
        await self.connect()
        if "ENABLE" not in self.capabilities:
            raise self.error("Server does not support ENABLE")
        typ, data = await self._simple_command("ENABLE", capability)
        if typ == "OK" and "UTF8=ACCEPT" in capability.upper():
            self._mode_utf8()
        return typ, data

    async def expunge(self):
        name = "EXPUNGE"
        typ, dat = await self._simple_command(name)
        return self._untagged_response(typ, dat, name)

    async def fetch(self, message_set, message_parts):
        name = "FETCH"
        typ, dat = await self._simple_command(name, message_set, message_parts)
        return self._untagged_response(typ, dat, name)

    async def getacl(self, mailbox):
        typ, dat = await self._simple_command("GETACL", mailbox)
        return self._untagged_response(typ, dat, "ACL")

    async def getannotation(self, mailbox, entry, attribute):
        typ, dat = await self._simple_command("GETANNOTATION", mailbox, entry, attribute)
        return self._untagged_response(typ, dat, "ANNOTATION")

    async def getquota(self, root):
        typ, dat = await self._simple_command("GETQUOTA", root)
        return self._untagged_response(typ, dat, "QUOTA")

    async def getquotaroot(self, mailbox):
        typ, dat = await self._simple_command("GETQUOTAROOT", mailbox)
        typ, quota = self._untagged_response(typ, dat, "QUOTA")
        typ, quotaroot = self._untagged_response(typ, dat, "QUOTAROOT")
        return typ, [quotaroot, quota]

    def idle(self, duration: float | None = None) -> AsyncIdler:
        return AsyncIdler(self, duration)

    async def list(self, directory='""', pattern="*"):
        name = "LIST"
        typ, dat = await self._simple_command(name, directory, pattern)
        return self._untagged_response(typ, dat, name)

    async def login(self, user, password):
        typ, dat = await self._simple_command("LOGIN", user, self._quote(password))
        if typ != "OK":
            raise self.error(dat[-1])
        self.state = "AUTH"
        return typ, dat

    async def login_cram_md5(self, user, password):
        self.user, self.password = user, password
        return await self.authenticate("CRAM-MD5", self._CRAM_MD5_AUTH)

    def _CRAM_MD5_AUTH(self, challenge):
        import hmac

        if isinstance(self.password, str):
            password = self.password.encode("utf-8")
        else:
            password = self.password

        try:
            authcode = hmac.HMAC(password, challenge, "md5")
        except ValueError:
            raise self.error("CRAM-MD5 authentication is not supported")
        return f"{self.user} {authcode.hexdigest()}"

    async def logout(self):
        self.state = "LOGOUT"
        typ, dat = await self._simple_command("LOGOUT")
        await self.shutdown()
        return typ, dat

    async def lsub(self, directory='""', pattern="*"):
        name = "LSUB"
        typ, dat = await self._simple_command(name, directory, pattern)
        return self._untagged_response(typ, dat, name)

    async def myrights(self, mailbox):
        typ, dat = await self._simple_command("MYRIGHTS", mailbox)
        return self._untagged_response(typ, dat, "MYRIGHTS")

    async def namespace(self):
        name = "NAMESPACE"
        typ, dat = await self._simple_command(name)
        return self._untagged_response(typ, dat, name)

    async def noop(self):
        return await self._simple_command("NOOP")

    async def partial(self, message_num, message_part, start, length):
        name = "PARTIAL"
        typ, dat = await self._simple_command(name, message_num, message_part, start, length)
        return self._untagged_response(typ, dat, "FETCH")

    async def proxyauth(self, user):
        return await self._simple_command("PROXYAUTH", user)

    async def rename(self, oldmailbox, newmailbox):
        return await self._simple_command("RENAME", oldmailbox, newmailbox)

    async def search(self, charset, *criteria):
        name = "SEARCH"
        if charset:
            if self.utf8_enabled:
                raise self.error("Non-None charset not valid in UTF8 mode")
            typ, dat = await self._simple_command(name, "CHARSET", charset, *criteria)
        else:
            typ, dat = await self._simple_command(name, *criteria)
        return self._untagged_response(typ, dat, name)

    async def select(self, mailbox="INBOX", readonly=False):
        self.untagged_responses = {}
        self.is_readonly = readonly
        if readonly:
            name = "EXAMINE"
        else:
            name = "SELECT"
        typ, dat = await self._simple_command(name, mailbox)
        if typ != "OK":
            self.state = "AUTH"
            return typ, dat
        self.state = "SELECTED"
        if "READ-ONLY" in self.untagged_responses and not readonly:
            raise self.readonly(f"{mailbox} is not writable")
        return typ, self.untagged_responses.get("EXISTS", [None])

    async def setacl(self, mailbox, who, what):
        return await self._simple_command("SETACL", mailbox, who, what)

    async def setannotation(self, *args):
        typ, dat = await self._simple_command("SETANNOTATION", *args)
        return self._untagged_response(typ, dat, "ANNOTATION")

    async def setquota(self, root, limits):
        typ, dat = await self._simple_command("SETQUOTA", root, limits)
        return self._untagged_response(typ, dat, "QUOTA")

    async def sort(self, sort_criteria, charset, *search_criteria):
        name = "SORT"
        if (sort_criteria[0], sort_criteria[-1]) != ("(", ")"):
            sort_criteria = f"({sort_criteria})"
        typ, dat = await self._simple_command(name, sort_criteria, charset, *search_criteria)
        return self._untagged_response(typ, dat, name)

    async def starttls(self, ssl_context=None):
        name = "STARTTLS"
        if not HAVE_SSL:
            raise self.error("SSL support missing")
        if self._tls_established:
            raise self.abort("TLS session already established")
        await self.connect()
        if name not in self.capabilities:
            raise self.abort("TLS not supported by server")
        if ssl_context is None:
            ssl_context = ssl._create_stdlib_context() if hasattr(ssl, "_create_stdlib_context") else ssl.create_default_context()
        typ, dat = await self._simple_command(name)
        if typ == "OK":
            if self._writer is None:
                raise self.abort("TLS upgrade requires socket transport")
            await self._writer.start_tls(ssl_context, server_hostname=self.host)
            self._tls_established = True
            await self._get_capabilities()
        else:
            raise self.error("Couldn't establish TLS session")
        return self._untagged_response(typ, dat, name)

    async def status(self, mailbox, names):
        name = "STATUS"
        typ, dat = await self._simple_command(name, mailbox, names)
        return self._untagged_response(typ, dat, name)

    async def store(self, message_set, command, flags):
        if (flags[0], flags[-1]) != ("(", ")"):
            flags = f"({flags})"
        typ, dat = await self._simple_command("STORE", message_set, command, flags)
        return self._untagged_response(typ, dat, "FETCH")

    async def subscribe(self, mailbox):
        return await self._simple_command("SUBSCRIBE", mailbox)

    async def thread(self, threading_algorithm, charset, *search_criteria):
        name = "THREAD"
        typ, dat = await self._simple_command(name, threading_algorithm, charset, *search_criteria)
        return self._untagged_response(typ, dat, name)

    async def uid(self, command, *args):
        command = command.upper()
        if command not in Commands:
            raise self.error(f"Unknown IMAP4 UID command: {command}")
        if self.state not in Commands[command]:
            raise self.error(
                "command %s illegal in state %s, only allowed in states %s"
                % (command, self.state, ", ".join(Commands[command]))
            )
        name = "UID"
        typ, dat = await self._simple_command(name, command, *args)
        if command in ("SEARCH", "SORT", "THREAD"):
            name = command
        else:
            name = "FETCH"
        return self._untagged_response(typ, dat, name)

    async def unsubscribe(self, mailbox):
        return await self._simple_command("UNSUBSCRIBE", mailbox)

    async def unselect(self):
        try:
            typ, data = await self._simple_command("UNSELECT")
        finally:
            self.state = "AUTH"
        return typ, data

    async def xatom(self, name, *args):
        name = name.upper()
        if name not in Commands:
            Commands[name] = (self.state,)
        return await self._simple_command(name, *args)

    def _append_untagged(self, typ, dat):
        if dat is None:
            dat = b""

        if self._idle_capture:
            if not self._idle_responses or isinstance(self._idle_responses[-1][1][-1], bytes):
                self._idle_responses.append((typ, [dat]))
            else:
                response = self._idle_responses[-1]
                response[1].append(dat)
            return

        ur = self.untagged_responses
        if typ in ur:
            ur[typ].append(dat)
        else:
            ur[typ] = [dat]

    def _check_bye(self):
        bye = self.untagged_responses.get("BYE")
        if bye:
            raise self.abort(bye[-1].decode(self._encoding, "replace"))

    async def _command(self, name, *args):
        if self.state not in Commands[name]:
            self.literal = None
            raise self.error(
                "command %s illegal in state %s, only allowed in states %s"
                % (name, self.state, ", ".join(Commands[name]))
            )

        for typ in ("OK", "NO", "BAD"):
            if typ in self.untagged_responses:
                del self.untagged_responses[typ]

        if "READ-ONLY" in self.untagged_responses and not self.is_readonly:
            raise self.readonly("mailbox status changed to READ-ONLY")

        tag = self._new_tag()
        bname = bytes(name, self._encoding)
        data = tag + b" " + bname
        for arg in args:
            if arg is None:
                continue
            if isinstance(arg, str):
                arg = bytes(arg, self._encoding)
            if _control_chars.search(arg):
                raise ValueError("Control characters not allowed in commands")
            data = data + b" " + arg

        literal = self.literal
        if literal is not None:
            self.literal = None
            if callable(literal):
                literator = literal
            else:
                literator = None
                if self.utf8_enabled:
                    data = data + bytes(f" UTF8 (~{{{len(literal)}}}", self._encoding)
                    literal = literal + b")"
                else:
                    data = data + bytes(f" {{{len(literal)}}}", self._encoding)

        try:
            await self.send(data + CRLF)
        except OSError as val:
            raise self.abort(f"socket error: {val}")

        if literal is None:
            return tag

        while True:
            while await self._get_response():
                if self.tagged_commands[tag]:
                    return tag

            if literator:
                literal = literator(self.continuation_response)

            try:
                await self.send(literal)
                await self.send(CRLF)
            except OSError as val:
                raise self.abort(f"socket error: {val}")

            if not literator:
                break

        return tag

    async def _command_complete(self, name, tag):
        logout = name == "LOGOUT"
        if not logout:
            self._check_bye()
        try:
            typ, data = await self._get_tagged_response(tag, expect_bye=logout)
        except self.abort as val:
            raise self.abort(f"command: {name} => {val}")
        except self.error as val:
            raise self.error(f"command: {name} => {val}")
        if not logout:
            self._check_bye()
        if typ == "BAD":
            raise self.error(f"{name} command error: {typ} {data}")
        return typ, data

    async def _get_capabilities(self):
        typ, dat = await self.capability()
        if dat == [None]:
            raise self.error("no CAPABILITY response from server")
        dat_str = str(dat[-1], self._encoding).upper()
        self.capabilities = tuple(dat_str.split())

    async def _get_response(self, start_timeout=False):
        if start_timeout is not False and self.socket() is not None:
            assert start_timeout is None or start_timeout > 0
            try:
                resp = await self._get_line(timeout=start_timeout)
            except asyncio.TimeoutError as err:
                raise self._responsetimeout from err
        else:
            resp = await self._get_line()

        if self._match(self.tagre, resp):
            tag = self.mo.group("tag")
            if tag not in self.tagged_commands:
                raise self.abort(f"unexpected tagged response: {resp!r}")

            typ = str(self.mo.group("type"), self._encoding)
            dat = self.mo.group("data")
            self.tagged_commands[tag] = (typ, [dat])
        else:
            dat2 = None
            if not self._match(Untagged_response, resp):
                if self._match(self.Untagged_status, resp):
                    dat2 = self.mo.group("data2")

            if self.mo is None:
                if self._match(Continuation, resp):
                    self.continuation_response = self.mo.group("data")
                    return None
                raise self.abort(f"unexpected response: {resp!r}")

            typ = str(self.mo.group("type"), self._encoding)
            dat = self.mo.group("data")
            if dat is None:
                dat = b""
            if dat2:
                dat = dat + b" " + dat2

            while self._match(self.Literal, dat):
                size = int(self.mo.group("size"))
                data = await self.read(size)
                self._append_untagged(typ, (dat, data))
                dat = await self._get_line()

            self._append_untagged(typ, dat)

        if typ in ("OK", "NO", "BAD") and self._match(Response_code, dat):
            typ = str(self.mo.group("type"), self._encoding)
            self._append_untagged(typ, self.mo.group("data"))

        return resp

    async def _get_tagged_response(self, tag, expect_bye=False):
        while True:
            result = self.tagged_commands[tag]
            if result is not None:
                del self.tagged_commands[tag]
                return result

            if expect_bye:
                typ = "BYE"
                bye = self.untagged_responses.pop(typ, None)
                if bye is not None:
                    return typ, bye

            self._check_bye()
            await self._get_response()

    async def _get_line(self, timeout=_UNSET):
        line = await self.readline(timeout=timeout)
        if not line:
            raise self.abort("socket error: EOF")
        if not line.endswith(CRLF):
            raise self.abort(f"socket error: unterminated line: {line!r}")
        return line[:-2]

    def _match(self, cre, s):
        self.mo = cre.match(s)
        return self.mo is not None

    def _new_tag(self):
        tag = self.tagpre + bytes(str(self.tagnum), self._encoding)
        self.tagnum += 1
        self.tagged_commands[tag] = None
        return tag

    def _quote(self, arg):
        arg = arg.replace("\\", "\\\\")
        arg = arg.replace('"', '\\"')
        return f'"{arg}"'

    async def _simple_command(self, name, *args, _literal=None):
        # Literal is passed as a keyword arg rather than through shared
        # self.literal state.  This prevents concurrent commands from
        # corrupting each other's payloads, and avoids the earlier bug
        # where connect()'s inner CAPABILITY command consumed the literal.
        await self.connect()
        async with self._command_lock:
            self.literal = _literal
            try:
                return await self._command_complete(name, await self._command(name, *args))
            finally:
                self.literal = None

    def _untagged_response(self, typ, dat, name):
        if typ == "NO":
            return typ, dat
        if name not in self.untagged_responses:
            return typ, [None]
        data = self.untagged_responses.pop(name)
        return typ, data

    def __getattr__(self, attr):
        if attr in Commands:
            return getattr(self, attr.lower())
        raise AttributeError(f"Unknown IMAP4 command: '{attr}'")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.state == "LOGOUT":
            await self.aclose()
            return
        try:
            await self.logout()
        except OSError:
            pass
        await self.aclose()

    async def aclose(self):
        await self.shutdown()


if HAVE_SSL:

    class IMAP4_SSL(IMAP4):
        def __init__(self, host: str = "", port: int = IMAP4_SSL_PORT, *, ssl_context=None, timeout=None):
            super().__init__(host=host, port=port, timeout=timeout)
            if ssl_context is None:
                ssl_context = ssl._create_stdlib_context() if hasattr(ssl, "_create_stdlib_context") else ssl.create_default_context()
            self.ssl_context = ssl_context

        async def open(self, host: str = "", port: int = IMAP4_SSL_PORT, timeout: float | None = None):
            if timeout is not None and not timeout:
                raise ValueError("Non-blocking socket (timeout=0) is not supported")
            self.host = host
            self.port = port
            self.timeout = timeout
            connect_host = host or "localhost"
            opener = asyncio.open_connection(connect_host, port, ssl=self.ssl_context, server_hostname=self.host or None)
            if timeout is None:
                self._reader, self._writer = await opener
            else:
                self._reader, self._writer = await asyncio.wait_for(opener, timeout)
            self._readbuf = bytearray()
            self._is_stream = False

    __all__.append("IMAP4_SSL")

else:

    class IMAP4_SSL(IMAP4):  # pragma: no cover
        def __init__(self, *args, **kwargs):
            raise IMAP4.error("SSL support missing")


class IMAP4_stream(IMAP4):
    def __init__(self, command):
        self.command = command
        super().__init__(host="", port=IMAP4_PORT, timeout=None)

    async def open(self, host=None, port=None, timeout=None):
        self.host = None
        self.port = None
        self.timeout = timeout
        self._reader = None
        self._writer = None
        self._proc = await asyncio.create_subprocess_shell(
            self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._proc_writer = self._proc.stdin
        self._proc_reader = self._proc.stdout
        self._is_stream = True
        self._readbuf = bytearray()


Months = " Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ")
Mon2num = {s.encode(): n + 1 for n, s in enumerate(Months[1:])}


def Internaldate2tuple(resp):
    mo = InternalDate.match(resp)
    if not mo:
        return None

    mon = Mon2num[mo.group("mon")]
    zonen = mo.group("zonen")

    day = int(mo.group("day"))
    year = int(mo.group("year"))
    hour = int(mo.group("hour"))
    minute = int(mo.group("min"))
    sec = int(mo.group("sec"))
    zoneh = int(mo.group("zoneh"))
    zonem = int(mo.group("zonem"))

    zone = (zoneh * 60 + zonem) * 60
    if zonen == b"-":
        zone = -zone

    tt = (year, mon, day, hour, minute, sec, -1, -1, -1)
    utc = calendar.timegm(tt) - zone
    return time.localtime(utc)


def Int2AP(num):
    val = b""
    AP = b"ABCDEFGHIJKLMNOP"
    num = int(abs(num))
    while num:
        num, mod = divmod(num, 16)
        val = AP[mod : mod + 1] + val
    return val


def ParseFlags(resp):
    mo = Flags.match(resp)
    if not mo:
        return ()
    return tuple(mo.group("flags").split())


def Time2Internaldate(date_time):
    if isinstance(date_time, (int, float)):
        dt = datetime.fromtimestamp(date_time, timezone.utc).astimezone()
    elif isinstance(date_time, tuple):
        try:
            gmtoff = date_time.tm_gmtoff
        except AttributeError:
            if time.daylight:
                dst = date_time[8]
                if dst == -1:
                    dst = time.localtime(time.mktime(date_time))[8]
                gmtoff = -(time.timezone, time.altzone)[dst]
            else:
                gmtoff = -time.timezone
        delta = timedelta(seconds=gmtoff)
        dt = datetime(*date_time[:6], tzinfo=timezone(delta))
    elif isinstance(date_time, datetime):
        if date_time.tzinfo is None:
            raise ValueError("date_time must be aware")
        dt = date_time
    elif isinstance(date_time, str) and (date_time[0], date_time[-1]) == ('"', '"'):
        return date_time
    else:
        raise ValueError("date_time not of a known type")
    fmt = '"%d-{}-%Y %H:%M:%S %z"'.format(Months[dt.month])
    return dt.strftime(fmt)


def __getattr__(name: str):
    if name == "__version__":
        warn("'__version__' is deprecated and slated for removal in Python 3.20", DeprecationWarning, stacklevel=2)
        return "2.60"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
