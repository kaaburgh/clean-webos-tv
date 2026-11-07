#!/usr/bin/env python3
"""Back up settings that the TV only shows in its menu: a screenshot plus the value read from it.

The network API of a G5 does not return most privacy settings, so the agent walks the menu with
`lgtv.py button`, captures `lgtv.py screenshot`, reads the value from the image and records it here.
Records live in <backup>/menu/values.jsonl next to the screenshots (private to the user).

  menu_backup.py list                               settings to record (tools/menu-settings/G5.json)
  menu_backup.py record <backup> <id> <value> <screenshot> [--path "menu path as seen"] [--note ...]
  menu_backup.py missing <backup>                   ids not recorded yet (exit 1 if any)
  menu_backup.py show <backup>                      what was recorded, for restore by hand
  menu_backup.py has <backup> <id>                  exit 0 if <id> was recorded (gate before a change)

Use `--value unavailable` with a screenshot when the item does not exist on this TV/region.
"""

import argparse
import json
import os
import shutil
import sys
import time

LIST_FILE = os.environ.get("TV_MENU_SETTINGS") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "menu-settings", "G5.json")


def load_list(path=None):
    with open(path or LIST_FILE, encoding="utf-8") as f:
        return {s["id"]: s for s in json.load(f)["settings"]}


def records(backup):
    path = os.path.join(backup, "menu", "values.jsonl")
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    out[entry["id"]] = entry  # the last record of an id wins (a re-read after a misstep)
    return out


def record(backup, setting, value, screenshot, path=None, note=None):
    if not value.strip():
        raise SystemExit("value is empty: write what the screenshot shows (on/off, the selected option, or 'unavailable')")
    if not os.path.isfile(screenshot) or os.path.getsize(screenshot) == 0:
        raise SystemExit(f"screenshot {screenshot} is missing or empty: a menu value is only recorded with its picture")
    old_umask = os.umask(0o077)
    try:
        menu_dir = os.path.join(backup, "menu")
        os.makedirs(menu_dir, exist_ok=True)
        ext = os.path.splitext(screenshot)[1] or ".jpg"
        target = os.path.join(menu_dir, setting["id"] + ext)
        if os.path.abspath(screenshot) != os.path.abspath(target):
            shutil.copyfile(screenshot, target)
        entry = {"ts": int(time.time()), "id": setting["id"], "name": setting["name"], "key": setting.get("key"),
                 "menu": path or setting["menu"], "value": value.strip(), "screenshot": os.path.basename(target)}
        if note:
            entry["note"] = note
        with open(os.path.join(menu_dir, "values.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    finally:
        os.umask(old_umask)
    return entry


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-file", help="settings list (default tools/menu-settings/G5.json or $TV_MENU_SETTINGS)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("record"); p.add_argument("backup"); p.add_argument("id"); p.add_argument("value")
    p.add_argument("screenshot"); p.add_argument("--path"); p.add_argument("--note")
    for name in ("missing", "show"):
        sub.add_parser(name).add_argument("backup")
    p = sub.add_parser("has"); p.add_argument("backup"); p.add_argument("id")
    args = ap.parse_args(argv)
    wanted = load_list(args.list_file)

    if args.cmd == "list":
        for s in wanted.values():
            print(f"{s['id']:24} {s['name']}\n{'':24} меню: {s['menu']}")
        return 0
    if args.cmd == "record":
        if args.id not in wanted:
            raise SystemExit(f"unknown id {args.id!r}; known: {', '.join(wanted)}")
        entry = record(args.backup, wanted[args.id], args.value, args.screenshot, args.path, args.note)
        print(f"recorded {entry['id']} = {entry['value']} ({entry['screenshot']})")
        return 0
    done = records(args.backup)
    if args.cmd == "missing":
        left = [i for i in wanted if i not in done]
        for i in left:
            print(f"{i:24} {wanted[i]['menu']}")
        return 1 if left else 0
    if args.cmd == "has":
        return 0 if args.id in done else 1
    if args.cmd == "show":
        if not done:
            print("  no menu settings were recorded")
        for e in done.values():
            print(f"  {e['name']}: {e['value']}  [{e['menu']}]  screenshot menu/{e['screenshot']}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
