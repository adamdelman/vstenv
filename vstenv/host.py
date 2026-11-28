"""Run Wine on the host from inside the Flatpak sandbox.

wineserver addresses its clients by pid (tgkill, ptrace, process_vm_readv).
Every `flatpak run` is its own pid namespace and the host is another, so a
wineserver in one namespace cannot serve a client in another: Electron's
renderer dies at once, Chromium's process broker fails, the server can spin
forever. DAWs load NI plugins on the host, so the prefix's wineserver has to
live on the host, and therefore so does every Wine process this app starts.

flatpak-spawn --host asks the Flatpak session helper to run a command in the
host session (permission --talk-name=org.freedesktop.Flatpak). The binary is
still the app's own pinned Wine build under the app's data directory, never
the distribution's wine. Outside Flatpak commands run directly.
"""
import os, shutil, subprocess
from pathlib import Path

FLATPAK_INFO = Path("/.flatpak-info")
# Variables that must never reach a Wine program. VS Code exports the first in its
# terminals; an Electron app started with it runs as plain Node and exits at once.
UNSET = ("ELECTRON_RUN_AS_NODE",)
# The sandbox's display and audio routing, passed on so host Wine opens its
# windows and connects its audio where vstenv itself was pointed (e.g.
# `flatpak run --env=DISPLAY=:77`), not where the host session points.
FORWARD = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "PULSE_SINK", "PULSE_SOURCE",
           "PULSE_SERVER", "PIPEWIRE_REMOTE", "PIPEWIRE_NODE", "PIPEWIRE_LATENCY",
           "JACK_DEFAULT_SERVER")
FORWARD_PREFIXES = ("WINEASIO_",)
# A display the sandbox does not have must not come from the host session either.
UNSET_IF_ABSENT = ("DISPLAY", "WAYLAND_DISPLAY")

def in_flatpak() -> bool:
    return FLATPAK_INFO.exists()

def wrap(cmd: list[str], env: dict | None = None, cwd: str | os.PathLike | None = None) -> list[str]:
    """The argv that runs `cmd` on the host with `env` added to the host session
    environment (unchanged outside Flatpak; the caller merges env then)."""
    if not in_flatpak(): return list(cmd)
    unset = [*UNSET, *(v for v in UNSET_IF_ABSENT if not os.environ.get(v))]
    out = ["flatpak-spawn", "--host", *(f"--unset-env={v}" for v in unset)]
    if cwd: out.append(f"--directory={cwd}")
    out += [f"--env={k}={v}" for k, v in {**sandbox_env(), **(env or {})}.items()]
    return out + ["--", *cmd]

def _sandbox_only(value: str) -> bool:
    """A path that exists only inside the sandbox (Flatpak's Xauthority copy,
    its PulseAudio socket proxy): meaningless to a host process."""
    return "/run/flatpak/" in value or value.startswith("/.flatpak")

def sandbox_env() -> dict:
    """The FORWARD variables set in this (sandboxed) process that mean the same
    on the host."""
    return {k: v for k, v in os.environ.items()
            if (k in FORWARD or k.startswith(FORWARD_PREFIXES)) and v and not _sandbox_only(v)}

def _kw(env: dict | None, cwd, kw: dict) -> dict:
    if in_flatpak():
        kw.pop("cwd", None)             # passed as --directory
    else:
        merged = {k: v for k, v in os.environ.items() if k not in UNSET}
        if env: merged.update(env)
        kw["env"] = merged
        if cwd: kw["cwd"] = cwd
    return kw

def run(cmd: list[str], env: dict | None = None, cwd=None, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(wrap(cmd, env, cwd), **_kw(env, cwd, kw))

def popen(cmd: list[str], env: dict | None = None, cwd=None, **kw) -> subprocess.Popen:
    return subprocess.Popen(wrap(cmd, env, cwd), **_kw(env, cwd, kw))

def sh(script: str, env: dict | None = None, timeout=60) -> str:
    """Run a POSIX sh script on the host (for /proc scans: the sandbox's /proc
    only shows the sandbox). Returns stdout."""
    try:
        return run(["sh", "-c", script], env=env, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""

def available() -> tuple[bool, str]:
    """Can we reach the host? Outside Flatpak always; inside, only with the
    org.freedesktop.Flatpak portal permission."""
    if not in_flatpak(): return True, "not sandboxed: Wine runs directly"
    if not shutil.which("flatpak-spawn"): return False, "flatpak-spawn not found in the sandbox"
    try:
        cp = subprocess.run(["flatpak-spawn", "--host", "--", "true"], capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"flatpak-spawn --host failed: {e}"
    if cp.returncode != 0:
        return False, "the sandbox may not run commands on the host (needs --talk-name=org.freedesktop.Flatpak): " + (cp.stderr.strip().splitlines() or ["?"])[-1]
    return True, "Wine processes run on the host (flatpak-spawn --host), in the same pid namespace as DAW plugins"


def which(name: str) -> str | None:
    """Path of a program on the HOST (the sandbox's PATH says nothing about the
    desktop the user actually runs)."""
    if not in_flatpak():
        import shutil
        return shutil.which(name)
    out = sh(f"command -v {name}", timeout=20).strip()
    return out or None

def desktop_tool(cmd: list[str], timeout=30) -> subprocess.CompletedProcess | None:
    """Run a desktop-database tool (xdg-mime, update-desktop-database) on the
    host, where the menu and mime databases it edits live. None when the tool
    is missing there or fails to start; nothing else is worth failing over."""
    if which(cmd[0]) is None: return None
    try: return run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError): return None

def notify(summary: str, body: str = "") -> None:
    """A desktop notification, shown by the host's notification daemon. Best
    effort: background work (e.g. an install finished behind a vendor app's
    back) should be announced, never fail over a missing notify-send."""
    from . import APP_ID, APP_NAME
    desktop_tool(["notify-send", "--app-name", APP_NAME, "--icon", APP_ID,
                  summary, *([body] if body else [])], timeout=15)
