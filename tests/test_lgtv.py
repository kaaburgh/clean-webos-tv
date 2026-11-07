"""lgtv.py: restore planning (pure) and the CLI against a fake SSAP TV (needs bscpylgtv)."""

import asyncio
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
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


class FakeTv:
    """SSAP subset: pairing, apps, settings, the alert-based luna trick, screenshots, input socket."""

    def __init__(self):
        self.settings = {"general": {"homePromotion": "on", "contentRecommendation": "on"}}
        self.buttons = []
        self.alerts = {}
        self.alert_count = 0
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
                bad = "keys are mandatory." if not p.get("keys") else (
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
                self.assertIn("general.goneKey: not readable", err)
                with self.assertRaises(SystemExit) as ctx:
                    self.cli("settings-get", "nosuchcategory")
                self.assertIn("pass keys explicitly", str(ctx.exception))
            finally:
                lgtv.KEYS_FILE = old

    def test_02e_bundled_g5_keys_cover_privacy_settings(self):
        with open(lgtv.KEYS_FILE) as f:
            table = json.load(f)
        for cat, key in (("general", "homeAutoLaunch"), ("general", "homePromotion"), ("option", "livePlus"),
                         ("other", "contentRecommendation")):
            self.assertIn(key, table[cat])

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
