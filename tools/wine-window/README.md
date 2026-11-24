# wine-window: look at and click Wine windows from a script (KDE Wayland)

`xwd` and `import` cannot read XWayland windows (`X_GetImage BadMatch`), and `spectacle`'s active-window mode captures whatever is active. These helpers find a window by title, activate it, check it really is the active window, and only then capture or click.

- `winshot "<title>" out.png`: screenshot of one window (needs python-xlib, xorg-xwininfo, spectacle).
- `winclick "<title>" X Y [--right] [--double]`, `--key Escape ...`, `--geometry`: XTEST input to one window. X,Y are window-relative; from a `winshot` image subtract (65, 81) on KDE.
- `safemode-ok`, `hub-create-empty`: Cubase 14's Safe Mode OK and the Hub's Create Empty, located from the window size (the two windows come in different sizes; a fixed click once selected "Delete program preferences").
- `cubase-quit`: close Cubase like its X button, so the next start does not show Safe Mode. `cubase-menu-shot`: restart, open a context menu, screenshot it.
- `spectrasonics-open-html`: `.html` handler for Omnisphere's Request Authorization (see docs/cubase-omnisphere-notes.md).

Written while debugging Cubase 14 and Omnisphere 2 under vstenv; paths assume the default vstenv prefix.
