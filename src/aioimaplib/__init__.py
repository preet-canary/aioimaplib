from .client import (
    AllowedVersions,
    AsyncIdler,
    Commands,
    Continuation,
    CRLF,
    Debug,
    IMAP4,
    IMAP4_PORT,
    IMAP4_SSL,
    IMAP4_SSL_PORT,
    IMAP4_stream,
    Int2AP,
    InternalDate,
    Internaldate2tuple,
    Literal,
    MapCRLF,
    ParseFlags,
    Response_code,
    Time2Internaldate,
    Untagged_response,
    Untagged_status,
    _MAXLINE,
)
from . import client as _client

__all__ = [
    "IMAP4",
    "IMAP4_SSL",
    "IMAP4_stream",
    "AsyncIdler",
    "Internaldate2tuple",
    "Int2AP",
    "ParseFlags",
    "Time2Internaldate",
    "CRLF",
    "Debug",
    "IMAP4_PORT",
    "IMAP4_SSL_PORT",
    "AllowedVersions",
    "Commands",
    "Continuation",
    "InternalDate",
    "Literal",
    "MapCRLF",
    "Response_code",
    "Untagged_response",
    "Untagged_status",
    "_MAXLINE",
]


def __getattr__(name):
    if name == "__version__":
        return _client.__getattr__(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
