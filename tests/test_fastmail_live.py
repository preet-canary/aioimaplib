from __future__ import annotations

import os
import ssl

import pytest

from aioimaplib import IMAP4_SSL


def _get_live_config():
    user = os.getenv("AIOIMAPLIB_FASTMAIL_USER")
    password = os.getenv("AIOIMAPLIB_FASTMAIL_PASSWORD")
    host = os.getenv("AIOIMAPLIB_FASTMAIL_IMAP_HOST", "imap.fastmail.com")
    port = int(os.getenv("AIOIMAPLIB_FASTMAIL_IMAP_PORT", "993"))
    if not user or not password:
        pytest.skip("Live Fastmail credentials are not set in environment")
    return user, password, host, port


async def test_fastmail_live_imap_login_select_logout():
    user, password, host, port = _get_live_config()

    ssl_context = ssl.create_default_context()
    client = IMAP4_SSL(host, port, ssl_context=ssl_context, timeout=30)

    try:
        typ, _ = await client.login(user, password)
        assert typ == "OK"

        typ, caps = await client.capability()
        assert typ == "OK"
        assert caps and caps != [None]

        typ, exists = await client.select("INBOX", readonly=True)
        assert typ == "OK"
        assert exists and exists[0] is not None

        typ, _ = await client.noop()
        assert typ == "OK"

        typ, bye_data = await client.logout()
        assert typ == "BYE"
        assert bye_data and bye_data[0] is not None
    finally:
        await client.aclose()
