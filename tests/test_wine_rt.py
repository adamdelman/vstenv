"""The patched wineserver's realtime threads (patches/wine/server-realtime-priority-*.patch), on a real Wine.

Runs only when VSTENV_RT_WINE names a Wine build's root and VSTENV_RT_SERVER the wineserver to test
(skipped otherwise, and without x86_64-w64-mingw32-gcc, prlimit or taskset). Each check starts its own
server in a scratch prefix under TMPDIR, with RLIMIT_RTPRIO set by prlimit, and reads the scheduling
of tools/rtcheck's named threads from outside. The spin check pins the server and two spinning
time-critical threads to two CPUs: with realtime throttling off, only a guard that does not need the
server's main loop gets them back. Every run is killed by PID after 20 s.

    VSTENV_RT_WINE=<wine root> VSTENV_RT_SERVER=<wineserver> python -m unittest tests.test_wine_rt
"""
import os, shutil, signal, subprocess, tempfile, threading, time, unittest
from pathlib import Path

WINE = os.environ.get("VSTENV_RT_WINE", "")
SERVER = os.environ.get("VSTENV_RT_SERVER", "")
SRC = Path(__file__).resolve().parent.parent / "tools" / "rtcheck" / "rtcheck.c"
RESET_ON_FORK = 0x40000000
OTHER, FIFO = os.SCHED_OTHER, os.SCHED_FIFO

def _hard_nice() -> int:
    import resource
    return resource.getrlimit(resource.RLIMIT_NICE)[1]

@unittest.skipUnless(WINE and SERVER and shutil.which("x86_64-w64-mingw32-gcc") and shutil.which("prlimit") and shutil.which("taskset"),
                     "set VSTENV_RT_WINE and VSTENV_RT_SERVER (and install mingw-w64, prlimit, taskset) to run")
class WineRealtimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vstenv-rt-"))
        cls.exe = cls.tmp / "rtcheck.exe"
        subprocess.run(["x86_64-w64-mingw32-gcc", "-O2", "-o", str(cls.exe), str(SRC)], check=True)
        cls.env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        cls.env.update(WINEPREFIX=str(cls.tmp / "prefix"), WINESERVER=SERVER, WINEDEBUG="-all", WINEDLLOVERRIDES="mscoree,mshtml=;winebus.sys,wineusb.sys,winebth.sys=d")   # each would hold every start 10 s
        cls._run_bounded([f"{WINE}/bin/wineboot", "-i"], 300)
        cls._stop()

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _server(cls, *args, prefix=()):
        subprocess.run([*prefix, SERVER, *args], env=cls.env, timeout=60)

    @classmethod
    def _prefix_pids(cls):
        """Every process of the scratch prefix (by WINEPREFIX in its environment), the server too."""
        want = f"WINEPREFIX={cls.env['WINEPREFIX']}".encode()
        pids = []
        for proc in Path("/proc").glob("[0-9]*"):
            try:
                if want in (proc / "environ").read_bytes().split(b"\0"): pids.append(int(proc.name))
            except OSError: continue
        return pids

    @classmethod
    def _kill_all(cls):
        for pid in cls._prefix_pids():
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass

    @classmethod
    def _stop(cls):
        """The prefix's server and all its programs gone."""
        cls._server("-k"); cls._server("-w"); cls._kill_all()

    @classmethod
    def _run_bounded(cls, cmd, limit):
        p = subprocess.Popen(cmd, env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t = threading.Timer(limit, p.kill); t.start()
        try: return p.wait()
        finally: t.cancel()

    def tearDown(self):
        self._stop()

    def _start(self, mode, *args, server_rtprio="98", client_rtprio="98", cpus=None, wait=True):
        """Start a fresh server and rtcheck.exe; returns the client Popen. Both are killed after 20 s."""
        pin = ["taskset", "-c", ",".join(map(str, cpus))] if cpus else []
        self._stop()
        self._server("-p5", prefix=["prlimit", f"--rtprio={server_rtprio}", *pin])
        p = subprocess.Popen(["prlimit", f"--rtprio={client_rtprio}", *pin, f"{WINE}/bin/wine", str(self.exe), mode, *args],
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.watch = threading.Timer(20, self._kill_all); self.watch.start()
        self.addCleanup(self.watch.cancel)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.addCleanup(p.stdout.close)
        if wait: self.assertEqual(p.stdout.readline().strip(), "started")
        return p

    def _threads(self, names, wait=5.0):
        """unix tid of each named thread of rtcheck.exe, found under /proc."""
        end = time.monotonic() + wait
        while time.monotonic() < end:
            found = {}
            for pid in self._prefix_pids():
                for task in Path(f"/proc/{pid}/task").glob("[0-9]*"):
                    try:
                        if b"rtcheck.exe" not in (task.parent.parent / "cmdline").read_bytes(): continue
                        comm = (task / "comm").read_text().strip()
                    except OSError: continue
                    if comm in names: found[comm] = int(task.name)
            if set(found) == set(names): return found
            time.sleep(0.02)
        self.fail(f"threads {names} not found (saw {sorted(found)})")

    @staticmethod
    def _sched(tid):
        """(policy, SCHED_FIFO priority, nice) of a thread."""
        return (os.sched_getscheduler(tid) & ~RESET_ON_FORK, os.sched_getparam(tid).sched_priority,
                os.getpriority(os.PRIO_PROCESS, tid))

    def test_time_critical_runs_fifo_70_and_goes_back(self):
        p = self._start("basic"); t = self._threads(["tc", "normal"])
        time.sleep(1.0)
        self.assertEqual(self._sched(t["tc"])[:2], (FIFO, 70))
        self.assertEqual(self._sched(t["normal"])[:2], (OTHER, 0))
        time.sleep(3.0)   # tc set back to normal at 3 s
        self.assertEqual(self._sched(t["tc"])[:2], (OTHER, 0))
        self.assertEqual(p.wait(10), 0)

    def test_without_rtprio_time_critical_gets_the_nice_value(self):
        p = self._start("basic", server_rtprio="0", client_rtprio="0"); t = self._threads(["tc", "normal"])
        time.sleep(1.0)
        policy, _, nice = self._sched(t["tc"])
        self.assertEqual(policy, OTHER)
        if _hard_nice() > 20: self.assertLess(nice, 0)
        self.assertEqual(p.wait(10), 0)

    def test_the_client_limit_caps_the_priority(self):
        """RLIMIT_RTPRIO is checked on the thread being changed: the client's 50, not the server's 98."""
        p = self._start("basic", client_rtprio="50"); t = self._threads(["tc", "normal"])
        time.sleep(1.0)
        self.assertEqual(self._sched(t["tc"])[:2], (FIFO, 50))
        self.assertEqual(p.wait(10), 0)

    def test_a_soft_limit_below_the_hard_one_is_raised(self):
        p = self._start("basic", client_rtprio="0:98"); t = self._threads(["tc", "normal"])
        time.sleep(1.0)
        self.assertEqual(self._sched(t["tc"])[:2], (FIFO, 70))
        self.assertEqual(p.wait(10), 0)

    def test_a_thread_someone_else_made_realtime_is_left_alone(self):
        p = self._start("ext"); t = self._threads(["ext"])
        os.sched_setscheduler(t["ext"], FIFO, os.sched_param(83))   # as pipewire-jack does its data loop
        for at, wait in ((3, 3.0), (5, 2.0)):   # time-critical from 2 s, normal from 4 s
            time.sleep(wait)
            self.assertEqual(self._sched(t["ext"])[:2], (FIFO, 83), f"at {at} s")
        self.assertEqual(p.wait(10), 0)

    def test_a_realtime_thread_someone_else_changed_is_theirs(self):
        """Made FIFO 70 by the server, then FIFO 83 from outside: going back to normal must not drop it to SCHED_OTHER."""
        p = self._start("basic"); t0 = time.monotonic(); t = self._threads(["tc", "normal"])
        time.sleep(2.97 - (time.monotonic() - t0))   # just before tc goes back to normal at 3 s, likely before the guard looks again
        self.assertEqual(self._sched(t["tc"])[:2], (FIFO, 70))
        os.sched_setscheduler(t["tc"], FIFO | RESET_ON_FORK, os.sched_param(83))   # unprivileged, it cannot drop the flag
        time.sleep(1.0)
        self.assertEqual(self._sched(t["tc"])[:2], (FIFO, 83))
        self.assertEqual(p.wait(10), 0)

    def test_spinners_on_every_cpu_they_have_are_held_back(self):
        """Two spinning FIFO threads and the server on the same two CPUs: each is held back within ~1 s and released."""
        cpus = sorted(os.sched_getaffinity(0))[-2:]
        # not waiting for "started": the main thread that prints it has to wait for the CPUs too
        p = self._start("spin", "2", cpus=cpus, wait=False); t = self._threads(["spin1", "spin2", "idle"], wait=15.0)
        start = time.monotonic(); fifo, held, released, idle = {}, {}, {}, set()
        while time.monotonic() - start < 3.5:
            for name, tid in t.items():
                try: policy = self._sched(tid)[0]
                except OSError: continue
                if name == "idle":
                    if policy == FIFO or idle: idle.add(policy)   # from when it is time-critical
                    continue
                now = time.monotonic() - start
                if policy == FIFO and name not in held: fifo.setdefault(name, now)
                elif policy == OTHER and name in fifo: held.setdefault(name, now - fifo[name])   # since it became FIFO
                elif policy == FIFO: released.setdefault(name, now)
            time.sleep(0.01)
        self.assertEqual(sorted(held), ["spin1", "spin2"], f"held {held}")
        self.assertLess(max(held.values()), 1.0, f"held after {held} s at SCHED_FIFO")
        self.assertEqual(sorted(released), ["spin1", "spin2"], f"released {released}")
        self.assertEqual(idle, {FIFO})
        self.assertEqual(p.wait(10), 0)

if __name__ == "__main__":
    unittest.main()
