"""L2Axis Control - floating panel to manage L2Axis (Lineage II) game clients.

Python backend (Windows API through ctypes) + an HTML/CSS interface styled after
the Lineage II windows, shown in a frameless window through pywebview.

It never interacts with the game itself (no automated input): it only opens,
arranges and monitors the windows, measures the network and tracks timers/votes.
"""
import base64
import calendar
import ctypes
import json
import math
import operator
import re
import os
import socket
import struct
import subprocess
import threading
import time
import webbrowser
import winsound
import zlib
from collections import deque
from ctypes import wintypes
from pathlib import Path

import secrets
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import webview

APP_DIR = Path(__file__).resolve().parent
DATA_FILE = APP_DIR / "data.json"
UI_FILE = APP_DIR / "ui.html"
LOG_FILE = APP_DIR / "errors.log"


def _anonymize(text):
    """Mask the Windows user and computer name (logs may be shared on the forum)."""
    text = re.sub(re.escape(str(Path.home())), "%USERPROFILE%", text, flags=re.IGNORECASE)
    for real, mask in ((os.environ.get("USERNAME", ""), "<user>"), (os.environ.get("COMPUTERNAME", ""), "<pc>")):
        if len(real) > 1:  # whole words only (a PC named "ANA" must not turn "BANANA" into "B<pc>NA")
            text = re.sub(r"\b" + re.escape(real) + r"\b", mask, text, flags=re.IGNORECASE)
    return text


def log_error(where, text):
    text = _anonymize(str(text))
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}: {text}\n")
    except OSError:
        pass
GAME_DIR = APP_DIR.parent / "system"
L2_EXE = GAME_DIR / "l2.exe"
# written by the modified BoardWnd (interface.u): HTML of every Alt+B page
BOARD_FILE = GAME_DIR / "bd.ini"
WARP_CLI = Path(r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe")

SERVER_IP = "148.251.46.248"
MAX_SLOTS = 5      # default number of account slots for a new install
SLOT_CAP = 10      # the player can add slots up to this (the server still limits clients per HWID)
HWID_LIMIT = 4
VOTE_COOLDOWN = 12 * 3600
PING_INTERVAL = 2.0
PING_HISTORY = 60
LAUNCH_SPACING = 5  # seconds between clients on "Open all"
# +12 px top/left: room for the GM medallion to stick out of the frame
WIN_W, WIN_H, COMPACT_H = 440, 720, 104   # default size (logical pixels); the user can resize
MIN_W, MIN_H, MAX_W, MAX_H = 392, 480, 1000, 1400
ASSETS_DIR = APP_DIR / "assets"
APP_ICON = ASSETS_DIR / "app.ico"
APP_ID = "L2Axis.Control"  # own taskbar identity, so Windows does not group the panel with Python
ASSETS = {"gm_96.png", "gm_256.png", "gm_512.png"}

# (key, name, url, main?) - the 4 main sites are the ones that count for .vote
VOTE_SITES = [
    ("topzone", "L2TopZone", "https://l2topzone.com/vote/id/18052", True),
    ("hopzone", "HopZone.net", "https://vgw.hopzone.net/site/vote/104740/1", True),
    ("l2network", "L2Network.eu", "https://l2network.eu/index.php?a=in&u=L2axis", True),
    ("l2topco", "L2Top.co", "https://l2top.co/vote/server/L2axis", True),
    ("xtremetop", "XtremeTop100", "https://www.xtremetop100.com/in.php?site=1132372054", False),
    ("hopzoneeu", "HopZone.eu", "https://hopzone.eu/vote/92", False),
    ("top100arena", "Top100Arena", "https://www.top100arena.com/category/lineage2?vote=98665", False),
    ("gtop100", "GTop100", "https://gtop100.com/topsites/Lineage-2/sitedetails/L2axis-100152?vote=1", False),
    ("topgamesites", "TopGameSites", "https://www.topgamesites.net/lineage2/vote51186", False),
    ("arenatop", "Arena-Top100", "https://www.arena-top100.com/index.php?a=in&u=L2axis", False),
    ("l2jbrasil", "Top L2JBrasil", "https://top.l2jbrasil.com/index.php?a=in&u=L2axis", False),
    ("l2jtop", "L2JTop", "https://l2jtop.com/server/l2axis/vote/", False),
    ("topg", "TopG", "https://topg.org/lineage2-private-servers/server-634775", False),
    ("mmtop200", "MMTop200", "https://mmtop200.com/vote/3636", False),
    ("gamingtop", "GamingTop100", "https://www.gamingtop100.net/in-18510", False),
    ("l2votes", "L2Votes", "https://l2votes.com/votes.php?sid=488", False),
    ("playl2", "PlayL2", "https://playl2.net/user/vote/79", False),
]

# (UI translation key, url)
LINKS = [
    ("linkSite", "https://l2axis.com/"),
    ("linkForum", "https://forum.l2axis.com/"),
    ("linkNews", "https://forum.l2axis.com/forums/news-and-announcements.2/"),
    ("linkDonate", "https://forum.l2axis.com/threads/donate-panel-update.695/"),
    ("linkRules", "https://forum.l2axis.com/threads/server-rules-enforced.18/"),
]
# About window credits (GM avatar used with permission): gm.json + assets/gm_*.png.
# Without gm.json the panel falls back to a generic medallion.
GM_FILE = APP_DIR / "gm.json"


def load_gm():
    try:
        gm = json.loads(GM_FILE.read_text(encoding="utf-8"))
        return {"name": str(gm["name"]), "email": str(gm.get("email", "")),
                "avatar": (ASSETS_DIR / "gm_96.png").exists() and (ASSETS_DIR / "gm_256.png").exists()}
    except (OSError, ValueError, KeyError):
        return None
FOLDERS = {"game": APP_DIR.parent, "system": GAME_DIR, "shots": APP_DIR.parent / "Screenshot"}

# ---------------------------------------------------------------- Win32
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
iphlpapi = ctypes.windll.iphlpapi

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x1
SW_MINIMIZE, SW_RESTORE = 6, 9
WM_CLOSE = 0x0010
SPI_GETWORKAREA = 0x30


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [("Address", ctypes.c_ulong), ("Status", ctypes.c_ulong),
                ("RoundTripTime", ctypes.c_ulong), ("DataSize", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                ("Options", IP_OPTION_INFORMATION)]


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.OpenProcess.restype = wintypes.HANDLE
iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
iphlpapi.IcmpSendEcho.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, wintypes.WORD,
                                  ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
ctypes.windll.gdi32.CreateRectRgn.restype = wintypes.HANDLE
ctypes.windll.gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
ctypes.windll.gdi32.CombineRgn.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.c_int]
ctypes.windll.gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def list_l2_pids():
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pids = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.lower() == "l2.exe":
            pids.append(entry.th32ProcessID)
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return pids


def process_memory_mb(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c = PROCESS_MEMORY_COUNTERS()
        c.cb = ctypes.sizeof(c)
        if kernel32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return c.WorkingSetSize / 1048576
        return None
    finally:
        kernel32.CloseHandle(h)


GAME_WINDOW_CLASS = "l2UnrealWWindowsViewportWindow"

# Server events, as listed in the server's "Server Features" post (forum.l2axis.com/threads/server-features.259/).
# "restart": first start N hours after a server restart, then every M hours.
# "weekly": (weekday 0=Mon, hour, minute) in GMT.
SERVER_EVENTS = [
    {"key": "pig", "name": "Super Lucky Pig", "aliases": ["Lucky Pig"], "restart": (1, 15),
     "drops": "Cloak of Lucky Pig, Earring of Pig, BEAS, BEWS, Golden Treasure Chest, Blessed Olf's Scroll, "
              "Hats, Neolithics, High Grade Raid Chest"},
    {"key": "iq", "name": "Ice Queen's Spirit", "restart": (3, 15), "drops": "BEAS, BEWS, Vesper armor parts"},
    {"key": "aq", "name": "Ant Queen's Spirit", "restart": (6, 17), "drops": "BEAS, BEWS, Vesper armor parts"},
    {"key": "pk", "name": "Pirate King's Spirit", "restart": (9, 19), "drops": "BEAS, BEWS, Vesper armor parts"},
    {"key": "zombie", "name": "Zombie Apocalypse", "aliases": ["zombie"], "weekly": [(5, 20, 0), (6, 20, 0)],
     "drops": "Cloak of Zombie, Zombie Hat, Ring of Zombie, A-S Cry Gems, A-S shot packs, Element Stones, "
              "Element Crystals, B-S Enchant Scrolls, Olf's Enchant Scrolls, Vesper Noble Stones"},
    # forum.l2axis.com/threads/new-events-border-blitz-battle-royale-fight-the-fakes.1258/ (server clock GMT+3)
    {"key": "blitz", "name": "Border Blitz", "daily": [(5, 30), (11, 0), (16, 30), (22, 30)], "tz": 3,
     "dur": 20, "reg": 30,
     "drops": "Winners: 1,000 Fame, 10,000 Knight's Epaulettes, 2 Blessed Enchant Weapon S, 4 Blessed Enchant Armor S. "
              "Losers: 40,000,000 Adena, 1,000 Knight's Epaulettes"},
    {"key": "royale", "name": "Battle Royale", "daily": [(0, 30), (7, 0), (12, 0), (17, 30)], "tz": 3,
     "dur": 30, "reg": 30,
     "drops": "Each round champion: 1,000 Fame. Grand Champion: +2,500 Fame, 1 High Level Raid Chest. "
              "Others (eligible): 40,000,000 Adena"},
    {"key": "fakes", "name": "Fight the Fakes", "aliases": ["fake players", "fakes"], "daily": [(2, 30), (9, 30), (14, 0), (21, 0)], "tz": 3,
     "dur": 30, "reg": 0,
     "drops": "Per fake: 175 Fame, 25,000,000 Adena, 150 Gold, 300 Silver, 600 Bronze Coins. "
              "Final duel of the top 2: one random grand prize"},
    # forum.l2axis.com/threads/new-giant-rats-pvp-zone-event.1074/ (EST)
    {"key": "rats", "name": "Giant Rats Invasion", "aliases": ["Giant Rats", "rats"], "daily": [(13, 0), (20, 0)], "tz": "us_eastern", "dur": 30, "reg": 30,
     "drops": "—"},
    # forum.l2axis.com/threads/treasure-hunt-event.993/ (5 times a day, times not published)
    {"key": "treasure", "name": "Treasure Hunt", "aliases": ["treasure"], "chat": True,
     "drops": "Adena, Gold/Silver/Bronze Coins, Medals of Glory (100 Fame)"},
]


def us_eastern_offset(day_utc):
    """Hours from GMT of US Eastern time on that day: -4 in summer time (2nd Sunday of March to
    1st Sunday of November), -5 otherwise."""
    y = time.gmtime(day_utc).tm_year

    def nth_sunday(month, n):
        first = calendar.timegm((y, month, 1, 0, 0, 0))
        wd = time.gmtime(first).tm_wday  # Monday = 0
        return first + ((6 - wd) % 7 + 7 * (n - 1)) * 86400

    return -4 if nth_sunday(3, 2) <= day_utc < nth_sunday(11, 1) else -5


def event_times(ev, restart_at, now=None, seen=None):
    """(last start, next start) in epoch seconds; None when unknown. `seen` = when the event was
    last seen starting in the announcements (better than the restart time)."""
    now = now or time.time()
    if seen and "restart" in ev:
        every = ev["restart"][1] * 3600
        n = max(0, int((now - seen) // every))
        return seen + n * every, seen + (n + 1) * every
    if ev.get("chat"):
        return None, None
    if "restart" in ev:
        if not restart_at:
            return None, None
        first, every = ev["restart"]
        t = restart_at + first * 3600
        if now < t:
            return None, t
        n = int((now - t) // (every * 3600))
        last = t + n * every * 3600
        return last, last + every * 3600
    starts = []
    day0 = (int(now) // 86400) * 86400  # midnight GMT today
    for back in range(-7, 8):
        d = day0 + back * 86400
        if "daily" in ev:  # times in the event's own time zone (hours from GMT)
            tz = us_eastern_offset(d) if ev.get("tz") == "us_eastern" else ev.get("tz", 0)
            starts += [d + (h - tz) * 3600 + m * 60 for h, m in ev["daily"]]
            continue
        wd = time.gmtime(d).tm_wday
        for w, h, m in ev["weekly"]:
            if w == wd:
                starts.append(d + h * 3600 + m * 60)
    past = [s for s in starts if s <= now]
    future = [s for s in starts if s > now]
    return (max(past) if past else None), (min(future) if future else None)


def main_windows_by_pid():
    """pid -> main window of that process: the game viewport when it exists (even minimized),
    otherwise the largest visible top-level window (the splash while the client loads)."""
    result = {}
    cls = ctypes.create_unicode_buffer(64)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        user32.GetClassNameW(hwnd, cls, 64)
        # the game window always wins, whatever its size (a minimized window is tiny)
        score = (cls.value == GAME_WINDOW_CLASS, (r.right - r.left) * (r.bottom - r.top))
        if pid.value not in result or score > result[pid.value][1]:
            result[pid.value] = (hwnd, score)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return {pid: v[0] for pid, v in result.items()}


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def focus_window(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # a released ALT key lifts the Windows foreground lock
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


def terminate_pid(pid):
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if h:
        kernel32.TerminateProcess(h, 0)
        kernel32.CloseHandle(h)


def process_cpu_seconds(pid):
    """CPU time (kernel + user, in seconds) a process has used so far, or None."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(h, *[ctypes.byref(x) for x in times]):
            return None
        kernel, user = times[2], times[3]
        return ((kernel.dwHighDateTime << 32 | kernel.dwLowDateTime) +
                (user.dwHighDateTime << 32 | user.dwLowDateTime)) / 1e7
    finally:
        kernel32.CloseHandle(h)


def wait_until_idle(pid, calm_seconds=5, threshold=0.3, mem_band=1.5, timeout=90):
    """Wait until a client stops loading. Both must hold for `calm_seconds` in a row:
    - CPU under `threshold` of one core (a loading client uses a whole core or more);
    - memory (working set) within `mem_band` MB: the client keeps loading data with a
      calm CPU for a while, and only then is the screen ready for typing.
    Returns False if the process is gone, None on timeout, True when ready."""
    deadline = time.time() + timeout
    last, calm, mems = process_cpu_seconds(pid), 0, []
    while calm < calm_seconds:
        if time.time() >= deadline:
            return None
        time.sleep(1)
        now, mem = process_cpu_seconds(pid), process_memory_mb(pid)
        if now is None or last is None or mem is None:
            return False  # process gone
        mems = (mems + [mem])[-(calm_seconds + 1):]
        steady = len(mems) > 1 and max(mems) - min(mems) < mem_band
        calm = calm + 1 if now - last < threshold and steady else 0
        if not steady:
            mems = mems[-1:]
        last = now
    return True


# ---------------------------------------------------------------- what each client is doing
# Read only from what Windows and the client already show: the TCP connections of each process
# and the client's own log file (system/l2.log for the first client, l2Second.log for the second).

# map square of the world -> region (the town you would respawn in), from the L2J datapack (GPL)
MAP_TILE_REGION = {
    "12_24": "Keucereus", "12_25": "Keucereus", "13_24": "Keucereus", "13_25": "Keucereus",
    "14_22": "Keucereus", "14_23": "Keucereus", "14_24": "Keucereus", "14_25": "Keucereus",
    "15_10": "Rune", "15_11": "Rune", "15_12": "Rune", "15_23": "Talking Island",
    "15_24": "Talking Island", "15_25": "Talking Island", "16_10": "Ironcastle Inner Oasis", "16_11": "Rune",
    "16_12": "Dimensional Rift", "16_19": "Soul Island", "16_20": "Gludin", "16_21": "Gludin",
    "16_22": "Gludin", "16_23": "Talking Island", "16_24": "Talking Island", "16_25": "Talking Island",
    "17_10": "Rune", "17_11": "Rune", "17_15": "Fantasy Island", "17_16": "Fantasy Island",
    "17_19": "Dark Elf Village", "17_20": "Gludin", "17_21": "Gludin", "17_22": "Gludin",
    "17_23": "Gludin", "17_24": "Talking Island", "17_25": "Talking Island", "18_10": "Dion",
    "18_14": "Orc Village", "18_15": "Fantasy Island", "18_16": "Fantasy Island", "18_18": "Dark Elf Village",
    "18_19": "Dark Elf Village", "18_20": "Dark Elf Village", "18_21": "Gludio", "18_22": "Gludin",
    "18_23": "Gludin", "18_24": "Gludin", "18_25": "Talking Island", "19_10": "Oren",
    "19_13": "Orc Village", "19_14": "Orc Village", "19_15": "Orc Village", "19_16": "Orc Village",
    "19_17": "Rune", "19_18": "Dark Elf Village", "19_19": "Dark Elf Village", "19_20": "Neutral Zone",
    "19_21": "Gludio", "19_22": "Gludio", "19_23": "Gludio", "19_24": "Gludin",
    "19_25": "Southern Wasteland", "19_26": "Gludio", "20_10": "Hunters Village", "20_13": "Orc Village",
    "20_14": "Orc Village", "20_15": "Orc Village", "20_16": "Rune", "20_17": "Primeval Island",
    "20_18": "Dark Elf Village", "20_19": "Elven Village", "20_20": "Elven Village", "20_21": "Dion",
    "20_22": "Dion", "20_23": "Floran", "20_24": "Giran Harbor", "20_25": "Southern Wasteland",
    "20_26": "Gludio", "21_14": "Schuttgart", "21_15": "Rune", "21_16": "Rune",
    "21_17": "Rune", "21_18": "Oren", "21_19": "Elven Village", "21_20": "Elven Village",
    "21_21": "Dion", "21_22": "Dion", "21_23": "Giran", "21_24": "Giran Harbor",
    "21_25": "Giran Harbor", "22_13": "Schuttgart", "22_14": "Schuttgart", "22_15": "Rune",
    "22_16": "Rune", "22_17": "Oren", "22_18": "Oren", "22_19": "Oren",
    "22_20": "Oren", "22_21": "Hunters Village", "22_22": "Giran", "22_23": "Heine",
    "22_24": "Heine", "22_25": "Heine", "23_10": "Dwarven Village", "23_11": "Dwarven Village",
    "23_12": "Dwarven Village", "23_13": "Schuttgart", "23_14": "Schuttgart", "23_15": "Rune",
    "23_16": "Goddard", "23_17": "Oren", "23_18": "Aden", "23_19": "Hunters Village",
    "23_20": "Hunters Village", "23_21": "Hunters Village", "23_22": "Giran", "23_23": "Heine",
    "23_24": "Heine", "23_25": "Heine", "24_10": "Dwarven Village", "24_11": "Dwarven Village",
    "24_12": "Dwarven Village", "24_14": "Goddard", "24_15": "Goddard", "24_16": "Goddard",
    "24_17": "Aden", "24_18": "Aden", "24_19": "Aden", "24_20": "Hunters Village",
    "24_21": "Hunters Village", "24_23": "Heine", "25_10": "Dwarven Village", "25_11": "Dwarven Village",
    "25_12": "Dwarven Village", "25_14": "Goddard", "25_15": "Goddard", "25_16": "Goddard",
    "25_17": "Aden", "25_18": "Aden", "25_19": "Aden", "25_20": "Hunters Village",
    "25_21": "Hunters Village", "26_14": "Goddard",
}
GAME_LOGS = ("l2.log", "l2Second.log")


def process_created(pid):
    """When a process was started (epoch seconds), or None."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        t = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            return None
        return (t[0].dwHighDateTime << 32 | t[0].dwLowDateTime) / 1e7 - 11644473600
    finally:
        kernel32.CloseHandle(h)


def tcp_by_pid():
    """pid -> list of (remote ip, remote port) of its established TCP connections (IPv4)."""
    iphlp = ctypes.windll.iphlpapi
    size = wintypes.DWORD(0)
    iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)  # AF_INET, TCP_TABLE_OWNER_PID_ALL
    buf = ctypes.create_string_buffer(size.value + 4096)
    size = wintypes.DWORD(len(buf))
    if iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 5, 0):
        return {}
    out = {}
    for k in range(struct.unpack_from("I", buf.raw, 0)[0]):
        state, _la, _lp, ra, rp, pid = struct.unpack_from("IIIIII", buf.raw, 4 + k * 24)
        if state == 5:  # ESTABLISHED
            out.setdefault(pid, []).append((socket.inet_ntoa(struct.pack("I", ra)), socket.ntohs(rp & 0xFFFF)))
    return out


class GameLog:
    """Follows one client log file and keeps what it says about the session."""
    _LINE = re.compile(r"\((\d\d):(\d\d):(\d\d)\) \w+: (.*)")

    def __init__(self, path):
        self.path = path
        self.pos = 0
        self.reset()

    def reset(self):
        self.opened = None      # epoch when the client opened the file (= client start)
        self.in_world = False
        self.entered = None     # epoch when the character entered the world
        self.tile = None
        self._day0 = None       # midnight of the day the log was opened
        self._last = 0

    def _when(self, h, m, s):
        if self._day0 is None:
            return time.time()
        secs = h * 3600 + m * 60 + s
        if secs + 3600 < self._last:  # passed midnight
            self._day0 += 86400
        self._last = secs
        return self._day0 + secs

    def update(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.pos:  # a new session started over the file
            self.pos = 0
            self.reset()
        if size == self.pos:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
        except OSError:
            return
        data = data[:len(data) - len(data) % 2]
        end = data.rfind("\n".encode("utf-16-le"))
        if end < 0:
            return
        if end % 2:
            end -= 1
        chunk, self.pos = data[:end + 2], self.pos + end + 2
        for line in chunk.decode("utf-16-le", "replace").lstrip("\ufeff").splitlines():
            self._line(line)

    def _line(self, line):
        if "Log file open," in line:
            m = re.search(r"open, (\d+)/(\d+)/(\d+) (\d+):(\d+):(\d+)", line)
            if m:
                mo, d, y, hh, mi, ss = map(int, m.groups())
                self.reset()
                self.opened = time.mktime((2000 + y % 100, mo, d, hh, mi, ss, 0, 0, -1))
                self._day0 = time.mktime((2000 + y % 100, mo, d, 0, 0, 0, 0, 0, -1))
                self._last = hh * 3600 + mi * 60 + ss
            return
        m = self._LINE.match(line)
        if not m:
            return
        text = m.group(4)
        if text.startswith(("RequestEnterWorldPacket", "LoadMap:", "Teleport from", "Browse: Lobby")):
            when = self._when(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if text.startswith("RequestEnterWorldPacket"):
                self.in_world, self.entered = True, when
            elif text.startswith("Browse: Lobby"):  # back to the login screen
                self.in_world, self.entered, self.tile = False, None, None
            else:
                t = re.search(r"(\d+_\d+)(?:\.unr)?\s*$", text.split("?")[0])
                if t:
                    self.tile = t.group(1)


class ClientWatch:
    """Per client: connected to the game server or not (and when it dropped), and the log
    status (in game, region, time online)."""

    def __init__(self):
        self.logs = [GameLog(GAME_DIR / name) for name in GAME_LOGS]
        self._had_game = {}   # pid -> it was connected to the game server
        self._handles = {}    # pid -> process handle, to read the exit code when it ends
        self._in_world = {}   # pid -> it was in game (last poll)
        self._lost = {}       # pid -> since when the connection is gone
        self._created = {}

    def _match_logs(self, pids):
        """Each log belongs to the client started just before it was opened (one log each)."""
        pairs = []
        for pid in pids:
            made = self._created.get(pid)
            if made is None:
                made = self._created[pid] = process_created(pid) or 0
            for log in self.logs:
                if log.opened and -5 <= log.opened - made <= 180:
                    pairs.append((abs(log.opened - made), pid, log))
        # in order: the first client started takes the first log opened, and so on
        out, used = {}, set()
        for pid in sorted({p for _, p, _ in pairs}, key=lambda p: self._created[p]):
            free = [log for _, q, log in pairs if q == pid and id(log) not in used]
            if free:
                log = min(free, key=lambda lg: lg.opened)
                out[pid] = log
                used.add(id(log))
        return out

    def poll(self, pids):
        """Returns pid -> status, and the pids that just lost the connection to the game."""
        for log in self.logs:
            log.update()
        conns = tcp_by_pid()
        now, out, dropped = time.time(), {}, []
        logs = self._match_logs(pids)
        for pid in pids:
            game = any(ip == SERVER_IP and port not in (2106,) for ip, port in conns.get(pid, []))
            if game:
                self._had_game[pid] = True
                self._lost.pop(pid, None)
            elif self._had_game.get(pid):
                since = self._lost.setdefault(pid, now)
                if now - since >= 4 and self._had_game[pid] is True:  # 2 polls: not a blip
                    self._had_game[pid] = "lost"
                    dropped.append(pid)
            log = logs.get(pid)
            st = {"net": "game" if game else ("lost" if self._had_game.get(pid) == "lost" else None)}
            if log:
                st["inWorld"] = log.in_world and game
                st["region"] = MAP_TILE_REGION.get(log.tile) if log.in_world else None
                st["online"] = now - log.entered if log.in_world and log.entered and game else None
            out[pid] = st
        for pid in pids:
            if pid not in self._handles:
                self._handles[pid] = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            self._in_world[pid] = bool(out[pid].get("inWorld")) or out[pid]["net"] == "game"
        self.crashed = []
        for gone in [p for p in list(self._handles) if p not in pids]:
            h = self._handles.pop(gone)
            code = wintypes.DWORD(0)
            ok = h and kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            if h:
                kernel32.CloseHandle(h)
            # it was in game, the connection had not already dropped, and it did not end normally
            if self._in_world.get(gone) and self._had_game.get(gone) is True and ok and code.value not in (0, 259):
                self.crashed.append(gone)
            self._in_world.pop(gone, None)
        for gone in [p for p in self._had_game if p not in pids]:
            self._had_game.pop(gone, None)
            self._lost.pop(gone, None)
            self._created.pop(gone, None)
        return out, dropped


def work_area():
    r = wintypes.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right - r.left, r.bottom - r.top


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


MonitorEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


def monitors():
    """Work area of every monitor (taskbar excluded), primary first, then left to right."""
    found = []

    def cb(hmon, _hdc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            w = info.rcWork
            found.append({"x": w.left, "y": w.top, "w": w.right - w.left, "h": w.bottom - w.top,
                          "primary": bool(info.dwFlags & 1),
                          "res": f"{info.rcMonitor.right - info.rcMonitor.left}x"
                                 f"{info.rcMonitor.bottom - info.rcMonitor.top}"})
        return True

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    if not found:
        x, y, w, h = work_area()
        found = [{"x": x, "y": y, "w": w, "h": h, "primary": True, "res": f"{w}x{h}"}]
    return sorted(found, key=lambda m: (not m["primary"], m["x"], m["y"]))


def monitor_of(hwnd, mons):
    """Index of the monitor holding the centre of a window."""
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    for k, m in enumerate(mons):
        if m["x"] <= cx < m["x"] + m["w"] and m["y"] - 50 <= cy < m["y"] + m["h"] + 50:
            return k
    return min(range(len(mons)), key=lambda k: abs(mons[k]["x"] + mons[k]["w"] // 2 - cx))


def grid(area, count):
    """Split an area into an even grid for `count` windows: (x, y, w, h) each, row by row."""
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    if area["w"] < area["h"] and cols > rows:  # portrait monitor: stack instead
        cols, rows = rows, cols
    cw, ch = area["w"] // cols, area["h"] // rows
    return [(area["x"] + (k % cols) * cw, area["y"] + (k // cols) * ch, cw, ch) for k in range(count)]


def icmp_ping(ip, timeout_ms=2000):
    """RTT in ms, or None if lost. Native ICMP: never opens a connection to the game server."""
    handle = iphlpapi.IcmpCreateFile()
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    try:
        payload = b"L2AxisControl---"
        size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(payload) + 8
        reply = ctypes.create_string_buffer(size)
        dest = struct.unpack("<L", socket.inet_aton(ip))[0]
        n = iphlpapi.IcmpSendEcho(handle, dest, payload, len(payload), None, reply, size, timeout_ms)
        if n == 0:
            return None
        rep = ICMP_ECHO_REPLY.from_buffer_copy(reply)
        return float(rep.RoundTripTime) if rep.Status == 0 else None
    finally:
        iphlpapi.IcmpCloseHandle(handle)


# ---------------------------------------------------------------- passwords (DPAPI)
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


crypt32 = ctypes.windll.crypt32
CRYPTPROTECT_UI_FORBIDDEN = 0x1
DPAPI_ENTROPY = b"L2AxisControl/v1"


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_encrypt(text):
    """Encrypt with the Windows user key: only this user, on this PC, can decrypt it."""
    src, _b1 = _blob(text.encode("utf-8"))
    ent, _b2 = _blob(DPAPI_ENTROPY)
    out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(src), "L2AxisControl", ctypes.byref(ent), None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("CryptProtectData falhou")
    try:
        return base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode()
    finally:
        kernel32.LocalFree(out.pbData)


def dpapi_decrypt(b64):
    src, _b1 = _blob(base64.b64decode(b64))
    ent, _b2 = _blob(DPAPI_ENTROPY)
    out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                                      CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("CryptUnprotectData falhou")
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(out.pbData)


# ---------------------------------------------------------------- sending text to a window
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
VK_TAB, VK_RETURN = 0x09, 0x0D
user32.MapVirtualKeyW.restype = wintypes.UINT


def post_key(hwnd, vk):
    scan = user32.MapVirtualKeyW(vk, 0)
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 1 | (scan << 16))
    time.sleep(0.03)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 1 | (scan << 16) | 0xC0000000)
    time.sleep(0.05)


def post_text(hwnd, text):
    """Send characters straight to the window (WM_CHAR). It does not depend on focus,
    so the text never lands in another program if the user switches windows."""
    for ch in text:
        user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 1)
        time.sleep(0.03)


# ---------------------------------------------------------------- recognising the client's screens
# Small grayscale crops (green channel, zlib + base64) of the buttons that mark each screen of
# the client: "Log In" (login screen), "Confirm" (server list), "Start" (character selection).
# The client draws them at a fixed pixel size, centred horizontally, whatever the window size.
SCREEN_ANCHORS = {
    "login": (186, 17, "eNq1ls1rE0EYxjNrKmh7iC2tYOpqk8luvrO73WyyTVMTaw42NFUrxUIPWqRpMUe9CD3Uk0hVqGJVBMUKIiiUQDzWHvoh2kLR1JVp/hhnN7vpwWxnse4PZiYz777v8yQT3iSXs4Nzk7OQDWFY7+x12RaJXNYWUlMLPkZ1zvgWplL2aGTMuIaan0+jGrqUIdBXXNKdw6ViX8YW0maMIdA8ICEHSKQPJllahrrz5VIybQuyGaMIqMuFd9Wdp/1y/vPvVWU/MIoe7WxPmebKYqmiOQ8ysFISZRKjNYTR611RcHn5EOjOP7w51Vl5kPy06Cop1L7z2hx970vA3Pl02XBenrbgHF8jRq/HUbr2wYhmFBDAc3rvTAc/uQYR3SUqoBEoIMdpcfeYaTJX3Hde5EQSdS3xxbIovn9SQOJ3fAcSKUkwYxgBPA/tOSLCSNWHl2EFNALaUCjT5OjYvOF8fiwqkKhrCemtWzfXO+MIDKt3wBOSODPyCOBZRic57sZaV62VyyugEdCGvm9GWBo0nA9KYY5EvqYoSpXjZr6tTxy9iEBdmwA0I4tO4PcF377siZfnWlZve+4qQA846kPfN8PD84ZznvdAElnte05BuFJpceONWv7fydYwGx7+dXXnYWv75c0fr3Y1p1dxP08QnQu9hvNewZJzbZ1Y2RpXN5lDOYe0A+OEPW0U5fJCtzO8WXfqxsdnaVyaPugzlwznEm/ZeexrYGajG2+EPV+cmMSyfj/j97MGzF9oxyP9oYXnzvojfjI+MWU4T4lkjUGtn48/W3TS5cfnkYO9/2sbkjRwdSvc+al87HYFrcLGBwznA3ELGl6tn9MurHC83QuCwQ5whJQTiURimCgJNwXagsSnomotXDKQaPSWXDJgVcMiukYsxvNq5+z9PwgCrxKRh4z/LXk5ZI8GfiWKUsIKSTKJBP7pwyVjct7DhMLhEOMZ6YvZo/EHU+3MWg=="),
    "confirm": (71, 16, "eNpzdKQKcKAOsKUOsEQFWXuvn1vsgybYe+l67XVLEkDmxWxe8SkFRiiCXnd0WZgUmfBqNEEB+yuYVYz1JJXsF125MNHc71bPhTNJJqfv3Drud8vE71bX+bpbfRfPZHddPJ2Mqs/ECBnY3ZGQA9GGy+dKiGxu97lTK12/T8X3FgOjxi1G3zul0mp3aqWqb9XKVB9QQdFoZIAMPO4w6IBoy9tyQnrxR8xvMUgbXOXwvsVoAMTetxhkvUAi18GiKBoNVJCB4R1BMG13m0FBxfmy+i0GFcfrjI4g6hYDlAKJgDEesC9PEUiq690SU1GJOKxCrjGJF6LVzNuTlBfNUjTd1EiCa9TVNTTUNDTUIUAtec+NczPlJY3nX7nQy218i0Hd+TqjM4i6xeByi0EDjK8zgjEK0NbWQgEy7IzM/Kpa6nxMzEKayoxaWkCMSsExMtDV1dUHAj1yAUgz2AxDQ0NgxBuTCYCJDASALBMTM3N8wAIPMDc3MzEBmgEAZEAeTA=="),
    "start": (109, 20, "eNpzd6cjcKUncKIncKAnsKcnsKUnsKInsKQnsKAnMCEa+K27en1vkd8tVMFbJiQAY2KB2fF6Ee7sNttbjMiivqhcAsCQWOBxh1taX1/xxJ1bt4xbLtw8nGDoc6vz/AUQl2gzDIgFxqcWGhgbGHjfYmBkDFFmqTis7H2nREoNxNUn1gw54sHcC1cXmdncYpCTk/cr6b/GAmQKWYO4RAMF4oE0t8aiDVa3GBQU5u+vr77OaAtkgjDxQJFYoAwmLmjcYlA0vSPKqnid0Q7IBGHigRKxwH6Jl6relDnCt1VN9G8ZKrReZ7S/xaBkCOQSbYSSMrFAd+KxGxemivC0XTurUHr5VMd1RodbDMrKIC7RZqgQDZT4WRiZ+JRUBBmZVUSYWAQYVeQYgMIg7qAEADrO2qA="),
}
_ANCHORS = {k: (w, h, zlib.decompress(base64.b64decode(d))) for k, (w, h, d) in SCREEN_ANCHORS.items()}
_ANCHOR_SLACK = 24  # pixels of horizontal play around the centre
gdi32 = ctypes.windll.gdi32


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


def capture_band(hwnd, band_w):
    """Green channel of a vertical band in the middle of a window's client area, read from the
    window itself (works while other windows cover it; a minimized window has no image).
    Returns (bytes, x0, width, height) or None."""
    r = wintypes.RECT()
    if user32.IsIconic(hwnd) or not user32.GetClientRect(hwnd, ctypes.byref(r)):
        return None
    w, h = r.right, r.bottom
    band_w = min(band_w, w)
    if band_w <= 0 or h <= 0:
        return None
    x0 = w // 2 - band_w // 2
    src = user32.GetDC(hwnd)
    if not src:
        return None
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, band_w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.BitBlt(mem, 0, 0, band_w, h, src, x0, 0, 0x00CC0020)  # SRCCOPY
        buf = ctypes.create_string_buffer(band_w * h * 4)
        bi = _BITMAPINFOHEADER(40, band_w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
        return buf.raw[1::4], x0, band_w, h
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, src)


def find_anchor(hwnd, name, max_mean=8):
    """Where a screen's button is in the window (client x, y of its centre), or None."""
    tw, th, tpl = _ANCHORS[name]
    cap = capture_band(hwnd, tw + 2 * _ANCHOR_SLACK)
    if not cap:
        return None
    band, x0, bw, bh = cap
    if bw < tw or bh < th:
        return None
    rows = [tpl[k * tw:(k + 1) * tw] for k in range(th)]
    mid = th // 2
    row_limit, total_limit = max_mean * tw, max_mean * tw * th
    sub = operator.sub
    for y in range(bh - th + 1):
        base = (y + mid) * bw
        for x in range(bw - tw + 1):
            # cheap test on the middle row first, then the whole button
            if sum(map(abs, map(sub, band[base + x:base + x + tw], rows[mid]))) > row_limit:
                continue
            total = 0
            for k in range(th):
                o = (y + k) * bw + x
                total += sum(map(abs, map(sub, band[o:o + tw], rows[k])))
                if total > total_limit:
                    break
            else:
                return x0 + x + tw // 2, y + th // 2
    return None


def wait_for_anchor(hwnd_of, name, alive, timeout, interval=0.8):
    """Wait until a screen's button shows up. `hwnd_of()` gives the current window, `alive()`
    says whether to keep waiting. Returns the button position, or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not alive():
            return None
        hwnd = hwnd_of()
        pos = find_anchor(hwnd, name) if hwnd else None
        if pos:
            return pos
        time.sleep(interval)
    return None


# ---------------------------------------------------------------- CP / HP / MP bars
# Measured on the status window (top left by default; its position is in system/WindowsInfo.ini).
# Offsets inside that window: bars from x 20 to 173, middle rows of CP / HP / MP, and the dark
# lines between the bars (to make sure the window is really there).
_BAR_X0, _BAR_X1 = 20, 173
_BAR_ROWS = {"cp": 29, "hp": 43, "mp": 56}
_BAR_GAPS = (34, 47, 60)
_status_pos = {"mtime": None, "pos": (0, 0)}


def status_window_pos():
    path = GAME_DIR / "WindowsInfo.ini"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0, 0
    if mtime != _status_pos["mtime"]:
        _status_pos["mtime"] = mtime
        try:
            text = path.read_text(encoding="latin-1")
            sec = re.search(r"\[StatusWnd\]([^\[]*)", text)
            x = re.search(r"posX=(-?\d+)", sec.group(1)) if sec else None
            y = re.search(r"posY=(-?\d+)", sec.group(1)) if sec else None
            _status_pos["pos"] = (int(x.group(1)) if x else 0, int(y.group(1)) if y else 0)
        except (OSError, AttributeError, ValueError):
            _status_pos["pos"] = (0, 0)
    return _status_pos["pos"]


def capture_rect(hwnd, x, y, w, h):
    """BGRX bytes of a rectangle of a window's client area (None when minimized)."""
    if user32.IsIconic(hwnd):
        return None
    src = user32.GetDC(hwnd)
    if not src:
        return None
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.BitBlt(mem, 0, 0, w, h, src, x, y, 0x00CC0020)
        buf = ctypes.create_string_buffer(w * h * 4)
        bi = _BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        return buf.raw if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0) else None
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, src)


def read_bars(hwnd):
    """{"cp", "hp", "mp"} in percent, or None if the status window is not where expected
    (moved, interface hidden, loading screen...)."""
    px, py = status_window_pos()
    w, h = _BAR_X1 + 4, max(_BAR_GAPS) + 2
    raw = capture_rect(hwnd, px, py, w, h)
    if not raw:
        return None

    def px_at(x, y):
        o = (y * w + x) * 4
        return raw[o + 2], raw[o + 1], raw[o]  # r, g, b

    # the dark lines between the bars must be there along the whole width
    for y in _BAR_GAPS:
        if any(max(px_at(x, y)) > 0x48 for x in range(_BAR_X0, _BAR_X1, 6)):
            return None
    out = {}
    for name, y in _BAR_ROWS.items():
        last = _BAR_X0 - 1
        for x in range(_BAR_X0, _BAR_X1):
            r, g, b = px_at(x, y)
            filled = (r > b + 50) if name != "mp" else (b > r + 50)
            if filled:
                last = x
        out[name] = round(100 * (last + 1 - _BAR_X0) / (_BAR_X1 - _BAR_X0))
    return out


WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON = 0x0201, 0x0202, 0x0001


def post_click(hwnd, x, y):
    lp = (int(y) << 16) | (int(x) & 0xFFFF)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.06)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def launch_l2():
    """Start l2.exe and return its PID. l2.exe requires administrator rights: if the
    panel is not elevated, Windows shows a UAC prompt for every client."""
    if is_admin():
        return subprocess.Popen([str(L2_EXE)], cwd=str(GAME_DIR)).pid
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(L2_EXE)
    info.lpDirectory = str(GAME_DIR)
    info.nShow = 1
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError()
    pid = kernel32.GetProcessId(info.hProcess)
    kernel32.CloseHandle(info.hProcess)
    return pid


def run_hidden(args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW)


# ---------------------------------------------------------------- data
def load_data():
    default = {"slots": [f"Account {i + 1}" for i in range(MAX_SLOTS)], "votes": {},
               "timers": [], "topmost": True, "pos": None, "compact": False,
               "rename_windows": True, "to_charselect": True, "opacity": 1.0, "size": None, "scale": 1.0, "accounts": {}, "login_delay": 15,
               "bosses": {}, "lang": "pt",
               "boss_view": {"sort": "respawn", "range": "", "min": "", "max": ""}}
    try:
        default.update(json.loads(DATA_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    default["slots"] = [str(s)[:20] for s in default["slots"]][:SLOT_CAP] or ["Account 1"]
    return default


# ---------------------------------------------------------------- boss database (client files)
BOSS_DB_FILE = APP_DIR / "bosses_db.json"
_L2_MOD = int("75b4d6de5c016544068a1acf125869f43d2e09fc55b8b1e289556daf9b8757635593446288b3653da1ce91c87bb1a5c18f16323495c55d7d72c0890a83f69bfd1fd9434eb1c02f3e4679edfa43309319070129c267c85604d87bb65bae205de3707af1d2108881abb567c3b3d069ae67c3a4c6a3aa93d26413d4c66094ae2039", 16)


def l2_decrypt413(path):
    """Decrypt a Lineage2Ver413 .dat/.ini file (the 'modified' public key used by this client)."""
    import zlib
    body = Path(path).read_bytes()[28:]
    out = bytearray()
    for i in range(0, len(body) - 127, 128):
        m = pow(int.from_bytes(body[i:i + 128], "big"), 0x1D, _L2_MOD).to_bytes(128, "big")
        size = m[3]
        pos = 128 - size - ((124 - size) % 4)
        out += m[pos:pos + size]
    return zlib.decompress(bytes(out[4:]))


class _DatReader:
    def __init__(self, data):
        self.d, self.p = data, 0

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        return v

    def ascf(self):
        b = self.d[self.p]
        self.p += 1
        neg, v = b & 0x80, b & 0x3F
        if b & 0x40:
            sh = 6
            while True:
                b = self.d[self.p]
                self.p += 1
                v |= (b & 0x7F) << sh
                sh += 7
                if not b & 0x80:
                    break
        if not neg:
            s = self.d[self.p:self.p + v].rstrip(b"\0").decode("latin1")
            self.p += v
        else:
            s = self.d[self.p:self.p + 2 * v].decode("utf-16le", "replace").rstrip("\0")
            self.p += 2 * v
        return s


def build_boss_db():
    """Level, coordinates and lore (raiddata-e.dat) + nearest hunting zone (HuntingZone-e.dat)."""
    raid_path, zone_path = GAME_DIR / "raiddata-e.dat", GAME_DIR / "HuntingZone-e.dat"
    if not (raid_path.exists() and zone_path.exists()):
        log_error("info", "game data files not found next to the panel: boss details disabled "
                          "(is the L2AxisControl folder inside the game folder?)")
        return {}
    stamp = f"{raid_path.stat().st_mtime}:{zone_path.stat().st_mtime}"
    try:
        cached = json.loads(BOSS_DB_FILE.read_text(encoding="utf-8"))
        if cached.get("stamp") == stamp:
            return cached["bosses"]
    except (OSError, ValueError, KeyError):
        pass

    r = _DatReader(l2_decrypt413(zone_path))
    zones = []
    for _ in range(r.u32()):
        zid, _typ, lvl, _unk = r.u32(), r.u32(), r.u32(), r.u32()
        x, y, z = r.f32(), r.f32(), r.f32()
        r.ascf()
        r.u32()
        name = r.ascf()
        if x or y:
            zones.append((x, y, name, lvl))

    r = _DatReader(l2_decrypt413(raid_path))
    bosses = {}
    for _ in range(r.u32()):
        _rid, npc, lvl, _zone = r.u32(), r.u32(), r.u32(), r.u32()
        x, y, z = r.f32(), r.f32(), r.f32()
        desc = r.ascf()
        if str(npc) in bosses:
            continue
        near = min(zones, key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2) if zones and (x or y) else None
        bosses[str(npc)] = {"level": lvl, "x": round(x), "y": round(y), "z": round(z), "desc": desc.strip(),
                            "zone": near[2] if near else "", "zoneLevel": near[3] if near else 0}
    BOSS_DB_FILE.write_text(json.dumps({"stamp": stamp, "bosses": bosses}, ensure_ascii=False), encoding="utf-8")
    return bosses

# ---------------------------------------------------------------- recipe database (client files)
RECIPE_DB_FILE = APP_DIR / "recipes_db.json"
# top-tier families only (the player asked for Dynasty and above)
CRAFT_FAMILIES = {"Dynasty": "S80", "Icarus": "S80", "Moirai": "S80", "Vesper": "S84"}
_JEWELRY = ("Earring", "Necklace", "Ring")


def _parse_recipes(data):
    """Recipe-c.dat: name ascf, id, recipe item, Create Item level, product, count, MP,
    success rate, material count, flag, then (item id, count) per material."""
    r = _DatReader(data)
    recipes = []
    for _ in range(r.u32()):
        r.ascf()
        rid, recipe_item, level, product, count, mp, rate, nmat, _flag = (r.u32() for _ in range(9))
        mats = [(r.u32(), r.u32()) for _ in range(nmat)]
        recipes.append({"id": rid, "recipeItem": recipe_item, "level": level, "product": product,
                        "count": count, "mp": mp, "rate": rate, "mats": mats})
    return recipes


def _item_names(data, wanted):
    """itemname-e.dat: id u32 + name/additional name (u32 byte length + UTF-16) + description ascf.
    The record tail has variable lists, so each wanted id is located directly and validated."""
    def ustr(p):
        ln = struct.unpack_from("<I", data, p)[0]
        if ln > 2000 or ln % 2:
            return None, p
        try:
            return data[p + 4:p + 4 + ln].decode("utf-16le"), p + 4 + ln
        except UnicodeDecodeError:
            return None, p

    def record_at(p, strict):
        name, q = ustr(p + 4)
        if not name or any(ord(c) < 32 for c in name):
            return None
        add, q = ustr(q)
        if add is None or any(ord(c) < 32 for c in add):
            return None
        if strict:
            r = _DatReader(data)
            r.p = q
            r.ascf()
            if r.p + 4 > len(data):
                return None
            popup = struct.unpack_from("<i", data, r.p)[0]
            if popup != -1 and not 0 <= popup < 1000:
                return None
        return name.replace("\xa0", " ").strip()

    names = {}
    for item_id in wanted:
        pattern = struct.pack("<I", item_id)
        for strict in (True, False):  # strict first; a few records only pass the relaxed check
            k = data.find(pattern)
            while k >= 0 and item_id not in names:
                found = record_at(k, strict)
                if found:
                    names[item_id] = found
                k = data.find(pattern, k + 1)
            if item_id in names:
                break
    return names


def build_recipe_db():
    """All recipes with item names; cached until the client files change."""
    recipe_path, names_path = GAME_DIR / "Recipe-c.dat", GAME_DIR / "itemname-e.dat"
    if not (recipe_path.exists() and names_path.exists()):
        log_error("info", "recipe/item data files not found next to the panel: Craft tab disabled")
        return None
    stamp = f"{recipe_path.stat().st_mtime}:{names_path.stat().st_mtime}"
    try:
        cached = json.loads(RECIPE_DB_FILE.read_text(encoding="utf-8"))
        if cached.get("stamp") == stamp:
            return cached
    except (OSError, ValueError):
        pass
    recipes = _parse_recipes(l2_decrypt413(recipe_path))
    wanted = {r["product"] for r in recipes} | {r["recipeItem"] for r in recipes} | \
             {m for r in recipes for m, _ in r["mats"]}
    names = _item_names(l2_decrypt413(names_path), wanted)
    db = {"stamp": stamp, "recipes": recipes, "names": {str(k): v for k, v in names.items()}}
    RECIPE_DB_FILE.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
    return db


# ---------------------------------------------------------------- item stats (weapongrp / armorgrp)
STATS_DB_FILE = APP_DIR / "stats_db.json"
_EQUIP_SOUND = "ItemSound.itemequip".encode("utf-16le")
# armor/jewelry slot, recognised from the item name (order matters: "Leather Breastplate" before "Breastplate")
_SLOTS = [("Earring", "earring"), ("Necklace", "necklace"), ("Ring", "ring"), ("Shield", "shield"),
          ("Sigil", "sigil"), ("Circlet", "helmet"), ("Helmet", "helmet"), ("Gloves", "gloves"),
          ("Gauntlet", "gloves"), ("Glove", "gloves"), ("Boots", "boots"), ("Shoes", "boots"),
          ("Gaiter", "legs"), ("Legging", "legs"), ("Stockings", "legs"), ("Tunic", "chest"),
          ("Breastplate", "chest"), ("Breast Plate", "chest"), ("Armor", "chest"), ("Robe", "chest")]


def _grp_records(data, tag):
    """Records start with tag u32 + id u32, then 20 bytes and the model path (ustr).
    Yields (start, end, item id); the first header for an id is the real one."""
    pattern = re.compile(re.escape(struct.pack("<I", tag)) + rb"(.{4}).{20}(.{4})((?:[ -~]\x00){4,24})", re.S)
    heads = []
    for m in pattern.finditer(data):
        item_id = struct.unpack_from("<I", m.group(1))[0]
        ln = struct.unpack_from("<I", m.group(2))[0]
        if not (0 < item_id < 1_000_000 and 8 <= ln <= 400 and ln % 2 == 0):
            continue
        if m.group(3).decode("utf-16le").startswith(("LineageEffect", "ItemSound")):
            continue  # effect/sound paths inside a record, not a record start
        heads.append((m.start(), item_id))
    seen = set()
    for (start, item_id), (nxt, _) in zip(heads, heads[1:] + [(len(data), 0)]):
        if item_id not in seen:
            seen.add(item_id)
            yield start, nxt, item_id


def _scan_grp(data, tag, nints):
    """Stats follow the equip sound string inside each record."""
    records = {}
    for start, nxt, item_id in _grp_records(data, tag):
        a = data.find(_EQUIP_SOUND, start, nxt)
        if a < 0:
            continue
        ln = struct.unpack_from("<I", data, a - 4)[0]
        if a + ln + 4 * nints <= len(data):
            records[item_id] = struct.unpack_from(f"<{nints}i", data, a + ln)
    return records


def _item_names_full(data, wanted):
    """Like _item_names, but returns (name, additional name) to tell SA/PvP variants apart."""
    names = {}
    for item_id in wanted:
        pattern = struct.pack("<I", item_id)
        k = data.find(pattern)
        while k >= 0:
            ln = struct.unpack_from("<I", data, k + 4)[0]
            if ln and ln <= 300 and ln % 2 == 0:
                try:
                    name = data[k + 8:k + 8 + ln].decode("utf-16le")
                    al = struct.unpack_from("<I", data, k + 8 + ln)[0]
                    if al <= 300 and al % 2 == 0 and not any(ord(c) < 32 for c in name):
                        add = data[k + 12 + ln:k + 12 + ln + al].decode("utf-16le")
                        names[item_id] = (name.replace("\xa0", " ").strip(), add.replace("\xa0", " ").strip())
                        break
                except (UnicodeDecodeError, struct.error):
                    pass
            k = data.find(pattern, k + 1)
    return names


def build_stats_db():
    """Base stats of S-grade-and-above weapons and armor, with names; cached until files change."""
    paths = [GAME_DIR / f for f in ("weapongrp.dat", "armorgrp.dat", "itemname-e.dat")]
    if not all(p.exists() for p in paths):
        log_error("info", "weapon/armor data files not found: item comparison disabled")
        return None
    stamp = ":".join(str(p.stat().st_mtime) for p in paths)
    try:
        cached = json.loads(STATS_DB_FILE.read_text(encoding="utf-8"))
        if cached.get("stamp") == stamp:
            return cached
    except (OSError, ValueError):
        pass
    weapons = _scan_grp(l2_decrypt413(paths[0]), 0, 16)
    armors = _scan_grp(l2_decrypt413(paths[1]), 1, 9)
    items = {}
    for item_id, v in weapons.items():
        if v[5] >= 5:   # S grade and above
            items[item_id] = {"kind": "weapon", "grade": v[5], "wtype": v[4], "patk": v[2], "matk": v[3],
                              "crit": v[6], "speed": v[11]}
    for item_id, v in armors.items():
        if v[3] >= 3 and (v[5] or v[6]):   # B grade and above (boss jewelry starts at B)
            items[item_id] = {"kind": "armor", "grade": v[3], "atype": v[2], "pdef": v[5], "mdef": v[6], "mp": v[7]}
    names = _item_names_full(l2_decrypt413(paths[2]), items)
    for item_id in list(items):
        if item_id not in names:
            del items[item_id]
            continue
        items[item_id]["name"], items[item_id]["add"] = names[item_id]
    db = {"stamp": stamp, "items": {str(k): v for k, v in items.items()}}
    STATS_DB_FILE.write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
    return db


# real body part of each armor piece (from data/items_db.json); filled when the panel starts
_BODYPARTS = {}
_BODYPART_SLOT = {"chest": "chest", "onepiece": "fullbody", "alldress": "fullbody", "legs": "legs",
                  "head": "helmet", "gloves": "gloves", "feet": "boots", "neck": "necklace",
                  "rear;lear": "earring", "rfinger;lfinger": "ring"}


def item_slot(name, item_id=None):
    """Equipment slot: the item's real body part when known, otherwise guessed from the name."""
    part = _BODYPARTS.get(str(item_id)) if item_id is not None else None
    if part == "lhand":
        return "sigil" if "Sigil" in name else "shield"
    if part in _BODYPART_SLOT:
        return _BODYPART_SLOT[part]
    slot = next((slot for word, slot in _SLOTS if word in name), None)
    # unknown item: one-piece armors/robes usually say "Armor"/"Robe" without "Breastplate"
    if slot == "chest" and ("Armor" in name or "Robe" in name) and "Breastplate" not in name:
        return "fullbody"
    return slot


def top_tier_item(it):
    """Dynasty and above: the client files Dynasty as plain S grade like older sets, so the name decides;
    boss jewelry and hero (Infinity) weapons are end game too."""
    if it["grade"] >= 6 or "Dynasty" in it["name"]:
        return True
    if it["kind"] == "weapon":
        return it["name"].startswith("Infinity ")
    return bool(_BOSS_JEWELS.search(it["name"]))


# pet gear and duplicate "Shadow/Common Item" copies are not useful in a comparison
_COMPARE_SKIP = re.compile(r"^(Shadow Item|Common Item)|\{PvP\}|Fenrir|Wolf|Hatchling|Strider|Wyvern|for NPC|^Transparent|Not In Use")


# ---------------------------------------------------------------- item icons (the user's own client)
# Icons are NCSoft art: they are read from the player's SysTextures folder and cached in icons/
# (gitignored), never shipped with the panel.
TEXTURE_DIR = APP_DIR.parent / "SysTextures"
ICON_DIR = APP_DIR / "icons"
_ICON_REF = re.compile(rb"((?:[A-Za-z0-9_]\x00){3,40})\.\x00((?:[A-Za-z0-9_]\x00){3,80})")


def _read_ver121(path):
    """Lineage2Ver121 files: 28-byte header, then XOR with a key made from the file name."""
    data = path.read_bytes()
    if data[:28].decode("utf-16le", "replace") != "Lineage2Ver121":
        return data
    key = sum(map(ord, path.name.lower())) & 0xFF
    return data[28:].translate(bytes(b ^ key for b in range(256)))


class _UPackage:
    """Just enough of an Unreal Engine 2 package (v123) to pull textures out."""

    def __init__(self, data):
        self.d = data
        _tag, _ver, _lic, _flags, nc, no, ec, eo, _ic, _io = struct.unpack_from("<IHHIIIIIII", data, 0)
        p, self.names = no, []
        for _ in range(nc):
            ln, p = self._ci(p)
            if ln < 0:  # UTF-16 name (some icons have Korean names)
                self.names.append(data[p:p - 2 * ln - 2].decode("utf-16le", "replace"))
                p += -2 * ln + 4
            else:
                self.names.append(data[p:p + ln - 1].decode("latin1"))
                p += ln + 4
        p, self.exports = eo, {}
        for _ in range(ec):
            _cls, p = self._ci(p)
            _sup, p = self._ci(p)
            p += 4
            name, p = self._ci(p)
            p += 4
            size, p = self._ci(p)
            off = 0
            if size > 0:
                off, p = self._ci(p)
            self.exports.setdefault(self.names[name].lower(), (off, size))

    def _ci(self, p):
        """Unreal compact index."""
        b = self.d[p]
        p += 1
        v = b & 0x3F
        if b & 0x40:
            shift = 6
            while True:
                c = self.d[p]
                p += 1
                v |= (c & 0x7F) << shift
                shift += 7
                if not c & 0x80:
                    break
        return (-v if b & 0x80 else v), p

    def _props(self, p):
        """Tagged properties until 'None' -> (int/byte values, position after)."""
        props = {}
        while True:
            idx, p = self._ci(p)
            name = self.names[idx]
            if name == "None":
                return props, p
            info = self.d[p]
            p += 1
            ptype, size_code, is_array = info & 0x0F, (info >> 4) & 0x07, info & 0x80
            if ptype == 10:  # struct name
                _, p = self._ci(p)
            size = {0: 1, 1: 2, 2: 4, 3: 12, 4: 16}.get(size_code)
            if size_code == 5:
                size, p = self.d[p], p + 1
            elif size_code == 6:
                size, p = struct.unpack_from("<H", self.d, p)[0], p + 2
            elif size_code == 7:
                size, p = struct.unpack_from("<I", self.d, p)[0], p + 4
            if is_array and ptype != 3:
                b = self.d[p]
                p += 1
                if b & 0x80:
                    p += 1 if not b & 0x40 else 3
            if ptype == 3:  # bool lives in the array bit
                continue
            raw = self.d[p:p + size]
            p += size
            if ptype == 1:
                props[name] = raw[0]
            elif ptype == 2:
                props[name] = struct.unpack_from("<i", raw)[0]

    def texture(self, name):
        """(format, width, height, first mip bytes) or None.
        L2 keeps extra data between the properties and the mips, so the mip array is found by its
        self-checking layout: count, absolute end offset (int32), size == size the format needs."""
        found = self.exports.get(name.lower())
        if not found or found[1] <= 0:
            return None
        off, size = found
        props, p = self._props(off)
        w, h, fmt = props.get("USize", 0), props.get("VSize", 0), props.get("Format", 0)
        if not (w and h) or fmt not in (3, 5, 7, 8):  # DXT1, RGBA8, DXT3, DXT5
            return None
        expected = w * h * 4 if fmt == 5 else (max(4, w) * max(4, h)) // (2 if fmt == 3 else 1)
        for q in range(p, off + size - 8):
            count, q2 = self._ci(q)
            if not 1 <= count <= 16:
                continue
            nxt = struct.unpack_from("<i", self.d, q2)[0]
            length, q3 = self._ci(q2 + 4)
            if length == expected and nxt == q3 + length:
                return fmt, w, h, self.d[q3:q3 + length]
        return None


def _rgb565(c):
    return ((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31


def _decode_texture(fmt, w, h, data):
    """DXT1 / DXT3 / DXT5 / BGRA8 -> RGBA bytes."""
    if fmt == 5:
        px = bytearray(data)
        px[0::4], px[2::4] = data[2::4], data[0::4]
        return bytes(px)
    out = bytearray(w * h * 4)
    block = 8 if fmt == 3 else 16
    pos = 0
    for by in range(0, h, 4):
        for bx in range(0, w, 4):
            alpha = None
            if fmt == 7:  # explicit 4-bit alpha
                bits = int.from_bytes(data[pos:pos + 8], "little")
                alpha = [((bits >> (4 * i)) & 15) * 17 for i in range(16)]
            elif fmt == 8:  # interpolated alpha
                a0, a1 = data[pos], data[pos + 1]
                if a0 > a1:
                    table = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
                else:
                    table = [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]
                bits = int.from_bytes(data[pos + 2:pos + 8], "little")
                alpha = [table[(bits >> (3 * i)) & 7] for i in range(16)]
            cpos = pos + (8 if fmt in (7, 8) else 0)
            c0, c1, bits = struct.unpack_from("<HHI", data, cpos)
            p0, p1 = _rgb565(c0), _rgb565(c1)
            if c0 > c1 or fmt != 3:
                colors = [p0 + (255,), p1 + (255,),
                          tuple((2 * a + b) // 3 for a, b in zip(p0, p1)) + (255,),
                          tuple((a + 2 * b) // 3 for a, b in zip(p0, p1)) + (255,)]
            else:
                colors = [p0 + (255,), p1 + (255,),
                          tuple((a + b) // 2 for a, b in zip(p0, p1)) + (255,), (0, 0, 0, 0)]
            for i in range(16):
                x, y = bx + (i & 3), by + (i >> 2)
                if x < w and y < h:
                    r, g, b, a = colors[(bits >> (2 * i)) & 3]
                    o = (y * w + x) * 4
                    out[o:o + 4] = bytes((r, g, b, alpha[i] if alpha else a))
            pos += block
    return bytes(out)


def _png(w, h, rgba):
    import zlib

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    rows = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


class IconStore:
    """item id -> PNG, extracted on first use from the client's icon packages and cached on disk."""

    def __init__(self):
        self._lock = threading.Lock()
        self._map = None
        self._packages = {}

    def _icon_map(self):
        if self._map is not None:
            return self._map
        paths = [GAME_DIR / f for f in ("weapongrp.dat", "armorgrp.dat", "etcitemgrp.dat")]
        self._map = {}
        if not all(p.exists() for p in paths):
            return self._map
        stamp = ":".join(str(p.stat().st_mtime) for p in paths)
        cache = ICON_DIR / "map.json"
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("stamp") == stamp:
                self._map = {int(k): v for k, v in cached["icons"].items()}
                return self._map
        except (OSError, ValueError):
            pass
        for path, tag in zip(paths, (0, 1, 2)):
            data = l2_decrypt413(path)
            for start, nxt, item_id in _grp_records(data, tag):
                for m in _ICON_REF.finditer(data, start, nxt):
                    package = m.group(1).decode("utf-16le")
                    if "icon" in package.lower():
                        self._map.setdefault(item_id, f"{package}.{m.group(2).decode('utf-16le')}")
                        break
        ICON_DIR.mkdir(exist_ok=True)
        cache.write_text(json.dumps({"stamp": stamp, "icons": self._map}), encoding="utf-8")
        return self._map

    def _package(self, name):
        if name not in self._packages:
            path = TEXTURE_DIR / f"{name}.utx"
            try:
                self._packages[name] = _UPackage(_read_ver121(path)) if path.exists() else None
            except Exception:
                log_error("icons", f"{name}.utx: {traceback.format_exc()}")
                self._packages[name] = None
        return self._packages[name]

    def png(self, item_id):
        cached = ICON_DIR / f"{item_id}.png"
        if cached.exists():
            return cached.read_bytes()
        with self._lock:
            ref = self._icon_map().get(item_id)
            if not ref:
                return None
            package_name, texture_name = ref.split(".", 1)
            package = self._package(package_name)
            tex = None
            if package:  # a few grp records carry a stray character after the icon name
                tex = package.texture(texture_name) or package.texture(texture_name[:-1])
            if not tex:
                return None
            fmt, w, h, data = tex
            png = _png(w, h, _decode_texture(fmt, w, h, data))
            ICON_DIR.mkdir(exist_ok=True)
            cached.write_bytes(png)
            return png


# ---------------------------------------------------------------- drop / exchange database (shipped)
# Generated once by tools/build_drops_db.py from the L2J High Five datapack (GPL-3.0, see data/NOTICE.md).
DROPS_DB_FILE = APP_DIR / "data" / "drops_db.json"


class DropBook:
    def __init__(self):
        try:
            self.db = json.loads(DROPS_DB_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log_error("info", "data/drops_db.json not found: drop sources disabled")
            self.db = None

    def sources(self, item_id, names):
        """Mobs that drop/spoil the item and NPC exchanges that give or upgrade it."""
        if not self.db:
            return None
        key = str(int(item_id))
        npcs, extra = self.db["npcs"], self.db["names"]

        def name(i):
            return names.get(str(i)) or extra.get(str(i)) or str(i)

        def mobs(kind):
            out = []
            for npc_id, chance, lo, hi in self.db["items"].get(key, {}).get(kind, []):
                npc = npcs[str(npc_id)]
                out.append({"npc": npc["name"], "level": npc["level"], "area": npc.get("area"),
                            "boss": npc["type"] in ("L2RaidBoss", "L2GrandBoss"),
                            "chance": chance, "min": lo, "max": hi})
            return out

        def deals(kind):
            return [{"npc": d["npc"], "productId": d["product"], "product": name(d["product"]),
                     "cost": [{"id": i, "name": name(i), "count": c} for i, c in d["cost"]]}
                    for d in self.db["shops"].get(key, {}).get(kind, [])]
        return {"name": name(key), "drop": mobs("drop"), "spoil": mobs("spoil"), "buy": deals("buy"),
                "into": deals("into")}


# ---------------------------------------------------------------- passive bonuses + masterwork (shipped)
# Generated once by tools/build_items_db.py from the L2J High Five datapack (GPL-3.0, see data/NOTICE.md).
ITEMS_DB_FILE = APP_DIR / "data" / "items_db.json"
# assumptions for the damage estimate: soulshots on, DEX bonus of a typical fighter, no buffs
_SS = 2.0
_DEX_BONUS = 1.2
_MAX_CRIT = 500


def _load_items_db():
    try:
        db = json.loads(ITEMS_DB_FILE.read_text(encoding="utf-8"))
        _BODYPARTS.update(db.get("bodyparts", {}))
        return db
    except (OSError, ValueError):
        log_error("info", "data/items_db.json not found: masterwork bonuses disabled")
        return {"bonus": {}, "masterwork": {}, "names": {}}


def weapon_final(it, effects):
    """Final weapon numbers with the item's passive skill, and an expected damage per second index.
    Normal hit ~ P.Atk x soulshot; a critical hit is 2 x crit damage x that, plus the flat critical
    damage bonus (the way the High Five damage formula adds critDmgAdd)."""
    patk, speed, crit = float(it["patk"]), float(it["speed"]), it["crit"] * 10 * _DEX_BONUS
    crit_mul, crit_add = 1.0, 0.0
    for op, stat, val in effects:
        if stat == "pAtk":
            patk = patk * val if op == "mul" else patk + val
        elif stat == "pAtkSpd":
            speed = speed * val if op == "mul" else speed + val
        elif stat == "critRate":
            crit = crit * val if op == "mul" else crit + val
        elif stat == "critDmg":
            crit_mul *= val if op == "mul" else 1 + val / 100
        elif stat == "critDmgAdd":
            crit_add += val
    chance = min(crit, _MAX_CRIT) / 1000
    hit = patk * _SS
    per_hit = (1 - chance) * hit + chance * (2 * crit_mul * hit + crit_add)
    return {"patk": round(patk), "speed": round(speed), "critStatus": round(min(crit, _MAX_CRIT)),
            "critAdd": round(crit_add), "dps": per_hit * speed}


# ---------------------------------------------------------------- character classes (High Five 3rd classes)
# client weapon type codes (weapongrp): 1 sword, 2 blunt, 3 dagger, 4 pole, 5 fist, 6 bow, 8 dual swords,
# 11 rapier, 12 crossbow, 13 ancient sword, 15 dual daggers, 0 shield. Speed 325 = two-handed.
_WTYPES = {3: "dagger", 4: "pole", 5: "fist", 6: "bow", 8: "dual", 11: "rapier", 12: "crossbow",
           13: "ancient", 15: "dualdagger", 0: "shield"}
MAGIC = ("magic1h", "magic2h")
_MAGE = (MAGIC, ("robe",), "sigil")
# class: (race, role, weapon categories, armor kinds, off-hand)
CHAR_CLASSES = {
    "Duelist": ("human", "fighter", ("dual",), ("heavy",), None),
    "Dreadnought": ("human", "fighter", ("pole",), ("heavy",), None),
    "Phoenix Knight": ("human", "tank", ("sword1h", "blunt1h"), ("heavy",), "shield"),
    "Hell Knight": ("human", "tank", ("sword1h", "blunt1h"), ("heavy",), "shield"),
    "Sagittarius": ("human", "archer", ("bow",), ("light",), None),
    "Adventurer": ("human", "dagger", ("dagger", "dualdagger"), ("light",), None),
    "Archmage": ("human", "mage") + _MAGE,
    "Soultaker": ("human", "mage") + _MAGE,
    "Arcana Lord": ("human", "summoner") + _MAGE,
    "Cardinal": ("human", "healer") + _MAGE,
    "Hierophant": ("human", "buffer") + _MAGE,
    "Eva's Templar": ("elf", "tank", ("sword1h", "blunt1h"), ("heavy",), "shield"),
    "Sword Muse": ("elf", "buffer", ("sword1h",), ("heavy",), "shield"),
    "Wind Rider": ("elf", "dagger", ("dagger", "dualdagger"), ("light",), None),
    "Moonlight Sentinel": ("elf", "archer", ("bow",), ("light",), None),
    "Mystic Muse": ("elf", "mage") + _MAGE,
    "Elemental Master": ("elf", "summoner") + _MAGE,
    "Eva's Saint": ("elf", "healer") + _MAGE,
    "Shillien Templar": ("darkelf", "tank", ("sword1h", "blunt1h"), ("heavy",), "shield"),
    "Spectral Dancer": ("darkelf", "buffer", ("dual",), ("heavy",), None),
    "Ghost Hunter": ("darkelf", "dagger", ("dagger", "dualdagger"), ("light",), None),
    "Ghost Sentinel": ("darkelf", "archer", ("bow",), ("light",), None),
    "Storm Screamer": ("darkelf", "mage") + _MAGE,
    "Spectral Master": ("darkelf", "summoner") + _MAGE,
    "Shillien Saint": ("darkelf", "healer") + _MAGE,
    "Titan": ("orc", "fighter", ("sword2h", "blunt2h"), ("heavy",), None),
    "Grand Khavatari": ("orc", "fighter", ("fist",), ("light", "heavy"), None),
    "Dominator": ("orc", "buffer") + _MAGE,
    "Doomcryer": ("orc", "buffer") + _MAGE,
    "Fortune Seeker": ("dwarf", "fighter", ("blunt1h", "blunt2h", "pole"), ("heavy",), None),
    "Maestro": ("dwarf", "fighter", ("blunt1h", "blunt2h"), ("heavy",), None),
    "Doombringer": ("kamael", "fighter", ("ancient",), ("light",), None),
    "Soul Hound": ("kamael", "mage", ("rapier",), ("light",), None),
    "Trickster": ("kamael", "archer", ("crossbow",), ("light",), None),
    "Judicator": ("kamael", "buffer", ("rapier", "crossbow", "ancient"), ("light",), None),
}
PLAN_SLOTS = ("weapon", "offhand", "helmet", "chest", "legs", "gloves", "boots", "necklace", "earring", "ring")


def weapon_cat(it):
    """sword1h, blunt2h, magic1h, bow... from the client's weapon type, speed and P.Atk/M.Atk ratio."""
    if it["wtype"] in _WTYPES:
        return _WTYPES[it["wtype"]]
    if it["wtype"] in (1, 2):
        hands = "2h" if it["speed"] <= 325 else "1h"
        if it["matk"] >= 0.6 * max(1, it["patk"]):
            return "magic" + hands
        return ("sword" if it["wtype"] == 1 else "blunt") + hands
    return None


def armor_kind(name):
    """heavy / light / robe / shield / sigil / jewelry, from the item name (helmets, gloves and boots
    carry no armor type in the client: the name tells which set they belong to)."""
    if any(j in name for j in _JEWELRY):
        return "jewelry"
    if "Sigil" in name:
        return "sigil"
    if "Shield" in name:
        return "shield"
    if "Leather" in name:
        return "light"
    if any(w in name for w in ("Tunic", "Stockings", "Circlet", "Gloves", "Shoes", "Robe")):
        return "robe"
    return "heavy"


def class_fits(cls, kind, cat):
    """Can this class use an item of this kind (weapon/armor/jewelry) and category?"""
    _race, _role, weapons, armors, offhand = CHAR_CLASSES[cls]
    if kind == "weapon":
        return cat in weapons or cat == offhand
    if kind == "jewelry" or cat == "jewelry":
        return True
    return cat in armors or cat == offhand


NPC_RECIPE_BASE = 1_000_000  # ids of the NPC "recipes" (dual weapons)
MW_RECIPE_BASE = 2_000_000  # masterwork: Blacksmith of Mammon varnish (and unseal)
FOUNDATION_RECIPE_BASE = 3_000_000  # the base recipe, counted until it gives the rare Foundation
OBTAIN_BASE = 4_000_000  # top gear without a recipe: where to get it
OBTAIN_FAMILIES = ("Dynasty", "Icarus", "Moirai", "Vesper", "Vorpal", "Elegia")
_OBTAIN_SKIP = re.compile(r"Royal Guard|Guard Earring|Cloak|^Sealed |^Foundation |\(Not In Use\)|Shadow|Transparent|"
                          r"for NPC|Master Yogi|Lizardspear|Peltast")
# boss jewelry is S grade or lower but still end game
_BOSS_JEWELS = re.compile(r"Antharas|Valakas|Baium|Queen Ant|Zaken|Frintezza|Freya|Beleth|Orfen|Core|Fafurion")


class RecipeBook:
    def __init__(self, db):
        self.accessories = {}
        self.recipes = {r["id"]: r for r in db["recipes"]}
        self.names = {int(k): v for k, v in db["names"].items()}
        # best recipe for each product: highest success rate, then fewest materials
        self.by_product = {}
        for r in db["recipes"]:
            cur = self.by_product.get(r["product"])
            if cur is None or (r["rate"], -len(r["mats"])) > (cur["rate"], -len(cur["mats"])):
                self.by_product[r["product"]] = r

    def name(self, item_id):
        return self.names.get(item_id, f"#{item_id}")

    def add_masterwork_recipes(self, extra):
        """Every masterwork gets its own entry: craft the base recipe until the rare Foundation
        comes out (chance = success rate x rarity), have the Blacksmith of Mammon varnish it and,
        for armor and jewelry, remove the seal. Expanding it shows the real average cost."""
        self.mw_recipes = {}
        for key, chain in (extra or {}).get("masterwork", {}).items():
            if chain.get("dual") or int(key) != chain["final"]:
                continue
            base = self.by_product.get(chain["product"])
            if not base or base.get("npc") or base.get("mw"):
                continue
            foundation, result, final = chain["foundation"], chain["result"], chain["final"]
            if foundation not in self.by_product:
                rare = {**base, "id": FOUNDATION_RECIPE_BASE + foundation, "product": foundation, "count": 1,
                        "rate": round(base["rate"] * chain["rarity"] / 100, 4),
                        "rare": {"rarity": chain["rarity"], "baseRate": base["rate"], "of": base["product"]}}
                self.recipes[rare["id"]] = rare
                self.by_product[foundation] = rare
            varnish = [tuple(v) for v in chain["varnish"]]
            mammon = [{"npc": "Blacksmith of Mammon", "cost": chain["varnish"][0]}] if varnish else []
            if result != final:  # armor/jewelry: varnish gives the sealed masterwork, then unseal it
                sealed = {"id": MW_RECIPE_BASE + result, "recipeItem": 0, "level": 0, "product": result, "count": 1,
                          "mp": 0, "rate": 100, "mats": [(foundation, 1)] + varnish, "npc": mammon, "mwStep": True}
                self.recipes[sealed["id"]] = sealed
                self.by_product.setdefault(result, sealed)
                mats = [(result, 1)] + [tuple(v) for v in chain.get("unseal", [])]
                npc = [{"npc": "Blacksmith of Mammon", "cost": chain["unseal"][0]}] if chain.get("unseal") else []
            else:
                mats, npc = [(foundation, 1)] + varnish, mammon
            recipe = {"id": MW_RECIPE_BASE + final, "recipeItem": 0, "level": 0, "product": final, "count": 1,
                      "mp": 0, "rate": 100, "mats": mats, "npc": npc,
                      "mw": {"rarity": chain["rarity"], "base": base["id"], "baseRate": base["rate"]}}
            self.recipes[recipe["id"]] = recipe
            self.by_product.setdefault(final, recipe)
            self.mw_recipes[final] = recipe["id"]

    def add_obtain_entries(self, stats_db, extra=None):
        """Top gear that has no recipe gets an entry too, so the guide can say how to get it."""
        if not stats_db:
            return
        masterwork = (extra or {}).get("masterwork", {})
        plain = lambda name: re.sub(r"s\b", "", name.lower())
        # items reached by crafting (directly or by unsealing a crafted item) are not "no recipe" items
        crafted_names = set()
        for pid, rec in self.by_product.items():
            name = self.name(pid)
            crafted_names.add(plain(name[7:] if name.startswith("Sealed ") else name))
        seen = set()
        sealed_of = {}
        for k, it in stats_db["items"].items():
            if it["name"].startswith("Sealed ") and not it["add"]:
                sealed_of.setdefault(re.sub(r"s\b", "", it["name"][7:].lower()), int(k))
        for k, it in stats_db["items"].items():
            item_id = int(k)
            if item_id in self.by_product or k in masterwork or it["add"] or _COMPARE_SKIP.search(it["name"]) \
                    or _OBTAIN_SKIP.search(it["name"]) or " - " in it["name"]:
                continue
            jewel = it["kind"] == "armor" and armor_kind(it["name"]) == "jewelry"
            hero = it["name"].startswith("Infinity ")  # Olympiad hero weapons are S grade
            if it["grade"] < 6 and not hero and not (jewel and _BOSS_JEWELS.search(it["name"])):
                continue
            # only real gear: a known weapon type, or an armor/jewelry slot
            if it["kind"] == "weapon":
                if weapon_cat(it) is None or (weapon_cat(it) != "shield" and it["patk"] < 200):
                    continue
            elif not item_slot(it["name"], item_id) or (jewel and it["mdef"] < 40) or (not jewel and not it["pdef"]
                                                                                and armor_kind(it["name"]) != "sigil"):
                continue
            key = plain(it["name"])
            if key in crafted_names or key in seen:
                continue  # craftable, or a second client copy of the same item
            seen.add(key)
            sealed = sealed_of.get(key)
            self.recipes[OBTAIN_BASE + item_id] = {
                "id": OBTAIN_BASE + item_id, "recipeItem": 0, "level": 0, "product": item_id, "count": 1,
                "mp": 0, "rate": 100, "mats": [], "obtain": {"sealed": sealed}}
            self.names.setdefault(item_id, it["name"])
            if sealed:
                self.names.setdefault(sealed, stats_db["items"][str(sealed)]["name"])

    def add_accessory_entries(self, extra, in_client=None):
        """Shirts, belts, bracelets, talismans, cloaks and hair accessories: no recipe, but they help.
        in_client: item ids the player's client knows (items missing there do not exist on L2Axis)."""
        junk = re.compile(r"limited period|not in use|common item|event|kratei", re.I)
        self.accessories, seen = {}, set()
        for k, v in (extra or {}).get("accessories", {}).items():
            name = self.names.get(int(k), v["name"])
            if (in_client is not None and int(k) not in in_client) or junk.search(name) or name in seen:
                continue  # missing from the client, junk, or a second copy of the same item
            seen.add(name)
            self.accessories[k] = v
        for k, acc in self.accessories.items():
            item_id = int(k)
            self.recipes[OBTAIN_BASE + item_id] = {
                "id": OBTAIN_BASE + item_id, "recipeItem": 0, "level": 0, "product": item_id, "count": 1,
                "mp": 0, "rate": 100, "mats": [], "obtain": {"sealed": None, "acc": acc["slot"]}}
            self.names.setdefault(item_id, acc["name"])

    def add_npc_recipes(self, extra):
        """Dual swords and dual daggers have no recipe: an NPC combines two weapons and a Dualsword
        Craft Stamp. They become recipes too, so the guide expands the two weapons' own recipes."""
        for k, v in (extra or {}).get("names", {}).items():
            self.names.setdefault(int(k), v)
        self.names.setdefault(57, "Adena")
        for combo in (extra or {}).get("combos", {}).values():
            rid = NPC_RECIPE_BASE + combo["product"]
            recipe = {"id": rid, "recipeItem": 0, "level": 0, "product": combo["product"], "count": 1, "mp": 0,
                      "rate": 100, "mats": [tuple(m) for m in combo["mats"]],
                      "npc": [{"npc": n["npc"], "cost": n["cost"]} for n in combo["npc"]]}
            self.recipes[rid] = recipe
            self.by_product.setdefault(combo["product"], recipe)

    def compare(self, recipe_id, stats_db, extra=None, limit=16):
        """Same-slot items to compare with the recipe's product (base stats from the client)."""
        recipe = self.recipes.get(int(recipe_id))
        if not recipe or not stats_db:
            return None
        items = {int(k): v for k, v in stats_db["items"].items()}
        me = items.get(recipe["product"])
        if not me:
            return None
        craftable = set(self.by_product)
        extra = extra or {}
        jewel = False
        if me["kind"] == "weapon":
            # same weapon type and attack speed (separates one-handed from two-handed)
            same = lambda it: it["kind"] == "weapon" and it["wtype"] == me["wtype"] and it["speed"] == me["speed"]
            key = lambda it: (it["patk"], it["matk"])
        else:
            slot = item_slot(me["name"], recipe["product"])
            jewel = slot in ("earring", "necklace", "ring")
            my_kind = armor_kind(me["name"])
            same = lambda it: (it["kind"] == "armor" and item_slot(it["name"], it["id"]) == slot
                               and (jewel or (it["atype"] == me["atype"] and armor_kind(it["name"]) == my_kind)))
            key = lambda it: (it["mdef"] if jewel else it["pdef"], it["mp"])
        bonus, masterwork = extra.get("bonus", {}), extra.get("masterwork", {})
        # masterwork versions of this recipe's product (sealed and unsealed for armor)
        my_mw = {int(k) for k, c in masterwork.items() if c["product"] == recipe["product"]}
        # the client is inconsistent with plurals ("Sealed Dynasty Gaiter" unseals into "Dynasty Gaiters")
        plain = lambda name: re.sub(r"s\b", "", name.lower())
        unsealed_name = plain(me["name"][len("Sealed "):]) if me["name"].startswith("Sealed ") else None
        # items made by crafting their sealed version and removing the seal
        unseal_craft = {plain(self.name(pid)[len("Sealed "):]) for pid, rec in self.by_product.items()
                        if self.name(pid).startswith("Sealed ") and not rec.get("mw") and not rec.get("mwStep")}
        rows, seen = [], set()
        for item_id, it in items.items():
            # skip Special Ability variants and {PvP} copies: same base stats as the plain item
            it = {**it, "id": item_id}
            if (it["add"] and item_id != recipe["product"]) or _COMPARE_SKIP.search(it["name"]) or not same(it) \
                    or (not top_tier_item(it) and item_id != recipe["product"]) \
                    or (it["name"].startswith("Sealed ") and item_id != recipe["product"]):
                continue
            mw = masterwork.get(str(item_id))
            label = it["name"] + (f" ({it['add']})" if it["add"] else "")
            if mw and " - " not in it["name"]:
                label += " (Masterwork)"  # armor masterworks keep the plain item's name
            if label in seen:
                continue
            seen.add(label)
            effects = [e for e in bonus.get(str(item_id), {}).get("effects", []) if not e[1].startswith("pvp")]
            row = {**it, "id": item_id, "label": label, "craft": item_id in craftable,
                   "me": item_id == recipe["product"], "mine": item_id in my_mw,
                   "mw": {"rarity": mw["rarity"], "foundation": self.name(mw["foundation"]) if
                          mw["foundation"] in self.names else extra["names"].get(str(mw["foundation"]), ""),
                          "varnish": mw["varnish"][0][1] if mw["varnish"] else 0} if mw else None,
                   "bonus": effects,
                   # the same item after the Blacksmith of Mammon removes the seal
                   "unsealed": bool(unsealed_name) and plain(it["name"]) == unsealed_name,
                   "unsealCraft": not mw and item_id not in craftable and plain(it["name"]) in unseal_craft}
            if me["kind"] == "weapon":
                row["final"] = weapon_final(it, effects)
            rows.append(row)
        # "Name - Special Ability" copies have the base item's stats: keep only the base item
        # (masterworks are named the same way but are real upgrades)
        bases = {r["name"] for r in rows}
        rows = [r for r in rows if r["me"] or r["mw"] or " - " not in r["name"] or r["name"].split(" - ")[0] not in bases]
        if me["kind"] == "weapon":
            base = next((r["final"]["dps"] for r in rows if r["me"]), 0) or 1
            for r in rows:
                r["final"]["index"] = round(r["final"]["dps"] / base * 100)
                del r["final"]["dps"]
            key = lambda r: (r["final"]["index"], r["matk"])
        else:
            hp = lambda r: sum(v for op, s, v in r["bonus"] if s == "maxHp")
            key = lambda r: (r["mdef"] if jewel else r["pdef"], hp(r), r["mp"])
        rows.sort(key=key, reverse=True)
        if len(rows) > limit:  # keep the product, its unsealed and masterwork versions visible
            pinned = lambda r: r["me"] or r["unsealed"] or r["mine"]
            keep = [r for r in rows if pinned(r)]
            top = [r for r in rows if not pinned(r)][:limit - len(keep)]
            rows = sorted(top + keep, key=key, reverse=True)
        return {"kind": me["kind"], "rows": rows}

    def char_plan(self, cls, stats_db, extra=None, per_slot=5):
        """Best items for a class, slot by slot, and how to get each one (craft, craft + unseal,
        masterwork, or drop/exchange)."""
        if cls not in CHAR_CLASSES or not stats_db:
            return None
        extra = extra or {}
        bonus, masterwork = extra.get("bonus", {}), extra.get("masterwork", {})
        role, weapons = CHAR_CLASSES[cls][1], CHAR_CLASSES[cls][2]
        mage = set(weapons) <= set(MAGIC)
        items = {int(k): v for k, v in stats_db["items"].items()}
        names = {}
        for k, v in items.items():
            names.setdefault(v["name"], []).append(k)
        crafted = set(self.by_product)
        by_unsealed = {}  # plain item -> the sealed item a recipe makes
        for pid in crafted:
            n = self.name(pid)
            if n.startswith("Sealed "):
                plain = re.sub(r"s\b", "", n[7:].lower())
                for name, ids in names.items():
                    if re.sub(r"s\b", "", name.lower()) == plain:
                        for item_id in ids:
                            by_unsealed.setdefault(item_id, pid)
        slots = {s: [] for s in PLAN_SLOTS}
        for item_id, it in items.items():
            if it["add"] or _COMPARE_SKIP.search(it["name"]) or it["name"].startswith("Sealed "):
                continue  # sealed items show up as their unsealed version, with the recipe
            mw = masterwork.get(str(item_id))
            if " - " in it["name"] and not mw:
                continue  # Special Ability copies
            if it["kind"] == "weapon":
                cat = weapon_cat(it)
                if cat == "shield":
                    slot = "offhand" if CHAR_CLASSES[cls][4] == "shield" else None
                elif cat in weapons:
                    slot = "weapon"
                else:
                    slot = None
                if not top_tier_item(it) and slot == "weapon":
                    slot = None  # S80 and above only
            else:
                cat = armor_kind(it["name"])
                if cat == "jewelry":
                    slot = item_slot(it["name"], item_id)
                elif cat == "sigil":
                    slot = "offhand" if CHAR_CLASSES[cls][4] == "sigil" else None
                else:
                    slot = item_slot(it["name"], item_id) if cat in CHAR_CLASSES[cls][3] and top_tier_item(it) else None
                if slot == "shield":
                    slot = None
            if slot not in slots:
                continue
            effects = [e for e in bonus.get(str(item_id), {}).get("effects", [])
                       if not e[1].startswith("pvp") and e[2] != (1 if e[0] == "mul" else 0)]
            if it["kind"] == "weapon" and cat != "shield":
                if mage:
                    m = it["matk"]
                    for op, stat, val in effects:
                        if stat == "mAtk":
                            m = m * val if op == "mul" else m + val
                    score = m
                else:
                    score = weapon_final(it, effects)["dps"]
            elif slot in ("necklace", "earring", "ring"):
                score = it["mdef"] + sum(v for op, s, v in effects if s == "maxMp") * .05
            elif slot == "offhand" and it["kind"] == "weapon":
                score = it["grade"] * 100 + it["patk"]  # shields: no defence value in the client data
            else:
                score = it["pdef"] + sum(v for op, s, v in effects if s == "maxHp") * .02
            # how to get it
            recipe = how = None
            base = mw["product"] if mw else None
            if item_id in getattr(self, "mw_recipes", {}):
                how, recipe = "mw", self.mw_recipes[item_id]
            elif item_id in crafted and not self.by_product[item_id].get("mw"):
                how, recipe = "craft", self.by_product[item_id]["id"]
            elif mw and base in crafted:
                how, recipe = "mw", self.by_product[base]["id"]
            elif item_id in by_unsealed:
                how, recipe = "unseal", self.by_product[by_unsealed[item_id]]["id"]
            else:
                how = "drop"
                if OBTAIN_BASE + item_id in self.recipes:
                    recipe = OBTAIN_BASE + item_id
            slots[slot].append({"id": item_id, "name": it["name"] + (" (Masterwork)" if mw and " - " not in it["name"] else ""),
                                "grade": it["grade"], "how": how, "recipe": recipe, "score": score,
                                "patk": it.get("patk"), "matk": it.get("matk"), "pdef": it.get("pdef"),
                                "mdef": it.get("mdef"), "bonus": effects,
                                "mw": {"rarity": mw["rarity"]} if mw else None})
        for rows in slots.values():
            by_name = {}
            for r in rows:
                by_name.setdefault(r["name"], []).append(r)
            for same in by_name.values():
                plain = [r for r in same if not r["bonus"]]
                for r in same:
                    if r["bonus"] and plain and not r["mw"]:
                        r["name"] += " (Masterwork)"
                        r["mw"] = {"rarity": 4.0}
                        if plain[0]["recipe"]:
                            r["how"], r["recipe"] = "mw", plain[0]["recipe"]
                # identical copies (same name, same bonus): keep one
                seen = set()
                for r in same:
                    key = (r["name"], str(r["bonus"]))
                    if key in seen:
                        rows.remove(r)
                    seen.add(key)
        out = []
        for slot in PLAN_SLOTS:
            rows = sorted(slots[slot], key=lambda r: -r["score"])
            if not rows:
                continue
            top = rows[0]["score"] or 1
            best_craft = next((r for r in rows if r["how"] != "drop"), None)
            shown = rows[:per_slot]
            if best_craft and best_craft not in shown:
                shown.append(best_craft)  # always show the best item the player can make
            for r in shown:
                r["rel"] = round(r["score"] / top * 100)
                del r["score"]
            out.append({"slot": slot, "rows": shown, "bestCraft": best_craft["id"] if best_craft else None})
        return {"cls": cls, "mage": mage, "role": role, "slots": out}

    def _power(self, item_id, stats_db, bonus):
        """How strong an item is for what it is meant to do, passive bonuses included:
        physical weapons = estimated damage per second, magic weapons = M.Atk, armor = P.Def (+HP),
        jewelry = M.Def. A sealed item counts as its unsealed version (that is what gets used)."""
        items = stats_db["items"]
        it = items.get(str(item_id))
        if not it:
            return None, None
        if it["name"].startswith("Sealed "):
            if not getattr(self, "_unsealed", None):
                self._unsealed = {}
                for k, v in items.items():  # plain item without a passive bonus (not the masterwork)
                    if not v["name"].startswith("Sealed ") and not v["add"] and "{PvP}" not in v["name"]:
                        key = re.sub(r"s\b", "", v["name"].lower())
                        if key not in self._unsealed or k not in bonus:
                            self._unsealed[key] = int(k)
            alt = self._unsealed.get(re.sub(r"s\b", "", it["name"][7:].lower()))
            if alt:
                item_id, it = alt, items[str(alt)]
        effects = [e for e in bonus.get(str(item_id), {}).get("effects", []) if not e[1].startswith("pvp")]
        if it["kind"] == "weapon":
            cat = weapon_cat(it)
            if cat == "shield":
                return it["grade"] * 100 + it["patk"], "offhand"
            if cat in MAGIC:
                m = float(it["matk"])
                for op, stat, val in effects:
                    if stat == "mAtk":
                        m = m * val if op == "mul" else m + val
                return m, "magic"
            return weapon_final(it, effects)["dps"] / 1000, "weapon"
        slot = item_slot(it["name"], item_id)
        if slot in ("earring", "necklace", "ring"):
            return it["mdef"] + sum(v for op, s, v in effects if s == "maxMp") * .05, slot
        if slot in ("shield", "sigil"):
            return it["pdef"] + it["mdef"], "offhand"
        pdef = float(it["pdef"])
        for op, stat, val in effects:
            if stat == "pDef" and op == "mul":
                pdef *= val
        return pdef + sum(v for op, s, v in effects if s == "maxHp") * .02, slot

    def top_tier(self, stats_db=None, extra=None):
        out = []
        for r in self.recipes.values():
            if r.get("rare") or r.get("mwStep"):
                continue  # internal steps of a masterwork
            product = self.name(r["product"])
            if r.get("obtain") and r["obtain"].get("acc"):
                acc = self.accessories[str(r["product"])]
                out.append({"id": r["id"], "productId": r["product"], "product": product, "family": "Acc",
                            "grade": {"NONE": "—"}.get(acc["grade"], acc["grade"]), "cat": acc["slot"],
                            "kind": "accessory", "rate": 100, "mp": 0, "level": 0, "npc": False, "mw": None,
                            "obtain": True})
                continue
            if r.get("obtain"):
                stats = (stats_db or {}).get("items", {}).get(str(r["product"]))
                if not stats:
                    continue
                if stats["kind"] == "weapon":
                    cat = weapon_cat(stats)
                    kind = "armor" if cat == "shield" else "weapon"
                else:
                    cat = armor_kind(product)
                    kind = "jewelry" if cat == "jewelry" else "armor"
                family = next((f for f in OBTAIN_FAMILIES if re.search(rf"\b{f}\b", product)), "Other")
                out.append({"id": r["id"], "productId": r["product"], "product": product, "family": family,
                            "grade": {7: "S84", 6: "S80"}.get(stats["grade"], "S"), "cat": cat, "kind": kind,
                            "rate": 100, "mp": 0, "level": 0, "npc": False, "mw": None, "obtain": True})
                continue
            classify = self.name(self.recipes[r["mw"]["base"]]["product"]) if r.get("mw") else product
            if r.get("mw") and " - " not in product:
                product += " (Masterwork)"  # armor masterworks keep the plain item's name
            family = next((f for f in CRAFT_FAMILIES if re.search(rf"\b{f}\b", classify)), None)
            if not family:
                continue
            kind = ("jewelry" if any(j in classify for j in _JEWELRY)
                    else "armor" if classify.startswith("Sealed ") else "weapon")
            stats = (stats_db or {}).get("items", {}).get(str(r["product"]))
            cat = weapon_cat(stats) if kind == "weapon" and stats else armor_kind(classify)
            if cat == "shield":
                kind = "armor"
            out.append({"id": r["id"], "productId": r["product"], "product": product, "family": family,
                        "grade": CRAFT_FAMILIES[family], "cat": cat,
                        "kind": kind, "rate": r["rate"], "mp": r["mp"],
                        "level": self.recipes[r["mw"]["base"]]["level"] if r.get("mw") else r["level"],
                        "npc": bool(r.get("npc")) and not r.get("mw"),
                        "mw": {"rarity": r["mw"]["rarity"], "rate": round(r["mw"]["baseRate"] * r["mw"]["rarity"] / 100, 2)}
                        if r.get("mw") else None})
        if stats_db:
            bonus = (extra or {}).get("bonus", {})
            for x in out:
                if x["kind"] != "accessory":
                    power, group = self._power(x["productId"], stats_db, bonus)
                    if power:
                        x["power"], x["pgroup"] = round(power, 2), group
        return sorted(out, key=lambda x: (x["grade"], x["family"], x["kind"], x["product"], -x["rate"]))

    def _expand(self, recipe, needed, expand_all):
        """Expand one recipe; returns (tree, base totals, craft steps, stats)."""
        totals, steps, stats = {}, {}, {"mp": 0, "crafts": 0, "maxLevel": 0}

        def walk(rec, need, depth):
            crafts = math.ceil(need / rec["count"])
            if rec.get("rare"):  # the base recipe, repeated until the Foundation comes out
                crafts = math.ceil(need / (rec["rate"] / 100))
            stats["mp"] += crafts * rec["mp"]
            stats["crafts"] += crafts
            stats["maxLevel"] = max(stats["maxLevel"], rec["level"])
            step = steps.setdefault(rec["product"], {"id": rec["product"], "name": self.name(rec["product"]),
                                                     "crafts": 0, "makes": 0, "level": rec["level"],
                                                     "rate": rec["rate"], "mp": 0, "depth": 0,
                                                     "npc": [n["npc"] for n in rec.get("npc", [])],
                                                     "rare": ({"rarity": rec["rare"]["rarity"],
                                                               "of": self.name(rec["rare"]["of"])}
                                                              if rec.get("rare") else None)})
            step["crafts"] += crafts
            step["makes"] += crafts * rec["count"]
            step["mp"] += crafts * rec["mp"]
            step["depth"] = max(step["depth"], depth)
            node = {"id": rec["product"], "name": self.name(rec["product"]), "qty": need, "crafts": crafts,
                    "rate": rec["rate"], "level": rec["level"], "mp": rec["mp"], "count": rec["count"],
                    "npc": [n["npc"] for n in rec.get("npc", [])], "children": []}
            for mat_id, mat_qty in rec["mats"]:
                if mat_id == rec["recipeItem"]:
                    continue  # the recipe itself is listed but never consumed
                mat_need = mat_qty * crafts
                sub = self.by_product.get(mat_id)
                if sub and depth < 8 and sub["id"] != rec["id"] and expand_all:
                    node["children"].append(walk(sub, mat_need, depth + 1))
                else:
                    totals[mat_id] = totals.get(mat_id, 0) + mat_need
                    node["children"].append({"id": mat_id, "name": self.name(mat_id), "qty": mat_need})
            return node

        root = walk(recipe, needed, 0)
        shopping = sorted(({"id": k, "name": self.name(k), "qty": v} for k, v in totals.items()),
                          key=lambda x: (-x["qty"], x["name"]))
        # crafting order: deepest intermediates first, the final item last
        plan = sorted(steps.values(), key=lambda s: (-s["depth"], s["level"], s["name"]))
        return root, shopping, plan, stats

    def tree(self, recipe_id, quantity, expand_all=True):
        """Material tree, shopping list and a step-by-step crafting plan.
        expand_all=False stops at the recipe's direct materials (buy the intermediates).
        For recipes below 100% it also returns the shopping list for the *average* number
        of attempts, so a player does not run out of materials halfway."""
        recipe = self.recipes.get(int(recipe_id))
        if not recipe:
            return None
        if recipe.get("obtain"):
            name = self.name(recipe["product"])
            return {"obtain": True, "product": recipe["product"], "sealed": recipe["obtain"]["sealed"],
                    "sealedName": self.name(recipe["obtain"]["sealed"]) if recipe["obtain"]["sealed"] else None,
                    "hero": name.startswith("Infinity "), "plan": [], "shopping": [], "root": None,
                    "acc": recipe["obtain"].get("acc"),
                    "bonus": self.accessories.get(str(recipe["product"]), {}).get("effects", [])
                    if recipe["obtain"].get("acc") else [],
                    "actives": self.accessories.get(str(recipe["product"]), {}).get("actives", [])
                    if recipe["obtain"].get("acc") else [],
                    "recipeItem": None, "npc": [], "mw": None, "crafts": 0, "mp": 0, "maxLevel": 0}
        quantity = max(1, min(999, int(quantity)))
        root, shopping, plan, stats = self._expand(recipe, quantity * recipe["count"], expand_all)
        out = {"root": root, "shopping": shopping, "plan": plan,
               "recipeItem": self.name(recipe["recipeItem"]) if recipe["recipeItem"] else None,
               "npc": [{"npc": n["npc"], "cost": self.name(n["cost"][0]), "count": n["cost"][1]}
                       for n in recipe.get("npc", [])],
               "mw": ({"rarity": recipe["mw"]["rarity"], "baseRate": recipe["mw"]["baseRate"],
                       "base": self.name(self.recipes[recipe["mw"]["base"]]["product"])} if recipe.get("mw") else None),
               "recipeItemId": recipe["recipeItem"],
               "quantity": quantity, "rate": recipe["rate"], **stats}
        if recipe["rate"] < 100:
            attempts = math.ceil(quantity / (recipe["rate"] / 100))
            _, avg_shopping, _, avg_stats = self._expand(recipe, attempts * recipe["count"], expand_all)
            out.update(avgAttempts=attempts, avgShopping=avg_shopping, avgMp=avg_stats["mp"])
        return out


# ---------------------------------------------------------------- bosses (Alt+B)
_ROW_START = '<td width=300 valign=top>'
_RE_NAME = re.compile(r'<font color="[0-9A-Fa-f]{6}">([^<]{2,60})</font>')
_RE_STATUS = re.compile(r'<td width=65[^>]*>\s*<font color="([0-9A-Fa-f]{6})">(?:<font[^>]*>)?([^<]+)<')
_RE_ID = re.compile(r'_bbs(epic|boss)_[^"\s]*\s+_(\d+)"')
_RE_TIME = re.compile(r'^(\d+):(\d{2}):(\d{2})$')
_RE_DETAIL_ID = re.compile(r'_bbs(?:epic|boss)_[^"\s]*\s+_(\d+)_\d+"')
_RE_HP = re.compile(r'(Max|Current) HP:\s*([\d,.]+)')


def parse_board_details(text):
    """A boss 'Info' page: {id: {'maxHp': n, 'hp': n}}."""
    m_id = _RE_DETAIL_ID.search(text)
    if not m_id:
        return {}
    hp = {k: int(v.replace(",", "").replace(".", "")) for k, v in _RE_HP.findall(text)}
    if not hp:
        return {}
    return {m_id.group(1): {"maxHp": hp.get("Max"), "hp": hp.get("Current")}}


def parse_board(text):
    """Extract the boss list rows from the Alt+B HTML.
    Returns [(id, name, type 'epic'|'raid', 'alive'|'dead'|text, seconds_to_respawn|None)]."""
    rows = []
    for chunk in text.split(_ROW_START)[1:]:
        m_id = _RE_ID.search(chunk)
        m_name = _RE_NAME.search(chunk)
        m_st = _RE_STATUS.search(chunk)
        if not (m_id and m_name and m_st):
            continue  # table header or something else
        status = m_st.group(2).strip()
        t = _RE_TIME.match(status)
        if t:
            secs = int(t.group(1)) * 3600 + int(t.group(2)) * 60 + int(t.group(3))
            rows.append((m_id.group(2), m_name.group(1).strip(), "epic" if m_id.group(1) == "epic" else "raid",
                         "dead", secs))
        else:
            rows.append((m_id.group(2), m_name.group(1).strip(), "epic" if m_id.group(1) == "epic" else "raid",
                         "alive" if status.lower() == "alive" else status, None))
    return rows


def clear_board_file():
    try:
        if BOARD_FILE.exists() and BOARD_FILE.stat().st_size > 64:
            BOARD_FILE.write_bytes(b"[B]\r\n")
    except OSError:
        pass


class BoardWatcher(threading.Thread):
    """Read bd.ini whenever the game writes a new Alt+B page and hand over the bosses."""

    def __init__(self, on_rows):
        super().__init__(daemon=True)
        self.on_rows = on_rows
        self.last_read = None

    def run(self):
        while True:
            try:
                self.check()
            except Exception:
                log_error("bosses", traceback.format_exc())
            time.sleep(1)

    def check(self):
        if not BOARD_FILE.exists():
            return
        mtime = BOARD_FILE.stat().st_mtime
        if mtime == self.last_read:
            return
        time.sleep(0.3)  # the game writes parts 101/102/103 in sequence
        mtime = BOARD_FILE.stat().st_mtime
        raw = BOARD_FILE.read_bytes()
        text = raw.decode("utf-16le" if raw[:2] == b"\xff\xfe" else "utf-8", errors="replace")
        rows = parse_board(text)
        details = parse_board_details(text)
        if rows or details:
            self.on_rows(rows, details, mtime)
        # clear it so old pages do not pile up (only if the game has not written again)
        if BOARD_FILE.stat().st_mtime == mtime and len(raw) > 64:
            BOARD_FILE.write_bytes(b"[B]\r\n")
        self.last_read = BOARD_FILE.stat().st_mtime


# ---------------------------------------------------------------- monitors
class PingMonitor(threading.Thread):
    def __init__(self, ip):
        super().__init__(daemon=True)
        self.ip = ip
        self.samples = deque(maxlen=PING_HISTORY)
        self.lock = threading.Lock()

    def run(self):
        while True:
            t0 = time.monotonic()
            rtt = icmp_ping(self.ip)
            with self.lock:
                self.samples.append(rtt)
            time.sleep(max(0.2, PING_INTERVAL - (time.monotonic() - t0)))

    def stats(self):
        with self.lock:
            s = list(self.samples)
        if not s:
            return None
        ok = [x for x in s if x is not None]
        return {
            "last": s[-1],
            "avg": sum(ok) / len(ok) if ok else None,
            "loss": 100 * (len(s) - len(ok)) / len(s),
            "jitter": (sum(abs(a - b) for a, b in zip(ok, ok[1:])) / (len(ok) - 1)) if len(ok) > 1 else 0,
            "samples": s,
        }


class WarpMonitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.state = "checking"
        self.wake = threading.Event()

    def run(self):
        while True:
            self.state = self.read()
            self.wake.wait(15)
            self.wake.clear()

    def read(self):
        if not WARP_CLI.exists():
            return "missing"
        try:
            out = run_hidden([str(WARP_CLI), "--accept-tos", "status"]).stdout
        except Exception:
            return "error"
        if "Connected" in out:
            return "connected"
        if "Connecting" in out:
            return "connecting"
        return "off"

    def toggle(self):
        action = "disconnect" if self.state in ("connected", "connecting") else "connect"
        self.state = "waiting"

        def work():
            try:
                run_hidden([str(WARP_CLI), "--accept-tos", action])
            except Exception:
                pass
            time.sleep(3)
            self.wake.set()
        threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- API exposed to the HTML
class Api:
    def __init__(self):
        self._data = load_data()
        self._lock = threading.RLock()
        self._slot_pid = [None] * len(self._data["slots"])
        self._slot_start = [None] * len(self._data["slots"])
        self._pending = []  # (slot, launched_at, popen_pid)
        self._known = set()
        self._windows = {}
        self._vote_ready = {}
        self._timer_fired = set()
        self._alerts = []
        self._window = None
        self._watch = ClientWatch()
        self._bars, self._bars_at, self._bar_armed = {}, {}, {}
        self._gone_slot = {}
        self._last_watch = {}
        self._event_live = {}           # event key -> {"state", "at", "start"} from the announcements
        self._announces = deque(maxlen=30)
        self._chat = ChatReader(self._chat_hwnd, self._on_announcement)
        self._chat.start()
        self._flows = LoginFlows(lambda: self._data.get("lang", "pt"))
        self._had_window = [False] * len(self._data["slots"])
        self._autologin_slots = set()  # slots opened by the panel that should log in by themselves
        self._login_lock = threading.Lock()  # one automatic login at a time
        self._ping = PingMonitor(SERVER_IP)
        self._ping.start()
        self._warp = WarpMonitor()
        self._warp.start()
        self._boss_alerted = set()
        try:
            self._boss_db = build_boss_db()
        except Exception:
            log_error("boss_db", traceback.format_exc())
            self._boss_db = {}
        try:
            db = build_recipe_db()
            self._recipes = RecipeBook(db) if db else None
        except Exception:
            log_error("recipe_db", traceback.format_exc())
            self._recipes = None
        try:
            self._stats = build_stats_db()
        except Exception:
            log_error("stats_db", traceback.format_exc())
            self._stats = None
        self._icons = IconStore()
        self._drops = DropBook()
        self._items = _load_items_db()
        if self._recipes:
            self._recipes.add_npc_recipes(self._items)
            self._recipes.add_masterwork_recipes(self._items)
            self._recipes.add_obtain_entries(self._stats, self._items)
            try:
                in_client = set(self._icons._icon_map()) or None
            except Exception:
                in_client = None
            self._recipes.add_accessory_entries(self._items, in_client)
        self._board = BoardWatcher(self._on_board_rows)
        self._board.start()
        self._adopt_running()

    # -------------------------------------------------- util
    def _save(self):
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DATA_FILE)

    def _check_bars(self, watch):
        """Read CP/HP/MP of the clients in game (window not minimized) and warn once per event:
        dead, HP low, MP low. The warning re-arms when the bar is back up."""
        if not self._data.get("bar_alerts", True):
            return
        now = time.time()
        for i, pid in enumerate(self._slot_pid):
            st = watch.get(pid) if pid else None
            if not st or not st.get("inWorld"):
                continue
            last = self._bars_at.get(pid, 0)
            if now - last < 1.5:
                st["bars"] = self._bars.get(pid)
                continue
            self._bars_at[pid] = now
            hwnd = self._windows.get(pid)
            bars = read_bars(hwnd) if hwnd else None
            self._bars[pid] = bars
            st["bars"] = bars
            if not bars:
                continue
            name = self._data["slots"][i]
            armed = self._bar_armed.setdefault(pid, {"dead": True, "hp": True, "mp": True})
            if bars["hp"] == 0:
                if armed["dead"]:
                    armed["dead"] = False
                    self._alert("dead", slot=name)
                    self._flows.set(i, name, "f_dead", "fail")
            elif bars["hp"] > 5:
                armed["dead"] = True
            hp_low = self._data.get("hp_alert", 30)
            if 0 < bars["hp"] < hp_low and armed["hp"]:
                armed["hp"] = False
                self._alert("hpLow", slot=name, pct=bars["hp"])
            elif bars["hp"] >= min(100, hp_low + 20):
                armed["hp"] = True
            if bars["mp"] < 10 and armed["mp"] and bars["hp"] > 0:
                armed["mp"] = False
                self._alert("mpLow", slot=name, pct=bars["mp"])
            elif bars["mp"] >= 30:
                armed["mp"] = True

    def _chat_hwnd(self):
        """A client in game whose window is not minimized (any one: announcements are global)."""
        for pid in self._slot_pid:
            st = self._last_watch.get(pid) if pid else None
            hwnd = self._windows.get(pid) if pid else None
            if st and st.get("inWorld") and hwnd and not user32.IsIconic(hwnd):
                return hwnd
        return None

    def _on_announcement(self, text, at):
        with self._lock:
            self._announces.append({"t": at, "text": text})
            # "server shutdown/restart in N minutes": that is the new restart time
            m = re.search(r"(shut ?down|restart|reboot)\D{0,40}?(\d+)\s*(second|minute|min|sec)", text.lower())
            if m:
                self._data["restart_at"] = at + int(m.group(2)) * (1 if m.group(3).startswith("sec") else 60)
                self._data.pop("event_seen", None)  # the spawn cycle starts over after a restart
                self._save()
            res = parse_announcement(text, at)
            if not res:
                return
            key, state, start = res
            prev = self._event_live.get(key) or {}
            if state == "info" and prev:
                return
            self._event_live[key] = {"state": state, "at": at, "start": start or prev.get("start")}
            ev = next(e for e in SERVER_EVENTS if e["key"] == key)
            if state == "started" and "restart" in ev:  # learn when it spawns
                self._data.setdefault("event_seen", {})[key] = at
                self._save()
            if state in ("reg", "started") and prev.get("state") != state and self._data.get("event_alerts", True):
                self._alert("evReg" if state == "reg" else "evStart", name=ev["name"])

    def _alert(self, key, **args):
        """Alerts are sent as a code + parameters; the interface translates them."""
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
        self._alerts.append({"k": key, **args})

    def _hwnd(self, i):
        pid = self._slot_pid[i]
        return self._windows.get(pid) if pid else None

    def _adopt_running(self):
        for pid in list_l2_pids():
            free = next((i for i in range(len(self._slot_pid)) if not self._slot_pid[i]), None)
            if free is not None:
                self._slot_pid[free] = pid
                self._slot_start[free] = time.time()
            self._known.add(pid)

    def _refresh_clients(self):
        pids = list_l2_pids()
        alive = set(pids)
        self._windows = main_windows_by_pid()
        now = time.time()
        for i in range(len(self._slot_pid)):
            if self._slot_pid[i] and self._slot_pid[i] not in alive:
                self._gone_slot[self._slot_pid[i]] = i
                self._slot_pid[i] = self._slot_start[i] = None
        # l2.exe may relaunch itself: a new process goes to the oldest pending slot
        self._pending = [p for p in self._pending if now - p[1] < 90]
        bound = set(self._slot_pid)
        for pid in pids:
            if pid in bound:
                continue
            match = next((p for p in self._pending if p[2] == pid), None)
            if not match and pid not in self._known and self._pending:
                match = self._pending[0]
            if match:
                self._pending.remove(match)
                self._slot_pid[match[0]] = pid
                self._slot_start[match[0]] = now
                bound.add(pid)
        self._known = alive

        mons = monitors()
        watch, dropped = self._watch.poll([p for p in self._slot_pid if p])
        for pid in self._watch.crashed:
            k = self._gone_slot.get(pid)
            if k is not None and k < len(self._data["slots"]):
                self._alert("crashed", slot=self._data["slots"][k])
                self._flows.set(k, self._data["slots"][k], "f_crash", "fail")
        self._gone_slot = {}
        self._check_bars(watch)
        self._last_watch = watch
        # every client in game lost the connection at the same time: the server went down
        in_game = [p for p in self._slot_pid if p and (self._last_watch.get(p) or {}).get("net") == "game"]
        if dropped and len(dropped) >= max(1, len(in_game)) and len(dropped) >= 2:
            self._data["restart_at"] = time.time()
            self._data.pop("event_seen", None)
            self._save()
        for pid in dropped:
            k = self._slot_pid.index(pid)
            self._alert("disconnected", slot=self._data["slots"][k])
            self._flows.set(k, self._data["slots"][k], "f_dc", "fail")
        slots = []
        for i in range(len(self._slot_pid)):
            pid = self._slot_pid[i]
            hwnd = self._windows.get(pid) if pid else None
            if hwnd and not self._had_window[i] and i in self._autologin_slots:
                self._autologin_slots.discard(i)
                self._schedule_login(i, pid, self._data["login_delay"])
            self._had_window[i] = bool(hwnd)
            if hwnd and self._data["rename_windows"]:
                want = f"{self._data['slots'][i]} - Lineage II"
                if window_title(hwnd) != want:
                    user32.SetWindowTextW(hwnd, want)
            slots.append({
                "name": self._data["slots"][i],
                "pid": pid,
                "window": bool(hwnd),
                "pending": any(p[0] == i for p in self._pending),
                "ram": process_memory_mb(pid) if pid else None,
                "uptime": now - self._slot_start[i] if pid else None,
                "account": self._account_info(i),
                "monitor": monitor_of(hwnd, mons) if hwnd and not user32.IsIconic(hwnd) else None,
                **(watch.get(pid) or {}),
            })
        external = [p for p in pids if p not in bound]
        return slots, external, len(pids)

    def _launch(self, i):
        if self._slot_pid[i] or any(p[0] == i for p in self._pending) or not L2_EXE.exists():
            return False
        try:
            pid = launch_l2()
        except OSError as exc:
            self._alert("launchFail", slot=self._data["slots"][i], err=str(exc))
            return False
        self._pending.append((i, time.time(), pid))
        acc = self._data["accounts"].get(str(i))
        if acc and acc.get("auto") and acc.get("pwd"):
            self._autologin_slots.add(i)
        return True

    # -------------------------------------------------- accounts
    def _account_info(self, i):
        acc = self._data["accounts"].get(str(i)) or {}
        return {"login": acc.get("login", ""), "hasPassword": bool(acc.get("pwd")),
                "auto": bool(acc.get("auto")), "enter": bool(acc.get("enter"))}

    def _schedule_login(self, i, pid, delay):
        """Log in as soon as each screen is really there, by looking at the client's window:
        type the login when the "Log In" button shows, press Enter when "Confirm" (server list)
        shows, and, if the account is set to enter the game, Enter again when "Start" shows.
        Works with the window in the background or covered, for several clients at once.
        If the login screen is never recognised (another interface skin, say), it falls back to
        waiting for the client to finish loading."""
        hwnd_of = lambda: self._hwnd(i)
        alive = lambda: self._slot_pid[i] == pid
        name = self._data["slots"][i] if i < len(self._data["slots"]) else f"#{i + 1}"
        step = lambda key, state="run": self._flows.set(i, name, key, state)

        def press_until_gone(anchor, pos, timeout=40):
            """Press the button (Enter, then a click on it) until its screen goes away. Checking
            the screen is cheap, so keep trying instead of giving up after one go."""
            deadline, tries = time.time() + timeout, 0
            while time.time() < deadline and alive():
                hwnd = hwnd_of()
                if not hwnd:
                    return False
                if tries % 2 == 0:
                    post_key(hwnd, VK_RETURN)
                else:
                    post_click(hwnd, *pos)
                tries += 1
                for _ in range(4):  # ~2 s to react
                    time.sleep(0.5)
                    hwnd = hwnd_of()
                    now = find_anchor(hwnd, anchor) if hwnd else None
                    if not now:
                        return True
                    pos = now
            return False

        def work():
            step("wait")
            if wait_for_anchor(hwnd_of, "login", alive, timeout=max(60, float(delay) + 90)):
                time.sleep(0.4)
            else:
                if not alive():
                    return step("f_closed", "fail")
                step("fallback")
                if wait_until_idle(pid, timeout=60) is False:
                    return step("f_closed", "fail")
            if not alive():
                return step("f_closed", "fail")
            step("typing")
            res = self.fill_login(i)
            if res != "ok":
                return step("f_closed" if res == "noWindow" else "f_account", "fail")
            enter = bool((self._data["accounts"].get(str(i)) or {}).get("enter"))
            if not (enter or self._data.get("to_charselect", True)):
                return step("done_login", "ok")
            # server list, with the server already selected: Confirm
            step("server")
            pos = wait_for_anchor(hwnd_of, "confirm", alive, timeout=45)
            if not pos:
                return step("f_closed" if not alive() else "f_server", "fail")
            if not press_until_gone("confirm", pos):
                return step("f_closed" if not alive() else "f_server", "fail")
            # character selection, with the last character played selected: Start
            step("charsel")
            pos = wait_for_anchor(hwnd_of, "start", alive, timeout=90)
            if not pos:
                return step("f_closed" if not alive() else "f_charsel", "fail")
            if not enter:
                return step("done_sel", "ok")
            step("start")
            time.sleep(0.8)
            if press_until_gone("start", pos):
                # the client log says when the character is really in the world (first 2 clients)
                for _ in range(40):
                    st = self._last_watch.get(pid) or {}
                    if st.get("inWorld") or "inWorld" not in st:
                        break
                    time.sleep(1)
                return step("done_game", "ok")
            step("f_closed" if not alive() else "f_start", "fail")
        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------- bosses
    def _on_board_rows(self, rows, details, captured_at):
        with self._lock:
            bosses = self._data["bosses"]
            for bid, det in details.items():
                if bid in bosses:
                    bosses[bid].update(det, hpAt=captured_at)
            for bid, name, kind, status, secs in rows:
                b = bosses.get(bid) or {"fav": kind == "epic"}
                if b.get("updated", 0) > captured_at:
                    continue
                b.update(name=name, type=kind, status=status, updated=captured_at,
                         respawn=captured_at + secs if secs is not None else None)
                bosses[bid] = b
                self._boss_alerted.discard(bid)
            self._save()

    def _boss_list(self, now):
        out = []
        for bid, b in self._data["bosses"].items():
            remaining = b["respawn"] - now if b.get("respawn") else None
            if remaining is not None and remaining <= 0 and b.get("fav") and bid not in self._boss_alerted:
                self._boss_alerted.add(bid)
                if remaining > -60:
                    self._alert("bossSpawn", name=b["name"])
            db = self._boss_db.get(bid, {})
            out.append({"id": bid, "name": b["name"], "type": b["type"], "status": b["status"],
                        "remaining": remaining, "respawnAt": b.get("respawn"), "fav": bool(b.get("fav")),
                        "age": now - b["updated"], "maxHp": b.get("maxHp"), "hp": b.get("hp"),
                        "hpAge": now - b["hpAt"] if b.get("hpAt") else None, **db})
        return out

    # -------------------------------------------------- craft
    def recipes_list(self):
        return self._recipes.top_tier(self._stats, self._items) if self._recipes else []

    def class_list(self):
        """Classes with what they can use, for the character filter."""
        return [{"name": n, "race": c[0], "role": c[1], "weapons": c[2], "armor": c[3], "offhand": c[4]}
                for n, c in CHAR_CLASSES.items()]

    def char_plan(self, cls):
        return self._recipes.char_plan(cls, self._stats, self._items) if self._recipes else None

    def recipe_tree(self, recipe_id, quantity, expand_all=True):
        return self._recipes.tree(recipe_id, quantity, bool(expand_all)) if self._recipes else None

    def item_sources(self, item_id):
        if not item_id:
            return None
        names = {}
        if self._recipes:
            names = {str(k): v for k, v in self._recipes.names.items()}
        if self._stats:
            names = {**{k: v["name"] for k, v in self._stats["items"].items()}, **names}
        return self._drops.sources(item_id, names)

    def recipe_compare(self, recipe_id):
        return self._recipes.compare(recipe_id, self._stats, self._items) if self._recipes else None

    def boss_fav(self, bid, fav):
        with self._lock:
            if bid in self._data["bosses"]:
                self._data["bosses"][bid]["fav"] = bool(fav)
                self._save()

    def boss_clear(self):
        with self._lock:
            self._data["bosses"] = {}
            self._save()

    # -------------------------------------------------- state (polled once per second by the interface)
    def get_state(self):
        try:
            state = self._get_state()
            if not getattr(self, "_ui_ok", False):
                self._ui_ok = True
                log_error("info", f"interface connected (admin={is_admin()})")
            return state
        except Exception:
            log_error("get_state", traceback.format_exc())
            raise

    def log_js(self, text):
        log_error("ui", str(text)[:2000])

    def _get_state(self):
        with self._lock:
            slots, external, running = self._refresh_clients()
            now = time.time()

            votes = []
            for key, name, url, main in VOTE_SITES:
                ts = self._data["votes"].get(key)
                rem = max(0, ts + VOTE_COOLDOWN - now) if ts else 0
                if rem == 0 and self._vote_ready.get(key) is False and main:
                    self._alert("voteReady", name=name)
                self._vote_ready[key] = rem == 0
                votes.append({"key": key, "name": name, "main": main, "remaining": rem})

            timers = []
            for t in sorted(self._data["timers"], key=lambda t: t["end"]):
                rem = t["end"] - now
                if rem <= 0 and t["id"] not in self._timer_fired:
                    self._timer_fired.add(t["id"])
                    if rem > -5:
                        self._alert("timerDone", name=t["name"])
                timers.append({"id": t["id"], "name": t["name"], "remaining": rem})

            bosses = self._boss_list(now)
            alerts, self._alerts = self._alerts, []
            return {
                "slots": slots, "external": external, "running": running, "slotCap": SLOT_CAP,
                "monitors": [{k: m[k] for k in ("x", "y", "w", "h", "primary", "res")} for m in monitors()],
                "hwidLimit": HWID_LIMIT, "ping": self._ping.stats(), "pingHistory": PING_HISTORY,
                "server": SERVER_IP, "warp": self._warp.state, "votes": votes, "timers": timers,
                "alerts": alerts, "topmost": self._data["topmost"], "compact": self._data["compact"],
                "renameWindows": self._data["rename_windows"], "opacity": self._data["opacity"],
                "toCharselect": self._data.get("to_charselect", True),
                "restartAt": self._data.get("restart_at"),
                "barAlerts": self._data.get("bar_alerts", True),
                "eventAlerts": self._data.get("event_alerts", True),
                "events": [dict(zip(("key", "name", "drops", "last", "next", "kind", "dur", "reg"),
                                    (e["key"], e["name"], e["drops"],
                                     *event_times(e, self._data.get("restart_at"),
                                                  seen=self._data.get("event_seen", {}).get(e["key"])),
                                     "chat" if e.get("chat") else "restart" if "restart" in e else "timed",
                                     e.get("dur", 120 if "weekly" in e else 30), e.get("reg", 0))))
                           for e in SERVER_EVENTS],
                "eventLive": self._event_live,
                "announces": list(self._announces)[-12:],
                "chatReading": self._chat.active,
                "scale": self._data.get("scale", 1.0), "char": self._data.get("char") or {},
                "loginDelay": self._data["login_delay"], "admin": is_admin(), "bosses": bosses,
                "lang": self._data["lang"], "bossView": self._data["boss_view"],
                "gm": load_gm(),
                "links": LINKS,
            }

    # -------------------------------------------------- clients
    def launch(self, i):
        with self._lock:
            return self._launch(int(i))

    def launch_many(self, slots):
        def work():
            for n, i in enumerate(slots):
                if n:
                    time.sleep(LAUNCH_SPACING)
                with self._lock:
                    self._launch(int(i))
        threading.Thread(target=work, daemon=True).start()

    def focus(self, i):
        h = self._hwnd(int(i))
        if h:
            focus_window(h)

    def minimize(self, i):
        h = self._hwnd(int(i))
        if h:
            user32.ShowWindow(h, SW_MINIMIZE)

    def close(self, i):
        pid = self._slot_pid[int(i)]
        if not pid:
            return
        h = self._hwnd(int(i))
        if h:
            user32.PostMessageW(h, WM_CLOSE, 0, 0)

        def ensure():  # if it does not close by itself within 8 s, kill the process
            time.sleep(8)
            if pid in list_l2_pids():
                terminate_pid(pid)
        threading.Thread(target=ensure, daemon=True).start()

    def close_all(self):
        for i in range(len(self._slot_pid)):
            self.close(i)
        bound = set(self._slot_pid)
        for pid in list_l2_pids():
            if pid not in bound and pid in self._windows:
                user32.PostMessageW(self._windows[pid], WM_CLOSE, 0, 0)

    def minimize_all(self):
        for pid in list_l2_pids():
            if pid in self._windows:
                user32.ShowWindow(self._windows[pid], SW_MINIMIZE)

    def _client_hwnds(self):
        """Game windows in slot order (slot 1 first), then clients not opened from the panel."""
        bound = [p for p in self._slot_pid if p]
        order = bound + [p for p in list_l2_pids() if p not in bound]
        return [self._windows[p] for p in order if p in self._windows]

    def tile(self, target="all"):
        """target "all": split the clients over every monitor (slot order, primary monitor first)
        and arrange each monitor in a grid; a number: put them all on that monitor."""
        hwnds = self._client_hwnds()
        if not hwnds:
            return False
        mons = monitors()
        if target != "all":
            mons = [mons[int(target)]] if 0 <= int(target) < len(mons) else mons[:1]
        per = math.ceil(len(hwnds) / len(mons))
        groups = [hwnds[i * per:(i + 1) * per] for i in range(len(mons))]
        for area, group in zip(mons, groups):
            for hwnd, (x, y, w, h) in zip(group, grid(area, len(group))):
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.MoveWindow(hwnd, x, y, w, h, True)
        return True

    def save_layout(self):
        """Remember where each account's window is (slot by slot), on any monitor."""
        layout = {}
        for i, pid in enumerate(self._slot_pid):
            hwnd = self._windows.get(pid) if pid else None
            if hwnd and not user32.IsIconic(hwnd):
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                layout[str(i)] = [r.left, r.top, r.right - r.left, r.bottom - r.top]
        with self._lock:
            self._data["layout"] = layout
            self._save()
        return len(layout)

    def restore_layout(self):
        layout = self._data.get("layout") or {}
        moved = 0
        for i, pid in enumerate(self._slot_pid):
            hwnd = self._windows.get(pid) if pid else None
            rect = layout.get(str(i))
            if hwnd and rect:
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.MoveWindow(hwnd, *rect, True)
                moved += 1
        return moved

    def move_to_monitor(self, i, target):
        """Send one account's window to a monitor: it joins the game windows already there and
        that monitor is re-arranged in a grid."""
        hwnd = self._hwnd(int(i))
        mons = monitors()
        target = int(target)
        if not hwnd or not 0 <= target < len(mons):
            return False
        others = [h for h in self._client_hwnds() if h != hwnd and not user32.IsIconic(h)
                  and monitor_of(h, mons) == target]
        group = others + [hwnd]
        for h, (x, y, w, hh) in zip(group, grid(mons[target], len(group))):
            user32.ShowWindow(h, SW_RESTORE)
            user32.MoveWindow(h, x, y, w, hh, True)
        return True

    def add_slot(self):
        with self._lock:
            if len(self._data["slots"]) >= SLOT_CAP:
                return False
            self._data["slots"].append(f"Account {len(self._data['slots']) + 1}")
            self._slot_pid.append(None)
            self._slot_start.append(None)
            self._had_window.append(False)
            self._save()
            return True

    def remove_slot(self, i):
        """Remove an account slot (its client must be closed). Later slots move up, with their
        saved login and window position."""
        i = int(i)
        with self._lock:
            n = len(self._data["slots"])
            if n <= 1 or not 0 <= i < n or self._slot_pid[i] or any(q[0] == i for q in self._pending):
                return False

            def shift(d):
                out = {}
                for k, v in (d or {}).items():
                    if k.isdigit() and int(k) != i:
                        out[str(int(k) - (int(k) > i))] = v
                    elif not k.isdigit():
                        out[k] = v
                return out

            self._data["slots"].pop(i)
            self._data["accounts"] = shift(self._data.get("accounts"))
            self._data["layout"] = shift(self._data.get("layout"))
            for lst in (self._slot_pid, self._slot_start, self._had_window):
                lst.pop(i)
            self._pending = [(s - (s > i), at, pid) for s, at, pid in self._pending]
            self._autologin_slots = {s - (s > i) for s in self._autologin_slots if s != i}
            self._save()
            return True

    def monitor_list(self):
        return [{"res": m["res"], "primary": m["primary"]} for m in monitors()]

    def rename(self, i, name):
        i = int(i)
        name = (name or "").strip()[:20] or f"Account {i + 1}"
        with self._lock:
            self._data["slots"][i] = name
            self._save()
        return name

    def get_account(self, i):
        return self._account_info(int(i))

    def set_account(self, i, login, password, auto, enter=False):
        """An empty password keeps the current one. The password is never sent back to the interface."""
        key = str(int(i))
        login = (login or "").strip()
        with self._lock:
            acc = self._data["accounts"].get(key, {})
            acc["login"] = login
            acc["auto"] = bool(auto)
            acc["enter"] = bool(enter)
            if password:
                acc["pwd"] = dpapi_encrypt(password)
            self._data["accounts"][key] = acc
            self._save()
        return self._account_info(int(i))

    def clear_account(self, i):
        with self._lock:
            self._data["accounts"].pop(str(int(i)), None)
            self._save()

    def fill_login(self, i):
        """Fill in login and password on the login screen of this slot's client."""
        i = int(i)
        acc = self._data["accounts"].get(str(i))
        hwnd = self._hwnd(i)
        if not hwnd:
            return "noWindow"
        if not acc or not acc.get("login") or not acc.get("pwd"):
            return "noAccount"
        try:
            pwd = dpapi_decrypt(acc["pwd"])
        except OSError:
            return "badPassword"
        post_text(hwnd, acc["login"])
        post_key(hwnd, VK_TAB)
        post_text(hwnd, pwd)
        del pwd
        post_key(hwnd, VK_RETURN)
        return "ok"

    def set_login_delay(self, seconds):
        with self._lock:
            self._data["login_delay"] = max(3, min(120, int(float(seconds))))
            self._save()
        return self._data["login_delay"]

    # -------------------------------------------------- network
    def warp_toggle(self):
        self._warp.toggle()

    # -------------------------------------------------- votes
    def vote(self, key):
        site = next((s for s in VOTE_SITES if s[0] == key), None)
        if not site:
            return
        webbrowser.open(site[2])
        with self._lock:
            self._data["votes"][key] = time.time()
            self._save()

    def vote_set_ago(self, key, minutes):
        """The player voted outside the panel: start the 12 h timer from `minutes` ago."""
        if key not in {s[0] for s in VOTE_SITES}:
            return False
        minutes = max(0.0, min(VOTE_COOLDOWN / 60, float(minutes)))
        with self._lock:
            self._data["votes"][key] = time.time() - minutes * 60
            self._save()
        return True

    def vote_reset(self, key):
        with self._lock:
            self._data["votes"].pop(key, None)
            self._save()

    def vote_ready(self, main_only):
        now = time.time()
        keys = [s[0] for s in VOTE_SITES if (s[3] or not main_only)
                and now >= self._data["votes"].get(s[0], 0) + VOTE_COOLDOWN]

        def work():
            for k in keys:
                self.vote(k)
                time.sleep(0.7)
        threading.Thread(target=work, daemon=True).start()
        return len(keys)

    # -------------------------------------------------- timers
    def timer_add(self, name, minutes):
        minutes = float(minutes)
        if minutes <= 0:
            return
        with self._lock:
            self._data["timers"].append({"id": f"{time.time():.3f}", "name": (name or "Timer")[:24],
                                         "end": time.time() + minutes * 60})
            self._save()

    def timer_remove(self, tid):
        with self._lock:
            self._data["timers"] = [t for t in self._data["timers"] if t["id"] != tid]
            self._save()

    # -------------------------------------------------- links / window
    def open_link(self, url):
        gm = load_gm()
        allowed = {u for _, u in LINKS} | ({f"mailto:{gm['email']}"} if gm and gm["email"] else set())
        if url in allowed:
            webbrowser.open(url)

    def open_folder(self, key):
        if key in FOLDERS and FOLDERS[key].exists():
            os.startfile(FOLDERS[key])

    def set_option(self, key, value):
        with self._lock:
            if key == "topmost":
                self._data["topmost"] = bool(value)
                self._window.on_top = bool(value)
            elif key == "compact":
                self._data["compact"] = bool(value)
                w, h = self._size()
                self._window.resize(w, COMPACT_H if value else h)
                threading.Timer(0.3, self._apply_shape).start()
            elif key == "eventAlerts":
                self._data["event_alerts"] = bool(value)
            elif key == "barAlerts":
                self._data["bar_alerts"] = bool(value)
            elif key == "restartAt":
                self._data["restart_at"] = float(value) if value else None
            elif key == "renameWindows":
                self._data["rename_windows"] = bool(value)
            elif key == "toCharselect":
                self._data["to_charselect"] = bool(value)
            elif key == "bossView" and isinstance(value, dict):
                allowed = {"sort": ("respawn", "lvlAsc", "lvlDesc", "name")}
                view = {k: str(value.get(k, ""))[:8] for k in ("sort", "range", "min", "max")}
                if view["sort"] not in allowed["sort"]:
                    view["sort"] = "respawn"
                self._data["boss_view"] = view
            elif key == "lang" and value in ("pt", "en", "de"):
                self._data["lang"] = value
            elif key == "opacity":
                self._data["opacity"] = max(0.5, min(1.0, float(value)))
                self._apply_opacity()
            elif key == "char" and isinstance(value, dict):
                cls = value.get("cls") if value.get("cls") in CHAR_CLASSES else ""
                race = value.get("race") if value.get("race") in {c[0] for c in CHAR_CLASSES.values()} else ""
                self._data["char"] = {"race": race, "cls": cls, "only": bool(value.get("only", True))}
            elif key == "scale":
                self._data["scale"] = max(0.9, min(1.5, float(value)))
            self._save()

    def minimize_self(self):
        self._window.minimize()

    def quit(self):
        """Close everything: the alerts popup too (a hidden window would keep the program alive)."""
        try:
            if self._flows._win:
                self._flows._win.destroy()
        except Exception:
            pass
        self._window.destroy()

        def force():  # whatever is left, end the process (clearing the Alt+B page first)
            clear_board_file()
            os._exit(0)
        threading.Timer(3, force).start()

    def _own_hwnd(self):
        me = os.getpid()
        found = []

        def cb(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == me and user32.IsWindowVisible(hwnd) and window_title(hwnd) == "L2Axis Control":
                found.append(hwnd)
            return True
        user32.EnumWindows(EnumWindowsProc(cb), 0)
        return found[0] if found else None

    def _apply_shape(self):
        """Shape the window: frame (inset 12 px) + the medallion square in the corner.
        WebView2 is not truly transparent, so the free strip is cut out of the window."""
        hwnd = self._own_hwnd()
        if not hwnd:
            return False
        try:
            scale = user32.GetDpiForWindow(hwnd) / 96.0
        except (AttributeError, OSError):
            scale = 1.0
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        inset, medal = round(12 * scale), round(47 * scale)
        gdi32 = ctypes.windll.gdi32
        # no right/bottom limit: the window can be resized and anything past it is ignored
        frame = gdi32.CreateRectRgn(inset, inset, 20000, 20000)
        corner = round(8 * scale)
        badge = gdi32.CreateRoundRectRgn(0, 0, medal + 1, medal + 1, corner, corner)
        gdi32.CombineRgn(frame, frame, badge, 2)  # RGN_OR
        gdi32.DeleteObject(badge)
        user32.SetWindowRgn(hwnd, frame, True)  # Windows takes ownership of the region
        # WebView2 draws through DirectComposition, which the region does not clip: on Windows 11 the
        # system backdrop (#202020) kept filling the cut-out strips. Without a backdrop they are clear.
        try:
            none = ctypes.c_int(1)  # DWMSBT_NONE
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(none), 4)  # DWMWA_SYSTEMBACKDROP_TYPE
        except (AttributeError, OSError):
            pass  # Windows 10: no system backdrop
        self._apply_opacity(hwnd)
        return True

    def _apply_opacity(self, hwnd=None):
        """Whole-window transparency (layered window alpha): the only kind WebView2 honours."""
        hwnd = hwnd or self._own_hwnd()
        if not hwnd:
            return
        ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        if not ex & 0x80000:  # WS_EX_LAYERED
            user32.SetWindowLongW(hwnd, -20, ex | 0x80000)
        alpha = int(round(255 * max(0.5, min(1.0, float(self._data.get("opacity", 1.0))))))
        user32.SetLayeredWindowAttributes(hwnd, 0, alpha, 2)  # LWA_ALPHA

    def ui_ready(self):
        """Called by the interface once it is built: applies the window shape."""
        def work():
            for _ in range(20):
                if self._apply_shape():
                    return
                time.sleep(0.25)
        threading.Thread(target=work, daemon=True).start()

    def _size(self):
        """Saved window size (logical pixels), within limits."""
        size = self._data.get("size") or [WIN_W, WIN_H]
        return max(MIN_W, min(MAX_W, int(size[0]))), max(MIN_H, min(MAX_H, int(size[1])))

    def resize_start(self, edge):
        """Resize from the right edge ("r"), bottom edge ("b") or corner ("rb") while the left
        button is held. Same approach as drag_start (works when the panel is elevated)."""
        hwnd = self._own_hwnd()
        if not hwnd or edge not in ("r", "b", "rb"):
            return False
        try:
            scale = user32.GetDpiForWindow(hwnd) / 96.0
        except (AttributeError, OSError):
            scale = 1.0
        compact = self._data["compact"]

        def work():
            pt = wintypes.POINT()
            r = wintypes.RECT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            x0, y0, w0, h0 = pt.x, pt.y, r.right - r.left, r.bottom - r.top
            # the real window is a bit smaller than the size it was created with: keep that offset so
            # the saved size reopens exactly as it was left
            want_w, want_h = self._size()
            off_w, off_h = want_w - w0 / scale, want_h - h0 / scale
            while user32.GetAsyncKeyState(0x01) & 0x8000:
                user32.GetCursorPos(ctypes.byref(pt))
                w = w0 + (pt.x - x0 if "r" in edge else 0)
                h = h0 + (pt.y - y0 if "b" in edge and not compact else 0)
                w = max(round(MIN_W * scale), min(round(MAX_W * scale), w))
                if not compact:
                    h = max(round(MIN_H * scale), min(round(MAX_H * scale), h))
                user32.SetWindowPos(hwnd, None, 0, 0, w, h, 0x0002 | 0x0004 | 0x0010)  # NOMOVE|NOZORDER|NOACTIVATE
                time.sleep(0.016)
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            with self._lock:
                old = self._size()
                self._data["size"] = [round((r.right - r.left) / scale + off_w),
                                      old[1] if compact else round((r.bottom - r.top) / scale + off_h)]
                self._save()
        threading.Thread(target=work, daemon=True).start()
        return True

    def drag_start(self):
        """Drag the window while the left button is held. Done through Windows: it does not
        depend on the pywebview JS bridge, which does not work when the panel is elevated."""
        hwnd = self._own_hwnd()
        if not hwnd:
            return False

        def work():
            pt = wintypes.POINT()
            r = wintypes.RECT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            dx, dy = pt.x - r.left, pt.y - r.top
            while user32.GetAsyncKeyState(0x01) & 0x8000:
                user32.GetCursorPos(ctypes.byref(pt))
                user32.SetWindowPos(hwnd, None, pt.x - dx, pt.y - dy, 0, 0, 0x0001 | 0x0004 | 0x0010)
                time.sleep(0.008)
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            with self._lock:
                self._data["pos"] = [r.left, r.top]
                self._save()
        threading.Thread(target=work, daemon=True).start()
        return True


# ---------------------------------------------------------------- local server for the interface
def make_server(api, token):
    """The interface talks to Python over local HTTP (127.0.0.1 + token) instead of the
    pywebview JS bridge, which never connects when the panel runs as administrator."""
    methods = {n for n in dir(api) if not n.startswith("_") and callable(getattr(api, n))}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/ui.html"):
                self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            elif path.startswith("/assets/") and path[8:] in ASSETS and (ASSETS_DIR / path[8:]).exists():
                self._send(200, (ASSETS_DIR / path[8:]).read_bytes(), "image/png")
            elif re.fullmatch(r"/icon/\d{1,7}\.png", path):
                try:
                    png = api._icons.png(int(path[6:-4]))
                except Exception:
                    log_error("icons", traceback.format_exc())
                    png = None
                if png:  # icons never change: let the browser keep them across re-renders
                    self._send(200, png, "image/png", "max-age=604800, immutable")
                else:
                    self._send(404, "{}")
            else:
                self._send(404, "{}")

        def do_POST(self):
            name = self.path.rsplit("/", 1)[-1]
            if self.headers.get("X-Token") != token or not self.path.startswith("/api/") or name not in methods:
                self._send(403, "{}")
                return
            try:
                args = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"[]")
                result = getattr(api, name)(*args)
                self._send(200, json.dumps(result, ensure_ascii=False, default=str))
            except Exception:
                log_error(f"api.{name}", traceback.format_exc())
                self._send(500, json.dumps({"error": "falhou"}))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------- login progress popup
FLOW_TEXT = {
    "pt": {"title": "L2Axis · Avisos", "wait": "Esperando a tela de login", "typing": "Digitando login",
           "server": "Confirmando o servidor", "charsel": "Esperando a seleção de personagem",
           "start": "Entrando no jogo", "done_sel": "Na seleção de personagem", "done_game": "No jogo",
           "done_login": "Login enviado", "f_closed": "O client foi fechado", "f_account": "Conta sem login/senha salvos",
           "f_login": "A tela de login não apareceu (janela minimizada?)",
           "f_server": "O login não passou: confira login e senha", "f_charsel": "A seleção de personagem não apareceu",
           "f_start": "Não consegui entrar no jogo", "fallback": "Tela não reconhecida, usando o modo antigo",
           "f_dc": "Caiu: perdeu a conexão com o servidor", "f_dead": "Morreu", "f_crash": "O client fechou sozinho (travou ou caiu)"},
    "en": {"title": "L2Axis · Alerts", "wait": "Waiting for the login screen", "typing": "Typing the login",
           "server": "Confirming the server", "charsel": "Waiting for character selection",
           "start": "Entering the game", "done_sel": "At character selection", "done_game": "In game",
           "done_login": "Login sent", "f_closed": "The client was closed", "f_account": "No saved login/password",
           "f_login": "The login screen did not show up (window minimized?)",
           "f_server": "Login did not go through: check login and password",
           "f_charsel": "Character selection did not show up", "f_start": "Could not enter the game",
           "fallback": "Screen not recognised, using the old mode",
           "f_dc": "Dropped: lost the connection to the server", "f_dead": "Died", "f_crash": "The client closed by itself (crashed or dropped)"},
    "de": {"title": "L2Axis · Hinweise", "wait": "Warte auf den Login-Bildschirm", "typing": "Login wird eingegeben",
           "server": "Server wird bestätigt", "charsel": "Warte auf die Charakterauswahl",
           "start": "Betrete das Spiel", "done_sel": "In der Charakterauswahl", "done_game": "Im Spiel",
           "done_login": "Login gesendet", "f_closed": "Der Client wurde geschlossen",
           "f_account": "Kein Login/Passwort gespeichert",
           "f_login": "Der Login-Bildschirm kam nicht (Fenster minimiert?)",
           "f_server": "Login fehlgeschlagen: Login und Passwort prüfen",
           "f_charsel": "Die Charakterauswahl kam nicht", "f_start": "Konnte das Spiel nicht betreten",
           "fallback": "Bildschirm nicht erkannt, alter Modus",
           "f_dc": "Getrennt: Verbindung zum Server verloren", "f_dead": "Gestorben", "f_crash": "Der Client hat sich selbst beendet (Absturz oder getrennt)"},
}

FLOW_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:#14100c;height:100%;font:13px 'Segoe UI',sans-serif;color:#e8dcc0;overflow:hidden;user-select:none}
.box{margin:0;border:1px solid #7a6334;background:#14100c;padding:8px 10px;box-sizing:border-box;min-height:100vh}
.hd{display:flex;align-items:center;gap:8px;color:#d9b45a;font-weight:600;margin-bottom:4px}
.hd span{flex:1}.x{cursor:pointer;color:#a99;padding:0 4px}.x:hover{color:#fff}
.r{display:flex;align-items:center;gap:8px;padding:4px 0;border-top:1px solid rgba(255,255,255,.06)}
.r:first-of-type{border-top:0}.n{font-weight:600;min-width:86px;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.m{flex:1;color:#cbbf9f}.d{width:9px;height:9px;border-radius:50%;flex:none}
.run .d{background:#d9b45a;animation:p 1s infinite}.ok .d{background:#5fbf6a}.fail .d{background:#e05a4a}
.fail .m{color:#f08a7a}@keyframes p{50%{opacity:.3}}
</style></head><body><div class="box"><div class="hd"><span id="t"></span><b class="x" onclick="pywebview.api.close()">&#10005;</b></div><div id="l"></div></div>
<script>function set(s){document.getElementById('t').textContent=s.title;
document.getElementById('l').innerHTML=s.rows.map(r=>'<div class="r '+r.state+'"><i class="d"></i><span class="n"></span><span class="m"></span></div>').join('');
[...document.querySelectorAll('.r')].forEach((e,k)=>{e.querySelector('.n').textContent=s.rows[k].name;e.querySelector('.m').textContent=s.rows[k].msg;});
return Math.ceil(document.getElementById('l').getBoundingClientRect().bottom)+10;}
</script></body></html>"""


# ---------------------------------------------------------------- server announcements from the chat
# The chat box is read from the window image with the OCR that comes with Windows (through
# PowerShell, nothing to install). Only the announcement colour (cyan) is kept, so players' chat
# is ignored. Any one client in game and not minimized is enough: announcements are server-wide.
OCR_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait() | Out-Null; $task.Result }
$eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
while ($true) {
  $path = [Console]::In.ReadLine()
  if ($path -eq $null) { break }
  try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $res = Await ($eng.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
    $stream.Dispose()
    foreach ($l in $res.Lines) {
      $x1 = 1e9; $y1 = 1e9; $x2 = 0; $y2 = 0
      foreach ($w in $l.Words) { $r = $w.BoundingRect
        $x1 = [Math]::Min($x1, $r.X); $y1 = [Math]::Min($y1, $r.Y)
        $x2 = [Math]::Max($x2, $r.X + $r.Width); $y2 = [Math]::Max($y2, $r.Y + $r.Height) }
      "{0} {1} {2} {3}`t{4}" -f [int]$x1, [int]$y1, [int]$x2, [int]$y2, $l.Text
    }
  } catch { "ERR`t$_" }
  "<<END>>"
}
"""
CHAT_W, CHAT_H, CHAT_ZOOM = 520, 420, 3   # bottom-left corner of the client; zoomed for the OCR


def _bmp_bytes(raw, w, h):
    """32-bit top-down BMP file from BGRX pixels."""
    head = struct.pack("<2sIHHI", b"BM", 54 + len(raw), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, len(raw), 2835, 2835, 0, 0)
    return head + info + raw


def capture_chat(hwnd):
    """(BGRX bytes, width, height) of the chat corner, zoomed CHAT_ZOOM times, or None."""
    r = wintypes.RECT()
    if user32.IsIconic(hwnd) or not user32.GetClientRect(hwnd, ctypes.byref(r)) or r.right < 300:
        return None
    sw, sh = min(CHAT_W, r.right), min(CHAT_H, r.bottom)
    w, h = sw * CHAT_ZOOM, sh * CHAT_ZOOM
    src = user32.GetDC(hwnd)
    if not src:
        return None
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.SetStretchBltMode(mem, 4)  # HALFTONE: smooth zoom, better for the OCR
        gdi32.StretchBlt(mem, 0, 0, w, h, src, 0, r.bottom - sh, sw, sh, 0x00CC0020)
        buf = ctypes.create_string_buffer(w * h * 4)
        bi = _BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
        return buf.raw, w, h
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, src)


def _is_announcement(raw, w, box):
    """Most coloured pixels of the line are the announcement cyan."""
    x1, y1, x2, y2 = box
    cyan = other = 0
    for y in range(max(0, y1), y2, 2):
        o = y * w
        for x in range(max(0, x1), min(w, x2), 3):
            p = (o + x) * 4
            b, g, r = raw[p], raw[p + 1], raw[p + 2]
            if max(r, g, b) < 120:
                continue  # background
            if b > 150 and g > 150 and r < 130:
                cyan += 1
            elif max(r, g, b) - min(r, g, b) > 40 or min(r, g, b) > 170:
                other += 1
    return cyan > 8 and cyan > other * 2


class ChatReader(threading.Thread):
    """Reads the server announcements from the chat every few seconds."""
    INTERVAL = 4

    def __init__(self, pick_hwnd, on_message):
        super().__init__(daemon=True)
        self._pick = pick_hwnd
        self._on = on_message
        self._ps = None
        self._seen = {}   # announcement text -> last time it was on screen
        self.active = False
        self._tmp_dir = Path(os.environ.get("TEMP", str(APP_DIR)))
        self._n = 0
        self._script = Path(os.environ.get("TEMP", str(APP_DIR))) / "l2axis_ocr.ps1"

    def _ocr(self, path):
        if self._ps is None or self._ps.poll() is not None:
            self._script.write_text(OCR_SCRIPT, encoding="utf-8")
            self._ps = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self._script)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, encoding="utf-8", errors="replace")
        self._ps.stdin.write(str(path) + "\n")
        self._ps.stdin.flush()
        lines = []
        while True:
            line = self._ps.stdout.readline()
            if not line or line.strip() == "<<END>>":
                break
            lines.append(line.rstrip("\n"))
        return lines

    def read_once(self):
        hwnd = self._pick()
        self.active = bool(hwnd)
        if not hwnd:
            return []
        cap = capture_chat(hwnd)
        if not cap:
            return []
        raw, w, h = cap
        # a new file each time (Windows may still hold the previous one for a moment)
        self._n = (self._n + 1) % 4
        tmp = self._tmp_dir / f"l2axis_chat{self._n}.bmp"
        try:
            tmp.write_bytes(_bmp_bytes(raw, w, h))
        except OSError:
            return []
        msgs = []
        for line in self._ocr(tmp):
            box, _, text = line.partition("\t")
            if box == "ERR" or not text.strip():
                continue
            try:
                x1, y1, x2, y2 = map(int, box.split())
            except ValueError:
                continue
            if not _is_announcement(raw, w, (x1, y1, x2, y2)):
                continue
            # drop a stray letter read from the scroll bar before the message
            text = re.sub(r"^.{0,3}?(?=\[|Announcements?:)", "", text.strip())
            # a long announcement wraps: lines that do not start a new message continue the last one
            if msgs and not re.match(r"^(\[|Announcements?:|[A-Z][\w' ]+:)", text):
                msgs[-1] = msgs[-1] + " " + text
            else:
                msgs.append(text)
        return msgs

    def run(self):
        while True:
            try:
                now = time.time()
                for text in self.read_once():
                    key = re.sub(r"\W+", "", text.lower())
                    last = self._seen.get(key)
                    self._seen[key] = now
                    if last is None or now - last > 20 * 60:  # still on screen = the same message
                        self._on(text, now)
                for k in [k for k, t in self._seen.items() if now - t > 3600]:
                    del self._seen[k]
            except Exception:
                log_error("chat reader", traceback.format_exc())
                time.sleep(10)
            time.sleep(self.INTERVAL)


def parse_announcement(text, at):
    """What an announcement says about an event: (event key, status, start time or None)."""
    low = text.lower()
    key = None
    for ev in SERVER_EVENTS:
        names = [ev["name"].lower()] + [a.lower() for a in ev.get("aliases", [])]
        if any(n in low for n in names):
            key = ev["key"]
            break
    if not key:
        return None
    m = re.search(r"registration is open for (\d+) seconds", low)
    if m:
        return key, "reg", at + int(m.group(1))
    m = re.search(r"starts? in (\d+) (minute|second)", low)
    if m:
        return key, "reg" if "regist" in low else "soon", at + int(m.group(1)) * (60 if m.group(2) == "minute" else 1)
    if re.search(r"cancel", low):
        return key, "cancelled", None
    if re.search(r"was killed|has been killed|disappeared|has ended|is over|finished|winner|won ", low):
        return key, "ended", None
    if re.search(r"has started|have started|begins|began|has begun|spawned|appeared|invade|is on|started", low):
        return key, "started", at
    return key, "info", None


class LoginFlows:
    """Progress of the automatic logins, shown in a small always-on-top window that does not
    take the focus. It hides by itself a few seconds after everything went fine; a failure
    stays until closed."""
    W, ROW, HEAD = 360, 27, 44

    def __init__(self, lang_of):
        self._lang_of = lang_of
        self._flows = {}  # slot -> {"name", "state", "key", "at"}
        self._lock = threading.Lock()
        self._win = None
        self._shown = False
        self._dirty = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def set(self, slot, name, key, state="run"):
        with self._lock:
            self._flows[slot] = {"name": name, "key": key, "state": state, "at": time.time()}
        self._dirty.set()

    def close(self):
        with self._lock:
            # hide everything; a login still running shows up again at its next step
            self._flows = {}
        self._dirty.set()

    def _view(self):
        txt = FLOW_TEXT.get(self._lang_of(), FLOW_TEXT["en"])
        with self._lock:
            now = time.time()
            # finished fine for a few seconds: drop the row
            self._flows = {k: f for k, f in self._flows.items() if not (f["state"] == "ok" and now - f["at"] > 6)}
            rows = [{"name": f["name"], "state": f["state"], "msg": txt.get(f["key"], f["key"])}
                    for _, f in sorted(self._flows.items())]
        return {"title": txt["title"], "rows": rows}

    def _place(self, n):
        x, y, w, h = work_area()
        height = self.HEAD + self.ROW * n
        return x + w - self.W - 12, y + h - 12 - (height if n else 0), height

    POPUP_TITLE = "L2Axis Alerts"

    def _find_hwnd(self):
        found = []

        def cb(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid() and window_title(hwnd) == self.POPUP_TITLE:
                found.append(hwnd)
            return True

        user32.EnumWindows(EnumWindowsProc(cb), 0)
        return found[0] if found else None

    def _loop(self):
        GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_APPWINDOW = -20, 0x80, 0x08000000, 0x40000
        SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
        HWND_TOPMOST = wintypes.HWND(-1)
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        hwnd = None
        while True:
            self._dirty.wait(1.0)
            self._dirty.clear()
            try:
                view = self._view()
                n = len(view["rows"])
                if not n:
                    if hwnd and self._shown:
                        user32.ShowWindow(hwnd, 0)  # SW_HIDE
                        self._shown = False
                    continue
                if not self._win:
                    before = user32.GetForegroundWindow()
                    x, y, height = self._place(n)
                    self._win = webview.create_window(
                        self.POPUP_TITLE, html=FLOW_HTML, js_api=_FlowApi(self), width=self.W, height=height,
                        x=x, y=y, frameless=True, easy_drag=True, resizable=False, on_top=True, focus=False,
                        background_color="#14100c")
                    self._win.events.loaded.wait(10)
                    hwnd = self._find_hwnd()
                    if hwnd:
                        # never takes the keyboard from the game, and no taskbar button
                        ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
                        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                                 (ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW)
                    if before and user32.GetForegroundWindow() != before:
                        user32.SetForegroundWindow(before)
                # long messages wrap: size the window to its content (CSS pixels -> screen pixels)
                css_h = int(self._win.evaluate_js(f"set({json.dumps(view)})") or 0) or (self.HEAD + self.ROW * n)
                if not hwnd:
                    hwnd = self._find_hwnd()
                if hwnd:
                    scale = (user32.GetDpiForWindow(hwnd) or 96) / 96
                    w, h = round(self.W * scale), round(css_h * scale)
                    x, y, _ = self._place(0)
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y - h, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
                    self._shown = True
            except Exception:  # the popup is a convenience: never break the login because of it
                log_error("login popup", traceback.format_exc())
                time.sleep(2)


class _FlowApi:
    def __init__(self, flows):
        self._flows = flows

    def close(self):
        self._flows.close()


def main():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass
    api = Api()
    token = secrets.token_urlsafe(24)
    server = make_server(api, token)
    pos = api._data.get("pos") or [40, 40]
    width, height = api._size()
    window = webview.create_window(
        "L2Axis Control", url=f"http://127.0.0.1:{server.server_port}/?t={token}"
                             + (f"&view={os.environ['L2AXIS_VIEW']}" if os.environ.get("L2AXIS_VIEW") else ""),
        width=width, height=COMPACT_H if api._data["compact"] else height,
        x=pos[0], y=pos[1], frameless=True, easy_drag=False, resizable=False,
        on_top=api._data["topmost"], transparent=True, background_color="#000000",
    )
    api._window = window
    clear_board_file()  # never leave an old Alt+B page (character name/IP) on disk
    webview.start(private_mode=True, icon=str(APP_ICON) if APP_ICON.exists() else None)
    clear_board_file()


if __name__ == "__main__":
    main()
