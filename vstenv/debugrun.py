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

import hashlib, os, platform, subprocess, time
from pathlib import Path

from . import host, paths

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

def set_crash_handler(p) -> dict:
    """Turn the crash handler on; returns what was there, for restore_crash_handler."""
    saved = {}
    for key, vals in CRASH_HANDLER.items():
        old = p.reg_query(key)
        saved[key] = {n: (kind, old[n]) if n in old else (None, "") for n, (kind, _) in vals.items()}
    p.reg_import_values(CRASH_HANDLER, name="vstenv-debug-on.reg")
    return saved

def restore_crash_handler(p, saved: dict):
    """Put the values back; a value that did not exist before is deleted."""
    p.reg_import_values(saved, name="vstenv-debug-off.reg")

# Runs on the host, where Wine's processes are (the sandbox's /proc shows only the
# sandbox). Finds the program by its name (Wine sets comm to the exe's name, cut
# to 15 characters) in this prefix, samples its threads every 3 s, and stops once
# the program has come and gone. Each sample lists the 12 threads that used the
# most CPU since the one before (a busy loop stands out; a deadlock is all zeros),
# with the thread's total too. The stat fields are counted after "(comm) ", which
# may hold spaces: utime and stime (fields 14 and 15) are then $12 and $13.
SAMPLER = r"""pid= prev=
while :; do
  state=$(sed 's/.*) //' "/proc/$pid/stat" 2>/dev/null | cut -c1)     # gone, or a zombie (Z): ended
  if [ -z "$pid" ] || [ -z "$state" ] || [ "$state" = Z ]; then
    [ -n "$pid" ] && exit 0
    for p in /proc/[0-9]*; do
      [ "$(cat "$p/comm" 2>/dev/null)" = "$VSTENV_COMM" ] || continue
      tr '\0' '\n' < "$p/environ" 2>/dev/null | grep -qxF -- "WINEPREFIX=$VSTENV_PREFIX" && { pid=${p#/proc/}; break; }
    done
  fi
  if [ -n "$pid" ]; then
    cur=$(for t in /proc/$pid/task/*; do
      echo "${t##*/} $(sed 's/.*) //' "$t/stat" 2>/dev/null | awk '{print $12+$13}') $(tr ' ' _ < "$t/comm" 2>/dev/null) $(cat "$t/wchan" 2>/dev/null)"
    done)
    { echo "### $(date +%T) pid $pid: cpu-ticks-since-last total tid name wchan"
      printf '%s\n' "$cur" | PREV="$prev" awk 'BEGIN { n = split(ENVIRON["PREV"], l, "\n"); for (i = 1; i <= n; i++) { split(l[i], f, " "); was[f[1]] = f[2] } }
        NF >= 2 { print $2 - ($1 in was ? was[$1] : 0), $2, $1, $3, $4 }' | sort -rn | head -12; } >> "$VSTENV_OUT"
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

def run(p, prog, topic="crash", reporter=None) -> Path:
    """Run `prog` with debug logging until it exits; returns the debug directory."""
    from . import programs
    d = debug_dir(prog.name); d.mkdir(parents=True, exist_ok=True)
    (d / "sysinfo.txt").write_text(sysinfo(p, topic))
    saved = set_crash_handler(p)
    p.debug = (d / "wine.log", debug_env(topic, d))
    sampler = None
    try:
        proc = programs.run(p, prog, reporter)
        if prog.exe: sampler = start_sampler(p, prog.exe.rsplit("\\", 1)[-1], d / "threads.log")
        # shortcut: waits for the process started, not for programs it hands off
        # to; a launcher that exits at once ends the debug session early.
        try: proc.wait()
        except KeyboardInterrupt: pass          # the user ends the session; the program runs on
    finally:
        p.debug = None
        if sampler is not None: sampler.terminate()
        restore_crash_handler(p, saved)
    return d

def hang_dump(p) -> Path:
    """Every thread's stack in every process of the prefix, while it hangs, into
    the newest debug directory. winedbg attaches, prints and detaches each process
    in turn: the program keeps running."""
    d = newest_dir()
    if d is None: d = debug_dir("hang"); d.mkdir(parents=True, exist_ok=True)
    out = d / f"backtrace-{time.strftime('%H%M%S')}.txt"
    cp = p.run(["winedbg", "--command", "bt all"], timeout=600)
    out.write_text((cp.stdout or "") + (cp.stderr or ""))
    return out
