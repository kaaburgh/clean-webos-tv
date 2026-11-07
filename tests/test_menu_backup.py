"""menu_backup.py: recording menu values with their screenshots, and the completeness gate."""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import menu_backup  # noqa: E402


class MenuBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backup = os.path.join(self.tmp.name, "backup", "snap")
        os.makedirs(self.backup)
        self.shot = os.path.join(self.tmp.name, "shot.jpg")
        with open(self.shot, "wb") as f:
            f.write(b"\xff\xd8jpeg")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = menu_backup.main(list(args))
        return code, out.getvalue()

    def test_bundled_list_covers_the_settings_the_g5_hides(self):
        ids = menu_backup.load_list()
        keys = {s["key"] for s in ids.values()}
        for key in ("option.watchedListCollection", "other.contentRecommendation", "general.adCookie",
                    "general.screenSaverAd", "general.personalRecommend", "general.doNotSellMyPersonalInformation",
                    "general.sportsAlarm"):
            self.assertIn(key, keys)

    def shot_with(self, content):
        path = os.path.join(self.tmp.name, f"shot-{len(content)}-{content[-1]}.jpg")
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_record_copies_screenshot_privately_and_gates(self):
        self.assertEqual(self.cli("has", self.backup, "ad-tracking")[0], 1)
        code, out = self.cli("record", self.backup, "ad-tracking", "off", self.shot, "--path", "Общие > Реклама")
        self.assertEqual(code, 0, out)
        menu = os.path.join(self.backup, "menu")
        entry = menu_backup.summary(self.backup)["ad-tracking"]["baseline"]
        with open(os.path.join(menu, entry["screenshot"]), "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8jpeg")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(menu, "values.jsonl")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(menu).st_mode), 0o700)
        self.assertEqual(self.cli("has", self.backup, "ad-tracking")[0], 0)
        _, shown = self.cli("show", self.backup)
        self.assertIn(f"was off  [Общие > Реклама]  screenshot menu/{entry['screenshot']}", shown)

    def test_unavailable_is_investigated_but_never_authorizes_a_change(self):
        self.cli("record", self.backup, "do-not-sell", "unavailable", self.shot)
        self.assertEqual(self.cli("has", self.backup, "do-not-sell")[0], 1)
        self.assertNotIn("do-not-sell", self.cli("missing", self.backup)[1])
        with self.assertRaises(SystemExit):
            self.cli("record", self.backup, "do-not-sell", "unavailable", self.shot, "--replace-baseline", "--note", "x")

    def test_has_fails_when_the_baseline_screenshot_is_gone(self):
        self.cli("record", self.backup, "ad-tracking", "on", self.shot)
        entry = menu_backup.summary(self.backup)["ad-tracking"]["baseline"]
        os.remove(os.path.join(self.backup, "menu", entry["screenshot"]))
        self.assertEqual(self.cli("has", self.backup, "ad-tracking")[0], 1)

    def test_baseline_is_frozen_and_every_screenshot_is_kept(self):
        first = self.shot_with(b"\xff\xd8before")
        later = self.shot_with(b"\xff\xd8after!")
        self.cli("record", self.backup, "sports-alerts", "on", first)
        self.cli("record", self.backup, "sports-alerts", "off", later, "--note", "read back after the change")
        item = menu_backup.summary(self.backup)["sports-alerts"]
        self.assertEqual(item["baseline"]["value"], "on")
        self.assertEqual(item["latest"]["value"], "off")
        self.assertEqual(item["latest"]["kind"], "observation")
        self.assertNotEqual(item["baseline"]["screenshot"], item["latest"]["screenshot"])
        menu = os.path.join(self.backup, "menu")
        with open(os.path.join(menu, item["baseline"]["screenshot"]), "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8before")
        with open(os.path.join(menu, item["latest"]["screenshot"]), "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8after!")
        _, shown = self.cli("show", self.backup)
        self.assertIn("was on", shown)
        self.assertIn("latest observation: off", shown)

    def test_replace_baseline_needs_a_note_and_keeps_the_old_picture(self):
        self.cli("record", self.backup, "screensaver-ad", "of", self.shot)
        with self.assertRaises(SystemExit):
            self.cli("record", self.backup, "screensaver-ad", "on", self.shot, "--replace-baseline")
        self.cli("record", self.backup, "screensaver-ad", "on", self.shot, "--replace-baseline", "--note", "misread")
        self.assertEqual(menu_backup.summary(self.backup)["screensaver-ad"]["baseline"]["value"], "on")
        self.assertEqual(len(os.listdir(os.path.join(self.backup, "menu"))), 3)  # 2 pictures + log

    def test_missing_and_status(self):
        self.cli("record", self.backup, "sports-alerts", "on", self.shot)
        code, out = self.cli("missing", self.backup)
        self.assertEqual(code, 1)
        self.assertNotIn("sports-alerts", out)
        self.assertIn("viewing-information", out)
        code, out = self.cli("status", self.backup)
        self.assertEqual(code, 1)
        self.assertIn("menu=INCOMPLETE baselines=1", out)
        for i in menu_backup.load_list():
            self.cli("record", self.backup, i, "unavailable", self.shot)
        self.assertEqual(self.cli("missing", self.backup)[0], 0)
        code, out = self.cli("status", self.backup)
        self.assertEqual(code, 0)
        self.assertIn("menu=COMPLETE baselines=1 unavailable=8", out)

    def test_record_keeps_status_file_current(self):
        status = os.path.join(self.backup, "STATUS")
        with open(status, "w", encoding="utf-8") as f:
            f.write("network=OK\nmenu=INCOMPLETE baselines=0 unavailable=0 not_investigated=9\n")
        self.cli("record", self.backup, "sports-alerts", "on", self.shot)
        with open(status, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, ["network=OK", "menu=INCOMPLETE baselines=1 unavailable=0 not_investigated=8"])

    def test_refuses_record_without_picture_or_value_or_known_id(self):
        for args in (("ad-tracking", "off", os.path.join(self.tmp.name, "nope.jpg")),
                     ("ad-tracking", " ", self.shot),
                     ("not-a-setting", "off", self.shot)):
            with self.assertRaises(SystemExit):
                self.cli("record", self.backup, *args)
        self.assertFalse(os.path.exists(os.path.join(self.backup, "menu", "values.jsonl")))


if __name__ == "__main__":
    unittest.main()
