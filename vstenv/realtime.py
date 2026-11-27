"""Real-time scheduling for audio: can the host user's threads get it?

Under WineASIO the ASIO callback runs in PipeWire's JACK thread, which asks for
SCHED_FIFO itself; it gets it from the user's rtprio limit, else through RTKit
(lower priority), else not at all. At small buffers a normal-priority callback
misses periods under load: clicks and dropped buffers.

The fix is the same as ntsync's (ntsync.py): one pkexec prompt on the host
writes a pam_limits file for this user alone, which works on Arch, Fedora and
Debian/Ubuntu without their distro-specific groups. pam_limits reads it at
login, so the limit applies after the person logs out and back in."""
from __future__ import annotations

import re

from . import host, paths

CONF = "/etc/security/limits.d/95-vstenv-realtime.conf"
DECLINED = paths.DATA / "realtime-declined"
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")

# Run as root on the host; $1 is the user, already checked against USER_RE.
# Writing the whole file each time makes it idempotent.
ENABLE_SCRIPT = f"""set -e
case "$1" in ''|[!a-z_]*|*[!a-z0-9_-]*) echo "bad user name" >&2; exit 2;; esac
printf '%s - rtprio 95\\n%s - memlock unlimited\\n' "$1" "$1" > {CONF}
chmod 0644 {CONF}
"""

_PROBE = rf"""
echo rtprio=$(ulimit -Hr)
if pgrep -x rtkit-daemon >/dev/null 2>&1; then echo rtkit=1; else echo rtkit=0; fi
echo user=$(id -un)
if [ -e {CONF} ]; then echo conf=1; else echo conf=0; fi
if command -v pkexec >/dev/null 2>&1; then echo pkexec=1; else echo pkexec=0; fi
"""

RELOGIN = "Log out and back in so the new limit applies"


def status() -> dict:
    """{rtprio, rtkit, user, conf, pkexec}: the HOST user's hard real-time
    priority limit (None: unlimited or unknown), whether RTKit is running, the
    host user name, whether our limits file exists, and whether pkexec does."""
    out = {"rtprio": None, "rtkit": False, "user": "", "conf": False, "pkexec": False}
    for line in host.sh(_PROBE, timeout=20).splitlines():
        k, _, v = line.partition("=")
        v = v.strip()
        if k == "rtprio": out[k] = int(v) if v.isdigit() else (99 if v == "unlimited" else None)
        elif k == "user": out[k] = v
        elif k in out: out[k] = v == "1"
    return out


def can_enable(s: dict) -> bool:
    """No real-time limit yet and our file is not written (written: only a re-login is missing)."""
    return s.get("rtprio") == 0 and not s.get("conf")


def fix(s: dict) -> str:
    if can_enable(s): return "vstenv realtime enable (asks for your password once), or Health → Real-time audio"
    if s.get("rtprio") == 0 and s.get("conf"): return RELOGIN.lower()
    return ""


def enable() -> tuple[bool, str]:
    """Write the limits file for the host user, through the host's password prompt."""
    s = status()
    if s["rtprio"]: return True, f"already on: real-time priority up to {s['rtprio']}"
    if s["conf"]: return True, f"already set up. {RELOGIN}."
    user = s["user"]
    if not USER_RE.match(user): return False, f"cannot set it up for the user name {user!r}"
    if not s["pkexec"]:
        return False, f"no pkexec on this system; as root, put '{user} - rtprio 95' and '{user} - memlock unlimited' in {CONF}"
    try:
        cp = host.run(["pkexec", "sh", "-c", ENABLE_SCRIPT, "sh", user], capture_output=True, text=True, timeout=300)
    except Exception as e:
        return False, f"could not ask for the password: {e}"
    if cp.returncode in (126, 127):
        return False, "not changed: the password prompt was cancelled or refused"
    if cp.returncode != 0:
        return False, (cp.stderr.strip().splitlines() or [f"failed (exit {cp.returncode})"])[-1][:160]
    if not status()["conf"]: return False, f"{CONF} was not written"
    return True, f"done. {RELOGIN}: until then audio keeps normal priority."


def should_offer() -> bool:
    """Ask once: no real-time rights, not set up, and the person has not said no."""
    if DECLINED.exists(): return False
    try: return can_enable(status())
    except Exception: return False


def decline():
    DECLINED.parent.mkdir(parents=True, exist_ok=True); DECLINED.write_text("asked once; the person chose not to\n")


def summary(s: dict) -> tuple[bool, str]:
    n = s.get("rtprio")
    if n == 0 and s.get("conf"): return False, f"set up, not active yet. {RELOGIN}"
    if n is None: return True, "not checked: the limit could not be read"
    if n > 0: return True, f"real-time priority up to {n}"
    if s.get("rtkit"): return False, "no real-time limit; only RTKit's lower priority: small ASIO buffers may click under load"
    return False, "no real-time priority: the audio callback runs at normal priority and small ASIO buffers click"
