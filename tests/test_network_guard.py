"""The test-run network guard in the root conftest.py.

Python-level sockets may only reach loopback while the suite runs, so a test
that forgets to mock `requests`/`urllib` fails loudly instead of quietly
calling a real host (or hanging for minutes polling one, which the
`deploy()` tests once did). See the comment block above `NetworkBlockedError`
in conftest.py for what is and is not covered.
"""
import os
import socket

import pytest
import requests

import conftest

pytestmark = pytest.mark.skipif(
    os.environ.get("CAS_TESTS_ALLOW_NETWORK") == "1",
    reason="network guard is switched off by CAS_TESTS_ALLOW_NETWORK=1",
)


@pytest.fixture(autouse=True)
def _forget_deliberate_attempts():
    """These tests trigger the guard on purpose; keep them out of the run summary."""
    before = len(conftest._blocked_attempts)
    yield
    del conftest._blocked_attempts[before:]


def test_dns_lookup_for_a_public_name_is_blocked():
    with pytest.raises(conftest.NetworkBlockedError, match="may not use the network"):
        socket.getaddrinfo("example.com", 443)


def test_connect_to_a_non_loopback_address_is_blocked():
    # 192.0.2.0/24 is reserved for documentation (RFC 5737); nothing is sent.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(conftest.NetworkBlockedError):
            sock.connect(("192.0.2.1", 9))
        with pytest.raises(conftest.NetworkBlockedError):
            sock.connect_ex(("192.0.2.1", 9))


def test_requests_to_a_public_url_fail_without_leaving_the_machine():
    with pytest.raises(requests.exceptions.ConnectionError, match="may not use the network"):
        requests.get("https://example.com/health", timeout=2)


def test_loopback_still_works():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2) as client:
            conn, _ = server.accept()
            conn.close()
            assert client.getpeername() == server.getsockname()
    finally:
        server.close()


def test_unix_sockets_still_work():
    a, b = socket.socketpair()
    try:
        a.sendall(b"ping")
        assert b.recv(4) == b"ping"
    finally:
        a.close()
        b.close()
