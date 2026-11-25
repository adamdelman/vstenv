import json, tempfile, unittest
from pathlib import Path
from unittest import mock
from vstenv import menu_colors, recipes

KDE = "[Colors:View]\nBackgroundNormal=1,2,3\n\n[Colors:Window]\nBackgroundNormal = 49,54,59\nForegroundNormal=252,252,252\n"

class FakePrefix:
    def __init__(self, d, rc=0): self.drive_c = Path(d); self.calls = []; self.rc = rc
    def run(self, args, **kw):
        self.calls.append(args); return mock.Mock(returncode=self.rc)

class MenuColorsTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); self.kde = self.d / "kdeglobals"

    def test_parse(self):
        self.kde.write_text(KDE)
        self.assertEqual(menu_colors.plasma_colors(self.kde),
                         {"Menu": "49 54 59", "MenuBar": "49 54 59", "MenuText": "252 252 252"})

    def test_missing_or_malformed(self):
        self.assertIsNone(menu_colors.plasma_colors(self.d / "none"))
        for body in ("", "[Colors:Window]\nBackgroundNormal=1,2,3\n", "[Colors:View]\nBackgroundNormal=1,2,3\nForegroundNormal=4,5,6\n",
                     "[Colors:Window]\nBackgroundNormal=1,2\nForegroundNormal=4,5,6\n",
                     "[Colors:Window]\nBackgroundNormal=a,b,c\nForegroundNormal=4,5,6\n",
                     "[Colors:Window]\nBackgroundNormal=1,2,300\nForegroundNormal=4,5,6\n"):
            self.kde.write_text(body); self.assertIsNone(menu_colors.plasma_colors(self.kde), body)

    def test_xdg_config_home(self):
        (self.d / "kdeglobals").write_text(KDE)
        with mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.d)}):
            self.assertIsNotNone(menu_colors.plasma_colors())

    def test_reg_command_then_noop_when_unchanged(self):
        self.kde.write_text(KDE); p = FakePrefix(self.d)
        self.assertTrue(menu_colors.apply(p, self.kde))
        self.assertIn(["reg", "add", "HKCU\\Control Panel\\Colors", "/v", "MenuText", "/t", "REG_SZ", "/d", "252 252 252", "/f"], p.calls)
        self.assertEqual(len(p.calls), 3)
        self.assertFalse(menu_colors.apply(p, self.kde)); self.assertEqual(len(p.calls), 3)
        self.kde.write_text(KDE.replace("49,54,59", "10,10,10"))
        self.assertTrue(menu_colors.apply(p, self.kde)); self.assertEqual(len(p.calls), 6)

    def test_nothing_written_without_kdeglobals_or_on_failure(self):
        p = FakePrefix(self.d); self.assertFalse(menu_colors.apply(p, self.d / "none")); self.assertEqual(p.calls, [])
        self.kde.write_text(KDE); p = FakePrefix(self.d, rc=1)
        self.assertFalse(menu_colors.apply(p, self.kde)); self.assertFalse((self.d / menu_colors.STATE).exists())

    def test_off_by_default(self):
        self.assertIn("plasma_menu_colors", recipes.KNOBS["programs"])
        p = FakePrefix(self.d)
        with mock.patch.object(menu_colors, "apply") as ap, mock.patch.object(recipes, "overrides_key", return_value="K"):
            p.reg_query = lambda k: {}
            recipes.apply_program(p, "x.exe", {})
            ap.assert_not_called()
            recipes.apply_program(p, "x.exe", {"plasma_menu_colors": "on"})
            ap.assert_called_once()

if __name__ == "__main__":
    unittest.main()
