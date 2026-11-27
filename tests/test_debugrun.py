import os, subprocess, tarfile, tempfile, time, unittest
from pathlib import Path
from unittest import mock
from vstenv import cli, debugrun, paths, programs, report, wine

class FakePrefix:
    """Registry as a dict; reg_import_values applies what the .reg file would."""
    def __init__(self, reg, path=Path("/pfx"), running=()):
        self.reg = reg; self.debug = None; self.path = path; self.running = running
        self.rc = 0
    def is_running(self, exe): return exe in self.running
    def reg_query(self, key): return dict(self.reg.get(key, {}))
    def reg_export(self, key):      # values stored as their exported line, or plain strings (REG_SZ)
        if key not in self.reg: return ""
        return "\r\n".join(["Windows Registry Editor Version 5.00", "", f"[{key}]",
                             *(v if v.startswith('"') else f'"{n}"="{v}"' for n, v in self.reg[key].items())]) + "\r\n"
    def reg_import_values(self, values, name=""):
        if self.rc: return self.rc
        for key, vals in values.items():
            for n, (kind, data) in vals.items():
                if kind is None: self.reg.get(key, {}).pop(n, None)
                elif kind == "raw":       # a REG_SZ line back to its string, other types kept as their line
                    self.reg.setdefault(key, {})[n] = wine._parse_reg_export(data)[n] if data.partition("=")[2].startswith('"') else data
                else: self.reg.setdefault(key, {})[n] = str(data)
        return 0

WINEDBG = r"HKCU\Software\Wine\WineDbg"

class DebugRunTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        p = mock.patch.object(paths, "LOGS", self.d); p.start(); self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ); p.start(); self.addCleanup(p.stop); os.environ.pop("WINEDEBUG", None)

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

    def test_users_winedebug_is_added(self):
        with mock.patch.dict(os.environ, {"WINEDEBUG": "+locale,warn+ole"}):
            self.assertTrue(debugrun.channels("crash").endswith(",fixme-all,+locale,warn+ole"))
            self.assertIn("+locale", debugrun.debug_env("graphics", self.d)["WINEDEBUG"])
        with mock.patch.dict(os.environ, {"WINEDEBUG": ""}):
            self.assertTrue(debugrun.channels("crash").endswith("fixme-all"))

    def test_crash_handler_set_and_restored(self):
        key = rf"HKLM\Software\{debugrun.AEDEBUG}"
        p = FakePrefix({key: {"Debugger": "mydbg %ld", "Other": "x"}})
        saved = debugrun.set_crash_handler(p)
        self.assertEqual(p.reg[key], {"Debugger": "winedbg --auto %ld %ld", "Auto": "1", "Other": "x"})
        self.assertEqual(p.reg[WINEDBG], {"ShowCrashDialog": "0"})
        debugrun.restore_crash_handler(p, saved)
        self.assertEqual(p.reg[key], {"Debugger": "mydbg %ld", "Other": "x"})   # Auto, absent before, is gone again
        self.assertEqual(p.reg[WINEDBG], {})

    def test_restore_keeps_the_original_type(self):
        key = rf"HKLM\Software\{debugrun.AEDEBUG}"
        expand = '"Debugger"=hex(2):6d,00,79,00,00,00'; binary = '"Auto"=hex:01,02'
        p = FakePrefix({key: {"Debugger": expand, "Auto": binary}})
        saved = debugrun.set_crash_handler(p)
        self.assertEqual(p.reg[key], {"Debugger": "winedbg --auto %ld %ld", "Auto": "1"})
        debugrun.restore_crash_handler(p, saved)
        self.assertEqual(p.reg[key], {"Debugger": expand, "Auto": binary})   # the exported lines, re-imported as they were

    def test_saved_to_the_debug_dir_first_and_a_leftover_restored(self):
        key = rf"HKLM\Software\{debugrun.AEDEBUG}"
        p = FakePrefix({key: {"Debugger": "mydbg %ld"}}); d = self.d / "debug-x-1"; d.mkdir()
        debugrun.set_crash_handler(p, d)                       # ...and the session dies here
        self.assertTrue((d / debugrun.SAVED).exists())
        with mock.patch.object(debugrun, "_alive", return_value=True), mock.patch.object(debugrun.os, "getpid", return_value=1):
            self.assertEqual(debugrun.restore_leftover(p), 0)   # its session still runs: left alone
        self.assertEqual(debugrun.restore_leftover(p), 1)
        self.assertEqual(p.reg[key], {"Debugger": "mydbg %ld"}); self.assertEqual(p.reg[WINEDBG], {})
        self.assertFalse((d / debugrun.SAVED).exists())

    def test_failed_restore_keeps_the_file_and_warns(self):
        p = FakePrefix({}); d = self.d / "debug-x-1"; d.mkdir()
        saved = debugrun.set_crash_handler(p, d); p.rc = 1
        with mock.patch("sys.stderr") as err: self.assertFalse(debugrun.restore_crash_handler(p, saved, d))
        self.assertTrue((d / debugrun.SAVED).exists()); self.assertIn("could not restore", "".join(c.args[0] for c in err.write.call_args_list))

    def test_sigterm_during_the_run_restores(self):
        import signal
        p = FakePrefix({}); prog = mock.Mock(exe=""); prog.name = "x"
        def killed(): os.kill(os.getpid(), signal.SIGTERM); time.sleep(5)
        with mock.patch.object(programs, "run", return_value=mock.Mock(wait=killed)), mock.patch.object(debugrun, "sysinfo", return_value=""):
            with self.assertRaises(SystemExit): debugrun.run(p, prog)
        self.assertEqual(p.reg[WINEDBG], {}); self.assertEqual(list(self.d.glob(f"*/{debugrun.SAVED}")), [])
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)

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

    def test_refuses_when_already_running(self):
        p = FakePrefix({}, running=("Cubase14.exe",)); prog = mock.Mock(exe="C:\\x\\Cubase14.exe"); prog.name = "Cubase 14"
        with mock.patch.object(programs, "run") as run, self.assertRaisesRegex(RuntimeError, "already running"):
            debugrun.run(p, prog)
        run.assert_not_called(); self.assertEqual(p.reg, {}); self.assertEqual(list(self.d.iterdir()), [])

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
        """The host script, run here against a stand-in process with the prefix in its environment:
        a busy one, so the second sample shows CPU used since the first."""
        import sys
        out = self.d / "threads.log"; pfx = str(self.d / "pfx")
        prog = subprocess.Popen([sys.executable, "-c", "import time\nt = time.time() + 5\nwhile time.time() < t: pass"],
                                env={**os.environ, "WINEPREFIX": pfx})
        comm = Path(f"/proc/{prog.pid}/comm").read_text().strip()
        s = debugrun.start_sampler(mock.Mock(path=pfx), comm, out)
        try: s.wait(20)                                     # it stops once the program has gone
        finally: s.kill(); prog.kill()
        samples = [b.splitlines() for b in out.read_text().split("### ")[1:]]
        self.assertGreaterEqual(len(samples), 2)
        self.assertIn(f"pid {prog.pid}", samples[0][0])
        delta, total, tid, name, sched = samples[1][1].split()[:5]
        self.assertEqual(sched, f"OTHER:{os.getpriority(os.PRIO_PROCESS, 0)}")
        self.assertEqual((tid, name), (str(prog.pid), comm))       # the busy thread is on top
        self.assertGreater(int(delta), 100); self.assertGreaterEqual(int(total), int(delta))

    def test_hang_dump_goes_to_the_newest_dir(self):
        old = self.d / "debug-a-1"; new = self.d / "debug-b-2"; old.mkdir(); new.mkdir(); os.utime(old, (1, 1))
        p = mock.Mock(); p.run.return_value = mock.Mock(stdout="Backtracing for thread 0034\n", stderr="")
        f = debugrun.hang_dump(p)
        self.assertEqual(f.parent, new); self.assertIn("Backtracing", f.read_text())
        self.assertEqual(p.run.call_args.args[0], ["winedbg", "--command", "bt all"])

    def test_hang_dump_starts_a_new_dir_when_the_newest_is_old(self):
        old = self.d / "debug-a-1"; old.mkdir(); (old / "wine.log").write_text("x"); os.utime(old / "wine.log", (1, 1)); os.utime(old, (1, 1))
        p = mock.Mock(); p.run.side_effect = subprocess.TimeoutExpired("winedbg", 600, output="Backtracing for thread 0034\n")
        f = debugrun.hang_dump(p, now=10_000)
        self.assertTrue(f.parent.name.startswith("debug-hang-")); self.assertNotEqual(f.parent, old)
        self.assertIn("Backtracing", f.read_text()); self.assertIn("timed out", f.read_text())
        live = self.d / "debug-b-2"; live.mkdir(); os.utime(live, (1, 1)); (live / "wine.log").write_text("x")   # a session still writing
        self.assertEqual(debugrun.hang_dump(mock.Mock(**{"run.return_value": mock.Mock(stdout="", stderr="")})).parent, live)

    def test_sampler_gives_up_when_the_program_never_appears(self):
        s = debugrun.host.popen(["sh", "-c", debugrun.SAMPLER], env={"VSTENV_PREFIX": "/nope", "VSTENV_COMM": "no-such-prog",
                                "VSTENV_OUT": str(self.d / "t.log"), "VSTENV_WAIT_ROUNDS": "0"})
        try: self.assertEqual(s.wait(20), 0)
        finally: s.kill()

    def test_sysinfo_degrades(self):
        p = mock.Mock(); p.build.version.side_effect = OSError; p.wine_env.return_value = {"WINEFSYNC": "1"}
        with mock.patch.object(debugrun.host, "sh", return_value=""), mock.patch("vstenv.ntsync.status", return_value={"usable": True}), \
             mock.patch("vstenv.winefixes.status", return_value={"files": []}), mock.patch("vstenv.dxvk.status", side_effect=RuntimeError):
            text = debugrun.sysinfo(p, "crash")
        self.assertIn("wine          ? (OSError)", text); self.assertIn("wine-fixes    none", text)
        self.assertIn("ntsync        usable=True", text); self.assertIn("dxvk          ? (RuntimeError)", text)

    def test_sysinfo_shows_the_users_dxvk_config(self):
        p = mock.Mock(); p.wine_env.return_value = {"DXVK_CONFIG": "ours"}
        with mock.patch.object(debugrun.host, "sh", return_value=""), mock.patch("vstenv.ntsync.status", return_value={}), \
             mock.patch("vstenv.winefixes.status", return_value={"files": []}), mock.patch("vstenv.dxvk.status", side_effect=RuntimeError):
            with mock.patch.dict(os.environ):
                os.environ.pop("DXVK_CONFIG", None); self.assertIn("DXVK_CONFIG   ours\n", debugrun.sysinfo(p, "crash"))
            with mock.patch.dict(os.environ, {"DXVK_CONFIG": "d3d11.x = 1"}):
                self.assertIn("DXVK_CONFIG   d3d11.x = 1 (yours)", debugrun.sysinfo(p, "crash"))

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

    def test_scrub_wine_forms(self):
        home = Path.home(); h = "\\".join(home.parts[1:])
        with mock.patch.object(report, "_login", return_value="loginname"):
            out = report.scrub(f"\\??\\unix\\{h}\\a /{'//'.join(home.parts[1:])}/b C:\\users\\loginname\\c C:\\users\\loginnamex")
        self.assertNotIn(home.name, out.replace("<user>", "")); self.assertIn("\\??\\unix~\\a", out)
        self.assertIn("users\\<user>\\c", out); self.assertIn("loginnamex", out)

    def test_scrub_host_and_login_names_as_words(self):
        # Steinberg Download Assistant's log names the machine and who started it.
        log = ("sam-desktop.lan: SDA 2.1 started by sam in C:\\Program Files (x86)\\Steinberg\n"
               "SAM-DESKTOP ok; samba sams sesame stay; user is SAM; xy too short")
        with mock.patch.object(report, "_login", return_value="sam"), \
             mock.patch.object(report, "_hostname", return_value="sam-desktop.lan"), \
             mock.patch.object(Path, "home", return_value=Path("/home/sam")):
            out = report.scrub(log)
        self.assertEqual(out, "<host>: SDA 2.1 started by <user> in C:\\Program Files (x86)\\Steinberg\n"
                              "<host> ok; samba sams sesame stay; user is <user>; xy too short")
        with mock.patch.object(report, "_login", return_value="user"), mock.patch.object(report, "_hostname", return_value="localhost"), \
             mock.patch.object(Path, "home", return_value=Path("/home/xy")):
            self.assertEqual(report.scrub("C:\\users\\user and user on localhost, xy"), "C:\\users\\<user> and <user> on localhost, xy")   # placeholders and localhost left alone

    def test_steinberg_crash_dumps(self):
        pfx = self.d / "pfx"; dumps = pfx / "u" / report.CRASH_DUMPS; dumps.mkdir(parents=True)
        (dumps / "Cubase 1.dmp").write_bytes(b"MDMP old"); os.utime(dumps / "Cubase 1.dmp", (1, 1))
        (dumps / "Cubase 2.dmp").write_bytes(b"MDMP\x00new")
        p = mock.Mock(exists=True, user_dir=pfx / "u")
        with mock.patch("vstenv.vendors.all", return_value=[]), mock.patch("vstenv.debugrun.restore_leftover"):
            plain = report.write_bundle(self.d / "plain.tar.gz", p)
            dest = report.write_bundle(self.d / "r.tar.gz", p, crash_dump=True)
        with tarfile.open(plain) as t:             # not without asking: a dump is memory, unscrubbed
            self.assertIn("report/crashdumps.txt", t.getnames()); self.assertFalse([n for n in t.getnames() if n.endswith(".dmp")])
        with tarfile.open(dest) as t:
            listing = t.extractfile("report/crashdumps.txt").read().decode()
            self.assertEqual(t.extractfile("report/crashdumps/Cubase 2.dmp").read(), b"MDMP\x00new")
            self.assertNotIn("report/crashdumps/Cubase 1.dmp", t.getnames())
        self.assertIn("Cubase 1.dmp", listing); self.assertIn("Cubase 2.dmp", listing)

if __name__ == "__main__": unittest.main()
