#!/usr/bin/env bash
# Start ares-inspect for an app and leave it running in the background, so tools/devtools.py
# can reach the app's DevTools. Writes the endpoint to .devtools-endpoint.
# Usage: tools/inspect.sh <ares-device> <app-id>      stop: tools/inspect.sh --stop
set -euo pipefail

ares() {
  local cmd="$1"; shift
  # ARES_GLOBAL=1 skips the project-local CLI (e.g. to use @webos-tools/cli instead of @webosose/ares-cli).
  if [[ "${ARES_GLOBAL:-0}" != 1 && -x node_modules/.bin/$cmd ]]; then node_modules/.bin/"$cmd" "$@"; else "$cmd" "$@"; fi
}

if [[ "${1:-}" == "--stop" ]]; then
  [[ -f .ares-inspect.pid ]] && kill "$(cat .ares-inspect.pid)" 2>/dev/null || true
  rm -f .ares-inspect.pid .devtools-endpoint
  exit 0
fi

device="${1:?usage: tools/inspect.sh <ares-device> <app-id>}"
app="${2:?usage: tools/inspect.sh <ares-device> <app-id>}"

[[ -f .ares-inspect.pid ]] && kill "$(cat .ares-inspect.pid)" 2>/dev/null || true
rm -f .devtools-endpoint
: >.ares-inspect.log
ares ares-inspect --device "$device" --app "$app" >.ares-inspect.log 2>&1 &
echo $! >.ares-inspect.pid

for _ in $(seq 1 60); do
  url="$(grep -oE 'https?://[A-Za-z0-9._-]+:[0-9]+' .ares-inspect.log | head -n1 || true)"
  if [[ -n "$url" ]]; then
    echo "$url" >.devtools-endpoint
    echo "DevTools endpoint: $url (ares-inspect pid $(cat .ares-inspect.pid), log .ares-inspect.log)"
    exit 0
  fi
  if ! kill -0 "$(cat .ares-inspect.pid)" 2>/dev/null; then
    echo "ares-inspect exited:" >&2; cat .ares-inspect.log >&2; exit 1
  fi
  sleep 0.5
done
echo "ares-inspect printed no URL in 30s:" >&2
cat .ares-inspect.log >&2
exit 1
