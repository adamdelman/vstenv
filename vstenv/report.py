"""A diagnostic bundle a user can send when something goes wrong.

Support by conversation is slow: the questions that matter (which wine, which
manager version, which prefix, what the installer did before it stopped) take
several round trips to ask. This collects them once into a single file.

What it deliberately does NOT collect: serial numbers, licence tokens, the
user's own file names, or anything from outside the prefix and this app's own
logs. Paths under $HOME are shortened to ~ so a username does not travel with
the report either.
"""
from __future__ import annotations

import getpass, io, json, os, platform, re, socket, subprocess, tarfile, time
from pathlib import Path

from . import APP_NAME, paths, wine, host

REDACT = [
    (re.compile(r"\b[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}\b"), "<serial>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "<token>"),
]

def scrub(text: str) -> str:
    """Remove the identifying things vendors' software writes into its logs."""
    home = Path.home()
    text = text.replace(str(home), "~")
    # Wine's own forms of it: Z:\home\<user>, \??\unix\home\<user> and any other
    # separator form, and the prefix's C:\users\<user> (the login name, which
    # may differ from the home directory's), with backslashes doubled where a
    # debug channel escapes them.
    sep = r"[\\/]+"; end = r"(?![\w.-])"
    homes = sep + sep.join(map(re.escape, home.parts[1:])) + end
    text = re.sub(r"(?i)z:" + homes, "~", text)
    text = re.sub(r"(?i)" + homes, "~", text)
    for user in {home.name, _login()} - {""}:
        text = re.sub(r"(?i)(" + sep + r"users" + sep + r")" + re.escape(user) + end, r"\1<user>", text)
    # The host and login names as words anywhere (an installer's "started by
    # <user>", a log line naming the machine); longest first, so a host name
    # holding the login name goes whole. Names under 3 letters would mangle text.
    host = _hostname()
    names = {n: "<host>" for n in (host, host.split(".")[0]) if n.lower() not in ("localhost", "localhost.localdomain")}
    names.update((n, "<user>") for n in (home.name, _login()))
    for n in sorted(names, key=len, reverse=True):
        if len(n) >= 3: text = re.sub(r"(?i)(?<![\w<])" + re.escape(n) + r"(?![\w>])", names[n], text)
    for rx, sub in REDACT: text = rx.sub(sub, text)
    return text

def _hostname() -> str:
    try: return socket.gethostname()
    except Exception: return ""

def _login() -> str:
    try: return getpass.getuser()
    except Exception: return ""

def summary(p: wine.Prefix | None = None) -> str:
    """The human-readable part: what this machine is and what state the app is in."""
    from . import yabridge, prefixes, doctor, dxvk, vendors, __version__
    out = io.StringIO(); w = out.write
    w(f"{APP_NAME} {__version__} report — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    w("=" * 60 + "\n\n")
    w("## Machine\n")
    w(f"python        {platform.python_version()}\n")
    w(f"kernel        {platform.release()}\n")
    try:
        osr = dict(l.split("=", 1) for l in Path("/etc/os-release").read_text(errors="replace").splitlines() if "=" in l)
        w(f"distribution  {osr.get('PRETTY_NAME', '?').strip('\"')}\n")
    except OSError: pass
    w(f"flatpak       {'yes' if host.in_flatpak() else 'no'}\n")
    ok, detail = host.available()
    w(f"host access   {'yes' if ok else 'no'} ({detail})\n")
    w(f"session       {os.environ.get('XDG_SESSION_TYPE', '?')} / {os.environ.get('XDG_CURRENT_DESKTOP', '?')}\n")
    w(f"vulkan        {dxvk.vulkan_ok()[1]}\n\n")

    b = wine.installed_build()
    if p is None and b is not None: p = wine.Prefix(paths.PREFIX, b)
    w("## Wine and prefix\n")
    build = p.build if p is not None and getattr(p, "build", None) is not None else b
    ver = None
    if build is not None:
        try: ver = build.version()
        except Exception: ver = None
    w(f"wine build    {ver or (str(build.root).replace(str(Path.home()), '~') if build else 'not provisioned')}\n")
    if p is not None:
        w(f"prefix        {str(p.path).replace(str(Path.home()), '~')} (exists: {p.exists})\n")
        others = prefixes.other_prefixes(p) if p.exists else []
        if others: w(f"other prefixes {', '.join(prefixes.short(o) for o in others)}\n")
    w("\n")

    if p is not None and p.exists:
        for v in vendors.all():
            w(f"## {v.name}\n")
            try:
                for k, val in v.status(p).items(): w(f"{k:<22} {val}\n")
                prods = v.products(p)
                if prods:
                    w("products:\n")
                    for x in prods[:60]: w(f"  {x.name}  [{x.kind}]  {'registered' if x.registered else 'NOT registered'}\n")
                st = v.staged_installs(p)
                if st:
                    w("interrupted installs:\n")
                    for x in st:
                        size = sum(f.stat().st_size for f in x.dir.rglob("*") if f.is_file()) // (1024 * 1024)
                        w(f"  {x.name}: {size} MB staged in {x.dir.name}, download {'kept' if x.download else 'gone'}\n")
            except Exception as e: w(f"(failed: {e})\n")
            w("\n")
        w("## yabridge\n")
        try:
            w(f"installed     {yabridge.installed()}\n")
            w(f"compatible    {yabridge.compatibility()}\n")
            broken = yabridge.broken_bundles()
            w(f"broken links  {[b.name for b in broken] if broken else 'none'}\n")
        except Exception as e: w(f"(failed: {e})\n")
        w("\n## DXVK\n")
        try: w(json.dumps(dxvk.status(p), indent=2) + "\n")
        except Exception as e: w(f"(failed: {e})\n")
        w("\n## Running in this prefix\n")
        try:
            procs = p.processes()
            if not procs: w("  nothing\n")
            for pid, cmd in procs[:30]: w(f"  {pid}  {cmd[:110]}\n")
        except Exception as e: w(f"(failed: {e})\n")

    w("\n## Health checks\n")
    try:
        for c in doctor.run(p):
            w(f"  [{'ok ' if c.ok else 'FAIL'}] {c.name}{(': ' + c.detail) if c.detail else ''}\n")
            if not c.ok and c.fix: w(f"         fix: {c.fix}\n")
    except Exception as e: w(f"(failed: {e})\n")
    return scrub(out.getvalue())

DEBUG_TAIL = 20_000_000       # how much of each file of the newest `run --debug` directory
CRASH_DUMPS = "Documents/Steinberg/CrashDumps"      # Cubase's minidumps, under the prefix's user
DUMP_MAX = 20_000_000

def tail(f: Path, n: int) -> str:
    """The last n characters of a log, scrubbed. Reads only the end of a large
    file; the margin lets a serial cut at the boundary still be recognized."""
    with open(f, "rb") as fh:
        fh.seek(max(0, fh.seek(0, 2) - n - 4096))
        return scrub(fh.read().decode(errors="replace"))[-n:]

def write_bundle(dest: Path | None = None, p: wine.Prefix | None = None, crash_dump=False) -> Path:
    """Write summary + logs to a .tar.gz the user can attach to a bug report.
    crash_dump adds Cubase's newest minidump, which cannot be scrubbed: the
    user asks for it explicitly."""
    from . import vendors, debugrun
    paths.ensure_dirs()
    if p is not None and p.exists:
        try: debugrun.restore_leftover(p)
        except Exception: pass
    dest = Path(dest) if dest else Path.home() / f"{APP_NAME}-report-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    text = summary(p)
    def add(t, name, body: str | bytes):
        b = body.encode() if isinstance(body, str) else body; i = tarfile.TarInfo(name); i.size = len(b); i.mtime = int(time.time()); t.addfile(i, io.BytesIO(b))
    with tarfile.open(dest, "w:gz") as t:
        add(t, "report/summary.txt", text)
        for log in sorted(paths.LOGS.glob("*")) if paths.LOGS.is_dir() else []:     # our logs, scrubbed; tail only
            if not log.is_file(): continue
            try: add(t, f"report/logs/{log.name}", tail(log, 400_000))
            except OSError: continue
        if (d := debugrun.newest_dir()) is not None:                               # the last `run --debug`
            for f in sorted(d.iterdir()):
                if f.is_file():
                    try: add(t, f"report/{d.name}/{f.name}", tail(f, DEBUG_TAIL))
                    except OSError: pass
        if p is not None and p.exists:                                             # the vendors' own logs
            for v in vendors.all():
                for name, f in v.logs(p).items():
                    if f.is_file():
                        try: add(t, f"report/logs/{v.id}-{name}", tail(f, 400_000))
                        except OSError: pass
            try: dumps = sorted((f for f in (p.user_dir / CRASH_DUMPS).glob("*.dmp") if f.is_file()), key=lambda f: f.stat().st_mtime)
            except OSError: dumps = []
            if dumps:                          # a minidump is binary memory: only on request, the newest only
                add(t, "report/crashdumps.txt", "".join(f"{f.stat().st_size:>12}  {f.name}\n" for f in dumps))
                if crash_dump and 0 < dumps[-1].stat().st_size < DUMP_MAX: add(t, f"report/crashdumps/{dumps[-1].name}", dumps[-1].read_bytes())
    return dest
