# Cubase 14 and Omnisphere on Arch Linux with vstenv (Wine)

Running log of every fix needed that is not code, so others can reproduce.
Prefix: `~/.local/share/vstenv/prefix`. Wine: `~/.local/share/vstenv/wine/wine-11.17-staging-amd64-wow64/bin/wine`.
All commands below assume:

```sh
export WINEPREFIX=~/.local/share/vstenv/prefix
WINE=~/.local/share/vstenv/wine/wine-11.17-staging-amd64-wow64/bin/wine
```

Tip: to debug, run the app from a terminal with `WINEDEBUG=err+all` and read the output.
`WINEDEBUG=-all` hides everything. Use `winedbg --command continue 'C:\full\path\app.exe'` for a crash backtrace.
Use `pkill -x`, not `pkill -f`, in scripts: `-f` can match your own shell.

## Cubase 14

Installer path: Steinberg Download Assistant, then `Cubase14.msi`, installed into the vstenv prefix.

### Crash at startup (fast-fail `c0000409`)

- Symptom: launch window opens, then the process dies. Log: `err:seh:NtRaiseException Unhandled exception code c0000409`.
- Cause: `Components\headtracking.dll` throws a C++ exception (HRESULT `0x80040154`, class not registered) and nothing catches it. Found with `winedbg`: backtrace was inside `headtracking`.
- Fix: head tracking is optional, so move the DLL aside:

```sh
C="$WINEPREFIX/drive_c/Program Files/Steinberg/Cubase 14/Components"
mv "$C/headtracking.dll" "$C/headtracking.dll.disabled"
```

- Result: Cubase starts and shows its main window. Harmless log noise: `A DeckLink discovery instance could not be created`, Mesa `dri2 screen` warnings.
- Reverse it by renaming the file back. A Cubase update may restore the DLL.
- Status: some UI issues remain (not yet described in detail).

## Spectrasonics Download Manager (for Omnisphere)

Program: `Spectrasonics Download Manager.exe`, a JUCE app (uses `Win32NativeFileChooser`).

### White window with no contents

- Log showed thousands of `DxgiFactory::CreateSwapChainForComposition: Not implemented`: the prefix's DXVK `dxgi` lacks it.
- Fix: per-app override to Wine's builtin D3D DLLs (same as the Steinberg apps):

```sh
K='HKCU\Software\Wine\AppDefaults\Spectrasonics Download Manager.exe\DllOverrides'
for d in d3d10core d3d11 d3d9 dxgi; do $WINE reg add "$K" /v $d /d builtin /f; done
```

- Result: window contents appear.

### Blank buttons and no text anywhere

- Symptom: after the builtin-D3D fix the banner image and layout draw, but every label, button caption and text field is empty.
- Tried, did not work:
  - `d2d1=disabled`: the exe imports `d2d1.dll` (and `dcomp.dll` needs it), so the app does not start (`c0000135`). Do not do this.
  - wined3d Vulkan renderer (`HKCU\Software\Wine\AppDefaults\<exe>\Direct3D` `renderer=vulkan`): the app aborts with
    `Assertion failed! dlls/wined3d/cs.c Line: 3289 Expression: flags & WINED3D_MAP_NOOVERWRITE`. Remove it:
    `$WINE reg delete "HKCU\Software\Wine\AppDefaults\Spectrasonics Download Manager.exe\Direct3D" /v renderer /f`
- Not an error: `class {aa509086-5ca9-4c25-8f95-589d3c07b48a} not registered` is the Virtual Desktop Manager that JUCE asks for. Harmless.
- Open. Ideas left: other wined3d settings, a different Wine build.

### Stuck windows that cannot be closed

- Stop every process in the prefix: `$WINE`'s sibling `wineserver -k` (same `bin` folder). It also closes Cubase and MediaBay.

### Cannot select a download folder

- Under investigation. Log shows an unregistered COM class `{aa509086-5ca9-4c25-8f95-589d3c07b48a}` (22 times) around the dialog.

### Root causes found (Opus consult)

- **"Download error" popup = not enough disk space, not the network.** The app writes its own log:
  `$WINEPREFIX/drive_c/users/<user>/AppData/Local/Temp/SpectrasonicsDownloadLog.txt`. Read its last lines first.
  Ours said `63.30 GB needed / 37.14 GB available on download drive` and `26.2 GB needed on system drive`.
  HTTPS to `dlm.spectrasonics.net` works (the app bundles OpenSSL and its own CA list; Wine's cert store is not involved).
  Fix: pick a download folder on a big disk, e.g. `Z:\mnt\windows_drive_d\...` (Z: is `/`), and keep about 26 GB free on `C:` (the prefix's drive, under `$HOME`).
  Optional drive letter: `ln -s /mnt/windows_drive_d $WINEPREFIX/dosdevices/d:`.
- **Missing text = missing font.** JUCE 8 detects Wine (`wine_get_version`) and switches its default fonts to
  "Bitstream Vera Sans / Serif / Sans Mono". If they are not installed, all default-font text draws as nothing while shapes and images still draw.
  Fix: `sudo pacman -S ttf-bitstream-vera`, then restart the app. If text is still missing, copy the fonts into the prefix:
  `cp /usr/share/fonts/TTF/Vera*.ttf $WINEPREFIX/drive_c/windows/Fonts/`.
  Related: Wine bug 57833 (JUCE 8 incompatibility), JUCE forum thread "Juce8 Direct2D WINE/yabridge".

## Cubase: dragging a file from the file manager freezes it

- Symptom: drop an mp3 on an audio track. The "Import Options" dialog opens from inside the drop call, cannot be clicked, and Cubase freezes (it looks like a crash).
- Cause (probable, not traced): the drop handler shows a modal dialog before it returns, so the XDND drag session of the Wayland/XWayland file manager never finishes and keeps the pointer grab.
- Workaround: do not drag from the file manager. Use File > Import > Audio File (Ctrl+I), or drag inside Cubase (MediaBay).
- Unfreeze: kill Cubase only: `pkill -9 -x Cubase14.exe`. `wineserver -k` would also kill every other Wine app in the prefix (e.g. a running download).

## Cubase: menus lag

- Run without debug logging: `WINEDEBUG=-all`. `err+all` / default fixme output printed thousands of `dxgi_surface_GetDC` lines and slowed the UI.
- Startup plugin scan uses 150%+ CPU for a few minutes; wait for it before judging speed.

### TODO (later): make drag-and-drop from the file manager work in Cubase

- Frozen-process backtrace (`winedbg --command "bt all" <winpid>`; find `<winpid>` with `winedbg --command "info process"`) shows the GUI thread in `cubase14` -> `ole32` drop handling -> `win32u` modal wait, i.e. Cubase shows the Import Options dialog inside IDropTarget::Drop and the external drag keeps the grab. Needs a Wine-side fix or a different drop path.

## Omnisphere: "Request Authorization" button does nothing

- Cause: the plugin writes `%LOCALAPPDATA%\Spectrasonics\rhurltemp.html` (a meta-refresh to spectrasonics.net/authorization) and ShellExecutes it. The prefix had no `.html` association, so Wine logged `No association found` and silently did nothing. Found with `WINEDEBUG=-all,err+all,+shell,+winebrowser,+exec`.
- Fix (must be under HKLM; HKCU alone was not picked up):

```sh
for e in .html .htm; do $WINE reg add "HKLM\\Software\\Classes\\$e" /ve /d htmlfile /f; done
$WINE reg add 'HKLM\Software\Classes\htmlfile\shell\open\command' /ve /d '"C:\windows\system32\winebrowser.exe" "%1"' /f
```

- Check: `WINEDEBUG=-all,+exec $WINE start 'C:\...\rhurltemp.html'` should show `winebrowser.exe` being run. The page opens in the host's default browser (xdg-open).
- Do not post or share `rhurltemp.html`: it contains your authorization challenge.

## Cubase: open issues (not fixed yet)

- Popup/context menu items show solid black squares where the icons should be (toolbar, mixer and media-bay icons draw fine). Probably 32-bit alpha menu bitmaps drawn without alpha blending by Wine's menu code; no registry fix known. Web search found nothing specific.
- General slowness: the `wined3d` render thread stays busy (35-65% CPU) even when idle. `StrictDrawOrdering=disabled` (HKCU\Software\Wine\AppDefaults\Cubase14.exe\Direct3D) set, effect not measured.
- Flicker in the transport position / locator selector (not yet analysed).
- MIDI: Wine lists ALSA sequencer devices only at app start, so restart Cubase after plugging in a MIDI device. A Launchpad Mini MK3 showed up as ALSA client 32 / `midiC4D0`; check with `cat /proc/asound/seq/clients`. Then enable the port in Studio > Studio Setup > MIDI Port Setup.
- Omnisphere "Request Authorization": handler script `bin/spectrasonics-open-html` in this folder, registered under `HKLM\Software\Classes\htmlfile\shell\open\command` as `Z:\home\<user>\audio-wine-notes\bin\spectrasonics-open-html "%1"`.

## Screenshots of Wine windows (KDE Wayland)

`xwd -root` / `xwd -id` / `import` fail on XWayland windows (`X_GetImage BadMatch`), and `spectacle -a` captures whichever window is active (may be a private one).
`bin/winshot "<title substring>" out.png` finds the window by title, activates it via EWMH (python-xlib), checks it really is active, then runs `spectacle -b -n -a`. It captures nothing if no window matches.
Example: `python3 bin/winshot "Cubase Pro Project" /tmp/cubase.png`
Needs: `python-xlib`, `xorg-xwininfo`, `spectacle`.

## Clicking in Wine windows (for automated checks)

`bin/winclick "<title>" X Y [--right] [--double]` clicks at window-relative pixel X,Y with XTEST (works on XWayland windows; it only moves the X pointer). `--key Escape Return ...` sends keys; `--geometry` prints the size and position. Same safety as `winshot`: it finds the window by title, activates it, verifies it is active, and does nothing otherwise.
Mapping from a `winshot` image to X,Y: x = image x - 65, y = image y - 81 (KDE's shadow and title bar). Example: the Drum Machine tab at image (692,1013) is `winclick "Cubase Pro Project" 627 932`. Verified: it switched the lower zone to the Drum Machine tab.
Not yet working: a plain click on the menu bar "File" (16,10) did not show an open menu in the next screenshot (menus may need a press-and-hold or a longer wait).

## Finding: menu slowness and black menu icons share a cause (probable)

- Trace (`WINEDEBUG=-all,+timestamp,+menu,+dxgi,+d3d,+d2d`, right-click on the track list): the context menu is built with one `SetMenuItemInfoW` per item (attaching an icon `hbitmap`), ~55 ms apart, ~20 items = ~1.1 s before the menu appears.
- Between two items there are ~7,000 trace lines: Direct2D renders the icon on a GPU texture (many wined3d state changes, shaders, blend states), then `dxgi_surface_GetDC` (still a FIXME "semi-stub") loads the texture back to system memory, wraps it in a DC from memory (`wined3d_texture_create_dc`), and `release_dc` destroys it again, each with a `wined3d_cs_finish` round trip.
- So the icons are made through the slowest path Wine has, and the black squares are probably the same path losing the alpha/content. Not yet proven: needs a patched wined3d/dxgi build to test (source cloned in ~/src/wine-11.17, ~/src/wine-staging-11.17).

## Black menu icons: measured (debug build)

Build setup (in `~/src`): `wine-11.17` + `wine-staging-11.17` (git tags), `work/` = Wine + full staging + vstenv's 13 patches, configured like vstenv's `scripts/build-wine-fixes.sh` (PE modules); `work2/` = copy reconfigured with X/fonts so `win32u.so` (the Unix half, where menu code lives) can be built: `make -j32 dlls/win32u/win32u.so`. `wine-run/` = copy of the vstenv Wine runtime where rebuilt files are dropped in; run Cubase with `~/src/wine-run/bin/wine` (kill the other runtime's server first: `wineserver -k`).
A temporary `ERR` print in `draw_bitmap_item` (`dlls/win32u/menu.c`) showed that every menu icon Cubase passes is a 13x13 32-bit DIB whose 169 pixels are all identical, alpha 0xff, colour `0x010200` (opaque near-black). So Wine's menu drawing is not at fault (it already alpha-blends 32-bit menu bitmaps in this staging tree): the bitmap content is wrong before it reaches the menu.
Each icon is produced by: Direct2D draw on a window-sized (1898x967) DXGI surface -> `copy_sub_resource_region` of the whole surface -> `dxgi_surface_GetDC` -> GDI copy of a 13x13 block into the icon DIB -> `ReleaseDC`. Next: find out whether the GetDC'd sysmem copy is black (copy/readback bug in wined3d) or the Direct2D draw of the glyph produced black (d2d1 issue). That same path is what makes the menu slow (full-window copy and readback per icon, several per item).

## More open issues (reported 2026-11-24)

- **Cursor line flicker**: the project position line under the downward triangle in the ruler flickers constantly, also idle (user report). A 60 fps recording of a session only showed it vanishing for 3-6 frames around menu/dialog open and close, so the recording did not capture the idle flicker. An idle `+dcomp` trace showed only ~2 `device_Commit` and ~900 `surface_QueryInterface/AddRef/Release` per 6 s, so the line is probably not redrawn by the dcomp commit loop; not yet traced further.
- **White window background after a restart**: right after a restart the first paint was sometimes white (baseline screenshot) and fine in a later run; probably a missing first repaint. Not reproduced reliably.
- **Ugly letter spacing in some dialogs** (Safe Mode dialog, light/italic text: uneven gaps inside words). The main window text (GDI menus, toolbar) looks fine, and turning off fontconfig hinting (`hintstyle=hintnone` via `FONTCONFIG_FILE`) made no visible difference there. Suspect: DirectWrite text in `Segoe UI Light/Semilight` (Selawik aliases from vstenv `fontalias.py`) measured with GDI-classic hinted advances.
- Scripts: `bin/cubase-menu-shot OUT.png` restarts Cubase (answers Safe Mode and the Hub), opens a context menu, takes a `winshot`; env such as `FONTCONFIG_FILE` or `WINEDEBUG` carries through for experiments.

### Experiment: skip the dcomp timer Present (dxgi `factory.c`, env `WINE_DCOMP_NO_TIMER_PRESENT`)
Built `~/src/work/dlls/dxgi` with the timer's `IDXGISwapChain4_Present(sc,0,0)` replaced by `dcomp_reblit_comp_buffer` when the env var is set; installed in `~/src/wine-run`. Result on Cubase 14: parts of the window top (toolbar left, track-list header) stayed white (unpainted), and a stray second cursor line appeared. So the timer Present is also what keeps the window repainted; simply skipping it is not a fix. The white top also showed up once on the unmodified runtime right after a restart, so it is a startup repaint race either way. Flicker not conclusively judged in this run.
User check: with the timer Present skipped the cursor line **still flickers** (and the window top stays white), so the timer Present is not the cause. Issue draft updated.

## Experiment result: composite off-screen (flicker fixed)
`dlls/dcomp/device.c`: when `WINE_DCOMP_BACKBUFFER` is set, each composite tick copies the window into an off-screen DC, lets `do_composite()` draw every visual into it (both blit sites use `back_dc`), then copies the result to the window once. User check on Cubase 14: **the cursor line flicker is gone**. The white patches at the top of the window remain (separate bug: those regions are only painted when the app repaints them, e.g. on hover).
Cause (probable): the compositor blits each visual straight onto the window DC, so a frame that is only half composited (base layer without the cursor overlay) can reach the screen.
Cost: two full-window copies per tick; needs measuring. Not yet a patch (env-gated experiment).

## Safe Mode dialog
Killing Cubase (or `wineserver -k`) makes the next start show "The application was terminated unexpectedly" (Safe Mode). `bin/cubase-quit` closes the project window via `_NET_CLOSE_WINDOW`, which exits cleanly (no dialog next time). `cubase-menu-shot` now calls it first.

## Safe Mode / Hub: never click by fixed coordinates
The Safe Mode dialog and the Hub come up in different sizes (Safe Mode 1110x514 and 1387x642; Hub 1255x689 and 1568x856). A hard-coded click meant for OK landed on the "Delete program preferences" option of the larger dialog (selecting it; the dialog says that cannot be undone). Use `bin/safemode-ok` (OK button from the dialog's own size, never touches the options) and `bin/hub-create-empty` (Create Empty from the Hub's size); `cubase-menu-shot` uses them. If Safe Mode shows up, choose "Use current program preferences" unless you really want to reset.
Idle CPU of Cubase (10 s, % of one core, right after start, so partly startup work): stock 858, with `WINE_DCOMP_BACKBUFFER=1` 626.

## Drag and drop (fixed)
The freeze was Wine's X11 driver not processing nested X events while IDropTarget::Drop runs (Cubase asks a question there). Fixed by patches/wine/winex11-xdnd-deferred-drop-11.17.patch: drop queued to the message loop, copy/link drops finished early. Verified with Dolphin -> Cubase audio track.
