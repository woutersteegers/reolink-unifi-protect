"""Tests must never reach a real camera (or anything beyond this machine).

Any socket connect or datagram send to a non-loopback address fails the test, so a
mistaken real ``Reboot`` or settings write cannot leave the test process.
"""

import socket

import pytest

_LOOPBACK = ("127.", "::1", "localhost")


def _is_local(address) -> bool:
    if isinstance(address, (str, bytes)):  # unix socket path
        return True
    host = str(address[0])
    return host.startswith(_LOOPBACK) or host in ("", "0.0.0.0")


@pytest.fixture(autouse=True)
def no_outside_network(monkeypatch):
    real_connect = socket.socket.connect
    real_sendto = socket.socket.sendto

    def connect(self, address):
        if not _is_local(address):
            raise AssertionError(f"test tried to connect to {address!r}")
        return real_connect(self, address)

    def sendto(self, data, *args):
        address = args[-1]
        if not _is_local(address):
            raise AssertionError(f"test tried to send to {address!r}")
        return real_sendto(self, data, *args)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
