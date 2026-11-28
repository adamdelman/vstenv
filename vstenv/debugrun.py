"""`vstenv run --debug`: run a program once with the logging a bug report needs.

A user whose program crashes or hangs can only say that much ("kernelbase.dll is
the culprit"); the developer needs which exception, where, which DLLs were
loaded around it, and what each thread was doing. A debug run writes all of it
into one directory, paths.LOGS/debug-<program>-<time>/:

  wine.log      the program's output with the topic's Wine channels, and the
                automatic backtrace Wine's crash handler writes on a crash
  threads.log   every 3 s, the busiest threads of the program: CPU time, name and
                kernel wait channel (a hang: a busy loop or a deadlock, and where)
  sysinfo.txt   the machine, session, GPU, Wine, its patched DLLs and DXVK
  backtrace-*   all threads' stacks, written by `vstenv hang-dump` during a hang
  *.log (DXVK)  DXVK's own logs, for the graphics topic

`vstenv report` includes the newest of these directories (report.py).
"""
from __future__ import annotations

import hashlib, json, os, platform, signal, subprocess, sys, time
from pathlib import Path

from . import host, paths, wine

# Every topic: +seh logs each exception with code and address (the crash, and what
# led to it); +loaddll one line per DLL, native or builtin; warn+module why a DLL or
# import failed to load (the usual start-up crash) -- the full +module trace is half
# of a Cubase start's log (a line per PE section and thread), so only the loader
# topic has it; +combase names a WinRT class a program asked for and could not get
# (Cubase's headtracking.dll dies of that), in a few lines; +timestamp,+pid order
# events and tell processes apart (Wine prefixes the thread id itself; there is no
# +tid); err+all is Wine's own errors; fixme-all drops the unimplemented-function
# chatter (a named channel such as +combase keeps its fixmes).
# Never +relay: gigabytes a minute. A 1-2 minute session stays in the tens of MB.
CRASH = ["+timestamp", "+pid", "+seh", "+loaddll", "warn+module", "+combase", "err+all", "fixme-all"]
TOPICS = {
    "crash": [],
    "graphics": ["+d2d", "+dcomp", "+dxgi", "+d3d11"],    # plus DXVK's log (debug_env)
    "windows": ["+x11drv", "+event", "+win"],             # Wine 11 has no focus channel
    "dragdrop": ["+ole", "+xdnd"],
    "loader": ["+module"],                                # how each DLL was searched for and mapped
}
TOPIC_NAMES = [*TOPICS, "all"]

def channels(topic: str) -> str:
    """The topic's channels, then any WINEDEBUG the user set (later entries win in Wine)."""
    extra = [c for cs in TOPICS.values() for c in cs] if topic == "all" else TOPICS[topic]
    return ",".join(CRASH + extra + [c for c in os.environ.get("WINEDEBUG", "").split(",") if c])

def debug_env(topic: str, d: Path) -> dict:
    """The variables a debug run adds to the program's environment."""
    e = {"WINEDEBUG": channels(topic)}
    if topic in ("graphics", "all"): e.update(DXVK_LOG_LEVEL="info", DXVK_LOG_PATH=str(d))
    return e

def debug_dir(name: str, now: float | None = None) -> Path:
    """paths.LOGS/debug-<program>-<YYYYmmdd-HHMMSS>, the name safe for a file."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip("-.") or "program"
    return paths.LOGS / f"debug-{safe}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}"

def _last_write(d: Path) -> float:
    """When anything in d was last written (a running session's wine.log grows
    without touching the directory's own mtime)."""
    return max((f.stat().st_mtime for f in d.iterdir() if f.is_file()), default=d.stat().st_mtime)

def newest_dir() -> Path | None:
    ds = [d for d in paths.LOGS.glob("debug-*") if d.is_dir()] if paths.LOGS.is_dir() else []
    return max(ds, key=lambda d: d.stat().st_mtime, default=None)

AEDEBUG = r"Microsoft\Windows NT\CurrentVersion\AeDebug"
# Wine's crash handler: winedbg attaches automatically and, with its dialog off,
# prints the backtrace to its output, which it inherits from the program: wine.log.
CRASH_HANDLER = {
    rf"HKLM\Software\{AEDEBUG}": {"Debugger": ("REG_SZ", "winedbg --auto %ld %ld"), "Auto": ("REG_SZ", "1")},
    rf"HKLM\Software\Wow6432Node\{AEDEBUG}": {"Debugger": ("REG_SZ", "winedbg --auto %ld %ld"), "Auto": ("REG_SZ", "1")},
    r"HKCU\Software\Wine\WineDbg": {"ShowCrashDialog": ("REG_DWORD", "0")},
}

SAVED = "crash-handler.json"    # in the debug directory while the handler is on

def set_crash_handler(p, d: Path | None = None) -> dict:
    """Turn the crash handler on; returns what was there, for restore_crash_handler.
    Each value is kept as the line `reg export` wrote, so it comes back with its
    own type. With d, that is written to d/crash-handler.json first, so a session
    that dies before restoring is put right by restore_leftover."""
    saved = {}
    for key, vals in CRASH_HANDLER.items():
        old = wine._reg_export_lines(p.reg_export(key))
        saved[key] = {n: ("raw", old[n]) if n in old else (None, "") for n in vals}
    if d is not None: (d / SAVED).write_text(json.dumps({"pid": os.getpid(), "saved": saved}))
    p.reg_import_values(CRASH_HANDLER, name="vstenv-debug-on.reg")
    return saved

def restore_crash_handler(p, saved: dict, d: Path | None = None) -> bool:
    """Put the values back; a value that did not exist before is deleted. False
    (and d/crash-handler.json kept for the next try) when regedit failed."""
    rc = p.reg_import_values({k: {n: tuple(v) for n, v in vals.items()} for k, vals in saved.items()}, name="vstenv-debug-off.reg")
    if rc != 0:
        print(f"warning: could not restore Wine's crash handler settings (regedit exit {rc}); "
              "the next run --debug, doctor or report tries again", file=sys.stderr)
        return False
    if d is not None: (d / SAVED).unlink(missing_ok=True)
    return True

def restore_leftover(p) -> int:
    """Restore the crash handler a debug session left on (vstenv killed, the
    machine off). Newest first, so the oldest, the real originals, end up in
    place. Skips a session still running. Returns how many were restored."""
    n = 0
    files = sorted(paths.LOGS.glob(f"debug-*/{SAVED}"), key=lambda f: f.stat().st_mtime, reverse=True) if paths.LOGS.is_dir() else []
    for f in files:
        try: data = json.loads(f.read_text())
        except (OSError, ValueError): continue
        if data.get("pid") != os.getpid() and _alive(data.get("pid")): continue
        n += restore_crash_handler(p, data.get("saved", {}), f.parent)
    return n

def _alive(pid) -> bool:
    try: os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError): return False
    except PermissionError: pass
    return True

# Runs on the host, where Wine's processes are (the sandbox's /proc shows only the
# sandbox). Finds the program by its name (Wine sets comm to the exe's name, cut
# to 15 characters) in this prefix, samples its threads every 3 s, and stops once
# the program has come and gone. Each sample lists the 12 threads that used the
# most CPU since the one before (a busy loop stands out; a deadlock is all zeros),
# with the thread's total and its scheduling (FIFO:70 is realtime priority 70,
# OTHER:0 normal at nice 0). The stat fields are counted after "(comm) ", which may
# hold spaces: utime, stime (14, 15), nice (19), rt_priority and policy (40, 41)
# are then $12, $13, $17, $38 and $39. It also stops when the process that started
# it is gone (vstenv killed; under Flatpak that is the session helper, which stays)
# or the program has not appeared within 10 minutes (200 rounds).
SAMPLER = r"""pid= prev= waited=0
while kill -0 "$PPID" 2>/dev/null; do
  state=$(sed 's/.*) //' "/proc/$pid/stat" 2>/dev/null | cut -c1)     # gone, or a zombie (Z): ended
  if [ -z "$pid" ] || [ -z "$state" ] || [ "$state" = Z ]; then
    [ -n "$pid" ] && exit 0
    waited=$((waited + 1)); [ "$waited" -gt "${VSTENV_WAIT_ROUNDS:-200}" ] && exit 0
    for p in /proc/[0-9]*; do
      [ "$(cat "$p/comm" 2>/dev/null)" = "$VSTENV_COMM" ] || continue
      tr '\0' '\n' < "$p/environ" 2>/dev/null | grep -qxF -- "WINEPREFIX=$VSTENV_PREFIX" && { pid=${p#/proc/}; break; }
    done
  fi
  if [ -n "$pid" ]; then
    cur=$(for t in /proc/$pid/task/*; do
      echo "${t##*/} $(sed 's/.*) //' "$t/stat" 2>/dev/null | awk '{split("OTHER FIFO RR BATCH ISO IDLE DEADLINE", pol, " ")
        print $12 + $13, pol[$39 + 1] ":" ($39 == 1 || $39 == 2 ? $38 : $17)}') $(tr ' ' _ < "$t/comm" 2>/dev/null) $(cat "$t/wchan" 2>/dev/null)"
    done)
    { echo "### $(date +%T) pid $pid: cpu-ticks-since-last total tid name sched wchan"
      printf '%s\n' "$cur" | PREV="$prev" awk 'BEGIN { n = split(ENVIRON["PREV"], l, "\n"); for (i = 1; i <= n; i++) { split(l[i], f, " "); was[f[1]] = f[2] } }
        NF >= 3 { print $2 - ($1 in was ? was[$1] : 0), $2, $1, $4, $3, $5 }' | sort -rn | head -12; } >> "$VSTENV_OUT"
    prev=$cur
  fi
  sleep 3
done"""

def start_sampler(p, exe: str, out: Path) -> subprocess.Popen:
    env = {"VSTENV_PREFIX": str(p.path), "VSTENV_COMM": exe[:15], "VSTENV_OUT": str(out)}
    return host.popen(["sh", "-c", SAMPLER], env=env, stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _sh(cmd: str) -> str:
    return host.sh(cmd, timeout=20).strip()

def sysinfo(p, topic: str) -> str:
    """What the developer asks first. Every part degrades to '?' on its own."""
    from . import winefixes, dxvk, ntsync
    lines = []
    def add(k, fn):
        try: v = fn()
        except Exception as e: v = f"? ({e.__class__.__name__})"
        lines.append(f"{k:<14}{v if v not in (None, '') else '?'}")
    def distro():
        f = Path("/run/host/os-release") if host.in_flatpak() else Path("/etc/os-release")
        osr = dict(l.split("=", 1) for l in f.read_text().splitlines() if "=" in l)
        return osr.get("PRETTY_NAME", "?").strip('"')
    def gpu():
        nv = Path("/proc/driver/nvidia/version")
        out = [nv.read_text().splitlines()[0]] if nv.exists() else []
        out.append(_sh("command -v vulkaninfo >/dev/null && vulkaninfo --summary 2>/dev/null | grep -E 'deviceName|driverName|driverInfo' | sort -u | sed 's/^ *//'"))
        out.append(_sh("command -v glxinfo >/dev/null && glxinfo -B 2>/dev/null | grep -E 'OpenGL (renderer|version)'"))
        return "\n  " + "\n  ".join(l for x in out for l in x.splitlines() if l) if any(out) else None
    def fixes():
        fs = winefixes.status(p.build)["files"]
        return "none" if not fs else "\n  " + "\n  ".join(
            f"{n}  {hashlib.sha256(winefixes.dest(p.build, n).read_bytes()).hexdigest()}" for n in fs)
    add("topic", lambda: topic)
    add("WINEDEBUG", lambda: channels(topic))
    add("kernel", platform.release)
    add("distribution", distro)
    add("flatpak", lambda: "yes" if host.in_flatpak() else "no")
    add("session", lambda: f"{os.environ.get('XDG_SESSION_TYPE', '?')}, WAYLAND_DISPLAY {'set' if os.environ.get('WAYLAND_DISPLAY') else 'unset'}, "
                           f"DISPLAY {'set' if os.environ.get('DISPLAY') else 'unset'}")
    add("desktop", lambda: os.environ.get("XDG_CURRENT_DESKTOP"))
    add("gpu", gpu)
    add("fsync", lambda: p.wine_env().get("WINEFSYNC"))
    add("ntsync", lambda: ", ".join(f"{k}={v}" for k, v in ntsync.status().items()))
    add("wine", p.build.version)
    add("wine-fixes", fixes)
    add("dxvk", lambda: (lambda s: f"{s['version'] or 'not installed'} (wanted {s['wanted']})")(dxvk.status(p)))
    # wine_env leaves a DXVK_CONFIG the user set to the environment the program inherits
    add("DXVK_CONFIG", lambda: f"{os.environ['DXVK_CONFIG']} (yours)" if "DXVK_CONFIG" in os.environ else p.wine_env().get("DXVK_CONFIG"))
    return "\n".join(lines) + "\n"

def _end_on_signal(sig, _frame):
    raise SystemExit(128 + sig)         # unwinds through run's finally, which restores

def run(p, prog, topic="crash", reporter=None) -> Path:
    """Run `prog` with debug logging until it exits; returns the debug directory."""
    from . import programs
    exe = prog.exe.rsplit("\\", 1)[-1] if prog.exe else ""
    # a second launch hands over to the running copy, which has none of the logging
    if exe and p.is_running(exe): raise RuntimeError(f"{prog.name} is already running: close it, then start the debug run again")
    restore_leftover(p)
    d = debug_dir(prog.name); d.mkdir(parents=True, exist_ok=True)
    (d / "sysinfo.txt").write_text(sysinfo(p, topic))
    old_handlers = {}
    try:                                # a closed terminal (SIGHUP) or a kill (SIGTERM) still restores
        for sig in (signal.SIGTERM, signal.SIGHUP): old_handlers[sig] = signal.signal(sig, _end_on_signal)
    except ValueError: pass             # not the main thread
    sampler = None
    try:
        saved = set_crash_handler(p, d)
        p.debug = (d / "wine.log", debug_env(topic, d))
        try:
            proc = programs.run(p, prog, reporter)
            if exe: sampler = start_sampler(p, exe, d / "threads.log")
            # shortcut: waits for the process started, not for programs it hands off
            # to; a launcher that exits at once ends the debug session early.
            try: proc.wait()
            except KeyboardInterrupt: pass          # the user ends the session; the program runs on
        finally:
            p.debug = None
            if sampler is not None: sampler.terminate()
            restore_crash_handler(p, saved, d)
    finally:
        for sig, h in old_handlers.items(): signal.signal(sig, h)
    return d

def hang_dump(p, now: float | None = None) -> Path:
    """Every thread's stack in every process of the prefix, while it hangs, into
    the newest debug directory (a new one when that is over an hour old, so it
    does not land in last week's run). winedbg attaches, prints and detaches each
    process in turn: the program keeps running."""
    d = newest_dir(); now = time.time() if now is None else now
    if d is None or now - _last_write(d) > 3600: d = debug_dir("hang", now); d.mkdir(parents=True, exist_ok=True)
    out = d / f"backtrace-{time.strftime('%H%M%S', time.localtime(now))}.txt"
    try:
        cp = p.run(["winedbg", "--command", "bt all"], timeout=600); text = (cp.stdout or "") + (cp.stderr or "")
    except subprocess.TimeoutExpired as e:   # what it wrote before stalling is still the answer
        text = "".join(x.decode(errors="replace") if isinstance(x, bytes) else x for x in (e.stdout, e.stderr) if x) + "\n(winedbg timed out)\n"
    out.write_text(text)
    return out
