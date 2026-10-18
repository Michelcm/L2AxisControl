"""L2Axis Control - floating panel to manage L2Axis (Lineage II) game clients.

Python backend (Windows API through ctypes) + an HTML/CSS interface styled after
the Lineage II windows, shown in a frameless window through pywebview.

It never interacts with the game itself (no automated input): it only opens,
arranges and monitors the windows, measures the network and tracks timers/votes.
"""
import base64
import ctypes
import json
import math
import re
import os
import socket
import struct
import subprocess
import threading
import time
import webbrowser
import winsound
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
MAX_SLOTS = 5
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
# GM tribute (published with the GM's permission): gm.json + assets/gm_*.png.
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


def main_windows_by_pid():
    """pid -> largest visible top-level window of that process."""
    result = {}

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        area = (r.right - r.left) * (r.bottom - r.top)
        if pid.value not in result or area > result[pid.value][1]:
            result[pid.value] = (hwnd, area)
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


def work_area():
    r = wintypes.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right - r.left, r.bottom - r.top


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
               "rename_windows": True, "opacity": 0.92, "size": None, "scale": 1.0, "accounts": {}, "login_delay": 15,
               "bosses": {}, "lang": "pt",
               "boss_view": {"sort": "respawn", "range": "", "min": "", "max": ""}}
    try:
        default.update(json.loads(DATA_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    default["slots"] = (default["slots"] + [f"Account {i + 1}" for i in range(MAX_SLOTS)])[:MAX_SLOTS]
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


def item_slot(name):
    slot = next((slot for word, slot in _SLOTS if word in name), None)
    # one-piece armors/robes cover chest + legs: never compare them with a breastplate/tunic
    if slot == "chest" and ("Armor" in name or "Robe" in name) and "Breastplate" not in name:
        return "fullbody"
    return slot


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
        return json.loads(ITEMS_DB_FILE.read_text(encoding="utf-8"))
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


class RecipeBook:
    def __init__(self, db):
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
            slot = item_slot(me["name"])
            jewel = slot in ("earring", "necklace", "ring")
            same = lambda it: (it["kind"] == "armor" and item_slot(it["name"]) == slot
                               and (jewel or it["atype"] == me["atype"]))
            key = lambda it: (it["mdef"] if jewel else it["pdef"], it["mp"])
        bonus, masterwork = extra.get("bonus", {}), extra.get("masterwork", {})
        # masterwork versions of this recipe's product (sealed and unsealed for armor)
        my_mw = {int(k) for k, c in masterwork.items() if c["product"] == recipe["product"]}
        # the client is inconsistent with plurals ("Sealed Dynasty Gaiter" unseals into "Dynasty Gaiters")
        plain = lambda name: re.sub(r"s\b", "", name.lower())
        unsealed_name = plain(me["name"][len("Sealed "):]) if me["name"].startswith("Sealed ") else None
        rows, seen = [], set()
        for item_id, it in items.items():
            # skip Special Ability variants and {PvP} copies: same base stats as the plain item
            if (it["add"] and item_id != recipe["product"]) or _COMPARE_SKIP.search(it["name"]) or not same(it):
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
                   "unsealed": bool(unsealed_name) and plain(it["name"]) == unsealed_name}
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

    def top_tier(self):
        out = []
        for r in self.recipes.values():
            product = self.name(r["product"])
            family = next((f for f in CRAFT_FAMILIES if re.search(rf"\b{f}\b", product)), None)
            if not family:
                continue
            kind = ("jewelry" if any(j in product for j in _JEWELRY)
                    else "armor" if product.startswith("Sealed ") else "weapon")
            out.append({"id": r["id"], "productId": r["product"], "product": product, "family": family,
                        "grade": CRAFT_FAMILIES[family],
                        "kind": kind, "level": r["level"], "rate": r["rate"], "mp": r["mp"]})
        return sorted(out, key=lambda x: (x["grade"], x["family"], x["kind"], x["product"], -x["rate"]))

    def _expand(self, recipe, needed, expand_all):
        """Expand one recipe; returns (tree, base totals, craft steps, stats)."""
        totals, steps, stats = {}, {}, {"mp": 0, "crafts": 0, "maxLevel": 0}

        def walk(rec, need, depth):
            crafts = math.ceil(need / rec["count"])
            stats["mp"] += crafts * rec["mp"]
            stats["crafts"] += crafts
            stats["maxLevel"] = max(stats["maxLevel"], rec["level"])
            step = steps.setdefault(rec["product"], {"id": rec["product"], "name": self.name(rec["product"]),
                                                     "crafts": 0, "makes": 0, "level": rec["level"],
                                                     "rate": rec["rate"], "mp": 0, "depth": 0})
            step["crafts"] += crafts
            step["makes"] += crafts * rec["count"]
            step["mp"] += crafts * rec["mp"]
            step["depth"] = max(step["depth"], depth)
            node = {"id": rec["product"], "name": self.name(rec["product"]), "qty": need, "crafts": crafts,
                    "rate": rec["rate"], "level": rec["level"], "mp": rec["mp"], "count": rec["count"],
                    "children": []}
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
        quantity = max(1, min(999, int(quantity)))
        root, shopping, plan, stats = self._expand(recipe, quantity * recipe["count"], expand_all)
        out = {"root": root, "shopping": shopping, "plan": plan, "recipeItem": self.name(recipe["recipeItem"]),
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
        self._slot_pid = [None] * MAX_SLOTS
        self._slot_start = [None] * MAX_SLOTS
        self._pending = []  # (slot, launched_at, popen_pid)
        self._known = set()
        self._windows = {}
        self._vote_ready = {}
        self._timer_fired = set()
        self._alerts = []
        self._window = None
        self._had_window = [False] * MAX_SLOTS
        self._autologin_slots = set()  # slots opened by the panel that should log in by themselves
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
        self._board = BoardWatcher(self._on_board_rows)
        self._board.start()
        self._adopt_running()

    # -------------------------------------------------- util
    def _save(self):
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, DATA_FILE)

    def _alert(self, key, **args):
        """Alerts are sent as a code + parameters; the interface translates them."""
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
        self._alerts.append({"k": key, **args})

    def _hwnd(self, i):
        pid = self._slot_pid[i]
        return self._windows.get(pid) if pid else None

    def _adopt_running(self):
        for pid in list_l2_pids():
            free = next((i for i in range(MAX_SLOTS) if not self._slot_pid[i]), None)
            if free is not None:
                self._slot_pid[free] = pid
                self._slot_start[free] = time.time()
            self._known.add(pid)

    def _refresh_clients(self):
        pids = list_l2_pids()
        alive = set(pids)
        self._windows = main_windows_by_pid()
        now = time.time()
        for i in range(MAX_SLOTS):
            if self._slot_pid[i] and self._slot_pid[i] not in alive:
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

        slots = []
        for i in range(MAX_SLOTS):
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
                "auto": bool(acc.get("auto"))}

    def _schedule_login(self, i, pid, delay):
        def work():
            time.sleep(float(delay))
            if self._slot_pid[i] == pid:
                self.fill_login(i)
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
        return self._recipes.top_tier() if self._recipes else []

    def recipe_tree(self, recipe_id, quantity, expand_all=True):
        return self._recipes.tree(recipe_id, quantity, bool(expand_all)) if self._recipes else None

    def item_sources(self, item_id):
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
                "slots": slots, "external": external, "running": running,
                "hwidLimit": HWID_LIMIT, "ping": self._ping.stats(), "pingHistory": PING_HISTORY,
                "server": SERVER_IP, "warp": self._warp.state, "votes": votes, "timers": timers,
                "alerts": alerts, "topmost": self._data["topmost"], "compact": self._data["compact"],
                "renameWindows": self._data["rename_windows"], "opacity": self._data["opacity"],
                "scale": self._data.get("scale", 1.0),
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
        for i in range(MAX_SLOTS):
            self.close(i)
        bound = set(self._slot_pid)
        for pid in list_l2_pids():
            if pid not in bound and pid in self._windows:
                user32.PostMessageW(self._windows[pid], WM_CLOSE, 0, 0)

    def minimize_all(self):
        for pid in list_l2_pids():
            if pid in self._windows:
                user32.ShowWindow(self._windows[pid], SW_MINIMIZE)

    def tile(self):
        bound = [p for p in self._slot_pid if p]
        order = bound + [p for p in list_l2_pids() if p not in bound]
        hwnds = [self._windows[p] for p in order if p in self._windows]
        if not hwnds:
            return
        x0, y0, w, h = work_area()
        cols = math.ceil(math.sqrt(len(hwnds)))
        rows = math.ceil(len(hwnds) / cols)
        cw, ch = w // cols, h // rows
        for k, hwnd in enumerate(hwnds):
            r, c = divmod(k, cols)
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.MoveWindow(hwnd, x0 + c * cw, y0 + r * ch, cw, ch, True)

    def rename(self, i, name):
        i = int(i)
        name = (name or "").strip()[:20] or f"Account {i + 1}"
        with self._lock:
            self._data["slots"][i] = name
            self._save()
        return name

    def get_account(self, i):
        return self._account_info(int(i))

    def set_account(self, i, login, password, auto):
        """An empty password keeps the current one. The password is never sent back to the interface."""
        key = str(int(i))
        login = (login or "").strip()
        with self._lock:
            acc = self._data["accounts"].get(key, {})
            acc["login"] = login
            acc["auto"] = bool(auto)
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
            elif key == "renameWindows":
                self._data["rename_windows"] = bool(value)
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
            elif key == "scale":
                self._data["scale"] = max(0.9, min(1.5, float(value)))
            self._save()

    def minimize_self(self):
        self._window.minimize()

    def quit(self):
        self._window.destroy()

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
        return True

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
