#!/usr/bin/env bash
# Snapshot the TV before changing anything: model/firmware, every installed app, the launcher
# entries, what the settings service reports, and a screenshot of the home screen.
# Usage: TV_HOST=192.168.1.50 tools/backup.sh [backup-dir]
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
: "${TV_HOST:?set TV_HOST to the TV IP}"
dir="${1:-backup/$(date +%Y%m%d-%H%M%S)}"
# The backup describes the owner's TV and accounts: keep it private to this user.
umask 077
mkdir -p "$dir"
chmod 700 "$dir"
lgtv() { python3 "$here/lgtv.py" --host "$TV_HOST" "$@"; }

lgtv info >"$dir/info.json"
lgtv apps >"$dir/launch-points.json"
lgtv apps --all >"$dir/apps-all.json"

# The settings service refuses reads without explicit keys and lets only a whitelist of keys
# through the network API, so lgtv.py asks for the keys a real G5 answered
# (tools/settings-keys/G5.json, override with TV_SETTINGS_KEYS), never PINs, passwords or tokens. Keys the TV rejects are skipped and listed in the .err file.
# The privacy settings this repository changes live in the essential categories: without them the
# backup is FAILED, cannot be restored from, and backup/latest is not moved to it.
essential=(general option other)
captured=()
missing=()
# On a G5 only these categories have keys the network API lets through (see the key file).
for category in general option other picture sound network time lock support; do
  if lgtv settings-get "$category" >"$dir/settings-$category.json" 2>"$dir/settings-$category.err"; then
    [[ -s "$dir/settings-$category.err" ]] || rm -f "$dir/settings-$category.err"
    captured+=("$category")
  else
    echo "settings $category: not readable (see $dir/settings-$category.err)"
    rm -f "$dir/settings-$category.json"
    missing+=("$category")
  fi
done

failed_essential=()
for category in "${essential[@]}"; do
  [[ " ${captured[*]} " == *" $category "* ]] || failed_essential+=("$category")
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

if [[ ${#failed_essential[@]} -gt 0 ]]; then
  {
    echo "FAILED"
    echo "essential settings not captured: ${failed_essential[*]}"
    echo "captured: ${captured[*]:-none}"
  } >"$dir/STATUS"
  echo "BACKUP FAILED: settings ${failed_essential[*]} were not captured; do not change the TV." >&2
  echo "backup/latest was not updated. Check the connection and the .err files in $dir." >&2
  exit 1
fi

if [[ ${#missing[@]} -gt 0 ]]; then
  printf 'PARTIAL\nnot captured: %s\n' "${missing[*]}" >"$dir/STATUS"
  echo "backup is PARTIAL: ${missing[*]} not captured (essential categories are in)"
else
  echo "OK" >"$dir/STATUS"
fi
ln -sfn "$(basename "$dir")" "$(dirname "$dir")/latest"
echo "backup written to $dir"
