#!/usr/bin/env python3
"""LG webOS TV over its network remote API (SSAP, wss://TV:3001), the webOS stand-in for adb.

Needs `pip install -r tools/requirements.txt` (bscpylgtv). The TV is $TV_HOST or --host
($TV_PORT and TV_NO_SSL=1 for firmware that only answers ws://TV:3000).
The first command shows a pairing prompt on the TV; the key is kept in
~/.config/clean-webos-tv/keys.json (or $CLEAN_WEBOS_TV_HOME/keys.json).

  lgtv.py pair                              pair once (accept the prompt on the TV)
  lgtv.py info                              model, firmware, current app
  lgtv.py apps [--all]                      launcher entries, or every installed app
  lgtv.py current                           foreground app id
  lgtv.py launch <appId> [--params JSON]    start an app
  lgtv.py close <appId>                     close an app
  lgtv.py button HOME DOWN ENTER            press remote buttons (names: lgtv.py buttons)
  lgtv.py screenshot shots/01.jpg [KEYS..]  press keys, then capture the whole screen
  lgtv.py settings-get <category> [keys..]  read system settings
  lgtv.py settings-set <category> k=v ..    change settings, logged for restore (--log)
  lgtv.py restore <changes.jsonl>           undo every logged settings change
  lgtv.py ssap <uri> [JSON]                 raw SSAP request (prints the response)
  lgtv.py luna <uri> [JSON]                 raw luna call via the alert trick (no response)
  lgtv.py toast <text>                      show a notification on the TV
  lgtv.py reboot                            reboot the TV
"""

import argparse
import asyncio
import ipaddress
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

CONFIG_DIR = os.environ.get("CLEAN_WEBOS_TV_HOME") or os.path.join(os.path.expanduser("~"), ".config", "clean-webos-tv")
KEY_FILE = os.path.join(CONFIG_DIR, "keys.json")


class JsonKeyStorage:
    """bscpylgtv StorageProto backed by a JSON file (avoids the sqlitedict dependency at runtime)."""

    def __init__(self, path=KEY_FILE):
        self.path = path

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    async def get_key(self, key):
        return self._load().get(key)

    async def set_key(self, key, val):
        data = self._load()
        data[key] = val
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.chmod(self.path, 0o600)

    async def list_keys(self):
        return self._load()


def parse_value(text):
    """`on` stays a string, `true`/`3`/`{"a":1}` become JSON values."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def parse_assignments(items):
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"expected key=value, got {item!r}")
        k, v = item.split("=", 1)
        out[k] = parse_value(v)
    return out


def read_changes(path):
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def plan_restore(entries):
    """Return [(category, {key: original_value})] that puts every touched key back.

    The original value of a key is the `old` of the first change to it; keys whose original
    value was unknown (the TV did not report it) are skipped and listed in the second result.
    """
    originals, unknown, order = {}, [], []
    for e in entries:
        ident = (e["category"], e["key"])
        if ident in originals or ident in unknown:
            continue
        if not e.get("old_known", True):
            unknown.append(ident)
            continue
        originals[ident] = e["old"]
        if e["category"] not in order:
            order.append(e["category"])
    plan = []
    for cat in reversed(order):
        settings = {k: v for (c, k), v in originals.items() if c == cat}
        plan.append((cat, settings))
    return plan, unknown


async def client(host, pair_ok=True):
    try:
        from bscpylgtv import WebOsClient
    except ImportError:
        raise SystemExit("bscpylgtv is missing: python3 -m venv .venv && . .venv/bin/activate && pip install -r tools/requirements.txt")
    storage = JsonKeyStorage()
    key = await storage.get_key(host)
    if not key and not pair_ok:
        raise SystemExit("not paired yet: run `lgtv.py pair` and accept the prompt on the TV")
    no_ssl = os.environ.get("TV_NO_SSL") == "1"  # old firmware: ws://TV:3000
    c = await WebOsClient.create(host, client_key=key, storage=storage, states=[], timeout_connect=5,
                                 without_ssl=no_ssl)
    if os.environ.get("TV_PORT"):
        c.port = int(os.environ["TV_PORT"])
    if key is None:
        print("Accept the pairing prompt on the TV...", file=sys.stderr)
    await c.connect()
    if c.client_key and c.client_key != key:
        await storage.set_key(host, c.client_key)
    return c


async def get_settings(c, category, keys):
    payload = {"category": category}
    if keys:
        payload["keys"] = list(keys)
    res = await c.request("settings/getSystemSettings", payload)
    return res.get("settings", {})


class _NoScreenshotRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow the TV's screenshot URL to an untrusted host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _matches_tv_host(uri_host, expected_host):
    """Accept the configured TV name or its resolved IP, never a foreign DNS name."""
    uri_host = (uri_host or "").lower().rstrip(".")
    expected_host = expected_host.lower().rstrip(".")
    if uri_host == expected_host:
        return True

    # Only resolve the explicitly configured TV hostname, not an arbitrary hostname
    # supplied by the remote imageUri (avoids DNS lookups for untrusted URLs).
    try:
        uri_ip = ipaddress.ip_address(uri_host)
    except ValueError:
        return False
    try:
        return uri_ip == ipaddress.ip_address(expected_host)
    except ValueError:
        pass
    try:
        addresses = socket.getaddrinfo(expected_host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for address in addresses:
        try:
            if ipaddress.ip_address(address[4][0]) == uri_ip:
                return True
        except ValueError:
            continue
    return False


def download_screenshot(uri, expected_host):
    """Download only from the paired TV host; accept its local HTTPS certificate.

    webOS may return a self-signed HTTPS imageUri for tv/executeOneShot. Disabling
    verification is confined to this one request, and HTTP redirects are forbidden.
    """
    try:
        parsed = urlsplit(uri)
        # Reading parsed.port also rejects malformed/non-numeric explicit ports.
        port = parsed.port
        valid_url = (parsed.scheme in ("http", "https") and
                     (port is None or port > 0) and
                     parsed.username is None and parsed.password is None and
                     _matches_tv_host(parsed.hostname, expected_host))
    except ValueError:
        raise SystemExit("refusing malformed TV screenshot URL") from None
    if not valid_url:
        raise SystemExit("refusing screenshot URL: expected HTTP(S) on the paired TV host")

    # A screenshot is a LAN request; never honor proxy settings from the environment.
    handlers = [urllib.request.ProxyHandler({}), _NoScreenshotRedirect()]
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        # The TV uses a locally issued certificate: scope the exception to its image URL.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(uri, timeout=20) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SystemExit(f"refusing screenshot redirect (HTTP {exc.code})") from None
        raise SystemExit(f"screenshot download failed: HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
        raise SystemExit(f"screenshot download failed: {exc}") from None


async def take_screenshot(c, path, expected_host):
    res = await c.request("tv/executeOneShot")
    uri = res.get("imageUri")
    if not uri:
        raise SystemExit(f"TV returned no imageUri: {res}")
    data = download_screenshot(uri, expected_host)
    if not data:
        print("empty screenshot (protected content or Live TV?)", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print(f"{path} ({len(data)} bytes)")
    return 0


async def press(c, names, delay):
    from bscpylgtv import buttons
    for i, name in enumerate(names):
        name = name.upper()
        if name == "HOME":
            # HOME is not in every firmware's button list; the input socket accepts it anyway.
            await c.button(name, checkValid=False)
        else:
            await c.button(name, checkValid=name in buttons.BUTTONS)
        if i < len(names) - 1 or delay:
            await asyncio.sleep(delay)


def dump(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


async def run(args):
    if args.cmd == "buttons":
        from bscpylgtv import buttons
        print(" ".join(sorted(set(buttons.BUTTONS) | {"HOME"})))
        return 0
    if args.cmd == "plan-restore":
        plan, unknown = plan_restore(read_changes(args.log))
        dump({"plan": plan, "unknown": unknown})
        return 0

    host = args.host or os.environ.get("TV_HOST")
    if not host:
        raise SystemExit("set TV_HOST or pass --host <tv ip>")
    c = await client(host, pair_ok=args.cmd == "pair")
    try:
        return await dispatch(c, args, host)
    finally:
        await c.disconnect()


async def dispatch(c, args, host):
    cmd = args.cmd
    if cmd == "pair":
        print("paired; key saved to", KEY_FILE)
    elif cmd == "info":
        info = {}
        for name, uri in (("system", "system/getSystemInfo"),
                          ("software", "com.webos.service.update/getCurrentSWInformation"),
                          ("foreground", "com.webos.applicationManager/getForegroundAppInfo")):
            try:
                info[name] = await c.request(uri)
            except Exception as e:  # noqa: BLE001 - report what the firmware refuses
                info[name] = {"error": str(e)}
        dump(info)
    elif cmd == "apps":
        if args.all:
            res = await c.request("com.webos.applicationManager/listApps")
            apps = [{k: a.get(k) for k in ("id", "title", "version", "systemApp", "removable", "visible", "vendor")}
                    for a in res.get("apps", [])]
        else:
            res = await c.request("com.webos.applicationManager/listLaunchPoints")
            apps = [{k: a.get(k) for k in ("id", "title", "removable", "systemApp")}
                    for a in res.get("launchPoints", [])]
        dump(sorted(apps, key=lambda a: a.get("id") or ""))
    elif cmd == "current":
        res = await c.request("com.webos.applicationManager/getForegroundAppInfo")
        print(res.get("appId", ""))
    elif cmd == "launch":
        payload = {"id": args.app_id}
        if args.params:
            payload["params"] = json.loads(args.params)
        dump(await c.request("system.launcher/launch", payload))
    elif cmd == "close":
        dump(await c.request("system.launcher/close", {"id": args.app_id}))
    elif cmd == "button":
        await press(c, args.names, args.delay)
    elif cmd == "screenshot":
        if args.keys:
            await press(c, args.keys, args.delay)
            await asyncio.sleep(args.settle)
        return await take_screenshot(c, args.path, host)
    elif cmd == "settings-get":
        dump(await get_settings(c, args.category, args.keys))
    elif cmd == "settings-set":
        wanted = parse_assignments(args.assignments)
        before = await get_settings(c, args.category, list(wanted))
        unreadable = [k for k in wanted if k not in before]
        if unreadable:
            # Without the original value the change could not be undone: refuse the whole batch.
            print(f"refusing to change {args.category}: the TV did not report the current value of "
                  f"{', '.join(unreadable)}, so restore could not put it back. Change it through the "
                  "menu and record it in manual.md instead.", file=sys.stderr)
            return 2
        if args.log and not args.dry_run:
            os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
            with open(args.log, "a", encoding="utf-8") as f:
                for k, v in wanted.items():
                    f.write(json.dumps({"ts": int(time.time()), "category": args.category, "key": k,
                                        "old": before[k], "new": v},
                                       ensure_ascii=False) + "\n")
        if args.dry_run:
            dump({"before": before, "would_set": wanted})
            return 0
        await c.luna_request("com.webos.settingsservice/setSystemSettings",
                             {"category": args.category, "settings": wanted})
        await asyncio.sleep(1.5)
        after = await get_settings(c, args.category, list(wanted))
        mismatched = {k: after.get(k) for k, v in wanted.items() if after.get(k) != v}
        dump({"before": before, "after": after, "not_applied": mismatched})
        return 1 if mismatched else 0
    elif cmd == "restore":
        plan, unknown = plan_restore(read_changes(args.log))
        failed = 0
        for category, settings in plan:
            print(f"restoring {category}: {json.dumps(settings, ensure_ascii=False)}")
            if args.dry_run:
                continue
            await c.luna_request("com.webos.settingsservice/setSystemSettings",
                                 {"category": category, "settings": settings})
            await asyncio.sleep(1.5)
            after = await get_settings(c, category, list(settings))
            for k, v in settings.items():
                if after.get(k) != v:
                    failed += 1
                    print(f"  NOT RESTORED {category}.{k}: now {after.get(k)!r}, want {v!r}", file=sys.stderr)
        for category, key in unknown:
            print(f"  NOT RESTORED {category}.{key}: original value was not recorded; set it by hand", file=sys.stderr)
        # Anything left changed, known or not, means the rollback is incomplete.
        return 1 if failed or unknown else 0
    elif cmd == "ssap":
        uri = args.uri[len("ssap://"):] if args.uri.startswith("ssap://") else args.uri
        dump(await c.request(uri, json.loads(args.payload) if args.payload else None))
    elif cmd == "luna":
        uri = args.uri[len("luna://"):] if args.uri.startswith("luna://") else args.uri
        await c.luna_request(uri, json.loads(args.payload) if args.payload else {})
        print("sent (the alert trick returns no result: verify by reading state back)")
    elif cmd == "reboot":
        await c.reboot()
    elif cmd == "toast":
        await c.request("system.notifications/createToast", {"message": args.text})
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", help="TV IP (default $TV_HOST)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pair"); sub.add_parser("info"); sub.add_parser("current"); sub.add_parser("buttons")
    sub.add_parser("reboot")
    p = sub.add_parser("apps"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("launch"); p.add_argument("app_id"); p.add_argument("--params")
    p = sub.add_parser("close"); p.add_argument("app_id")
    p = sub.add_parser("button"); p.add_argument("names", nargs="+"); p.add_argument("--delay", type=float, default=0.4)
    p = sub.add_parser("screenshot"); p.add_argument("path"); p.add_argument("keys", nargs="*")
    p.add_argument("--delay", type=float, default=0.4); p.add_argument("--settle", type=float, default=1.0)
    p = sub.add_parser("settings-get"); p.add_argument("category"); p.add_argument("keys", nargs="*")
    p = sub.add_parser("settings-set"); p.add_argument("category"); p.add_argument("assignments", nargs="+")
    p.add_argument("--log", help="append old/new values here (backup/<date>/changes.jsonl)")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("restore"); p.add_argument("log"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("plan-restore"); p.add_argument("log")
    p = sub.add_parser("ssap"); p.add_argument("uri"); p.add_argument("payload", nargs="?")
    p = sub.add_parser("luna"); p.add_argument("uri"); p.add_argument("payload", nargs="?")
    p = sub.add_parser("toast"); p.add_argument("text")
    args = ap.parse_args(argv)
    if args.cmd == "settings-set" and not args.log and not args.dry_run:
        raise SystemExit("settings-set needs --log backup/<date>/changes.jsonl so the change can be undone")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
