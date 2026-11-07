#!/usr/bin/env bash
# One iteration of the development loop for a webOS web app:
# build -> package -> ares-install -> ares-launch -> ares-inspect (DevTools endpoint).
# Run from the app repository root. Defaults fit kaaburgh/kinopub.webos.
#
#   ARES_DEVICE=lg-g5 path/to/clean-webos-tv/tools/devloop.sh
#   options: --skip-build  --no-inspect  --app-id ID  --params JSON
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
device="${ARES_DEVICE:-lg-g5}"
app_id=""
skip_build=0
inspect=1
params=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-build) skip_build=1 ;;
    --no-inspect) inspect=0 ;;
    --app-id) app_id="$2"; shift ;;
    --params) params="$2"; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

ares() {
  local cmd="$1"; shift
  # ARES_GLOBAL=1 skips the project-local CLI (e.g. to use @webos-tools/cli instead of @webosose/ares-cli).
  if [[ "${ARES_GLOBAL:-0}" != 1 && -x node_modules/.bin/$cmd ]]; then node_modules/.bin/"$cmd" "$@"; else "$cmd" "$@"; fi
}

if [[ -z "$app_id" ]]; then
  appinfo="$(find . -name appinfo.json -not -path './node_modules/*' -not -path './build/*' -not -path './out/*' | head -n1)"
  app_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$appinfo")"
fi

if [[ "$skip_build" == 0 ]]; then
  rm -rf build out
  yarn build
  yarn package
fi

# shellcheck disable=SC2012 # package names are ours: <id>_v<version>.ipk
ipk="$(ls -t out/*.ipk | head -n1)"
echo "installing $ipk on $device"
ares ares-install --device "$device" "$ipk"

ares ares-launch --device "$device" --close "$app_id" >/dev/null 2>&1 || true
if [[ -n "$params" ]]; then
  ares ares-launch --device "$device" "$app_id" --params "$params"
else
  ares ares-launch --device "$device" "$app_id"
fi

if [[ "$inspect" == 1 ]]; then
  "$here/inspect.sh" "$device" "$app_id"
  python3 "$here/devtools.py" eval 'location.href + " | " + document.title'
fi
