#!/usr/bin/env bash
# Snapshot the TV before changing anything: model/firmware, every installed app, the launcher
# entries, what the settings service reports, and a screenshot of the home screen.
# Usage: TV_HOST=192.168.1.50 tools/backup.sh [backup-dir]
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
: "${TV_HOST:?set TV_HOST to the TV IP}"
dir="${1:-backup/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$dir"
lgtv() { python3 "$here/lgtv.py" --host "$TV_HOST" "$@"; }

lgtv info >"$dir/info.json"
lgtv apps >"$dir/launch-points.json"
lgtv apps --all >"$dir/apps-all.json"

# The settings service refuses reads without explicit keys, so lgtv.py asks for every key known
# for the category (tools/settings-keys/G5.json, override with TV_SETTINGS_KEYS). Keys this
# firmware lacks are skipped and listed in the .err file; a failed category does not stop the backup.
for category in general option other picture sound network time lock caption channel aiPicture \
                aspectRatio push support voiceframework timer; do
  if lgtv settings-get "$category" >"$dir/settings-$category.json" 2>"$dir/settings-$category.err"; then
    [[ -s "$dir/settings-$category.err" ]] || rm -f "$dir/settings-$category.err"
  else
    echo "settings $category: not readable (see $dir/settings-$category.err)"
  fi
done

if [[ -n "${ARES_DEVICE:-}" ]] && command -v ares-device >/dev/null; then
  ares-device --system-info --device "$ARES_DEVICE" >"$dir/ares-system-info.txt" 2>&1 || true
fi

lgtv screenshot "$dir/home-before.jpg" HOME || echo "screenshot failed; continue, but check by eye"

touch "$dir/changes.jsonl"
if [[ ! -f "$dir/manual.md" ]]; then
  cat >"$dir/manual.md" <<'MD'
# Changes made through the TV menus

tools/restore.sh undoes only the settings logged in changes.jsonl. Everything changed with the
remote (agreements withdrawn, apps deleted, menu toggles) goes here, one line each, with the
menu path and the value before, so it can be put back by hand.

MD
fi

ln -sfn "$(basename "$dir")" "$(dirname "$dir")/latest"
echo "backup written to $dir"
