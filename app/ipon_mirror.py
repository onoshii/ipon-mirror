"""Ipon Mirror - iPhone の画面を無線 (AirPlay) で Windows に映すアプリ

受信エンジンは UxPlay (https://github.com/FDH2/UxPlay, GPL-3.0) に
小さな改造 (engine/uxplay-ipon.patch) を加えたもの。
このファイルは GPL-3.0 で配布されます。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk

APP_NAME = "Ipon Mirror"
VERSION = "1.0.0"

# ---------------------------------------------------------------- paths
if getattr(sys, "frozen", False):  # PyInstaller でまとめた配布版
    BASE_DIR = os.path.dirname(sys.executable)
    RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # リポジトリ直下
    RES_DIR = os.path.dirname(os.path.abspath(__file__))

ICON_ICO = os.path.join(RES_DIR, "assets", "icon.ico")
ICON_PNG = os.path.join(RES_DIR, "assets", "icon_256.png")
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "IponMirror")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOG_FILE = os.path.join(DATA_DIR, "engine.log")


def find_engine():
    """(uxplay.exe のパス, 実行時の環境変数) を返す。見つからなければ (None, None)"""
    env = dict(os.environ)
    bundled = os.path.join(BASE_DIR, "engine")
    exe = os.path.join(bundled, "uxplay.exe")
    if os.path.exists(exe):  # 配布版: エンジンと GStreamer を同梱
        env["PATH"] = bundled + os.pathsep + env.get("PATH", "")
        env["GST_PLUGIN_SYSTEM_PATH_1_0"] = os.path.join(bundled, "lib", "gstreamer-1.0")
        env["GST_PLUGIN_PATH_1_0"] = ""
        env["GST_PLUGIN_SCANNER_1_0"] = os.path.join(bundled, "libexec", "gst-plugin-scanner.exe")
        env["GST_REGISTRY_1_0"] = os.path.join(DATA_DIR, "gst-registry.bin")
        return exe, env
    exe = os.path.join(BASE_DIR, "build", "UxPlay", "build", "uxplay.exe")
    if os.path.exists(exe):  # 開発版: MSYS2 でビルドしたもの
        env["PATH"] = r"C:\msys64\ucrt64\bin" + os.pathsep + env.get("PATH", "")
        return exe, env
    return None, None


DEFAULTS = {"name": APP_NAME, "audio": True}

# 低遅延: 映像を音声に同期させない + GPU (D3D12) でデコードから表示まで完結
FAST_ARGS = ["-vsync", "no", "-vd", "d3d12h264dec", "-vc", "d3d12convert", "-vs", "d3d12videosink"]
SAFE_ARGS = ["-vsync", "no"]

BG, FG, SUB, ACCENT, OK, ERR, BTN = ("#16171b", "#f2f2f4", "#9a9ba3", "#0a84ff",
                                     "#30d158", "#ff453a", "#2a2b31")

# ---------------------------------------------------------------- Win32
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
user32.SetParent.argtypes = [wt.HWND, wt.HWND]
user32.SetParent.restype = wt.HWND
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumChildWindows.argtypes = [wt.HWND, WNDENUMPROC, wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.EnableWindow.argtypes = [wt.HWND, wt.BOOL]
user32.MoveWindow.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.BOOL]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_uint]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
dwmapi.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
WS_CAPTION, WS_THICKFRAME, WS_SYSMENU = 0x00C00000, 0x00040000, 0x00080000
WS_POPUP, WS_CHILD, WS_MINMAX, WS_CLIPCHILDREN = 0x80000000, 0x40000000, 0x00030000, 0x02000000


def s32(v):
    """32bit の符号なし値を SetWindowLongW 用の符号付きに"""
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def monitor_rect(hwnd):
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        r = mi.rcMonitor
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    return None


def child_windows(hwnd):
    found = []
    cb = WNDENUMPROC(lambda h, _: found.append(h) or True)
    user32.EnumChildWindows(hwnd, cb, 0)
    return found


def top_windows_of_pid(pid):
    found = []

    def cb(h, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(h):
            found.append(h)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


# --- アプリ終了時にエンジンも必ず終わらせる (Job Object)
class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rt", "wt", "ot")]

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
user32.IsWindowEnabled.argtypes = [wt.HWND]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]


def make_kill_job():
    job = kernel32.CreateJobObjectW(None, None)
    if job:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    return job


def kill_leftover_engines(exe_path):
    """前回の異常終了などで残ったエンジン (同じ exe) を終了させる。
    残っていると iPhone から同じ ID の受信機が 2 つ見え、接続が不安定になる。"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq uxplay.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except OSError:
        return
    target = os.path.normcase(os.path.abspath(exe_path))
    for row in out.splitlines():
        parts = row.split('","')
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        pid = int(parts[1])
        h = kernel32.OpenProcess(0x1000 | 0x0001, False, pid)  # QUERY_LIMITED_INFORMATION | TERMINATE
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                if os.path.normcase(buf.value) == target:
                    kernel32.TerminateProcess(h, 1)
        finally:
            kernel32.CloseHandle(h)


# ---------------------------------------------------------------- settings
def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(s):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- video window
class VideoWindow:
    """タイトルバーなしの映像ウィンドウ。
    ドラッグで移動 / ホイールで拡大縮小 / ダブルクリックで全画面 / 右クリックでメニュー / 右上の × で終了"""

    def __init__(self, root, on_close):
        self.root = root
        self.on_close = on_close
        self.win = tk.Toplevel(root, bg="black")
        self.win.title(f"{APP_NAME} - 画面")
        self.win.overrideredirect(True)
        self.win.withdraw()
        self.surface = tk.Frame(self.win, bg="black", highlightthickness=0, bd=0)
        self.surface.pack(fill="both", expand=True)
        self.win.update_idletasks()
        self.hwnd_surface = self.surface.winfo_id()
        user32.SetWindowLongW(self.hwnd_surface, GWL_STYLE,
                              s32(user32.GetWindowLongW(self.hwnd_surface, GWL_STYLE) | WS_CLIPCHILDREN))
        self.last_src = (884, 1920)
        self.aspect = 884 / 1920
        self.fullscreen = False
        self.saved_center = None
        self.topmost = tk.BooleanVar(value=False)
        self._drag = None
        self._hide_close_job = None

        # 閉じるボタン (マウスを動かすと右上に出る)
        self.close_btn = tk.Label(self.win, text="✕", bg="#2a2b31", fg="#f2f2f4", cursor="hand2",
                                  font=("Segoe UI", 11, "bold"), padx=9, pady=2)
        self.close_btn.bind("<Button-1>", lambda e: self.on_close())
        self.close_btn.bind("<Enter>", lambda e: self.close_btn.config(bg=ERR))
        self.close_btn.bind("<Leave>", lambda e: self.close_btn.config(bg="#2a2b31"))

        for w in (self.win, self.surface):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
            w.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
            w.bind("<MouseWheel>", self._wheel)
            w.bind("<Button-3>", self._menu)
            w.bind("<Motion>", lambda e: self._show_close())
        self.win.bind("<Escape>", lambda e: self.fullscreen and self.toggle_fullscreen())
        self.surface.bind("<Configure>", lambda e: self.fit_children())

        self.menu = tk.Menu(self.win, tearoff=0)
        self.menu.add_checkbutton(label="常に手前に表示", variable=self.topmost,
                                  command=lambda: self.win.attributes("-topmost", self.topmost.get()))
        self.menu.add_command(label="全画面 / 元に戻す", command=self.toggle_fullscreen)
        self.menu.add_separator()
        self.menu.add_command(label="ミラーリングを終了", command=self.on_close)

        self.win.after(10, self._setup_frame)
        self._watch()

    def frame_hwnd(self):
        return user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()

    def _setup_frame(self):
        # Win11 の角丸 + 通常のアプリ窓として扱う (タスクバー / Alt+Tab / Discord 等の画面共有に出す)
        try:
            hwnd = self.frame_hwnd()
            pref = ctypes.c_int(2)
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, s32((ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW))
        except (OSError, AttributeError):
            pass

    # 映像 (エンジン側の子ウィンドウ) をマウス無効にして、操作はこの窓で受ける
    def fit_children(self):
        w, h = self.surface.winfo_width(), self.surface.winfo_height()
        for c in child_windows(self.hwnd_surface):
            if user32.GetParent(c) != self.hwnd_surface:
                continue
            if user32.IsWindowEnabled(c):
                user32.EnableWindow(c, False)
            rc = wt.RECT()
            user32.GetClientRect(c, ctypes.byref(rc))
            if (rc.right, rc.bottom) != (w, h):
                user32.MoveWindow(c, 0, 0, w, h, True)

    def _watch(self):
        if self.win.winfo_viewable():
            self.fit_children()
        self.win.after(500, self._watch)

    def _show_close(self):
        self.close_btn.place(relx=1.0, x=-10, y=10, anchor="ne")
        self.close_btn.lift()
        if self._hide_close_job:
            self.win.after_cancel(self._hide_close_job)
        self._hide_close_job = self.win.after(2000, self._hide_close)

    def _hide_close(self):
        self._hide_close_job = None
        x, y = self.win.winfo_pointerxy()
        if self.win.winfo_containing(x, y) is self.close_btn:  # ボタン上なら出したまま
            self._hide_close_job = self.win.after(1000, self._hide_close)
        else:
            self.close_btn.place_forget()

    def _screen(self):
        r = monitor_rect(self.frame_hwnd()) if self.win.winfo_viewable() else None
        return r or (0, 0, self.win.winfo_screenwidth(), self.win.winfo_screenheight())

    def _fit_size(self, long_side, mon):
        mx, my, sw, sh = mon
        if self.aspect >= 1:
            w, h = long_side, int(long_side / self.aspect)
        else:
            w, h = int(long_side * self.aspect), long_side
        k = min(1.0, sw * 0.95 / w, sh * 0.95 / h)
        return max(int(w * k), 50), max(int(h * k), 50)

    def _place(self, w, h, cx, cy, mon):
        mx, my, sw, sh = mon
        cx = min(max(cx, mx + w // 2), mx + sw - w // 2)
        cy = min(max(cy, my + h // 2), my + sh - h // 2)
        self.win.geometry(f"{w}x{h}+{cx - w // 2}+{cy - h // 2}")

    def show(self, src_w, src_h):
        self.last_src = (src_w, src_h)
        self.aspect = src_w / src_h
        if self.fullscreen:  # 全画面中は映像側が自動で収まる。戻すときに新しい向きで作り直す
            return
        mon = self._screen()
        if self.win.winfo_viewable():  # 回転: 中心と長辺の長さを保って縦横を入れ替える
            cx = self.win.winfo_x() + self.win.winfo_width() // 2
            cy = self.win.winfo_y() + self.win.winfo_height() // 2
            long_side = max(self.win.winfo_width(), self.win.winfo_height())
            first = False
        else:
            cx, cy = mon[0] + mon[2] // 2, mon[1] + mon[3] // 2
            long_side = int(mon[3] * 0.85)
            first = True
        w, h = self._fit_size(long_side, mon)
        self._place(w, h, cx, cy, mon)
        if first:
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()

    def hide(self):
        if self.fullscreen:
            self.toggle_fullscreen()
        self.close_btn.place_forget()
        self.win.withdraw()

    def toggle_fullscreen(self):
        if not self.win.winfo_viewable():
            return
        if not self.fullscreen:
            self.saved_center = (self.win.winfo_x() + self.win.winfo_width() // 2,
                                 self.win.winfo_y() + self.win.winfo_height() // 2,
                                 max(self.win.winfo_width(), self.win.winfo_height()))
            mx, my, sw, sh = self._screen()  # 今いるモニターで全画面
            self.win.geometry(f"{sw}x{sh}+{mx}+{my}")
            self.fullscreen = True
        else:
            self.fullscreen = False
            mon = self._screen()
            cx, cy, long_side = self.saved_center
            w, h = self._fit_size(long_side, mon)  # 全画面中に回転していても今の向きで戻す
            self._place(w, h, cx, cy, mon)
        self.win.focus_force()

    def _drag_start(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag_move(self, e):
        if self._drag and not self.fullscreen:
            self.win.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _wheel(self, e):
        if self.fullscreen:
            return
        f = 1.08 if e.delta > 0 else 1 / 1.08
        w0, h0 = self.win.winfo_width(), self.win.winfo_height()
        long_side = max(200, int(max(w0, h0) * f))
        w, h = self._fit_size(long_side, (0, 0, 10 ** 5, 10 ** 5))
        self.win.geometry(f"{w}x{h}+{self.win.winfo_x() + (w0 - w) // 2}+{self.win.winfo_y() + (h0 - h) // 2}")

    def _menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)


# ---------------------------------------------------------------- main window
class MirrorApp:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.proc_started = 0.0
        self.last_restart = 0.0
        self.use_fast = True
        self.device = ""
        self.recent = []  # エンジンの直近の出力 (エラー表示用)
        self.q = queue.Queue()
        self.job = make_kill_job()
        self.settings = load_settings()
        self.engine, self.engine_env = find_engine()

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.resizable(False, False)
        try:
            root.iconbitmap(default=ICON_ICO)
        except tk.TclError:
            pass
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.update_idletasks()
        try:  # ダークなタイトルバー
            dark = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(user32.GetParent(root.winfo_id()), 20,
                                         ctypes.byref(dark), ctypes.sizeof(dark))
        except (OSError, AttributeError):
            pass

        self.video = VideoWindow(root, self.end_mirroring)
        self._build_ui()

        self.root.after(100, self.poll)
        if self.engine:
            kill_leftover_engines(self.engine)
            self.start()
        else:
            self.set_state("error", "受信エンジンが見つかりません")

    def _build_ui(self):
        wrap = tk.Frame(self.root, bg=BG, padx=28, pady=24)
        wrap.pack()
        try:
            self.logo = tk.PhotoImage(file=ICON_PNG).subsample(4, 4)
            tk.Label(wrap, image=self.logo, bg=BG).pack()
        except tk.TclError:
            pass
        tk.Label(wrap, text=APP_NAME, bg=BG, fg=FG, font=("Yu Gothic UI", 15, "bold")).pack(pady=(10, 2))

        # メイン画面と設定画面を同じ場所に重ねて切り替える (ウィンドウサイズは変わらない)
        body = tk.Frame(wrap, bg=BG)
        body.pack(fill="both", expand=True)
        self.main_view = tk.Frame(body, bg=BG)
        self.settings_view = tk.Frame(body, bg=BG)
        for v in (self.main_view, self.settings_view):
            v.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)

        self.status = tk.Label(self.main_view, text="", bg=BG, fg=SUB, font=("Yu Gothic UI", 9),
                               wraplength=240, justify="center", height=2)
        self.status.pack(pady=(0, 16))
        self.btn = tk.Label(self.main_view, text="", bg=BTN, fg=FG,
                            font=("Yu Gothic UI", 11, "bold"), width=18, pady=8, cursor="hand2")
        self.btn.pack()
        self.btn.bind("<Button-1>", lambda e: self.toggle())
        gear = tk.Label(self.main_view, text="設定", bg=BG, fg=SUB,
                        font=("Yu Gothic UI", 9, "underline"), cursor="hand2")
        gear.pack(pady=(14, 0))
        gear.bind("<Button-1>", lambda e: self.settings_view.tkraise())

        sv = self.settings_view
        tk.Label(sv, text="表示名", bg=BG, fg=SUB, font=("Yu Gothic UI", 9)).pack(anchor="w")
        self.name_var = tk.StringVar(value=self.settings["name"])
        tk.Entry(sv, textvariable=self.name_var, bg=BTN, fg=FG, insertbackground=FG,
                 relief="flat", font=("Yu Gothic UI", 10)).pack(fill="x", ipady=4, pady=(2, 10))
        self.audio_var = tk.BooleanVar(value=self.settings["audio"])
        tk.Checkbutton(sv, text="音声もPCで鳴らす", variable=self.audio_var, bg=BG, fg=FG,
                       selectcolor=BTN, activebackground=BG, activeforeground=FG,
                       font=("Yu Gothic UI", 9)).pack(anchor="w")
        done = tk.Label(sv, text="完了", bg=BG, fg=SUB, font=("Yu Gothic UI", 9, "underline"),
                        cursor="hand2")
        done.pack(side="bottom")
        done.bind("<Button-1>", lambda e: self.close_settings())
        self.main_view.tkraise()

    # ---- 表示 ----
    def set_state(self, state, detail=""):
        name = self.settings["name"]
        if state == "waiting":
            self.status.config(text=f"iPhoneの「画面ミラーリング」で\n「{name}」を選んでください", fg=SUB)
            self.btn.config(text="停止", bg=BTN)
        elif state == "mirroring":
            self.status.config(text=f"● {self.device} を表示中" if self.device else "● 表示中", fg=OK)
            self.btn.config(text="停止", bg=BTN)
        elif state == "stopped":
            self.status.config(text="停止しています", fg=SUB)
            self.btn.config(text="開始", bg=ACCENT)
        else:
            self.status.config(text=detail, fg=ERR)
            self.btn.config(text="開始", bg=ACCENT)

    def close_settings(self):
        self.main_view.tkraise()
        name = self.name_var.get().strip() or DEFAULTS["name"]
        self.name_var.set(name)
        if (name, self.audio_var.get()) == (self.settings["name"], self.settings["audio"]):
            return
        self.settings.update(name=name, audio=self.audio_var.get())
        save_settings(self.settings)
        if self.proc:  # 動作中なら受信をやり直して反映
            self.stop()
            self.start()

    # ---- 受信エンジン ----
    def toggle(self):
        if self.proc:
            self.stop()
        elif self.engine:
            self.start()

    def start(self):
        if self.proc or not self.engine:
            return
        name = self.settings["name"]
        args = [self.engine, "-n", name, "-nh", "-s", "1920x1920@60", "-fps", "60", "-nohold"]
        args += FAST_ARGS if self.use_fast else SAFE_ARGS
        if not self.settings["audio"]:
            args += ["-as", "0"]
        try:
            self.proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=self.engine_env, cwd=os.path.dirname(self.engine),
                creationflags=subprocess.CREATE_NO_WINDOW,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            self.proc = None
            self.set_state("error", f"起動できませんでした: {e}")
            return
        if self.job:
            kernel32.AssignProcessToJobObject(self.job, wt.HANDLE(int(self.proc._handle)))
        self.proc_started = time.time()
        self.recent = []
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.set_state("waiting")

    def stop(self):
        proc, self.proc = self.proc, None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.device = ""
        self.video.hide()
        self.set_state("stopped")

    def end_mirroring(self):
        """映像ウィンドウの × : iPhone との接続を切って待ち受けに戻る"""
        self.stop()
        self.start()

    def _reader(self, proc):
        log = None
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            mode = "a" if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) < 1_000_000 else "w"
            log = open(LOG_FILE, mode, encoding="utf-8")
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(proc.args[1:])}\n")
        except OSError:
            log = None
        for line in proc.stdout:
            if log:
                log.write(line)
                log.flush()
            self.q.put((proc, line))
        self.q.put((proc, None))
        if log:
            log.close()

    def poll(self):
        try:
            while True:
                proc, line = self.q.get_nowait()
                if proc is not self.proc:
                    continue  # 止めた古いエンジンの出力は無視
                if line is None:
                    self._on_exit()
                else:
                    self._on_line(line)
        except queue.Empty:
            pass
        self._adopt_engine_window()
        self.root.after(30, self.poll)

    def _on_line(self, line):
        self.recent = (self.recent + [line.strip()])[-8:]
        m = re.search(r"connection request from (.+?) \(", line)
        if m:
            self.device = m.group(1)
        m = (re.search(r"geometry is \(w,h\)=\((\d+),(\d+)\) \(source\)", line)
             or re.search(r"iPhoneMirror: size (\d+)x(\d+)", line))
        if m and int(m.group(1)) > 0 and int(m.group(2)) > 0:
            self.video.show(int(m.group(1)), int(m.group(2)))
            self.set_state("mirroring")
        if "iPhoneMirror: all clients disconnected" in line:
            self.device = ""
            self.video.hide()
            self.set_state("waiting")

    def _on_exit(self):
        quick = time.time() - self.proc_started < 4
        self.proc = None
        self.device = ""
        self.video.hide()
        if quick and self.use_fast:
            self.use_fast = False  # GPU デコードが使えない環境 → 通常モードでやり直す
            self.start()
        elif time.time() - self.last_restart > 30:
            self.last_restart = time.time()  # 予期せず止まった → 1 回だけ自動で再起動
            self.start()
        else:
            reason = next((r for r in reversed(self.recent) if "rror" in r), "")
            self.set_state("error", "受信エンジンが止まりました。「開始」で再開できます"
                           + (f"\n({reason[:60]})" if reason else ""))

    def _adopt_engine_window(self):
        # エンジンが開いた映像ウィンドウを、タイトルバーを外して自前の窓の中へ移す
        if not self.proc:
            return
        for h in top_windows_of_pid(self.proc.pid):
            style = user32.GetWindowLongW(h, GWL_STYLE) & 0xFFFFFFFF
            style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINMAX)
            user32.ShowWindow(h, 0)  # 一瞬見えるのを防ぐ
            user32.SetWindowLongW(h, GWL_STYLE, s32(style | WS_CHILD))
            user32.SetParent(h, self.video.hwnd_surface)
            user32.SetWindowPos(h, None, 0, 0, 0, 0, 0x0027)  # FRAMECHANGED|NOMOVE|NOSIZE|NOZORDER
            user32.ShowWindow(h, 4)  # SW_SHOWNOACTIVATE
            if not self.video.win.winfo_viewable():
                self.video.show(*self.video.last_src)
            self.video.fit_children()

    def quit(self):
        self.stop()
        self.root.destroy()


def main():
    # 二重起動を防ぐ (2 つ動くと同じ受信機が 2 台見えて不安定になる)
    kernel32.CreateMutexW(None, False, "IponMirror.SingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        h = user32.FindWindowW(None, APP_NAME)
        if h:
            user32.ShowWindow(h, 9)  # SW_RESTORE
            user32.SetForegroundWindow(h)
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("IponMirror.App")
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    MirrorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
