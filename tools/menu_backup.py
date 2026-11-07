#!/usr/bin/env python3
"""Back up settings that the TV only shows in its menu: a screenshot plus the value read from it.

The network API of a G5 does not return most privacy settings, so the agent walks the menu with
`lgtv.py button`, captures `lgtv.py screenshot`, reads the value from the image and records it here.
Records live in <backup>/menu/values.jsonl next to the screenshots (private to the user).

The first readable record of an item is its baseline: the value to restore. It is frozen. Later
records of the same item are observations (e.g. a read-back after a change) and never replace it;
a misread baseline is fixed only with --replace-baseline, before anything was changed. Every
screenshot is kept under its own file name.

  menu_backup.py list                               settings to record (tools/menu-settings/G5.json)
  menu_backup.py record <backup> <id> <value> <screenshot> [--path "menu path as seen"] [--note ...]
                 [--replace-baseline --note "why"]
  menu_backup.py missing <backup>                   ids not investigated yet (exit 1 if any)
  menu_backup.py show <backup>                      baselines (and latest observations), for restore by hand
  menu_backup.py has <backup> <id>                  exit 0 only if <id> has a restorable baseline whose
                                                    screenshot exists: the gate before a menu change
  menu_backup.py status <backup>                    menu=COMPLETE|INCOMPLETE with counts

Use the value `unavailable` with a screenshot when the item does not exist on this TV/region: the
item then counts as investigated, but it has no baseline, so `has` refuses a change to it.
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


UNAVAILABLE = {"unavailable", "n/a", "unknown", "?"}


def entries(backup):
    path = os.path.join(backup, "menu", "values.jsonl")
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            out = [json.loads(line) for line in f if line.strip()]
    return out


def summary(backup):
    """{id: {"baseline": entry or None, "latest": entry}} from the append-only log."""
    out = {}
    for e in entries(backup):
        item = out.setdefault(e["id"], {"baseline": None, "latest": None})
        item["latest"] = e
        if e.get("kind") == "baseline":
            item["baseline"] = e
    return out


def restorable(backup, item):
    base = item and item["baseline"]
    return bool(base) and os.path.isfile(os.path.join(backup, "menu", base["screenshot"]))


def record(backup, setting, value, screenshot, path=None, note=None, replace_baseline=False):
    value = value.strip()
    if not value:
        raise SystemExit("value is empty: write what the screenshot shows (on/off, the selected option, or 'unavailable')")
    if not os.path.isfile(screenshot) or os.path.getsize(screenshot) == 0:
        raise SystemExit(f"screenshot {screenshot} is missing or empty: a menu value is only recorded with its picture")
    current = summary(backup).get(setting["id"])
    has_baseline = bool(current and current["baseline"])
    readable = value.lower() not in UNAVAILABLE
    if replace_baseline:
        if not note:
            raise SystemExit("--replace-baseline needs --note saying why (a misread before any change)")
        if not readable:
            raise SystemExit("a baseline must be a readable value, not 'unavailable'")
        kind = "baseline"
    elif readable and not has_baseline:
        kind = "baseline"
    else:
        kind = "observation" if readable else "unavailable"
    old_umask = os.umask(0o077)
    try:
        menu_dir = os.path.join(backup, "menu")
        os.makedirs(menu_dir, exist_ok=True)
        ext = os.path.splitext(screenshot)[1] or ".jpg"
        seq = len(entries(backup)) + 1
        name = f"{setting['id']}-{seq:03d}-{kind}{ext}"
        target = os.path.join(menu_dir, name)
        if os.path.exists(target):
            raise SystemExit(f"{target} already exists; refusing to overwrite a recorded screenshot")
        shutil.copyfile(screenshot, target)
        entry = {"ts": int(time.time()), "seq": seq, "kind": kind, "id": setting["id"], "name": setting["name"],
                 "key": setting.get("key"), "menu": path or setting["menu"], "value": value, "screenshot": name}
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
    p.add_argument("--replace-baseline", action="store_true", help="fix a misread baseline (only before any change)")
    for name in ("missing", "show", "status"):
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
        entry = record(args.backup, wanted[args.id], args.value, args.screenshot, args.path, args.note,
                       args.replace_baseline)
        print(f"recorded {entry['kind']} {entry['id']} = {entry['value']} (menu/{entry['screenshot']})")
        return 0
    done = summary(args.backup)
    if args.cmd == "missing":
        left = [i for i in wanted if i not in done]
        for i in left:
            print(f"{i:24} {wanted[i]['menu']}")
        return 1 if left else 0
    if args.cmd == "has":
        return 0 if restorable(args.backup, done.get(args.id)) else 1
    if args.cmd == "status":
        ready = [i for i in wanted if restorable(args.backup, done.get(i))]
        unavailable = [i for i in wanted if i in done and not done[i]["baseline"]]
        left = [i for i in wanted if i not in done]
        state = "COMPLETE" if not left else "INCOMPLETE"
        print(f"menu={state} baselines={len(ready)} unavailable={len(unavailable)} not_investigated={len(left)}")
        return 0 if not left else 1
    if args.cmd == "show":
        if not done:
            print("  no menu settings were recorded")
        for i, item in done.items():
            base, latest = item["baseline"], item["latest"]
            if base:
                print(f"  {base['name']}: was {base['value']}  [{base['menu']}]  screenshot menu/{base['screenshot']}")
                if latest is not base:
                    print(f"      latest observation: {latest['value']} (menu/{latest['screenshot']})")
            else:
                print(f"  {latest['name']}: no baseline ({latest['value']}), screenshot menu/{latest['screenshot']}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
