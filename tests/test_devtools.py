"""devtools.py against a fake DevTools endpoint (HTTP /json/list + CDP over WebSocket)."""

import base64
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

ROOT = os.path.join(os.path.dirname(__file__), "..", "tools")
sys.path.insert(0, ROOT)

import devtools  # noqa: E402
import wsmini  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakeDevTools:
    """One-thread-per-connection server that speaks just enough HTTP and CDP."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.calls = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(1)
        text = head.decode()
        path = text.split(" ")[1]
        if "upgrade: websocket" not in text.lower():
            body = json.dumps([{
                "type": "page", "title": "Kinopub", "url": "file:///media/developer/apps/kinopub/index.html",
                # Advertise the TV address, as a tunnelled TV does; the client must keep the endpoint host.
                "webSocketDebuggerUrl": "ws://10.0.0.99:9998/devtools/page/1",
            }]).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            conn.close()
            return
        assert path == "/devtools/page/1", path
        key = [l.split(":", 1)[1].strip() for l in text.split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {wsmini.accept_key(key)}\r\n\r\n").encode())
        send = lambda obj: conn.sendall(wsmini.encode_frame(wsmini.OP_TEXT, json.dumps(obj).encode(), mask=False))
        try:
            while True:
                fin, op, payload = wsmini.read_frame(conn)
                if op == wsmini.OP_CLOSE:
                    return
                msg = json.loads(payload)
                self.calls.append(msg)
                m, p, i = msg["method"], msg.get("params", {}), msg["id"]
                if m == "Runtime.evaluate":
                    expr = p["expression"]
                    if "throw" in expr:
                        send({"id": i, "result": {"result": {}, "exceptionDetails": {"text": "boom"}}})
                    else:
                        send({"id": i, "result": {"result": {"type": "object", "value": {"expr": expr[:40]}}}})
                elif m == "Page.captureScreenshot":
                    send({"id": i, "result": {"data": base64.b64encode(PNG).decode()}})
                elif m == "Runtime.enable":
                    # An event before the reply must not be lost.
                    send({"method": "Runtime.consoleAPICalled",
                          "params": {"type": "warn", "args": [{"type": "string", "value": "[hls] level switched"}]}})
                    send({"id": i, "result": {}})
                    send({"method": "Runtime.exceptionThrown",
                          "params": {"exceptionDetails": {"exception": {"description": "TypeError: x"}}}})
                else:
                    send({"id": i, "result": {}})
        except (wsmini.WebSocketError, OSError):
            return
        finally:
            conn.close()


class DevtoolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeDevTools()
        cls.endpoint = f"http://127.0.0.1:{cls.server.port}"

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = devtools.main(["--endpoint", self.endpoint, *args])
        return code, out.getvalue()

    def test_eval_returns_value(self):
        code, out = self.run_cli("eval", "document.title")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"expr": "document.title"})

    def test_eval_exception_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("eval", "throw new Error()")
        self.assertIn("boom", str(ctx.exception))

    def test_screenshot_writes_png(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "shots", "a.png")
            code, _ = self.run_cli("screenshot", path)
            self.assertEqual(code, 0)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), PNG)

    def test_console_keeps_events_around_replies(self):
        code, out = self.run_cli("console", "--seconds", "0.5")
        self.assertEqual(code, 0)
        self.assertIn("console.warn: [hls] level switched", out)
        self.assertIn("EXCEPTION: TypeError: x", out)

    def test_console_grep(self):
        _, out = self.run_cli("console", "--seconds", "0.5", "--grep", "HLS")
        self.assertIn("[hls]", out)
        self.assertNotIn("TypeError", out)

    def test_keys_use_webos_codes(self):
        before = len(self.server.calls)
        code, _ = self.run_cli("key", "Down", "Back", "Blue", "10133", "--delay", "0")
        self.assertEqual(code, 0)
        sent = [c["params"]["windowsVirtualKeyCode"] for c in self.server.calls[before:]
                if c["method"] == "Input.dispatchKeyEvent" and c["params"]["type"] == "rawKeyDown"]
        self.assertEqual(sent, [40, 461, 406, 10133])

    def test_synthetic_keys_go_through_js(self):
        before = len(self.server.calls)
        self.run_cli("key", "Enter", "--synthetic")
        exprs = [c["params"]["expression"] for c in self.server.calls[before:] if c["method"] == "Runtime.evaluate"]
        self.assertEqual(len(exprs), 2)
        self.assertIn("(13, \"Enter\", \"keydown\")", exprs[0])

    def test_unknown_key(self):
        with self.assertRaises(SystemExit):
            devtools.key_code("Teleport")


class FrameTest(unittest.TestCase):
    def test_roundtrip_lengths(self):
        for n in (0, 125, 126, 65535, 65536):
            a, b = socket.socketpair()
            payload = os.urandom(n)
            threading.Thread(target=a.sendall, args=(wsmini.encode_frame(wsmini.OP_BINARY, payload),)).start()
            fin, op, got = wsmini.read_frame(b)
            self.assertTrue(fin)
            self.assertEqual(op, wsmini.OP_BINARY)
            self.assertEqual(got, payload)
            a.close(); b.close()


if __name__ == "__main__":
    unittest.main()
