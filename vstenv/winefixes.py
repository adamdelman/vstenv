"""Patched Wine DLLs installed over the pinned Wine build.

The pinned Wine is used as upstream ships it, with a few exceptions: DLLs that
vstenv patches because a vendor's programs need what Wine lacks (the patches
live in patches/wine/, with a README). Today:

- dcomp.dll: DirectComposition. Steinberg's current products draw through it
  (their graphics2d.dll: HALion Sonic 7, and the same library in Cubase and
  Dorico of that generation); Wine's dcomp is stubs and they abort at start.
- advapi32.dll: Credential Manager attributes. Steinberg's License Engine
  stores its sign-in (the refresh token) with a credential attribute; Wine
  dropped attributes, so every restart of the engine found an "old format"
  token, reset it, and the user was "signed out automatically".

CI builds the DLLs from the Wine sources with those patches
(scripts/build-wine-fixes.sh) and attaches wine-fixes-<wine version>.tar.gz to
every release; this module installs them into the Wine build's PE directory,
keeping the originals next to them as .orig. Wine loads a builtin DLL from the
build directory even though the prefix's system32 holds a copy, so nothing in
the prefix needs touching.

The DLLs land in the build, not the prefix, so everything that runs this
Wine gets them: standalone programs and the yabridge plugin hosts alike.
"""
import hashlib, json, os, re, shutil, subprocess, tarfile, tempfile
from datetime import date
from pathlib import Path
from . import paths, wine
from .download import fetch
from .progress import null_reporter

RELEASE_REPO = "adamdelman/vstenv"   # Cubase test build: the patched DLLs come from this fork's release
# The exact tarball this build installs, pinned like wine.WINE_BUILD: a different
# file (a re-uploaded asset, a swapped download) is refused by its sha256 before
# anything is unpacked. Its manifest.json then checks every DLL inside.
WINE_FIXES = {"tag": "v0.1.169-cubase.7",
              "url": f"https://github.com/{RELEASE_REPO}/releases/download/v0.1.169-cubase.7/wine-fixes-11.17.tar.gz",
              "sha256": "ed846eddc762f4ae5920c583e923b6c3644bbc64fdf420efcba68a0c64699749"}
PE_DIR = "lib/wine/x86_64-windows"
MARKER_NAME = "vstenv-wine-fixes.json"
FIXES = {"dcomp.dll": "DirectComposition (Steinberg's VSTGUI: HALion Sonic 7, Cubase, Dorico; JUCE 8 GUIs: Spitfire Audio), "
                     "and hands composition-swapchain content to the host window",
         "dxgi.dll": "DXGI composition swapchains that present into the host window (JUCE 8 apps like the Spitfire app), and follow the app's resizes (TONEX drew pixelated after a resize)",
         "wined3d.dll": "keep-back-buffers swapchains and the composition GL present path (composition swapchains)",
         "win32u.dll": "window and OpenGL plumbing for composition swapchains",
         "d2d1.dll": "Direct2D's copy blend mode (JUCE 8 plugins clear cached images with it; Minimal Audio's readouts smeared when they changed)",
         "dwrite.dll": "font fallback reads an app's text once, as Windows does (SINE Player 1.4.5 crashed at start: JUCE has its text at position 0 only)",
         "sensapi.dll": "\"is the network up?\" answers the way Windows does (KORG Software Pass said \"No Internet Connection\")",
         "ntdll.dll": "a plugin host can hide Wine from JUCE 8.0.13 plugins (Minimal Audio's sign-in panel and interface stayed empty)",
         "user32.dll": "a plugin host can be made DPI-aware on its own (Jamstix 4 will not embed its editor in an unaware host), and an embedded browser stops drawing when its page is hidden (SINE Player's Store page stayed over the Library)",
         "advapi32.dll": "credential attributes (Steinberg's License Engine keeps its sign-in)",
         "ole32.dll": "drag-drop teardown survives a stale target (WebView2 apps crashed at startup)",
         "hidparse.sys": "HID controls with hundreds of usages reach Windows programs (Komplete Kontrol S-series MK1 keyboards)",
         "iphlpapi.dll": "listing the network adapters is cheap (KORG's TRITON lists them thousands of times as it starts: a minute, now seconds)",
         "x86_64-unix/nsiproxy.so": "the adapter list holds only interfaces that exist (it grew with every container or VPN interface since Wine started)",
         "powershell.exe": "answers \"is this program running?\" honestly (electron-builder installers: Softube Central)",
         "i386-windows/powershell.exe": "the same, for 32-bit installers",
         "wineasio.dll": "WineASIO's PE half: an ASIO driver over JACK/PipeWire for low-latency audio (Cubase); off until `vstenv wineasio enable`",
         "x86_64-unix/wineasio.dll.so": "WineASIO's unix half, under the name Wine loads it by",
         "nirescan.exe": "NI controllers plugged in after Wine started are seen (Komplete Kontrol keyboards, Maschine; from ni-wine)",
         "x86_64-unix/winex11.so": "a cursor set from another process shows as an arrow, not nothing (WebView2 windows: Minimal Hub); a dropped file's dialog takes clicks, one taskbar click restores a minimized window, and closing a window ends its open menu first (Cubase 14)",
         "x86_64-unix/win32u.so": "menu icons keep their alpha (32-bit menu bitmaps are alpha-blended); built with FreeType and fontconfig",
         "bin/wineserver": "threads at priority 15 and up (Cubase's audio workers) run SCHED_FIFO 70-80 where RLIMIT_RTPRIO allows (WineASIO at 64 samples dropped periods)",
         "msi.dll": "a feature's cost over its children reads the child features (Cubase 16's Setup.exe crashed in MsiGetFeatureCost, so the Download Assistant's install failed)",
         "windows.globalization.dll": "the Windows.Globalization.Calendar class (Cubase 14-16's head-tracking module died at start without it)",
         "windows.perception.stub.dll": "SpatialLocator.GetDefault says \"no headset\" (Cubase 14-16's head-tracking module died at start)"}
# WinRT classes the patched DLLs add. A prefix only finds a class through its
# registry entry, which Wine writes at a prefix update, not when a DLL is copied.
WINRT_CLASSES = {"windows.globalization.dll": "Windows.Globalization.Calendar",
                 "windows.perception.stub.dll": "Windows.Perception.Spatial.SpatialLocator"}

def dest(build: wine.WineBuild, name: str) -> Path:
    """Where a packaged file goes in the Wine build: a bare name is a 64-bit PE
    module (lib/wine/x86_64-windows); a name with a folder, such as
    i386-windows/powershell.exe, is relative to lib/wine; bin/wineserver to the build's root."""
    if name.startswith("bin/"): return build.root / name
    return build.root / ("lib/wine/" + name if "/" in name else f"{PE_DIR}/{name}")

def wine_version(build=None) -> str:
    """The pinned Wine's version number, from the build's directory name."""
    name = (build.root.name if build is not None else wine.WINE_BUILD["name"])
    m = re.search(r"wine-(\d+(?:\.\d+)*)", name)
    return m.group(1) if m else "unknown"

def asset_name(build=None) -> str: return f"wine-fixes-{wine_version(build)}.tar.gz"

def _sha(f: Path) -> str: return hashlib.sha256(f.read_bytes()).hexdigest()

def marker(build: wine.WineBuild) -> dict | None:
    try:
        f = build.root / MARKER_NAME
        return json.loads(f.read_text()) if f.exists() else None
    except (OSError, ValueError): return None

def installed(build: wine.WineBuild | None) -> dict[str, str]:
    """The patched DLLs actually in place: name -> sha256, per the marker and
    verified against the files (a re-extracted Wine silently puts the stock DLL back)."""
    if build is None: return {}
    m = marker(build) or {}
    out = {}
    for name, sha in (m.get("files") or {}).items():
        f = dest(build, name)
        if f.exists() and _sha(f) == sha: out[name] = sha
    return out

def has(build: wine.WineBuild | None, dll: str) -> bool: return dll in installed(build)

def status(build: wine.WineBuild | None) -> dict:
    inst = installed(build)
    # only what the installed tarball carries: an older release without bin/wineserver is complete, not missing it for good
    offered = set((marker(build) or {}).get("offered") or FIXES)
    missing = [d for d in FIXES if d in offered and d not in inst]
    return {"installed": not missing, "files": sorted(inst), "missing": missing, "asset": asset_name(build), "marker": marker(build) if build else None}

def _locate(build, r) -> tuple[Path | None, str, str]:
    """Where the fixes come from: VSTENV_WINE_FIXES (a local tarball, for builds made
    by hand), else the pinned release asset (WINE_FIXES), not yet fetched.
    Returns (local path or None, label, tag)."""
    override = os.environ.get("VSTENV_WINE_FIXES")
    if override:
        f = Path(override).expanduser()
        if f.is_file(): return f, f"{f.name} (VSTENV_WINE_FIXES)", "local"
        r.log(f"VSTENV_WINE_FIXES={override} does not exist; ignoring")
    return None, f"{asset_name(build)} ({RELEASE_REPO} release {WINE_FIXES['tag']})", WINE_FIXES["tag"]

def _runs(exe: Path) -> bool:
    """The program starts and answers --version (the rebuilt wineserver is linked against this system's libraries)."""
    try: return subprocess.run([str(exe), "--version"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError): return False

def install(build: wine.WineBuild, reporter=None, force=False) -> dict[str, str]:
    """Put the pinned release's patched DLLs into the Wine build. Idempotent: nothing is
    touched when the installed set came from the pinned tag (or, offline, when
    every file is in place). Returns what is installed."""
    r = null_reporter(reporter)
    r.step("Wine fixes (patched DLLs)")
    tgz, label, tag = _locate(build, r)
    in_place = not status(build)["missing"]
    current = (marker(build) or {}).get("release")
    if not force and in_place and tag == current:
        r.skip(f"in place: {', '.join(sorted(installed(build)))}" + (f" ({current})" if current else "")); return installed(build)
    if tgz is None:   # cached per tag: the same asset name carries a different build on every release
        if not WINE_FIXES["url"].endswith("/" + asset_name(build)):
            r.fail(f"the pinned Wine fixes are not for this Wine ({asset_name(build)}); Steinberg's DirectComposition programs stay unavailable"); return installed(build)
        try: tgz = fetch(WINE_FIXES["url"], paths.DOWNLOADS / f"{tag}-{asset_name(build)}", sha256=WINE_FIXES["sha256"], reporter=r, label="wine fixes")
        except Exception as e:   # offline, or the file is not the pinned one: keep what is in place
            r.fail(f"could not get {label}: {str(e)[:80]}" + ("; keeping the installed set" if in_place else "")); return installed(build)
            return installed(build)
    with tempfile.TemporaryDirectory(prefix="wine-fixes-") as tmp:
        with tarfile.open(tgz) as t: t.extractall(tmp, filter="tar")
        src = next((d for d in Path(tmp).rglob("manifest.json")), None)
        if src is None: r.fail(f"{tgz.name} has no manifest.json"); return installed(build)
        man = json.loads(src.read_text()); src = src.parent
        if man.get("wine_version") != wine_version(build):
            r.fail(f"{tgz.name} is for Wine {man.get('wine_version')}, this build is {wine_version(build)}"); return installed(build)
        done = {}
        try:
            for name, sha in man.get("files", {}).items():
                f = src / name
                if not f.exists() or _sha(f) != sha: r.log(f"{name}: missing or hash mismatch in {tgz.name}; skipped"); continue
                dst = dest(build, name); dst.parent.mkdir(parents=True, exist_ok=True)
                orig = dst.with_suffix(dst.suffix + ".orig")
                if dst.exists() and not orig.exists() and _sha(dst) != sha: shutil.copy2(dst, orig)
                if dst.exists() and _sha(dst) == sha: done[name] = sha; continue
                tmpf = dst.with_name(f".{dst.name}.new")
                shutil.copy2(f, tmpf); tmpf.replace(dst)      # rename: a running program keeps its old mapping
                if name.startswith("bin/") and not _runs(dst):   # a server that does not start breaks every program
                    r.log(f"{name} from {tgz.name} does not run here; the stock one is back")
                    if orig.exists(): shutil.copy2(orig, tmpf); tmpf.replace(dst)
                    continue
                done[name] = sha
        finally:   # record what did land even if a later copy failed (disk full): status() must not deny a DLL that is in place
            m = marker(build) or {}
            files = dict(m.get("files") or {}); files.update(done)
            (build.root / MARKER_NAME).write_text(json.dumps({"wine_version": wine_version(build), "files": files, "source": label, "release": tag, "offered": sorted(man.get("files", {})),
                                                               "patches": man.get("patches", []), "installed": date.today().isoformat()}, indent=2))
    (r.ok if done else r.fail)(", ".join(f"{n} ({FIXES.get(n, 'patched')})" for n in done) if done else "nothing usable in the tarball")
    return installed(build)

def remove(build: wine.WineBuild, reporter=None) -> list[str]:
    """Put the stock DLLs back from their .orig copies; a fix with no stock original is an addition (WineASIO) and is deleted."""
    r = null_reporter(reporter); r.step("Restoring stock Wine DLLs")
    back = []
    for name in list((marker(build) or {}).get("files") or {}):
        dst = dest(build, name); orig = dst.with_suffix(dst.suffix + ".orig")
        if orig.exists(): orig.replace(dst); back.append(name)
        elif dst.exists(): dst.unlink(); back.append(name)
    (build.root / MARKER_NAME).unlink(missing_ok=True)
    r.ok(", ".join(back) if back else "nothing to restore")
    return back

def register_classes(p: wine.Prefix, reporter=None) -> bool:
    """Run a prefix update when a patched DLL's WinRT class is not registered yet
    (RoGetActivationFactory fails with "class not registered" until then). Returns True if it ran."""
    r = null_reporter(reporter); r.step("WinRT classes of the patched DLLs")
    def missing(): return [c for dll, c in WINRT_CLASSES.items() if has(p.build, dll)
                           and not p.reg_query(rf"HKLM\Software\Microsoft\WindowsRuntime\ActivatableClassId\{c}")]
    before = missing()
    if not before: r.skip(); return False
    (p.path / ".update-timestamp").unlink(missing_ok=True)
    cp = p.run(["wineboot", "-u"], timeout=900)
    # wineboot skips a DLL a running program has in use, and says so only in its exit code
    left = missing()
    if left or cp.returncode != 0:
        r.fail(f"still not registered: {', '.join(left or before)}" + (f" (wineboot -u exited {cp.returncode})" if cp.returncode else "")
               + "; close every Windows program and run setup again")
    else: r.ok("registered " + ", ".join(before))
    return True
