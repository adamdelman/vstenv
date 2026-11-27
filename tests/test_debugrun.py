import os, subprocess, tarfile, tempfile, time, unittest
from pathlib import Path
from unittest import mock
from vstenv import cli, debugrun, paths, programs, report, wine

class FakePrefix:
    """Registry as a dict; reg_import_values applies what the .reg file would."""
    def __init__(self, reg, path=Path("/pfx")): self.reg = reg; self.debug = None; self.path = path
    def reg_query(self, key): return dict(self.reg.get(key, {}))
    def reg_import_values(self, values, name=""):
        for key, vals in values.items():
            for n, (kind, data) in vals.items():
                if kind is None: self.reg.get(key, {}).pop(n, None)
                else: self.reg.setdefault(key, {})[n] = str(data)
        return 0

WINEDBG = r"HKCU\Software\Wine\WineDbg"

class DebugRunTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        p = mock.patch.object(paths, "LOGS", self.d); p.start(); self.addCleanup(p.stop)

    def test_dir_name(self):
        t = time.mktime((2026, 11, 27, 9, 5, 7, 0, 0, -1))
        self.assertEqual(debugrun.debug_dir("Cubase 14", t), self.d / "debug-Cubase-14-20261127-090507")
        self.assertEqual(debugrun.debug_dir("a/../b: c", t).name, "debug-a-..-b--c-20261127-090507")
        self.assertEqual(debugrun.debug_dir("/", t).name, "debug-program-20261127-090507")

    def test_topics(self):
        crash = debugrun.channels("crash").split(",")
        for c in ("+seh", "+loaddll", "warn+module", "+timestamp", "+pid", "+combase", "err+all"): self.assertIn(c, crash)
        self.assertNotIn("+module", crash); self.assertIn("+module", debugrun.channels("loader").split(","))
        self.assertIn("+dcomp", debugrun.channels("graphics")); self.assertIn("+xdnd", debugrun.channels("dragdrop"))
        self.assertIn("+x11drv", debugrun.channels("windows"))
        al = debugrun.channels("all").split(",")
        for t in debugrun.TOPICS: self.assertTrue(set(debugrun.channels(t).split(",")) <= set(al), t)
        for t in debugrun.TOPIC_NAMES: self.assertNotIn("relay", debugrun.channels(t))
        self.assertEqual(debugrun.debug_env("crash", self.d), {"WINEDEBUG": debugrun.channels("crash")})
        e = debugrun.debug_env("graphics", self.d)
        self.assertEqual((e["DXVK_LOG_LEVEL"], e["DXVK_LOG_PATH"]), ("info", str(self.d)))
        self.assertIn("DXVK_LOG_PATH", debugrun.debug_env("all", self.d))

    def test_crash_handler_set_and_restored(self):
        key = rf"HKLM\Software\{debugrun.AEDEBUG}"
        p = FakePrefix({key: {"Debugger": "mydbg %ld", "Other": "x"}})
        saved = debugrun.set_crash_handler(p)
        self.assertEqual(p.reg[key], {"Debugger": "winedbg --auto %ld %ld", "Auto": "1", "Other": "x"})
        self.assertEqual(p.reg[WINEDBG], {"ShowCrashDialog": "0"})
        debugrun.restore_crash_handler(p, saved)
        self.assertEqual(p.reg[key], {"Debugger": "mydbg %ld", "Other": "x"})   # Auto, absent before, is gone again
        self.assertEqual(p.reg[WINEDBG], {})

    def test_run_holds_env_and_handler_until_exit_then_restores(self):
        p = FakePrefix({}); seen = {}
        def fake_run(pp, prog, r=None):
            seen["debug"] = pp.debug; seen["dbg"] = dict(pp.reg[WINEDBG])
            return mock.Mock(wait=mock.Mock(side_effect=KeyboardInterrupt))    # Ctrl+C ends the session
        prog = mock.Mock(exe="C:\\Program Files\\Steinberg\\Cubase 14\\Cubase14.exe"); prog.name = "Cubase 14"
        sampler = mock.Mock()
        with mock.patch.object(programs, "run", fake_run), mock.patch.object(debugrun, "sysinfo", return_value="info\n"), \
             mock.patch.object(debugrun, "start_sampler", return_value=sampler) as ss:
            d = debugrun.run(p, prog, "graphics")
        self.assertEqual(seen["debug"], (d / "wine.log", debugrun.debug_env("graphics", d)))
        self.assertEqual(seen["dbg"], {"ShowCrashDialog": "0"})
        self.assertEqual(ss.call_args.args[1:], ("Cubase14.exe", d / "threads.log")); sampler.terminate.assert_called_once()
        self.assertIsNone(p.debug); self.assertEqual(p.reg[WINEDBG], {})
        self.assertTrue(d.name.startswith("debug-Cubase-14-")); self.assertEqual((d / "sysinfo.txt").read_text(), "info\n")

    def test_restored_when_the_launch_fails(self):
        p = FakePrefix({}); prog = mock.Mock(exe=""); prog.name = "x"
        with mock.patch.object(programs, "run", side_effect=RuntimeError("no")), mock.patch.object(debugrun, "sysinfo", return_value=""):
            with self.assertRaises(RuntimeError): debugrun.run(p, prog)
        self.assertIsNone(p.debug); self.assertEqual(p.reg[WINEDBG], {})

    def test_spawn_uses_the_debug_log_and_env(self):
        p = wine.Prefix(self.d / "pfx", mock.Mock(wine="wine")); log = self.d / "wine.log"
        p.debug = (log, {"WINEDEBUG": "+seh", "DXVK_LOG_LEVEL": "info"})
        with mock.patch.object(wine.host, "popen") as po, mock.patch.object(p, "wine_env", side_effect=lambda e: e):
            p.spawn(["x.exe"], env={"A": "1"})
        kw = po.call_args.kwargs
        self.assertEqual(kw["env"], {"A": "1", "WINEDEBUG": "+seh", "DXVK_LOG_LEVEL": "info"})
        self.assertEqual(kw["stdout"].name, str(log)); kw["stdout"].close()

    def test_reg_delete_syntax(self):
        p = wine.Prefix(self.d / "pfx", mock.Mock())
        with mock.patch.object(p, "reg_import", side_effect=lambda text, name: text):
            text = p.reg_import_values({r"HKCU\Software\X": {"A": (None, ""), "B": ("REG_SZ", "v")}})
        self.assertIn('"A"=-', text); self.assertIn('"B"="v"', text)

    def test_sampler_writes_the_programs_threads(self):
        """The host script, run here against a stand-in process with the prefix in its environment."""
        out = self.d / "threads.log"; pfx = str(self.d / "pfx")
        prog = subprocess.Popen(["sleep", "4"], env={**os.environ, "WINEPREFIX": pfx})
        s = debugrun.start_sampler(mock.Mock(path=pfx), "sleep", out)
        try: s.wait(15)                                     # it stops once the program has gone
        finally: s.kill(); prog.kill()
        text = out.read_text()
        self.assertIn(f"pid {prog.pid}", text); self.assertRegex(text, rf"\n\d+ {prog.pid} sleep ")

    def test_hang_dump_goes_to_the_newest_dir(self):
        old = self.d / "debug-a-1"; new = self.d / "debug-b-2"; old.mkdir(); new.mkdir(); os.utime(old, (1, 1))
        p = mock.Mock(); p.run.return_value = mock.Mock(stdout="Backtracing for thread 0034\n", stderr="")
        f = debugrun.hang_dump(p)
        self.assertEqual(f.parent, new); self.assertIn("Backtracing", f.read_text())
        self.assertEqual(p.run.call_args.args[0], ["winedbg", "--command", "bt all"])

    def test_sysinfo_degrades(self):
        p = mock.Mock(); p.build.version.side_effect = OSError; p.wine_env.return_value = {"WINEFSYNC": "1"}
        with mock.patch.object(debugrun.host, "sh", return_value=""), mock.patch("vstenv.ntsync.status", return_value={"usable": True}), \
             mock.patch("vstenv.winefixes.status", return_value={"files": []}), mock.patch("vstenv.dxvk.status", side_effect=RuntimeError):
            text = debugrun.sysinfo(p, "crash")
        self.assertIn("wine          ? (OSError)", text); self.assertIn("wine-fixes    none", text)
        self.assertIn("ntsync        usable=True", text); self.assertIn("dxvk          ? (RuntimeError)", text)

class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        with mock.patch.object(cli, "_prefix"), mock.patch.object(cli.programs, "find", side_effect=lambda p, n: n), \
             mock.patch.object(cli, "_run_debug") as rd:
            cli.main(["run", *argv])
        return rd.call_args.args[1:3]

    def test_debug_forms(self):
        self.assertEqual(self.run_cli("--debug", "Cubase 14"), ("Cubase 14", "crash"))
        self.assertEqual(self.run_cli("Cubase 14", "--debug"), ("Cubase 14", "crash"))
        self.assertEqual(self.run_cli("--debug=graphics", "Cubase 14"), ("Cubase 14", "graphics"))
        with self.assertRaises(SystemExit): self.run_cli("--debug=nope", "Cubase 14")

class ReportDebugLogsTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); self.logs = self.d / "logs"; self.logs.mkdir()
        for patch in (mock.patch.object(paths, "LOGS", self.logs), mock.patch.object(paths, "ensure_dirs"),
                      mock.patch.object(report, "summary", return_value="s"), mock.patch.object(report, "DEBUG_TAIL", 1000)):
            patch.start(); self.addCleanup(patch.stop)

    def test_newest_debug_dir_capped_and_scrubbed(self):
        home = Path.home()
        for i, n in enumerate(("debug-a-1", "debug-b-2")):
            dd = self.logs / n; dd.mkdir()
            (dd / "wine.log").write_text("x" * 5000 + f"\nC:\\\\users\\\\{home.name}\\\\AppData Z:\\home\\{home.name.upper()}\\y {home}/z "
                                         "ABCDE-12345-ABCDE-12345-ABCDE\n")
            (dd / "threads.log").write_text("### 10:00:00\n")
            os.utime(dd, (1000 + i, 1000 + i))
        (self.logs / "install.log").write_text("plain\n")
        with tarfile.open(report.write_bundle(self.d / "r.tar.gz")) as t:
            names = sorted(t.getnames()); body = t.extractfile("report/debug-b-2/wine.log").read().decode()
        self.assertEqual(names, ["report/debug-b-2/threads.log", "report/debug-b-2/wine.log", "report/logs/install.log", "report/summary.txt"])
        self.assertLessEqual(len(body), 1000)
        self.assertNotIn(home.name, body.replace("<user>", ""))
        self.assertIn("users\\\\<user>", body); self.assertIn("<serial>", body); self.assertIn("~\\y", body)

    def test_steinberg_crash_dumps(self):
        pfx = self.d / "pfx"; dumps = pfx / "u" / report.CRASH_DUMPS; dumps.mkdir(parents=True)
        (dumps / "Cubase 1.dmp").write_bytes(b"MDMP old"); os.utime(dumps / "Cubase 1.dmp", (1, 1))
        (dumps / "Cubase 2.dmp").write_bytes(b"MDMP\x00new")
        p = mock.Mock(exists=True, user_dir=pfx / "u")
        with mock.patch("vstenv.vendors.all", return_value=[]):
            dest = report.write_bundle(self.d / "r.tar.gz", p)
        with tarfile.open(dest) as t:
            listing = t.extractfile("report/crashdumps.txt").read().decode()
            self.assertEqual(t.extractfile("report/crashdumps/Cubase 2.dmp").read(), b"MDMP\x00new")
            self.assertNotIn("report/crashdumps/Cubase 1.dmp", t.getnames())
        self.assertIn("Cubase 1.dmp", listing); self.assertIn("Cubase 2.dmp", listing)

if __name__ == "__main__": unittest.main()
