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

    def test_record_copies_screenshot_privately_and_gates(self):
        self.assertEqual(self.cli("has", self.backup, "ad-tracking")[0], 1)
        code, out = self.cli("record", self.backup, "ad-tracking", "off", self.shot, "--path", "Общие > Реклама")
        self.assertEqual(code, 0, out)
        menu = os.path.join(self.backup, "menu")
        with open(os.path.join(menu, "ad-tracking.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8jpeg")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(menu, "values.jsonl")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(menu).st_mode), 0o700)
        self.assertEqual(self.cli("has", self.backup, "ad-tracking")[0], 0)
        _, shown = self.cli("show", self.backup)
        self.assertIn("off  [Общие > Реклама]  screenshot menu/ad-tracking.jpg", shown)

    def test_last_record_wins_and_missing_lists_the_rest(self):
        self.cli("record", self.backup, "sports-alerts", "on", self.shot)
        self.cli("record", self.backup, "sports-alerts", "off", self.shot, "--note", "re-read after a misstep")
        self.assertEqual(menu_backup.records(self.backup)["sports-alerts"]["value"], "off")
        code, out = self.cli("missing", self.backup)
        self.assertEqual(code, 1)
        self.assertNotIn("sports-alerts", out)
        self.assertIn("viewing-information", out)
        for i in menu_backup.load_list():
            self.cli("record", self.backup, i, "unavailable", self.shot)
        self.assertEqual(self.cli("missing", self.backup)[0], 0)

    def test_refuses_record_without_picture_or_value_or_known_id(self):
        for args in (("ad-tracking", "off", os.path.join(self.tmp.name, "nope.jpg")),
                     ("ad-tracking", " ", self.shot),
                     ("not-a-setting", "off", self.shot)):
            with self.assertRaises(SystemExit):
                self.cli("record", self.backup, *args)
        self.assertFalse(os.path.exists(os.path.join(self.backup, "menu", "values.jsonl")))


if __name__ == "__main__":
    unittest.main()
