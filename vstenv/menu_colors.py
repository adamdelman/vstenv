"""Plasma's window colours for Wine's native menu bars (opt-in: recipe setting plasma_menu_colors).

Wine draws a program's native menu bar (Cubase's File/Edit/...) from HKCU\\Control Panel\\Colors
Menu, MenuBar and MenuText, white by default, so the bar stays white on a dark Plasma theme.
With the setting on, Menu and MenuBar take [Colors:Window] BackgroundNormal of kdeglobals and
MenuText its ForegroundNormal. Nothing is written when the file or a key is missing. The last
applied colours are kept in the prefix (drive_c/vstenv-menu-colors.json) so a launch costs no Wine
run when they have not changed.
"""
import json, os
from pathlib import Path
from .wine import Prefix

KEY = r"HKCU\Control Panel\Colors"
STATE = "vstenv-menu-colors.json"

def _rgb(s) -> str | None:
    """'61,174,233' -> '61 174 233' (the registry's form); None when it is not three 0-255 numbers."""
    try: parts = [int(x) for x in str(s).split(",")]
    except ValueError: return None
    return " ".join(map(str, parts)) if len(parts) == 3 and all(0 <= x <= 255 for x in parts) else None

def plasma_colors(path: Path | None = None) -> dict[str, str] | None:
    """{value name: 'r g b'} from kdeglobals, or None when the file or either key is missing or malformed."""
    path = path or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kdeglobals"
    try: lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError: return None
    sec, got = None, {}
    for line in lines:
        line = line.strip()
        if line.startswith("["): sec = line
        elif sec == "[Colors:Window]" and "=" in line:
            k, v = line.split("=", 1); got[k.strip()] = v.strip()
    bg, fg = _rgb(got.get("BackgroundNormal")), _rgb(got.get("ForegroundNormal"))
    return {"Menu": bg, "MenuBar": bg, "MenuText": fg} if bg and fg else None

def apply(p: Prefix, kdeglobals: Path | None = None) -> bool:
    """Write the colours when they differ from the last applied ones. True when Wine was run."""
    want = plasma_colors(kdeglobals)
    if not want: return False
    state = p.drive_c / STATE
    try: have = json.loads(state.read_text())
    except (OSError, ValueError): have = None
    if have == want: return False
    for k, v in want.items():
        cp = p.run(["reg", "add", KEY, "/v", k, "/t", "REG_SZ", "/d", v, "/f"], timeout=60)
        if cp.returncode != 0: return False          # not recorded, so the next start tries again
    state.write_text(json.dumps(want)); return True
