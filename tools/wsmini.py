"""Minimal RFC 6455 WebSocket client on the standard library.

Used by devtools.py to speak the Chrome DevTools Protocol to a webOS web app, so that the
development loop needs no pip packages. Plain ws:// only: the DevTools endpoint that
ares-inspect exposes (directly on the TV or through its local tunnel) is not TLS.
"""

import base64
import hashlib
import os
import socket
import struct
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def encode_frame(opcode, payload, mask=True):
    """Encode one final frame. Clients must mask, servers must not."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 1 << 16:
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + key + masked


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WebSocketError("connection closed")
        buf += chunk
    return bytes(buf)


def read_frame(sock):
    """Read one frame and return (fin, opcode, payload), unmasking if needed."""
    b0, b1 = _recv_exact(sock, 2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _recv_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _recv_exact(sock, 8))
    key = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length)
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


class WebSocket:
    def __init__(self, url, timeout=10.0):
        parsed = urlparse(url)
        if parsed.scheme != "ws":
            raise WebSocketError(f"only ws:// is supported, got {url}")
        host = parsed.hostname
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(1)
            if not chunk:
                raise WebSocketError("handshake: connection closed")
            response += chunk
        head = response.decode("latin-1")
        status = head.split("\r\n", 1)[0]
        if " 101 " not in status + " ":
            raise WebSocketError(f"handshake failed: {status}")
        headers = {}
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise WebSocketError("handshake: bad Sec-WebSocket-Accept")

    def settimeout(self, seconds):
        self.sock.settimeout(seconds)

    def send(self, text):
        self.sock.sendall(encode_frame(OP_TEXT, text.encode("utf-8")))

    def recv(self):
        """Return the next text/binary message as str; answers pings transparently."""
        parts = []
        while True:
            fin, opcode, payload = read_frame(self.sock)
            if opcode == OP_PING:
                self.sock.sendall(encode_frame(OP_PONG, payload))
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                raise WebSocketError("closed by peer")
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self):
        try:
            self.sock.sendall(encode_frame(OP_CLOSE, b"\x03\xe8"))
        except OSError:
            pass
        self.sock.close()
