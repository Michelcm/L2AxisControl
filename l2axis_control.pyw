"""L2Axis Control - floating panel to manage L2Axis (Lineage II) game clients.

Python backend (Windows API through ctypes) + an HTML/CSS interface styled after
the Lineage II windows, shown in a frameless window through pywebview.

It never interacts with the game itself (no automated input): it only opens,
arranges and monitors the windows, measures the network and tracks timers/votes.
"""
import base64
import ctypes
import json
import re
import math
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
WIN_W, WIN_H, COMPACT_H = 392, 652, 104
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
               "rename_windows": True, "opacity": 0.92, "accounts": {}, "login_delay": 15,
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
                self._window.resize(WIN_W, COMPACT_H if value else WIN_H)
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
        # no bottom limit: the height changes in compact mode and anything past the window is ignored
        frame = gdi32.CreateRectRgn(inset, inset, w, 20000)
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

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/ui.html"):
                self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            elif path.startswith("/assets/") and path[8:] in ASSETS and (ASSETS_DIR / path[8:]).exists():
                self._send(200, (ASSETS_DIR / path[8:]).read_bytes(), "image/png")
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
    window = webview.create_window(
        "L2Axis Control", url=f"http://127.0.0.1:{server.server_port}/?t={token}"
                             + (f"&view={os.environ['L2AXIS_VIEW']}" if os.environ.get("L2AXIS_VIEW") else ""),
        width=WIN_W, height=COMPACT_H if api._data["compact"] else WIN_H,
        x=pos[0], y=pos[1], frameless=True, easy_drag=False, resizable=False,
        on_top=api._data["topmost"], transparent=True, background_color="#000000",
    )
    api._window = window
    clear_board_file()  # never leave an old Alt+B page (character name/IP) on disk
    webview.start(private_mode=True, icon=str(APP_ICON) if APP_ICON.exists() else None)
    clear_board_file()


if __name__ == "__main__":
    main()
