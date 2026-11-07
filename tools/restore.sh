#!/usr/bin/env bash
# Undo everything tools/lgtv.py settings-set logged, then list the menu changes to undo by hand.
# Usage: TV_HOST=192.168.1.50 tools/restore.sh [backup-dir]   (default: backup/latest)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
: "${TV_HOST:?set TV_HOST to the TV IP}"
dir="${1:-backup/latest}"
status=0

if [[ -s "$dir/changes.jsonl" ]]; then
  python3 "$here/lgtv.py" --host "$TV_HOST" restore "$dir/changes.jsonl" || status=$?
else
  echo "no logged settings changes in $dir"
fi

if [[ -f "$dir/manual.md" ]]; then
  echo
  echo "Menu changes to put back by hand ($dir/manual.md):"
  cat "$dir/manual.md"
fi

if [[ -f "$dir/apps-all.json" ]]; then
  echo
  echo "Apps installed at backup time but missing now (reinstall them from LG Content Store):"
  python3 "$here/lgtv.py" --host "$TV_HOST" apps --all >"$dir/apps-now.json"
  python3 - "$dir/apps-all.json" "$dir/apps-now.json" <<'PY'
import json, sys
before = {a["id"]: a.get("title") for a in json.load(open(sys.argv[1]))}
now = {a["id"] for a in json.load(open(sys.argv[2]))}
missing = sorted(set(before) - now)
print("\n".join(f"  {i}  {before[i]}" for i in missing) or "  none")
PY
fi
exit "$status"
