#!/usr/bin/env python3
"""Drive a webOS web app through Chrome DevTools Protocol (the endpoint ares-inspect opens).

Standard library only. The endpoint is taken from --endpoint, then $DEVTOOLS_URL, then the
.devtools-endpoint file that tools/inspect.sh writes in the root of this repository.

  devtools.py targets                          list inspectable pages
  devtools.py eval 'document.title'            evaluate JS, print JSON result
  devtools.py run snippet.js                   evaluate a JS file
  devtools.py console --seconds 30             stream console, exceptions and log entries
  devtools.py network --seconds 30 --grep m3u8 stream network requests (URL tokens redacted;
                                               --raw-urls to keep them)
  devtools.py dom '#root' --text               outerHTML (or innerText) of a selector
  devtools.py screenshot shots/01.png          capture the app's web layer (video is black)
  devtools.py key Down Down Enter --delay 0.4  dispatch remote keys (Back=461, Blue=406, ...)
  devtools.py reload                           reload the page
"""

import argparse
import base64
import json
import os
import sys
import time
import re
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wsmini import WebSocket  # noqa: E402

# windowsVirtualKeyCode values the webOS remote produces in a web app.
KEYS = {
    "Enter": 13, "Ok": 13,
    "Left": 37, "Up": 38, "Right": 39, "Down": 40,
    "Back": 461,
    "Red": 403, "Green": 404, "Yellow": 405, "Blue": 406,
    "Play": 415, "Pause": 19, "Stop": 413, "Rewind": 412, "FastForward": 417,
    "ChannelUp": 33, "ChannelDown": 34,
    "0": 48, "1": 49, "2": 50, "3": 51, "4": 52, "5": 53, "6": 54, "7": 55, "8": 56, "9": 57,
}
DOM_KEY = {"Enter": "Enter", "Ok": "Enter", "Left": "ArrowLeft", "Up": "ArrowUp",
           "Right": "ArrowRight", "Down": "ArrowDown"}


def key_code(name):
    if name.isdigit() and len(name) > 1:
        return int(name)  # raw keyCode, e.g. 10133
    for known, code in KEYS.items():
        if known.lower() == name.lower():
            return code
    raise SystemExit(f"unknown key {name!r}; known: {', '.join(KEYS)} or a numeric keyCode")


# inspect.sh keeps its runtime files in this repository (git-ignored), never in the app's worktree.
ENDPOINT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".devtools-endpoint")

URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s\"'<>)]+")
# Path segments that look like tokens/signatures: long runs of base64/hex-ish characters.
SECRET_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_=~.+-]{24,}$")


def redact_url(url):
    """Drop userinfo, query values, fragment and token-like path segments from one URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<url>"
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    segments = []
    for seg in parts.path.split("/"):
        stem = seg.rsplit(".", 1)[0] if "." in seg[-6:] else seg
        segments.append("<redacted>" if SECRET_SEGMENT_RE.match(stem) else seg)
    out = f"{parts.scheme}://{host}{'/'.join(segments)}"
    if parts.query:
        names = [pair.split("=", 1)[0] for pair in parts.query.split("&") if pair]
        out += "?" + "&".join(f"{n}=<redacted>" for n in names)
    return out


def redact_text(text):
    return URL_RE.sub(lambda m: redact_url(m.group(0)), text)


def find_endpoint(arg):
    if arg:
        return arg.rstrip("/")
    if os.environ.get("DEVTOOLS_URL"):
        return os.environ["DEVTOOLS_URL"].rstrip("/")
    if os.path.exists(ENDPOINT_FILE):
        with open(ENDPOINT_FILE, encoding="utf-8") as f:
            return f.read().strip().rstrip("/")
    raise SystemExit("no DevTools endpoint: run tools/inspect.sh, or pass --endpoint http://host:port")


def list_targets(endpoint):
    last = None
    for path in ("/json/list", "/json"):
        try:
            with urllib.request.urlopen(endpoint + path, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - try the next path
            last = e
    raise SystemExit(f"cannot list targets at {endpoint}: {last}")


def pick_target(endpoint, match=None):
    targets = [t for t in list_targets(endpoint) if t.get("webSocketDebuggerUrl")]
    pages = [t for t in targets if t.get("type", "page") == "page"] or targets
    if match:
        pages = [t for t in pages if match in t.get("url", "") or match in t.get("title", "")]
    if not pages:
        raise SystemExit("no inspectable page (is the app running and was ares-inspect started?)")
    return pages[0]


class Cdp:
    def __init__(self, ws_url):
        self.ws = WebSocket(ws_url)
        self.next_id = 0
        self.events = []

    def call(self, method, params=None, timeout=30):
        self.next_id += 1
        my_id = self.next_id
        self.ws.send(json.dumps({"id": my_id, "method": method, "params": params or {}}))
        self.ws.settimeout(timeout)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == my_id:
                if "error" in msg:
                    raise SystemExit(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def next_event(self, timeout):
        if self.events:
            return self.events.pop(0)
        self.ws.settimeout(timeout)
        try:
            return json.loads(self.ws.recv())
        except (TimeoutError, OSError):
            return None

    def evaluate(self, expression, await_promise=True):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": await_promise,
        })
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text")
            raise SystemExit(f"JS exception: {text}")
        value = result.get("result", {})
        return value.get("value", value.get("description"))

    def close(self):
        self.ws.close()


def connect(args):
    endpoint = find_endpoint(args.endpoint)
    target = pick_target(endpoint, args.target)
    ws_url = target["webSocketDebuggerUrl"]
    # Through an ares-inspect tunnel the TV may advertise its own address; keep the tunnel's.
    if endpoint.startswith("http://"):
        host = endpoint[len("http://"):]
        ws_url = "ws://" + host + "/" + ws_url.split("/", 3)[3]
    return Cdp(ws_url)


def fmt_remote_object(obj):
    if "value" in obj:
        v = obj["value"]
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return obj.get("description") or obj.get("type", "")


def stream(cdp, seconds, grep, formatter, raw_urls=False):
    deadline = time.time() + seconds
    while time.time() < deadline:
        msg = cdp.next_event(max(0.1, deadline - time.time()))
        if not msg:
            continue
        line = formatter(msg)
        if line and not raw_urls:
            # Signed CDN links in playlists/segments are bearer credentials; keep them out of transcripts.
            line = redact_text(line)
        if line and (not grep or grep.lower() in line.lower()):
            print(line, flush=True)


def event_time(p):
    """Console events carry ms since epoch; Runtime.enable replays older ones, so prefer it."""
    ms = p.get("timestamp") or p.get("entry", {}).get("timestamp")
    if isinstance(ms, (int, float)) and ms > 1e12:
        return time.strftime("%H:%M:%S", time.localtime(ms / 1000)) + f".{int(ms % 1000):03d}"
    return time.strftime("%H:%M:%S")


def console_line(msg):
    method, p = msg.get("method"), msg.get("params", {})
    ts = event_time(p)
    if method == "Runtime.consoleAPICalled":
        text = " ".join(fmt_remote_object(a) for a in p.get("args", []))
        return f"{ts} console.{p.get('type')}: {text}"
    if method == "Runtime.exceptionThrown":
        d = p.get("exceptionDetails", {})
        return f"{ts} EXCEPTION: {d.get('exception', {}).get('description') or d.get('text')}"
    if method == "Log.entryAdded":
        e = p.get("entry", {})
        return f"{ts} log.{e.get('level')} [{e.get('source')}]: {e.get('text')} {e.get('url', '')}".rstrip()
    return None


def network_line(msg):
    method, p = msg.get("method"), msg.get("params", {})
    ts = time.strftime("%H:%M:%S")
    rid = p.get("requestId", "")
    if method == "Network.requestWillBeSent":
        return f"{ts} -> {rid} {p['request'].get('method')} {p['request'].get('url')}"
    if method == "Network.responseReceived":
        r = p.get("response", {})
        return f"{ts} <- {rid} {r.get('status')} {r.get('mimeType')} {r.get('url')}"
    if method == "Network.loadingFinished":
        return f"{ts} ok {rid} {int(p.get('encodedDataLength', 0))} bytes"
    if method == "Network.loadingFailed":
        return f"{ts} FAIL {rid} {p.get('errorText')} canceled={p.get('canceled', False)}"
    return None


SYNTHETIC_KEY_JS = """
(function (keyCode, key, type) {
  var ev = new KeyboardEvent(type, {key: key, bubbles: true, cancelable: true});
  Object.defineProperty(ev, 'keyCode', {get: function () { return keyCode; }});
  Object.defineProperty(ev, 'which', {get: function () { return keyCode; }});
  (document.activeElement || document.body).dispatchEvent(ev);
  return true;
})(%d, %s, %s)
"""


def press(cdp, name, synthetic):
    code = key_code(name)
    dom_key = DOM_KEY.get(name.capitalize(), name)
    if synthetic:
        for t in ("keydown", "keyup"):
            cdp.evaluate(SYNTHETIC_KEY_JS % (code, json.dumps(dom_key), json.dumps(t)))
        return
    for t in ("rawKeyDown", "keyUp"):
        cdp.call("Input.dispatchKeyEvent", {
            "type": t, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code, "key": dom_key,
        })


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", help="http://host:port printed by ares-inspect")
    ap.add_argument("--target", help="substring of the page URL/title to pick")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("targets")
    p = sub.add_parser("eval"); p.add_argument("expression")
    p = sub.add_parser("run"); p.add_argument("file")
    for name in ("console", "network"):
        p = sub.add_parser(name)
        p.add_argument("--seconds", type=float, default=20)
        p.add_argument("--grep")
        p.add_argument("--raw-urls", action="store_true",
                       help="print URLs verbatim, tokens included (default: query values and token-like path parts redacted)")
    p = sub.add_parser("dom"); p.add_argument("selector", nargs="?", default="body")
    p.add_argument("--text", action="store_true", help="innerText instead of outerHTML")
    p = sub.add_parser("screenshot"); p.add_argument("path")
    p = sub.add_parser("key"); p.add_argument("keys", nargs="+")
    p.add_argument("--delay", type=float, default=0.4)
    p.add_argument("--synthetic", action="store_true", help="dispatch a JS KeyboardEvent instead of Input.dispatchKeyEvent")
    sub.add_parser("reload")
    args = ap.parse_args(argv)

    if args.cmd == "targets":
        for t in list_targets(find_endpoint(args.endpoint)):
            print(json.dumps({k: t.get(k) for k in ("type", "title", "url", "webSocketDebuggerUrl")}, ensure_ascii=False))
        return 0

    cdp = connect(args)
    try:
        if args.cmd == "eval":
            v = cdp.evaluate(args.expression)
            print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2))
        elif args.cmd == "run":
            with open(args.file, encoding="utf-8") as f:
                v = cdp.evaluate(f.read())
            print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2))
        elif args.cmd == "console":
            cdp.call("Runtime.enable")
            try:
                cdp.call("Log.enable")
            except SystemExit:
                pass
            stream(cdp, args.seconds, args.grep, console_line, args.raw_urls)
        elif args.cmd == "network":
            cdp.call("Network.enable")
            stream(cdp, args.seconds, args.grep, network_line, args.raw_urls)
        elif args.cmd == "dom":
            prop = "innerText" if args.text else "outerHTML"
            expr = f"(function(){{var e=document.querySelector({json.dumps(args.selector)});return e?e.{prop}:null}})()"
            v = cdp.evaluate(expr)
            if v is None:
                print(f"no element matches {args.selector}", file=sys.stderr)
                return 1
            print(v)
        elif args.cmd == "screenshot":
            data = cdp.call("Page.captureScreenshot", {"format": "png"}, timeout=60).get("data", "")
            raw = base64.b64decode(data)
            if not raw:
                print("empty screenshot", file=sys.stderr)
                return 1
            os.makedirs(os.path.dirname(os.path.abspath(args.path)), exist_ok=True)
            with open(args.path, "wb") as f:
                f.write(raw)
            print(f"{args.path} ({len(raw)} bytes; the video plane is not part of this capture)")
        elif args.cmd == "key":
            for i, k in enumerate(args.keys):
                press(cdp, k, args.synthetic)
                if i < len(args.keys) - 1:
                    time.sleep(args.delay)
        elif args.cmd == "reload":
            cdp.call("Page.reload", {"ignoreCache": True})
    finally:
        cdp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
