from __future__ import annotations

import socket
import socketserver
import threading
import time
from contextlib import contextmanager

HOST = "127.0.0.1"


class TestTCPServer(socketserver.TCPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        self.close_request(request)
        self.server_close()
        raise


class SimpleIMAPHandler(socketserver.StreamRequestHandler):
    continuation = None
    capabilities = ""

    def setup(self):
        super().setup()
        self.server.is_selected = False
        self.server.logged = None

    def _send(self, message: bytes):
        self.wfile.write(message)

    def _send_line(self, message: bytes):
        self._send(message + b"\r\n")

    def _send_textline(self, message: str):
        self._send_line(message.encode("ascii"))

    def _send_tagged(self, tag: str, code: str, message: str):
        self._send_textline(" ".join((tag, code, message)))

    def handle(self):
        self._send_textline("* OK IMAP4rev1")
        while True:
            line = b""
            while True:
                try:
                    part = self.rfile.read(1)
                    if part == b"":
                        return
                    line += part
                except OSError:
                    return
                if line.endswith(b"\r\n"):
                    break

            if self.continuation:
                try:
                    self.continuation.send(line)
                except StopIteration:
                    self.continuation = None
                continue

            splitline = line.decode("ascii").split()
            if len(splitline) < 2:
                return

            tag = splitline[0]
            cmd = splitline[1]
            args = splitline[2:]

            method = getattr(self, f"cmd_{cmd}", None)
            if method is None:
                self._send_tagged(tag, "BAD", cmd + " unknown")
                continue

            continuation = method(tag, args)
            if continuation:
                self.continuation = continuation
                next(continuation)

    def cmd_CAPABILITY(self, tag, args):
        caps = "IMAP4rev1 " + self.capabilities if self.capabilities else "IMAP4rev1"
        self._send_textline("* CAPABILITY " + caps)
        self._send_tagged(tag, "OK", "CAPABILITY completed")

    def cmd_LOGOUT(self, tag, args):
        self.server.logged = None
        self._send_textline("* BYE IMAP4ref1 Server logging out")
        self._send_tagged(tag, "OK", "LOGOUT completed")

    def cmd_LOGIN(self, tag, args):
        self.server.logged = args[0]
        self._send_tagged(tag, "OK", "LOGIN completed")

    def cmd_SELECT(self, tag, args):
        self.server.is_selected = True
        self._send_line(b"* 2 EXISTS")
        self._send_tagged(tag, "OK", "[READ-WRITE] SELECT completed.")

    def cmd_UNSELECT(self, tag, args):
        if self.server.is_selected:
            self.server.is_selected = False
            self._send_tagged(tag, "OK", "Returned to authenticated state. (Success)")
        else:
            self._send_tagged(tag, "BAD", "No mailbox selected")

    def cmd_NOOP(self, tag, args):
        self._send_tagged(tag, "OK", "NOOP completed")


class LsubHandler(SimpleIMAPHandler):
    def cmd_LSUB(self, tag, args):
        self._send_textline('* LSUB () "." directoryA')
        self._send_tagged(tag, "OK", "LSUB completed")


class EnableHandler(SimpleIMAPHandler):
    capabilities = "AUTH ENABLE UTF8=ACCEPT"

    def cmd_ENABLE(self, tag, args):
        self._send_textline("* ENABLED UTF8=ACCEPT")
        self._send_tagged(tag, "OK", "ENABLE completed")


class IdleCmdDenyHandler(SimpleIMAPHandler):
    capabilities = "IDLE"

    def cmd_IDLE(self, tag, args):
        self._send_tagged(tag, "NO", "IDLE is not allowed at this time")


class IdleCmdHandler(SimpleIMAPHandler):
    capabilities = "IDLE"

    def cmd_IDLE(self, tag, args):
        self._send_line(b"* 0 EXISTS")
        self._send_textline("+ idling")
        self._send_line(b"* 2 EXISTS")
        self._send_line(b"* 1 FETCH (BODY[HEADER.FIELDS (DATE)] {41}")
        self._send(b"Date: Fri, 06 Dec 2024 06:00:00 +0000\r\n\r\n")
        self._send_line(b")")
        self._send_line(b"* 3 EXISTS")
        time.sleep(0.2)
        self._send_line(b"* 1 RECENT")
        r = yield
        if r == b"DONE\r\n":
            self._send_line(b"* 9 RECENT")
            self._send_tagged(tag, "OK", "Idle completed")
        else:
            self._send_tagged(tag, "BAD", "Expected DONE")


class IdleCmdDelayedPacketHandler(SimpleIMAPHandler):
    capabilities = "IDLE"

    def cmd_IDLE(self, tag, args):
        self._send_textline("+ idling")
        self._send(b"* 1 EX")
        time.sleep(0.2)
        self._send(b"IS")
        time.sleep(0.6)
        self._send(b"TS\r\n")
        r = yield
        if r == b"DONE\r\n":
            self._send_tagged(tag, "OK", "Idle completed")
        else:
            self._send_tagged(tag, "BAD", "Expected DONE")


class AuthHandlerCRAMMD5(SimpleIMAPHandler):
    capabilities = "LOGINDISABLED AUTH=CRAM-MD5"

    def cmd_AUTHENTICATE(self, tag, args):
        self._send_textline("+ PDE4OTYuNjk3MTcwOTUyQHBvc3RvZmZpY2UucmVzdG9uLm1jaS5uZXQ=")
        r = yield
        if r == b"dGltIGYxY2E2YmU0NjRiOWVmYTFjY2E2ZmZkNmNmMmQ5ZjMy\r\n":
            self._send_tagged(tag, "OK", "CRAM-MD5 successful")
        else:
            self._send_tagged(tag, "NO", "No access")


class NoWelcomeHandler(socketserver.StreamRequestHandler):
    def handle(self):
        return


class BadTerminationHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"* OK IMAP4rev1\n")


class TooLongLineHandler(socketserver.StreamRequestHandler):
    line_size = 0

    def handle(self):
        self.wfile.write(b"* OK " + b"x" * self.line_size + b"\r\n")


class TruncatedLiteralHandler(socketserver.StreamRequestHandler):
    literal_size = 32768

    def handle(self):
        self.wfile.write(f"* OK {{{self.literal_size}}}\r\n".encode("ascii"))
        self.wfile.write(b"IMAP4rev1\r\n")


class AllCommandsHandler(SimpleIMAPHandler):
    capabilities = (
        "AUTH ENABLE UTF8=ACCEPT IDLE STARTTLS AUTH=CRAM-MD5 "
        "ACL ANNOTATE-EXPERIMENT-1 QUOTA NAMESPACE UIDPLUS SORT THREAD=REFERENCES MOVE"
    )

    def _ok(self, tag: str, name: str):
        self._send_tagged(tag, "OK", f"{name} completed")

    def cmd_APPEND(self, tag, args):
        self._send_textline("+ go ahead")
        _ = yield
        self._ok(tag, "APPEND")

    def cmd_AUTHENTICATE(self, tag, args):
        self._send_textline("+ U29tZS1jaGFsbGVuZ2U=")
        response = yield
        if response == b"*\r\n":
            self._send_tagged(tag, "NO", "[AUTHENTICATIONFAILED] aborted")
            return
        self._send_tagged(tag, "OK", "AUTHENTICATE completed")

    def cmd_CHECK(self, tag, args):
        self._ok(tag, "CHECK")

    def cmd_CLOSE(self, tag, args):
        self.server.is_selected = False
        self._ok(tag, "CLOSE")

    def cmd_COPY(self, tag, args):
        self._ok(tag, "COPY")

    def cmd_CREATE(self, tag, args):
        self._ok(tag, "CREATE")

    def cmd_DELETE(self, tag, args):
        self._ok(tag, "DELETE")

    def cmd_DELETEACL(self, tag, args):
        self._ok(tag, "DELETEACL")

    def cmd_ENABLE(self, tag, args):
        self._send_textline("* ENABLED UTF8=ACCEPT")
        self._ok(tag, "ENABLE")

    def cmd_EXAMINE(self, tag, args):
        self.server.is_selected = True
        self._send_line(b"* 4 EXISTS")
        self._send_tagged(tag, "OK", "[READ-ONLY] EXAMINE completed")

    def cmd_EXPUNGE(self, tag, args):
        self._send_line(b"* 1 EXPUNGE")
        self._ok(tag, "EXPUNGE")

    def cmd_FETCH(self, tag, args):
        self._send_line(b"* 1 FETCH (FLAGS (\\Seen))")
        self._ok(tag, "FETCH")

    def cmd_GETACL(self, tag, args):
        self._send_textline("* ACL INBOX user lr")
        self._ok(tag, "GETACL")

    def cmd_GETANNOTATION(self, tag, args):
        self._send_textline('* ANNOTATION INBOX "/comment" "value"')
        self._ok(tag, "GETANNOTATION")

    def cmd_GETQUOTA(self, tag, args):
        self._send_textline('* QUOTA "" (STORAGE 10 512)')
        self._ok(tag, "GETQUOTA")

    def cmd_GETQUOTAROOT(self, tag, args):
        self._send_textline('* QUOTAROOT INBOX ""')
        self._send_textline('* QUOTA "" (STORAGE 10 512)')
        self._ok(tag, "GETQUOTAROOT")

    def cmd_IDLE(self, tag, args):
        self._send_textline("+ idling")
        line = yield
        if line == b"DONE\r\n":
            self._send_tagged(tag, "OK", "IDLE completed")
        else:
            self._send_tagged(tag, "BAD", "Expected DONE")

    def cmd_LIST(self, tag, args):
        self._send_textline('* LIST () "/" "INBOX"')
        self._ok(tag, "LIST")

    def cmd_LSUB(self, tag, args):
        self._send_textline('* LSUB () "/" "INBOX"')
        self._ok(tag, "LSUB")

    def cmd_MOVE(self, tag, args):
        self._ok(tag, "MOVE")

    def cmd_MYRIGHTS(self, tag, args):
        self._send_textline("* MYRIGHTS INBOX lrswipkxte")
        self._ok(tag, "MYRIGHTS")

    def cmd_NAMESPACE(self, tag, args):
        self._send_textline('* NAMESPACE (("" "/")) NIL NIL')
        self._ok(tag, "NAMESPACE")

    def cmd_NOOP(self, tag, args):
        self._send_line(b"* 1 RECENT")
        self._ok(tag, "NOOP")

    def cmd_PARTIAL(self, tag, args):
        self._send_line(b'* 1 FETCH (BODY[TEXT] "partial")')
        self._ok(tag, "PARTIAL")

    def cmd_PROXYAUTH(self, tag, args):
        self._ok(tag, "PROXYAUTH")

    def cmd_RENAME(self, tag, args):
        self._ok(tag, "RENAME")

    def cmd_SEARCH(self, tag, args):
        self._send_line(b"* SEARCH 1 2 3")
        self._ok(tag, "SEARCH")

    def cmd_SELECT(self, tag, args):
        self.server.is_selected = True
        self._send_line(b"* 4 EXISTS")
        self._send_tagged(tag, "OK", "[READ-WRITE] SELECT completed.")

    def cmd_SETACL(self, tag, args):
        self._ok(tag, "SETACL")

    def cmd_SETANNOTATION(self, tag, args):
        self._send_textline('* ANNOTATION INBOX "/comment" "value"')
        self._ok(tag, "SETANNOTATION")

    def cmd_SETQUOTA(self, tag, args):
        self._send_textline('* QUOTA "" (STORAGE 10 512)')
        self._ok(tag, "SETQUOTA")

    def cmd_SORT(self, tag, args):
        self._send_line(b"* SORT 3 2 1")
        self._ok(tag, "SORT")

    def cmd_STARTTLS(self, tag, args):
        self._send_tagged(tag, "OK", "Begin TLS negotiation now")

    def cmd_STATUS(self, tag, args):
        self._send_textline('* STATUS INBOX (MESSAGES 4 RECENT 1 UIDNEXT 5 UIDVALIDITY 1)')
        self._ok(tag, "STATUS")

    def cmd_STORE(self, tag, args):
        self._send_line(b"* 1 FETCH (FLAGS (\\Seen))")
        self._ok(tag, "STORE")

    def cmd_SUBSCRIBE(self, tag, args):
        self._ok(tag, "SUBSCRIBE")

    def cmd_THREAD(self, tag, args):
        self._send_line(b"* THREAD (1)(2 3)")
        self._ok(tag, "THREAD")

    def cmd_UID(self, tag, args):
        subcmd = args[0].upper() if args else ""
        if subcmd == "SEARCH":
            self._send_line(b"* SEARCH 101 102")
        elif subcmd == "SORT":
            self._send_line(b"* SORT 102 101")
        elif subcmd == "THREAD":
            self._send_line(b"* THREAD (101)(102 103)")
        else:
            self._send_line(b"* 1 FETCH (UID 101 FLAGS (\\Seen))")
        self._ok(tag, "UID")

    def cmd_UNSUBSCRIBE(self, tag, args):
        self._ok(tag, "UNSUBSCRIBE")


def get_unused_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


@contextmanager
def run_server(handler):
    server = TestTCPServer((HOST, 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
