# Distribution checklist

Plan: ship the panel (and, once the server staff approves, the optional client tweaks) so that it can be unpacked into
any L2Axis install on Windows x64. This file lists what must **never** leave the developer's PC and how the client
tweaks must be applied.

## 1. Never package these (they identify the person or their PC)

| File | Why |
|---|---|
| `L2AxisControl/data.json` | Account logins in plain text, DPAPI-encrypted passwords, slots, timers, captured boss data. |
| `L2AxisControl/errors.log` | Local log (anonymized, but still local). |
| `L2AxisControl/bosses_db.json` | Generated from the client's data files at runtime. |
| `system/bd.ini` | Last Alt+B page: **character name, class, level and IP address**. The panel clears it on start/exit and after each read. |
| `system/l2.log`, `system/l2Second.log` | Windows user path (`C:\Users\<name>\...`), CPU/GPU info. |
| `system/StrixPlatform/log/*` | Anti-cheat logs (may contain machine identifiers). |
| `system/Option.ini`, `WindowsInfo.ini`, `chatfilter.ini`, `Running.ini`, `s_info.ini`, `CheckGrp.log` | Personal settings/runtime state; would overwrite the player's own. |
| `system/*.bak`, `system/_backup_*`, `Screenshot/` | Leftovers from the developer's machine. |

Build the release from a clean checkout (`git clone` into an empty folder), never by zipping a game folder.

## 2. Client tweaks: ship a patcher, not modified files

Modified copies of `Engine.u`, `interface.u`, `systemmsg-e.dat` or `user.ini` must **not** be distributed as whole files:

- they are the server's / NCSOFT's files (redistribution);
- they break as soon as L2Axis ships a new patch (our copy would silently undo the server's changes);
- `user.ini` holds each player's key bindings and options — replacing it wipes them.

The patcher must, for each target file:

1. verify the SHA-256 of the player's **original** file against the known version — refuse if it differs;
2. back up the original next to it (`*.bak`) before writing;
3. apply only the specific change (decrypt → change bytes/values → re-encrypt, fix the footer CRC);
4. re-read and verify the result (e.g. decode the patched UnrealScript bytecode and check the function size);
5. offer an **Uninstall** that restores every `.bak`.

`user.ini` must be edited key by key (e.g. set `MaxZoomingDist`), never replaced.

## 3. Building an executable

- When freezing with PyInstaller, build from a neutral path such as `C:\build\l2axis-control\`. Python embeds source
  paths in the compiled code, so building from `C:\Users\<name>\...` leaks the Windows user name into tracebacks.
- The panel needs administrator rights (because `l2.exe` requires them); an unsigned elevated `.exe` triggers
  SmartScreen warnings — consider code signing before a wide release.
- Requires the Microsoft Edge WebView2 runtime (bundled with Windows 11; Windows 10 may need the Evergreen bootstrapper).

## 4. Screenshots / support

- Screenshots for the forum: generic slot names ("Account 1…"), no login shown, no Alt+B home page (shows name/IP).
- Logs (`errors.log`) are anonymized: the user profile path, Windows user name and computer name are masked.
