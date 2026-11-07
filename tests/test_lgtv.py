"""lgtv.py: restore planning (pure) and the CLI against a fake SSAP TV (needs bscpylgtv)."""

import asyncio
import stat
import subprocess
import http.server
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import lgtv  # noqa: E402

try:
    import bscpylgtv  # noqa: F401
    from websockets.asyncio.server import serve
    HAVE_CLIENT = True
except ImportError:
    HAVE_CLIENT = False


class PlanRestoreTest(unittest.TestCase):
    def test_first_old_value_wins_and_unknown_is_reported(self):
        entries = [
            {"category": "general", "key": "homePromotion", "old": "on", "new": "off"},
            {"category": "general", "key": "homePromotion", "old": "off", "new": "on"},
            {"category": "option", "key": "liveplus", "old": None, "old_known": False, "new": "off"},
            {"category": "option", "key": "x", "old": 3, "new": 0},
        ]
        plan, unknown = lgtv.plan_restore(entries)
        self.assertEqual(plan, [("option", {"x": 3}), ("general", {"homePromotion": "on"})])
        self.assertEqual(unknown, [("option", "liveplus")])

    def test_parse_values(self):
        self.assertEqual(lgtv.parse_assignments(["a=on", "b=3", "c=true", 'd={"x":1}']),
                         {"a": "on", "b": 3, "c": True, "d": {"x": 1}})
        with self.assertRaises(SystemExit):
            lgtv.parse_assignments(["oops"])


@unittest.skipUnless(HAVE_CLIENT, "bscpylgtv not installed")
class SettingsReaderTest(unittest.TestCase):
    class Client:
        def __init__(self, known=None, fail=None):
            self.known, self.fail, self.calls = known or {}, fail, 0

        async def request(self, uri, payload):
            from bscpylgtv.exceptions import PyLGTVCmdError
            self.calls += 1
            if self.fail:
                raise self.fail
            if any(k not in self.known for k in payload["keys"]):
                raise PyLGTVCmdError({"type": "error", "error": "500 Application error"})
            return {"returnValue": True, "settings": {k: self.known[k] for k in payload["keys"]}}

    def read(self, client, keys):
        reader = lgtv.SettingsReader(client, "general", report_skipped=False)
        return asyncio.run(reader.read(keys)), reader

    def test_connection_errors_propagate_single_and_batch(self):
        from bscpylgtv.exceptions import PyLGTVCmdException
        for keys in (["a"], ["a", "b", "c"]):
            client = self.Client(fail=PyLGTVCmdException("Not connected, can't execute command."))
            with self.assertRaises(PyLGTVCmdException):
                self.read(client, keys)
            self.assertEqual(client.calls, 1)

    def test_one_bad_key_among_many_is_found_by_bisection(self):
        keys = [f"k{i}" for i in range(64)]
        client = self.Client(known={k: i for i, k in enumerate(keys) if k != "k37"})
        values, reader = self.read(client, keys)
        self.assertEqual(len(values), 63)
        self.assertEqual(reader.skipped, ["k37"])
        self.assertLessEqual(client.calls, 2 * 6 + 1)

    def test_bulk_reads_leave_out_pins_passwords_and_tokens(self):
        table = {"lock": ["systemPinCode", "camPinCode", "initialPinCode", "systemPin", "secretMode"],
                 "option": ["password_ipcontrol", "IPControlSecureKey", "livePlus"],
                 "voiceframework": ["googleAssistantStateToken", "voice_mic"]}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "keys.json")
            with open(path, "w") as f:
                json.dump(table, f)
            old, lgtv.KEYS_FILE = lgtv.KEYS_FILE, path
            try:
                self.assertEqual(lgtv.known_keys("lock"), ["secretMode"])
                self.assertEqual(lgtv.known_keys("option"), ["livePlus"])
                self.assertEqual(lgtv.known_keys("voiceframework"), ["voice_mic"])
            finally:
                lgtv.KEYS_FILE = old

    def test_bundled_g5_list_has_no_sensitive_keys(self):
        with open(lgtv.KEYS_FILE) as f:
            table = json.load(f)
        for cat, keys in table.items():
            if not cat.startswith("_"):
                self.assertEqual([k for k in keys if lgtv.SENSITIVE_KEY_RE.search(k)], [], cat)


class ScreenshotDownloadTest(unittest.TestCase):
    def test_rejects_foreign_hosts_and_non_http_urls(self):
        invalid = [
            "https://evil.example:3001/image.jpg",
            "file:///tmp/image.jpg",
            "https://user:password@192.168.1.50:3001/image.jpg",
            "https://192.168.1.50.evil.example:3001/image.jpg",
            "https://192.168.1.51:3001/image.jpg",
            "https://192.168.1.50:bad/image.jpg",
            "https://192.168.1.50:0/image.jpg",
        ]
        with patch.object(lgtv.urllib.request, "build_opener") as mock_open:
            for url in invalid:
                with self.subTest(url=url), self.assertRaises(SystemExit):
                    lgtv.download_screenshot(url, "192.168.1.50")
            mock_open.assert_not_called()

    def test_foreign_hostname_is_not_resolved(self):
        with patch.object(lgtv.socket, "getaddrinfo") as lookup:
            with self.assertRaises(SystemExit):
                lgtv.download_screenshot("https://evil.example:3001/image.jpg", "tv.local")
            lookup.assert_not_called()

    def test_tv_hostname_resolves_to_image_ip(self):
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.50", 0))
        with patch.object(lgtv.socket, "getaddrinfo", return_value=[address]) as lookup:
            with patch.object(lgtv.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"JPEG"
                self.assertEqual(lgtv.download_screenshot("https://192.168.1.50:3001/capture.jpg",
                                                          "LGwebOSTV.local"), b"JPEG")
                lookup.assert_called_once_with("lgwebostv.local", None, type=socket.SOCK_STREAM)

    def test_tv_hostname_must_resolve_to_image_ip(self):
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.51", 0))
        with patch.object(lgtv.socket, "getaddrinfo", return_value=[address]):
            with patch.object(lgtv.urllib.request, "build_opener") as mock_open:
                with self.assertRaises(SystemExit):
                    lgtv.download_screenshot("https://192.168.1.50:3001/capture.jpg", "tv.local")
                mock_open.assert_not_called()

    def test_hostname_trailing_dot_is_accepted(self):
        with patch.object(lgtv.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"JPEG"
            self.assertEqual(lgtv.download_screenshot("https://tv.local.:3001/capture.jpg",
                                                      "TV.LOCAL"), b"JPEG")

    def test_https_uses_unverified_context_and_no_proxy(self):
        captured = {}
        context = lgtv.ssl.create_default_context()

        class Opener:
            def open(self, url, timeout):
                captured["url"] = url
                captured["timeout"] = timeout
                return io.BytesIO(b"JPEG")

        def build(*handlers):
            captured["handlers"] = handlers
            return Opener()

        with patch.object(lgtv.ssl, "create_default_context", return_value=context) as make_context:
            with patch.object(lgtv.urllib.request, "build_opener", side_effect=build):
                result = lgtv.download_screenshot("https://192.168.1.50:3001/capture.jpg", "192.168.1.50")

        self.assertEqual(result, b"JPEG")
        self.assertEqual(captured["timeout"], 20)
        make_context.assert_called_once_with()
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, lgtv.ssl.CERT_NONE)
        self.assertTrue(any(isinstance(h, lgtv.urllib.request.HTTPSHandler) for h in captured["handlers"]))
        self.assertTrue(any(isinstance(h, lgtv._NoScreenshotRedirect) for h in captured["handlers"]))
        self.assertTrue(any(isinstance(h, lgtv.urllib.request.ProxyHandler) and h.proxies == {}
                            for h in captured["handlers"]))

    def test_default_http_port_is_accepted(self):
        with patch.object(lgtv.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"JPEG"
            self.assertEqual(lgtv.download_screenshot("http://192.168.1.50/capture.jpg",
                                                      "192.168.1.50"), b"JPEG")

    def test_http_does_not_create_unverified_context(self):
        with patch.object(lgtv.ssl, "create_default_context") as make_context:
            with patch.object(lgtv.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"JPEG"
                self.assertEqual(lgtv.download_screenshot("http://192.168.1.50:3000/capture.jpg",
                                                          "192.168.1.50"), b"JPEG")
            make_context.assert_not_called()

    def test_http_redirect_is_not_followed(self):
        paths = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/redirected?token=secret")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/capture.jpg"
            with self.assertRaises(SystemExit) as err:
                lgtv.download_screenshot(url, "127.0.0.1")
            self.assertIn("refusing screenshot redirect (HTTP 302)", str(err.exception))
            self.assertNotIn("secret", str(err.exception))
            self.assertEqual(paths, ["/capture.jpg"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_download_http_error_is_readable(self):
        with patch.object(lgtv.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError(
                "http://192.168.1.50:3000/capture.jpg", 403, "Forbidden", {}, None)
            with self.assertRaises(SystemExit) as err:
                lgtv.download_screenshot("http://192.168.1.50:3000/capture.jpg", "192.168.1.50")
            self.assertIn("screenshot download failed: HTTP 403", str(err.exception))




class FakeTv:
    """SSAP subset: pairing, apps, settings, the alert-based luna trick, screenshots, input socket."""

    def __init__(self):
        self.settings = {"general": {"homePromotion": "on", "contentRecommendation": "on"},
                         "option": {"livePlus": "off"}, "other": {"simplinkEnable": "on"}}
        self.buttons = []
        self.alerts = {}
        self.alert_count = 0
        self.settings_broken = False
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.http = http.server.HTTPServer(("127.0.0.1", 0), self._image_handler())
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)

    def _image_handler(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200); self.end_headers(); self.wfile.write(b"\xff\xd8JPEG")

            def log_message(self, *a):
                pass
        return H

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def start():
            self.server = await serve(self._handle, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            self.ready.set()
            await asyncio.Future()
        self.loop.run_until_complete(start())

    def apply_luna(self, uri, params):
        if uri == "luna://com.webos.settingsservice/setSystemSettings":
            self.settings.setdefault(params["category"], {}).update(params["settings"])

    async def _handle(self, ws):
        if ws.request.path == "/input":
            async for msg in ws:
                self.buttons.append(msg.split("name:")[1].split("\n")[0])
            return
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "register":
                await ws.send(json.dumps({"type": "response", "id": msg["id"], "payload": {"pairingType": "PROMPT"}}))
                await ws.send(json.dumps({"type": "registered", "id": msg["id"], "payload": {"client-key": "KEY123"}}))
                continue
            uri = msg["uri"][len("ssap://"):]
            p = msg.get("payload") or {}
            out = {"returnValue": True}
            if uri == "settings/getSystemSettings":
                cat = self.settings.get(p["category"], {})
                # Like a real G5: no keys is an error, and so is any key this firmware lacks.
                bad = "service unavailable" if self.settings_broken else "keys are mandatory." if not p.get("keys") else (
                    "unknown key" if any(k not in cat for k in p["keys"]) else None)
                if bad:
                    await ws.send(json.dumps({"type": "error", "id": msg["id"], "error": "500 Application error",
                                              "payload": {"returnValue": False, "errorCode": -1000, "errorText": bad}}))
                    continue
                out["settings"] = {k: cat[k] for k in p["keys"]}
            elif uri == "com.webos.applicationManager/listApps":
                out["apps"] = [{"id": "com.lg.ads", "title": "Ads", "removable": True, "systemApp": False}]
            elif uri == "system.notifications/createAlert":
                self.alert_count += 1
                aid = f"a{self.alert_count}"
                self.alerts[aid] = p
                out["alertId"] = aid
            elif uri == "system.notifications/closeAlert":
                close = self.alerts.pop(p["alertId"])["onclose"]
                self.apply_luna(close["uri"], close["params"])
            elif uri == "tv/executeOneShot":
                out["imageUri"] = f"http://127.0.0.1:{self.http.server_port}/capture.jpg"
            elif uri == "com.webos.service.networkinput/getPointerInputSocket":
                out["socketPath"] = f"ws://127.0.0.1:{self.port}/input"
            await ws.send(json.dumps({"type": "response", "id": msg["id"], "payload": out}))


@unittest.skipUnless(HAVE_CLIENT, "bscpylgtv not installed")
class CliAgainstFakeTvTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tv = FakeTv()
        cls.home = tempfile.TemporaryDirectory()
        cls.env = {"TV_NO_SSL": "1", "TV_PORT": str(cls.tv.port), "TV_HOST": "127.0.0.1"}

    def setUp(self):
        self._old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        lgtv.KEY_FILE = os.path.join(self.home.name, "keys.json")
        lgtv.JsonKeyStorage.__init__.__defaults__ = (lgtv.KEY_FILE,)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lgtv.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_01_pair_then_apps(self):
        code, _, _ = self.cli("pair")
        self.assertEqual(code, 0)
        with open(lgtv.KEY_FILE) as f:
            self.assertEqual(json.load(f), {"127.0.0.1": "KEY123"})
        code, out, _ = self.cli("apps", "--all")
        self.assertEqual(json.loads(out)[0]["id"], "com.lg.ads")

    def test_02_settings_set_logs_and_restore_undoes(self):
        self.cli("pair")
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "changes.jsonl")
            code, out, _ = self.cli("settings-set", "general", "homePromotion=off", "--log", log)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.tv.settings["general"]["homePromotion"], "off")
            self.assertEqual(json.loads(out)["not_applied"], {})
            with open(log) as f:
                entry = json.loads(f.read())
            self.assertEqual((entry["old"], entry["new"]), ("on", "off"))

            code, _, _ = self.cli("restore", log)
            self.assertEqual(code, 0)
            self.assertEqual(self.tv.settings["general"]["homePromotion"], "on")

    def test_02b_refuses_batch_when_an_original_value_is_unreadable(self):
        self.cli("pair")
        alerts_before = self.tv.alert_count
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "changes.jsonl")
            code, _, err = self.cli("settings-set", "general", "homePromotion=off", "liveplus=off", "--log", log)
            self.assertEqual(code, 2)
            self.assertIn("liveplus", err)
            self.assertFalse(os.path.exists(log))
        self.assertEqual(self.tv.alert_count, alerts_before, "no luna write may happen")
        self.assertEqual(self.tv.settings["general"]["homePromotion"], "on")

    def test_02c_restore_with_unknown_original_fails(self):
        self.cli("pair")
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "changes.jsonl")
            with open(log, "w") as f:
                f.write(json.dumps({"category": "general", "key": "homePromotion", "old": "on", "new": "off"}) + "\n")
                f.write(json.dumps({"category": "option", "key": "liveplus", "old": None,
                                    "old_known": False, "new": "off"}) + "\n")
            code, _, err = self.cli("restore", log)
        self.assertEqual(code, 1)
        self.assertIn("NOT RESTORED option.liveplus", err)

    def test_02d_settings_get_without_keys_uses_known_keys_and_skips_missing(self):
        self.cli("pair")
        with tempfile.TemporaryDirectory() as d:
            keys = os.path.join(d, "keys.json")
            with open(keys, "w") as f:
                json.dump({"_source": "test", "general": ["homePromotion", "contentRecommendation", "goneKey"]}, f)
            old = lgtv.KEYS_FILE
            lgtv.KEYS_FILE = keys
            try:
                code, out, err = self.cli("settings-get", "general")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out), {"homePromotion": "on", "contentRecommendation": "on"})
                self.assertIn("general.goneKey: rejected by the TV, skipped", err)
                with self.assertRaises(SystemExit) as ctx:
                    self.cli("settings-get", "nosuchcategory")
                self.assertIn("pass keys explicitly", str(ctx.exception))
            finally:
                lgtv.KEYS_FILE = old

    def test_02e_bundled_g5_keys_cover_privacy_settings(self):
        with open(lgtv.KEYS_FILE) as f:
            table = json.load(f)
        for cat, key in (("general", "homeAutoLaunch"), ("general", "homePromotion"), ("option", "livePlus"),
                         ("support", "automaticUpdate")):
            self.assertIn(key, table[cat])

    def test_02f_backup_is_private_and_reads_settings(self):
        self.cli("pair")
        tools = os.path.join(os.path.dirname(__file__), "..", "tools")
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "backup", "snap")
            env = dict(os.environ, CLEAN_WEBOS_TV_HOME=self.home.name)
            run = subprocess.run(["bash", os.path.join(tools, "backup.sh"), target], env=env,
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)
            with open(os.path.join(target, "STATUS")) as f:
                self.assertEqual(f.readline().strip(), "PARTIAL")  # the fake TV lacks most categories
            path = os.path.join(target, "settings-general.json")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path) as f:
                self.assertEqual(json.load(f), {"homePromotion": "on"})

    def test_02g_backup_fails_when_settings_are_unreadable(self):
        self.cli("pair")
        tools = os.path.join(os.path.dirname(__file__), "..", "tools")
        self.tv.settings_broken = True
        try:
            with tempfile.TemporaryDirectory() as d:
                root = os.path.join(d, "backup")
                target = os.path.join(root, "snap")
                env = dict(os.environ, CLEAN_WEBOS_TV_HOME=self.home.name)
                run = subprocess.run(["bash", os.path.join(tools, "backup.sh"), target], env=env,
                                     capture_output=True, text=True, timeout=120)
                self.assertNotEqual(run.returncode, 0, run.stdout)
                self.assertIn("BACKUP FAILED", run.stderr)
                self.assertFalse(os.path.lexists(os.path.join(root, "latest")))
                with open(os.path.join(target, "STATUS")) as f:
                    self.assertTrue(f.read().startswith("FAILED"))
        finally:
            self.tv.settings_broken = False

    def test_03_settings_set_requires_log(self):
        with self.assertRaises(SystemExit):
            self.cli("settings-set", "general", "homePromotion=off")

    def test_04_buttons_and_screenshot(self):
        self.cli("pair")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jpg")
            code, _, _ = self.cli("screenshot", path, "home", "down", "--delay", "0", "--settle", "0")
            self.assertEqual(code, 0)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"\xff\xd8JPEG")
        self.assertEqual(self.tv.buttons[-2:], ["HOME", "DOWN"])


if __name__ == "__main__":
    unittest.main()
