"""Real-time scheduling for audio: can the host user's threads get it?

Under WineASIO the ASIO callback runs in PipeWire's JACK thread, which asks for
SCHED_FIFO itself; it gets it from the user's rtprio limit, else through RTKit
(lower priority), else not at all. At small buffers a normal-priority callback
misses periods under load: clicks and dropped buffers."""
from __future__ import annotations

from . import host

_PROBE = r"""
echo rtprio=$(ulimit -Hr)
if pgrep -x rtkit-daemon >/dev/null 2>&1; then echo rtkit=1; else echo rtkit=0; fi
"""

FIX = ("give your user real-time rights, then log out and in. Arch: install realtime-privileges and "
       "'usermod -aG realtime $USER'. Fedora: install realtime-setup and 'usermod -aG realtime $USER'. "
       "Debian/Ubuntu: 'usermod -aG audio $USER' with '@audio - rtprio 95' in /etc/security/limits.d/audio.conf")


def status() -> dict:
    """{rtprio, rtkit}: the HOST user's hard real-time priority limit (None:
    unlimited or unknown) and whether RTKit is running."""
    out = {"rtprio": None, "rtkit": False}
    for line in host.sh(_PROBE, timeout=20).splitlines():
        k, _, v = line.partition("=")
        v = v.strip()
        if k == "rtprio": out[k] = int(v) if v.isdigit() else (99 if v == "unlimited" else None)
        elif k == "rtkit": out[k] = v == "1"
    return out


def summary(s: dict) -> tuple[bool, str]:
    n = s.get("rtprio")
    if n is None: return True, "not checked: the limit could not be read"
    if n > 0: return True, f"real-time priority up to {n}"
    if s.get("rtkit"): return False, "no real-time limit; only RTKit's lower priority: small ASIO buffers may click under load"
    return False, "no real-time priority: the audio callback runs at normal priority and small ASIO buffers click"
