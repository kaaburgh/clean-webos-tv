#!/usr/bin/env bash
# Start ares-inspect for an app and leave it running in the background, so tools/devtools.py
# can reach the app's DevTools. Runtime files (.devtools-endpoint, .ares-inspect.log/.pid) live in
# the root of this repository, where they are git-ignored, not in the app's worktree.
# Usage: tools/inspect.sh <ares-device> <app-id>      stop: tools/inspect.sh --stop
set -euo pipefail

ares() {
  local cmd="$1"; shift
  # ARES_GLOBAL=1 skips the project-local CLI (e.g. to use @webos-tools/cli instead of @webosose/ares-cli).
  if [[ "${ARES_GLOBAL:-0}" != 1 && -x node_modules/.bin/$cmd ]]; then node_modules/.bin/"$cmd" "$@"; else "$cmd" "$@"; fi
}

state="$(cd "$(dirname "$0")/.." && pwd)"
endpoint_file="$state/.devtools-endpoint"
log="$state/.ares-inspect.log"
pidfile="$state/.ares-inspect.pid"

if [[ "${1:-}" == "--stop" ]]; then
  [[ -f "$pidfile" ]] && kill "$(cat "$pidfile")" 2>/dev/null || true
  rm -f "$pidfile" "$endpoint_file"
  exit 0
fi

device="${1:?usage: tools/inspect.sh <ares-device> <app-id>}"
app="${2:?usage: tools/inspect.sh <ares-device> <app-id>}"

[[ -f "$pidfile" ]] && kill "$(cat "$pidfile")" 2>/dev/null || true
rm -f "$endpoint_file"
: >"$log"
ares ares-inspect --device "$device" --app "$app" >"$log" 2>&1 &
echo $! >"$pidfile"

for _ in $(seq 1 60); do
  url="$(grep -oE 'https?://[A-Za-z0-9._-]+:[0-9]+' "$log" | head -n1 || true)"
  if [[ -n "$url" ]]; then
    echo "$url" >"$endpoint_file"
    echo "DevTools endpoint: $url (ares-inspect pid $(cat "$pidfile"), log $log)"
    exit 0
  fi
  if ! kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "ares-inspect exited:" >&2; cat "$log" >&2; exit 1
  fi
  sleep 0.5
done
echo "ares-inspect printed no URL in 30s:" >&2
cat "$log" >&2
exit 1
