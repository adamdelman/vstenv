"""winecfg /v win10 writes ProductName "Microsoft Windows 10"; Steinberg's setup
(Cubase 14) wants it to start with "Windows 10", as on Windows (code 241 otherwise)."""
import tempfile, unittest
from pathlib import Path
from unittest import mock
from vstenv.wine import Prefix, WineBuild

REG = """WINE REGISTRY Version 2

[Software\\\\Microsoft\\\\Windows NT\\\\CurrentVersion] 1795807556
#time=1dc5f7b2b4c4a2e
"CurrentBuild"="19045"
"ProductName"="%s"

[Software\\\\Microsoft\\\\Windows NT\\\\CurrentVersion\\\\Fonts] 1795807556
"ProductName"="Microsoft Windows 10"
"""

class ProductNameTest(unittest.TestCase):
    def check(self, name):
        with tempfile.TemporaryDirectory() as tmp:
            p = Prefix(Path(tmp), WineBuild(Path(tmp) / "wine"))
            (Path(tmp) / "system.reg").write_text(REG % name)
            with mock.patch.object(p, "reg_add") as add: p.fix_product_name()
            return add.call_args_list
    def test_winecfg_name_is_rewritten(self):
        self.assertEqual([c.args for c in self.check("Microsoft Windows 10")],
                         [(Prefix.NT_CURRENT_VERSION, "ProductName", "Windows 10 Pro")])
        self.assertEqual(len(self.check("Microsoft Windows 11")), 1)
    def test_windows_like_names_and_other_keys_are_left_alone(self):
        for n in ("Windows 10 Pro", "Microsoft Windows 7", "Microsoft Windows XP"):
            self.assertEqual(self.check(n), [], n)
    def test_32_bit_view_too_and_a_section_at_the_end_of_the_file(self):
        reg = (REG % "Windows 10 Pro") + """
[Software\\\\Wow6432Node\\\\Microsoft\\\\Windows NT\\\\CurrentVersion] 1795807556
#time=1dc5f7b2b4c4a2e
"CurrentBuild"="19045"
"ProductName"="Microsoft Windows 10"
"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Prefix(Path(tmp), WineBuild(Path(tmp) / "wine"))
            (Path(tmp) / "system.reg").write_text(reg)
            with mock.patch.object(p, "reg_add") as add: p.fix_product_name()
            self.assertEqual([c.args for c in add.call_args_list], [(Prefix.NT_CURRENT_VERSION_32, "ProductName", "Windows 10 Pro")])
            (Path(tmp) / "system.reg").write_text(reg.replace("Windows 10 Pro", "Microsoft Windows 11"))
            with mock.patch.object(p, "reg_add") as add: p.fix_product_name()
            self.assertEqual([c.args[0] for c in add.call_args_list], [Prefix.NT_CURRENT_VERSION, Prefix.NT_CURRENT_VERSION_32])
        with tempfile.TemporaryDirectory() as tmp:    # the 64-bit section last in the file
            p = Prefix(Path(tmp), WineBuild(Path(tmp) / "wine"))
            (Path(tmp) / "system.reg").write_text('WINE REGISTRY Version 2\n\n[Software\\\\Microsoft\\\\Windows NT\\\\CurrentVersion] 1\n"ProductName"="Microsoft Windows 10"\n')
            with mock.patch.object(p, "reg_add") as add: p.fix_product_name()
            self.assertEqual(len(add.call_args_list), 1)
    def test_no_prefix_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Prefix(Path(tmp) / "none", WineBuild(Path(tmp) / "wine"))
            with mock.patch.object(p, "reg_add") as add: p.fix_product_name()
            add.assert_not_called()
