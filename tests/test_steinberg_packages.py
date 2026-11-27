"""Steinberg packages: the Download Assistant's Install Assistant refuses them
under Wine (signed prerun scripts); vstenv finishes them from the package."""
import json, subprocess, tempfile, unittest, zipfile
from pathlib import Path
from unittest import mock
from vstenv.installers import steinberg as pkg
from vstenv.wine import Prefix, WineBuild

SETUP_XML = """<?xml version='1.0'?>
<Setup>
  <product file="MediaBay.msi" arm64compatible="true"><title>MediaBay</title></product>
  <include>
    <prerun prerun="Additional Content/Installer Data/preinstall.ps1" package="MediaBay.msi"/>
    <path msi="Additional Content/Installer"/>
  </include>
  <msiPackage file="MediaBay.msi"/>
  <msiPackage file="LibraryManager.msi"/>
  <libraryManager name="VST Sound Content Update" source="x" customDir="default"/>
</Setup>"""

def make_zip(path: Path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("MediaBay 1.3.100/setup.xml", SETUP_XML)
        z.writestr("MediaBay 1.3.100/MediaBay.msi", "msi")
        z.writestr("MediaBay 1.3.100/Additional Content/Installer/LibraryManager.msi", "msi")
        z.writestr("MediaBay 1.3.100/Additional Content/Installer Data/preinstall.ps1", "exit 0")

class PackageTest(unittest.TestCase):
    def test_setup_xml_lists_product_msi_first_then_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            x = Path(tmp) / "setup.xml"; x.write_text(SETUP_XML)
            s = pkg.parse_setup(x)
            self.assertEqual((s.title, s.msis), ("MediaBay", ["MediaBay.msi", "LibraryManager.msi"]))
            self.assertEqual(s.prerun, ["Additional Content/Installer Data/preinstall.ps1"]); self.assertEqual(len(s.library), 1)
    def test_zip_with_setup_xml_is_a_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            z = Path(tmp) / "MediaBay_Installer_win64.zip"; make_zip(z)
            self.assertTrue(pkg.is_package(z)); self.assertFalse(pkg.is_package(Path(tmp) / "nope.zip"))
    def test_tokens_match_status_file_to_package(self):
        self.assertEqual(pkg._token("Steinberg_MediaBay_Installer.json"), pkg._token("MediaBay_Installer_win64.zip"))
        self.assertEqual(pkg._token("Steinberg_Library_Manager_Installer.json"), pkg._token("Steinberg_Library_Manager_win64.zip"))
        self.assertEqual(pkg._token("Steinberg_Install_Assistant_Installer.json"), pkg._token("Steinberg_Install_Assistant_Installer_win.exe"))
        self.assertNotEqual(pkg._token("Steinberg_MediaBay_Installer.json"), pkg._token("Steinberg_Library_Manager_win64.zip"))
    def test_install_runs_every_msi_silently_and_skips_the_prerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Prefix(Path(tmp) / "prefix", WineBuild(Path(tmp) / "wine")); p.drive_c.mkdir(parents=True)
            z = Path(tmp) / "MediaBay_Installer_win64.zip"; make_zip(z)
            calls = []
            def run(argv, **kw): calls.append(argv); return subprocess.CompletedProcess(argv, 0)
            with mock.patch.object(p, "run", side_effect=run), mock.patch.object(pkg.paths, "CACHE", Path(tmp)):
                res = pkg.install(p, z)
            self.assertEqual(res["name"], "MediaBay"); self.assertEqual(res["installed"], ["MediaBay.msi", "LibraryManager.msi"]); self.assertEqual(res["failed"], [])
            self.assertTrue(all(a[0] == "msiexec" and "/qn" in a for a in calls)); self.assertEqual(len(calls), 2)
            self.assertFalse(any("preinstall" in " ".join(a) for a in calls))
    def test_cli_install_takes_a_product_zip_to_the_package_path(self):
        # The zip also holds Setup.exe, MSIs and vc_redist: the plugin-file copier
        # used to take it and stop at "holds several installers".
        import argparse
        from vstenv import cli
        with tempfile.TemporaryDirectory() as tmp:
            z = Path(tmp) / "Cubase_15.0.10_Installer_win64.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("Cubase 15/Setup.exe", "MZ")
                zf.writestr("Cubase 15/Additional Content/Installer Data/setup.xml", "<Setup><product file='Cubase15.msi'/></Setup>")
                zf.writestr("Cubase 15/Additional Content/Installer/Cubase15.msi", "msi")
                zf.writestr("Cubase 15/Additional Content/Installer/vc_redist.x64.exe", "MZ")
            a = argparse.Namespace(installer=str(z), generic=False, no_sync=True, keep_trace=False, remedy=None)
            with mock.patch.object(cli, "_prefix"), mock.patch.object(pkg, "install", return_value={"name": "Cubase 15", "method": "msi"}) as inst, \
                 mock.patch("vstenv.plugfiles.install") as copier:
                cli.cmd_install(a)
            inst.assert_called_once(); copier.assert_not_called()
    def test_cli_install_unpacks_a_manager_named_zip_first(self):
        # A manager's installer shipped in a zip (IK Product Manager, Arturia Software
        # Center) is classified "manager" by its name, but the copier unpacks it as before.
        import argparse
        from vstenv import cli, vendors
        with tempfile.TemporaryDirectory() as tmp:
            z = Path(tmp) / "IK_Product_Manager_Win.zip"
            with zipfile.ZipFile(z, "w") as zf: zf.writestr("IK Product Manager Setup.exe", "MZ")
            a = argparse.Namespace(installer=str(z), generic=False, no_sync=True, keep_trace=False, remedy=None)
            v = mock.Mock(manager_name="Product Manager")
            with mock.patch.object(cli, "_prefix"), mock.patch.object(vendors, "classify_installer", return_value=("manager", v)), \
                 mock.patch.object(cli, "_install_plugin_files", return_value=None) as copier, mock.patch.object(pkg, "install") as inst:
                cli.cmd_install(a)
            copier.assert_called_once(); inst.assert_not_called(); v.install_manager.assert_not_called()
    def test_only_a_steinberg_setup_xml_makes_a_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            def zipped(name, xml):
                z = Path(tmp) / name
                with zipfile.ZipFile(z, "w") as zf: zf.writestr("Thing 1/setup.xml", xml); zf.writestr("Thing 1/Setup.exe", "MZ")
                return z
            self.assertFalse(pkg.is_package(zipped("a.zip", "<configuration><setting name='x'/></configuration>")))
            self.assertFalse(pkg.is_package(zipped("b.zip", "<Setup><product><title>x</title></product></Setup>")))
            self.assertFalse(pkg.is_package(zipped("c.zip", "not xml")))
            self.assertTrue(pkg.is_package(zipped("d.zip", "<Setup><msiPackage file='Runtime.msi'/></Setup>")))
            self.assertTrue(pkg.is_package(zipped("e.zip", SETUP_XML)))
    def test_run_finishes_staged_packages_when_the_download_assistant_closes(self):
        # `vstenv run "Steinberg Download Assistant"` (a terminal or script launch)
        # left MediaBay staged: only the GUI and `manager launch --wait` finished it.
        import argparse
        from vstenv import cli, programs
        sda = programs.Program("Steinberg Download Assistant", publisher="Steinberg Media Technologies GmbH", exe="C:\\sda.exe")
        cubase = programs.Program("Steinberg Cubase 15", publisher="Steinberg Media Technologies GmbH", exe="C:\\c.exe")
        for prog, finished in ((sda, 1), (cubase, 0)):
            proc = mock.Mock(spec=["wait", "poll"])
            with mock.patch.object(cli, "_prefix"), mock.patch.object(programs, "find", return_value=prog), \
                 mock.patch.object(programs, "run", return_value=proc), mock.patch.object(programs, "watch_plugins"), \
                 mock.patch.object(cli.setup, "after_change"), mock.patch.object(pkg, "finish_staged", return_value=[]) as fin:
                cli.cmd_run(argparse.Namespace(name=prog.name, debug=None, wait=False))
            self.assertEqual(fin.call_count, finished, prog.name)
        for wait in (False, True):     # a vendor's failure does not skip the plugin sync
            proc = mock.Mock(spec=["wait", "poll"])
            with mock.patch.object(cli, "_prefix"), mock.patch.object(programs, "find", return_value=sda), \
                 mock.patch.object(programs, "run", return_value=proc), mock.patch.object(programs, "watch_plugins"), \
                 mock.patch.object(cli.setup, "after_change") as sync, mock.patch.object(pkg, "finish_staged", side_effect=RuntimeError("boom")), \
                 mock.patch("sys.stderr"):
                cli.cmd_run(argparse.Namespace(name=sda.name, debug=None, wait=wait))
            sync.assert_called()
    def test_install_finds_the_msi_beside_a_nested_setup_xml(self):
        # Cubase 16: setup.xml in "Additional Content/Installer Data", the MSI in "Additional Content/Installer"
        with tempfile.TemporaryDirectory() as tmp:
            p = Prefix(Path(tmp) / "prefix", WineBuild(Path(tmp) / "wine")); p.drive_c.mkdir(parents=True)
            z = Path(tmp) / "Cubase_16.0.5_Installer_win64.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("Cubase 16/Additional Content/Installer Data/setup.xml",
                            "<Setup><product file='Cubase16.msi'><title>Cubase 16</title></product>"
                            "<include><path msi='Additional Content/Installer'/></include></Setup>")
                zf.writestr("Cubase 16/Additional Content/Installer/Cubase16.msi", "msi")
            calls = []
            def run(argv, **kw): calls.append(argv); return subprocess.CompletedProcess(argv, 0)
            with mock.patch.object(p, "run", side_effect=run), mock.patch.object(pkg.paths, "CACHE", Path(tmp)):
                res = pkg.install(p, z)
            self.assertEqual((res["installed"], res["failed"]), (["Cubase16.msi"], []))
            self.assertTrue(calls[0][2].endswith("Cubase16.msi"))
    def test_include_path_from_the_package_top_before_a_search(self):
        # an older copy elsewhere in the package must not win over the path setup.xml names
        with tempfile.TemporaryDirectory() as tmp:
            top = Path(tmp) / "Cubase 16"; setup_dir = top / "Additional Content/Installer Data"; setup_dir.mkdir(parents=True)
            want = top / "Additional Content/Installer/Cubase16.msi"; want.parent.mkdir(); want.write_text("msi")
            (top / "Old").mkdir(); (top / "Old/Cubase16.msi").write_text("old")
            with mock.patch.object(Path, "rglob", side_effect=AssertionError("searched")):
                self.assertEqual(pkg._find_msi(setup_dir, "Cubase16.msi", ["Additional Content/Installer"], root=Path(tmp)), want)
            self.assertIsNone(pkg._find_msi(setup_dir, "Gone.msi", [], root=Path(tmp)))
            self.assertEqual(pkg._find_msi(setup_dir, "Cubase16.msi", [], root=Path(tmp)).name, "Cubase16.msi")   # the search, last
    def test_staged_finds_failed_status_files_with_their_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Prefix(Path(tmp) / "prefix", WineBuild(Path(tmp) / "wine")); (p.drive_c / "users/me").mkdir(parents=True)
            job = p.user_dir / "AppData/Local/Temp/6dcf"; job.mkdir(parents=True)
            make_zip(job / "MediaBay_Installer_win64.zip")
            (job / "Steinberg_MediaBay_Installer.json").write_text(json.dumps({"artifactId": "Steinberg_MediaBay_Installer", "success": False, "errors": [{"code": "231", "message": "preinstall.ps1: not trusted."}]}))
            (job / "Steinberg_Library_Manager_Installer.json").write_text(json.dumps({"success": True}))
            make_zip(job / "Steinberg_Library_Manager_win64.zip")
            st = pkg.staged(p)
            self.assertEqual([(s.name, s.package.name) for s in st], [("MediaBay", "MediaBay_Installer_win64.zip")])

if __name__ == "__main__": unittest.main()
