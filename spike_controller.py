#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hyena Control — تطبيق التحكم الكامل بروبوت SPIKE Prime (Pybricks) عبر البلوتوث

المميزات:
- اتصال/قطع الاتصال مع إعادة المحاولة التلقائية
- إرسال ملفات .py وترجمتها وتشغيلها (مع/بدون انتظار الانتهاء)
- تحميل البرنامج في ذاكرة الروبوت بدون تشغيله
- إيقاف البرنامج الحالي
- استماع حيّ لمخرجات الروبوت (التدريب بدون كمبيوتر)
- محرر أكواد مع تلوين الصيغة وأرقام الأسطر
- إدارة الملفات (فتح/حفظ/حفظ باسم/آخر الملفات)
- تصدير سجل المخرجات وحفظ تلقائي
- توليد برنامج سباق من بيانات التدريب المسجّلة
- مؤشر حالة البرنامج وتحذير البطارية المنخفضة

التشغيل: python spike_controller.py
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import queue
import re
import sys
import threading
import time
import tempfile
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from spike_core import (MAX_LINES, MAX_LINE_BYTES, MAX_HUB_LOG_BYTES,
                        EventQueue, atomic_write, training_line, validated_config)
from spike_documents import EditorDocument, canonical_path, tab_names
from spike_icons import ICON_PATHS, icon_pixels
from spike_transfer import (TransferFailure, configure_ble_upload, needs_fresh_status,
                            trace_upload_writes, transfer_environment, wait_hub_idle,
                            upload_progress, finish_upload_progress)

# مسار التطبيق أولاً (نحتاجه لتهيئة سجل المخرجات)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
# User-writable storage also works when installed under Program Files.
DATA_DIR = os.environ.get("HYENA_CONTROL_DATA_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    or os.path.join(os.path.expanduser("~"), ".local", "state"), "HyenaControl")
CONFIG_FILE = os.path.join(DATA_DIR, "spike_controller_config.json")
LEGACY_CONFIG_FILE = os.path.join(APP_DIR, "spike_controller_config.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")

# عند التشغيل بـ pythonw (start.bat بدون نافذة كونسول) تكون stdout/stderr
# = None، فيتعطل شريط تقدم الرفع داخل pybricksdev (None.write).
# نهيّئهما لملف سجل حتى يعمل الإرسال في كلتا الحالتين.
if sys.stdout is None or sys.stderr is None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _f = open(os.path.join(LOG_DIR, "app_console.log"), "a",
                  encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _f
        if sys.stderr is None:
            sys.stderr = _f
    except Exception:
        pass

# ترميز UTF-8 للكونسول (ويندوز cp1252 يكسر النصوص العربية)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- عرض النص العربي بشكل صحيح في Tk ---
# ودجات Tk لا تنفذ خوارزمية bidi، فتعرض النص العربي بترتيبه المنطقي
# (من اليسار لليمين) أي الكلمات معكوسة. الحل: نعيد تشكيل الحروف
# (presentation forms) ثم نرتبها ترتيباً بصرياً قبل الإدراج.
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    RTL_OK = True
except Exception:
    RTL_OK = False

_AR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F]")


def to_visual(text: str) -> str:
    """إرجاع نص جاهز للعرض في ودجات Tk بحيث يظهر عربياً صحيحاً.

    النصوص اللاتينية/الرقمية تبقى كما هي دون أي تغيير.
    """
    if not RTL_OK or not _AR_RE.search(text):
        return text
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text

DEFAULTS = {
    "geometry": "1220x780",
    "sash": 440,
    "editor_fraction": 0.78,
    "timestamps": True,
    "autolog": True,
    "scan_timeout": 20,
    "retries": 6,
    "recent": [],
    "autoconnect": False,
    "hub_name": "",
}

SETUP_TEMPLATE = '''\
# --- برنامج سباق ---
from pybricks.hubs import PrimeHub
from pybricks.pupdevices import Motor, ColorSensor
from pybricks.parameters import Port, Direction, Color
from pybricks.robotics import DriveBase
from pybricks.tools import wait

hub = PrimeHub()
left_motor  = Motor(Port.A, Direction.COUNTERCLOCKWISE)
right_motor = Motor(Port.B)
left_sensor  = ColorSensor(Port.C)
right_sensor = ColorSensor(Port.D)
robot = DriveBase(left_motor, right_motor, 62.4, 110)
robot.use_gyro(True)
hub.imu.reset_heading(0)

# --- بداية المسار ---
hub.light.on(Color.GREEN)

# --- نهاية المسار ---
robot.stop()
hub.light.on(Color.BLUE)
hub.speaker.beep(900, 200)
'''

STARTER_TEXT = '''\
# اكتب كودك هنا أو افتح ملفاً (.py)
# مثال:
# robot.straight(172)
# robot.turn(32)
#
# للمساعدة اضغط F1
# للإرسال والتشغيل اضغط F5
'''

HELP_TEXT = """\
=== شرح استخدام التطبيق ===

1) الاتصال:
   - شغّل الروبوت (يرمش أزرق) وقرّبه من اللابتوب
   - اضغط «بلوتوث» — يعيد المحاولة تلقائياً حتى يظهر الروبوت
   - عند الاتصال تظهر الفرموير في شريط الحالة

2) إرسال وتشغيل:
   - افتح ملف .py في تبويب مستقل أو اكتبه في تبويب جديد
   - «إرسال وتشغيل» (F5): يرفع البرنامج ويشغّله فوراً
   - «إرسال وانتظار الانتهاء» (Ctrl+F5): ينتظر حتى ينتهي البرنامج
   - «تحميل فقط»: يحفظ البرنامج في الروبوت بدون تشغيل
   - «تشغيل المخزن»: يشغّل آخر برنامج حُمّل

3) وضع التدريب (مهم):
   - أرسل برنامج التدريب ثم خذ الروبوت للسجادة
   - درّب بالزرار: RIGHT حركة، LEFT حساسان، LEFT+RIGHT حذف
   - ارجع بالروبوت واتصل بالتطبيق — يبدأ الاستقبال تلقائياً
   - اضغط CENTER في الروبوت ← تظهر جميع التسجيلات هنا
   - ⚠ لا تضغط «إرسال وتشغيل» أثناء التدريب — سيمسح التسجيل

4) توليد برنامج سباق:
   - قبل طباعة تسجيل تدريب جديد اضغط «جلسة جديدة / مسح»
   - بعد استقبال بيانات التدريب اضغط «توليد ملف سباق»
   - يستخرج الأسطر المسجّلة (robot.straight / robot.turn)
   - راجع المعاينة ثم «فتح في تبويب جديد» أو «حفظ كملف»
   - الإرسال يستخدم التبويب النشط ولا يغيّر برنامج التدريب في تبويبه

5) الحفظ:
   - كل مخرجات الروبوت تُحفظ تلقائياً في مجلد logs
   - «تصدير السجل» يحفظ نسخة مختارة يدوياً

6) اختصارات:
   F5 إرسال وتشغيل | Ctrl+F5 إرسال مع الانتظار
   Esc إيقاف البرنامج | F1 مساعدة
   Ctrl+N/O/S تبويب جديد/فتح/حفظ | Ctrl+Shift+S حفظ باسم
   Ctrl+W إغلاق التبويب | Ctrl+Tab / Ctrl+Shift+Tab تنقل
   Ctrl+L جلسة جديدة ومسح البيانات
   Ctrl+Shift+C نسخ المحدد من الكونسول دون الوقت (أو زر يمين في الكونسول)

7) الكونسول:
   - حدّد أي نص بالفأرة وانسخه (Ctrl+C أو قائمة يمين)
   - الكونسول قراءة فقط — لا يُمسح بمجرد الضغط على المفاتيح
"""

# ---------------------------------------------------------------------------
# استيراد pybricksdev (اختياري — رسالة خطأ واضحة إن لم يُثبَّت)
# ---------------------------------------------------------------------------
try:
    from pybricksdev.ble import find_device
    from pybricksdev.connections import ConnectionState
    from pybricksdev.connections.pybricks import PybricksHubBLE, PybricksHubUSB

    PB_OK = True
    PB_ERR = ""
except Exception as _e:  # pragma: no cover
    PB_OK = False
    PB_ERR = str(_e)

FLAG_USER_RUN = 1 << 6       # StatusFlag.USER_PROGRAM_RUNNING
FLAG_BATT_LOW = (1 << 0) | (1 << 1)  # تحذير/إيقاف البطارية


# ---------------------------------------------------------------------------
# التقاط مخرجات الروبوت
# ---------------------------------------------------------------------------
class HubCapture:
    """يستبدل _line_handler في pybricksdev ويحوّل كل سطر إلى واجهة التطبيق.

    يدعم أيضاً ملفات السجل التي يكتبها الروبوت (PB_OF:/PB_EOF)
    ويحفظها في مجلد logs.
    """

    def __init__(self, out: "queue.Queue"):
        self.out = out
        self.log_file = None
        self.log_path = None
        self.log_bytes = 0
        self.subs = []

    def _close_file(self):
        if self.log_file:
            try:
                self.log_file.close()
            except OSError as exc:
                self.out.put(("console", f"تعذر إغلاق سجل الروبوت: {exc}", "err"))
            finally:
                self.log_file = None

    def handler(self, line) -> None:
        raw = bytes(line) if isinstance(line, (bytes, bytearray)) else str(line).encode("utf-8", "replace")
        if len(raw) > MAX_LINE_BYTES:
            self.out.put(("data_loss", "سطر الروبوت تجاوز الحد المسموح"))
            return
        text = raw.decode("utf-8", "replace")
        prefix = next((p for p in (b"PB_OF:", b"_file_begin_ ") if raw.startswith(p)), None)
        if prefix:
            self._close_file()
            name = raw[len(prefix):].decode("utf-8", "replace").strip()
            safe = re.sub(r"[^a-zA-Z0-9_-]", "_", os.path.basename(name.replace("\\", "/")))[:48]
            try:
                fd, self.log_path = tempfile.mkstemp(prefix="hub_" + safe + "_", suffix=".txt", dir=LOG_DIR)
                self.log_file = os.fdopen(fd, "w", encoding="utf-8")
                self.log_bytes = 0
                self.out.put(("console", f"[روبوت] بدأ حفظ سجل: {self.log_path}", "ok"))
            except OSError as exc:
                self.out.put(("console", f"تعذر فتح سجل الروبوت: {exc}", "err"))
            return
        if raw.strip() in (b"PB_EOF", b"_file_end_"):
            self._close_file()
            self.out.put(("console", "[روبوت] انتهى حفظ السجل", "ok"))
            return
        if self.log_file:
            try:
                size = len((text + "\n").encode("utf-8"))
                if self.log_bytes + size > MAX_HUB_LOG_BYTES:
                    raise OSError("تم بلوغ الحد الأقصى لحجم سجل الروبوت")
                self.log_file.write(text + "\n")
                self.log_bytes += size
            except OSError as exc:
                self._close_file()
                self.out.put(("console", f"تعذر حفظ سجل الروبوت: {exc}", "err"))
        # Keep training visible/available even while the hub is sending a file.
        self.out.put(("console", text, "out"))

    def close(self) -> None:
        self._close_file()
        for subscription in self.subs:
            try:
                subscription.dispose()
            except Exception:
                traceback.print_exc()
        self.subs = []


# ---------------------------------------------------------------------------
# مساعدات
# ---------------------------------------------------------------------------
def ts() -> str:
    return time.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# التطبيق الرئيسي
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = self._load_config()

        self.queue: "queue.Queue" = EventQueue()
        self.hub = None                 # UI connection snapshot
        self._worker_hub = None         # worker-thread connection owner
        self._download_uncertain = False
        self.capture = None
        self.busy = False
        self._operations = {}  # UI-thread futures, removed only by their own completion
        self._next_op = 0
        self._async_tasks = {}  # worker-thread tasks
        self._op_lock = None
        self._closing = False
        self._confirming_close = False
        self._poll_job = None
        self.connected = False
        self.documents: list[EditorDocument] = []
        self.active_document = None
        self._untitled_counter = 0
        self._pressed_close = None
        self._sending_path = None
        self._send_op_id = None
        self._upload_progress_id = None
        self._upload_state = "idle"
        self._upload_percent = 0
        self.rx_count = 0
        # نسخة خام من أسطر الكونسول (بلا تحويل العرض) لتستخدمها
        # أدوات التحليل: مولّد السباق وحفظ السجل — حتى لا تتأثر بالترتيب البصري
        self.console_raw: list[str] = []
        self.training_lines: list[str] = []
        self.training_overflow = False
        self.training_revision = 0
        self._disconnecting_hub = None
        self.session_id = 1
        self.session_file = None
        self.serial_running = False
        self.serial_stop = None
        self.serial_thread = None

        os.makedirs(LOG_DIR, exist_ok=True)

        self._build_style()
        self._build_ui()
        self._bind_shortcuts()
        self._refresh_buttons()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()

        if not PB_OK:
            self.console(f"تعذر تحميل pybricksdev: {PB_ERR}", "err")
            self.console("ثبّته بـ: python -m pip install -r requirements.txt", "info")

        self.console("مرحباً — اضغط «بلوتوث» بعد تشغيل الروبوت (F1 للمساعدة)", "info")

        if self.cfg.get("autoconnect"):
            self.root.after(500, self.action_connect)

    @property
    def editor(self):
        return self.active_document.editor

    @property
    def ln(self):
        return self.active_document.gutter

    @property
    def sb_ed(self):
        return self.active_document.scrollbar

    @property
    def current_path(self):
        return self.active_document.path

    @current_path.setter
    def current_path(self, value):
        self.active_document.path = value

    @property
    def _saved_text(self):
        return self.active_document.saved_text

    @_saved_text.setter
    def _saved_text(self, value):
        self.active_document.saved_text = value

    @property
    def _hl_job(self):
        return self.active_document.highlight_job

    @_hl_job.setter
    def _hl_job(self, value):
        self.active_document.highlight_job = value

    # ---------------- إعدادات ----------------
    def _load_config(self) -> dict:
        for path in (CONFIG_FILE, LEGACY_CONFIG_FILE):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return validated_config(json.load(f), DEFAULTS)
            except (OSError, ValueError):
                continue
        return validated_config({}, DEFAULTS)

    def _save_config(self) -> None:
        try:
            self.cfg["geometry"] = self.root.geometry()
            try:
                self.cfg["sash"] = self.paned.sashpos(0)
                height = self.paned.winfo_height()
                if height > 0:
                    self.cfg["editor_fraction"] = max(0.2, min(0.9, self.cfg["sash"] / height))
            except Exception:
                pass
            atomic_write(CONFIG_FILE, json.dumps(self.cfg, ensure_ascii=False, indent=2))
        except Exception as exc:
            self.console(f"تعذر حفظ الإعدادات: {exc}", "err")

    # ---------------- واجهة ----------------
    def _build_style(self) -> None:
        self.BG = "#1e1f26"
        self.PANEL = "#262833"
        self.FG = "#e8e9ed"
        self.ENTRY_BG = "#14151b"
        self.ACCENT = "#3a78d8"
        self.OK = "#4cc38a"
        self.ERR = "#e5484d"
        self.WARN = "#f2a73b"

        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        self.root.configure(bg=self.BG)
        st.configure(".", background=self.BG, foreground=self.FG,
                     fieldbackground=self.PANEL, borderwidth=0,
                     font=("Segoe UI", 10))
        st.configure("TButton", background=self.PANEL, foreground=self.FG,
                     padding=(10, 6), relief="flat")
        st.map("TButton", background=[("active", "#33364a"), ("disabled", "#20222b")],
               foreground=[("disabled", "#666a78")])
        st.configure("Accent.TButton", background=self.ACCENT, foreground="#ffffff")
        st.map("Accent.TButton", background=[("active", "#4a86e6"), ("disabled", "#2b3550")])
        st.configure("Danger.TButton", background="#5a2a2e", foreground="#ffd9d9")
        st.map("Danger.TButton", background=[("active", "#7a353b"), ("disabled", "#2b2226")])
        st.configure("TLabelframe", background=self.BG, foreground=self.FG)
        st.configure("TLabelframe.Label", background=self.BG, foreground="#9aa3b5")
        st.configure("TFrame", background=self.BG)
        st.configure("TLabel", background=self.BG, foreground=self.FG)
        st.configure("Header.TLabel", background=self.BG, foreground="#9aa3b5",
                     font=("Segoe UI", 9))
        st.configure("TCheckbutton", background=self.BG, foreground=self.FG)
        st.map("TCheckbutton", background=[("active", self.BG)])

        st.configure("Toolbar.TButton", background="#34394b", foreground=self.FG,
                     bordercolor="#454d64", borderwidth=1, padding=(8, 4),
                     font=("Segoe UI", 9), anchor="center", relief="flat")
        st.map("Toolbar.TButton",
               background=[("disabled", "#2a2d39"), ("pressed", "#272e42"), ("active", "#46516c")],
               foreground=[("disabled", "#70788d")],
               bordercolor=[("focus", self.ACCENT), ("active", "#66769c")])
        st.configure("Primary.Toolbar.TButton", background="#346bd5", foreground="#ffffff",
                     bordercolor="#5286e6")
        st.map("Primary.Toolbar.TButton",
               background=[("disabled", "#293651"), ("pressed", "#2857b1"), ("active", "#447ff0")])
        st.configure("Stop.Toolbar.TButton", background="#733440", foreground="#fff1f3",
                     bordercolor="#a64b5c")
        st.map("Stop.Toolbar.TButton",
               background=[("disabled", "#3c2c35"), ("pressed", "#6b2534"), ("active", "#a14358")])

        st.configure("Toolbar.TMenubutton", background="#34394b", foreground=self.FG,
                     padding=(8, 4), font=("Segoe UI", 9), borderwidth=1)
        st.map("Toolbar.TMenubutton", background=[("active", "#46516c")])
        st.configure("Upload.Horizontal.TProgressbar", background=self.OK,
                     troughcolor=self.ENTRY_BG, borderwidth=0, thickness=6,
                     lightcolor=self.OK, darkcolor=self.OK)
        st.configure("Failed.Upload.Horizontal.TProgressbar", background=self.ERR,
                     lightcolor=self.ERR, darkcolor=self.ERR)
        st.configure("Cancelled.Upload.Horizontal.TProgressbar", background=self.WARN,
                     lightcolor=self.WARN, darkcolor=self.WARN)

    def _build_ui(self) -> None:
        self.root.title("Hyena Control — تحكم روبوت SPIKE Prime (Pybricks)")
        try:
            self.root.geometry(self.cfg.get("geometry", DEFAULTS["geometry"]))
        except Exception:
            self.root.geometry("1220x780")
        self.root.minsize(900, 560)

        self._build_menu()

        self._build_toolbar()

        # Reserve a full row so the in-flight target cannot be squeezed by buttons.
        self.lbl_send_target = ttk.Label(self.root, text="", width=1, anchor="w",
                                         style="Header.TLabel")
        self.lbl_send_target.pack(side="top", fill="x", padx=12, pady=(2, 4))

        # ---- شريط الحالة ----
        # Clam's native progress element ignores "thickness" on some Tk builds.
        # Constrain its actual allocated height, not just its requested style.
        self.upload_track = ttk.Frame(self.root, height=6)
        self.upload_track.pack(side="bottom", fill="x", padx=8, pady=(0, 3))
        self.upload_track.pack_propagate(False)
        self.upload_bar = ttk.Progressbar(self.upload_track, orient="horizontal", mode="determinate",
                                          maximum=100, value=0, style="Upload.Horizontal.TProgressbar")
        self.upload_bar.pack(fill="both", expand=True)
        sb = ttk.Frame(self.root, padding=(10, 3))
        sb.pack(side="bottom", fill="x")
        sb.columnconfigure(0, weight=1)
        sb.columnconfigure(1, weight=1)
        self.lbl_upload = ttk.Label(sb, text="0%", style="Header.TLabel", anchor="e")
        self.lbl_upload.grid(row=0, column=5, sticky="e", padx=(8, 0))
        self.lbl_state = ttk.Label(sb, text="● غير متصل", foreground=self.ERR, width=1)
        self.lbl_state.grid(row=0, column=0, sticky="ew")
        self.lbl_prog = ttk.Label(sb, text="", style="Header.TLabel", width=1)
        self.lbl_prog.grid(row=0, column=1, sticky="ew", padx=8)
        self.chk_ts = tk.Checkbutton(sb, text="طوابع زمنية", variable=self._ts_var(),
                                     command=self._toggle_ts, bg=self.BG, fg="#9aa3b5",
                                     selectcolor=self.PANEL, activebackground=self.BG,
                                     activeforeground=self.FG)
        self.chk_ts.grid(row=0, column=2, padx=4)
        self.chk_al = tk.Checkbutton(sb, text="حفظ تلقائي", variable=self._al_var(),
                                     command=self._toggle_al, bg=self.BG, fg="#9aa3b5",
                                     selectcolor=self.PANEL, activebackground=self.BG,
                                     activeforeground=self.FG)
        self.chk_al.grid(row=0, column=3, padx=4)
        self.lbl_rx = ttk.Label(sb, text="0 سطر", style="Header.TLabel")
        self.lbl_rx.grid(row=0, column=4, padx=4)

        # ---- منطقة رئيسية: محرر + كونسول ----
        self.paned = ttk.PanedWindow(self.root, orient="vertical")
        self.paned.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # محرر
        ed_frame = ttk.Frame(self.paned)
        self.paned.add(ed_frame, weight=5)
        # Keep the document identity out of the crowded connection/status bar.
        document_header = ttk.Frame(ed_frame)
        document_header.pack(fill="x", pady=(0, 2))
        document_header.columnconfigure(0, weight=1, minsize=120)
        document_header.columnconfigure(1, weight=3)
        self.lbl_file = ttk.Label(document_header, text="", anchor="w", width=1,
                                  font=("Segoe UI", 10, "bold"))
        self.lbl_file.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(document_header, text="+", width=3, command=self.action_new).grid(row=0, column=2)
        ttk.Button(document_header, text=to_visual("إغلاق التبويب"),
                   command=self.action_close_tab).grid(row=0, column=3, padx=4)
        self.file_path_var = tk.StringVar(value="")
        self.file_path_entry = ttk.Entry(document_header, textvariable=self.file_path_var,
                                         state="readonly", takefocus=True, width=1)
        self.file_path_entry.grid(row=0, column=1, sticky="ew", padx=8)

        self._build_tab_style()
        self.notebook = ttk.Notebook(ed_frame, style="Documents.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.notebook.bind("<ButtonPress-1>", self._tab_close_press, add="+")
        self.notebook.bind("<ButtonRelease-1>", self._tab_close_release, add="+")
        self._new_document()

        # كونسول
        cs_frame = ttk.Frame(self.paned)
        self.paned.add(cs_frame, weight=1)

        cs_hdr = ttk.Frame(cs_frame)
        cs_hdr.pack(fill="x")
        ttk.Label(cs_hdr, text="مخرجات الروبوت", style="Header.TLabel").pack(side="right")
        ttk.Button(cs_hdr, text="نسخ المحدد", command=self._copy_console).pack(
            side="right", padx=2)
        ttk.Button(cs_hdr, text="تحديد الكل", command=self._select_all_console).pack(
            side="right", padx=2)

        cs_box = tk.Frame(cs_frame, bg=self.BG)
        cs_box.pack(fill="both", expand=True)
        # الكونسول بحالة عادية (state=normal) حتى يعمل التحديد والنسخ،
        # لكن _console_key يمنع أي تعديل (قراءة فقط عملياً)
        self.console_w = tk.Text(cs_box, wrap="word", border=0,
                                 bg=self.ENTRY_BG, fg=self.FG,
                                 font=("Consolas", 11), padx=8, pady=6,
                                 cursor="arrow")
        self.sb_cs = ttk.Scrollbar(cs_box, orient="vertical", command=self.console_w.yview)
        self.console_w.configure(yscrollcommand=self.sb_cs.set)
        self.sb_cs.pack(side="right", fill="y")
        self.console_w.pack(side="left", fill="both", expand=True)

        self.console_w.tag_configure("out", foreground=self.FG)
        self.console_w.tag_configure("info", foreground="#79b8ff")
        self.console_w.tag_configure("ok", foreground=self.OK)
        self.console_w.tag_configure("err", foreground=self.ERR)
        self.console_w.tag_configure("ts", foreground="#666e80")

        # الكونسول: قراءة فقط مع إمكانية التحديد والنسخ + قائمة يمين
        self.console_w.bind("<Key>", self._console_key)
        self.console_w.bind("<<Copy>>", self._on_console_copy)
        for event in ("<<Paste>>", "<<Cut>>", "<<Clear>>", "<<PasteSelection>>"):
            self.console_w.bind(event, lambda e: "break")
        self.console_w.bind("<Button-3>", self._console_menu)

        # Restore the proportional split after real geometry is known.
        self._sash_restored = False
        self.paned.bind("<Configure>", self._layout_panes)
        self._set_file_label()

    def _layout_panes(self, event):
        if self._closing or event.height < 200:
            return
        console_min = max(80, min(130, round(event.height * 0.2)))
        upper = event.height - console_min
        lower = min(180, upper)
        requested = (round(event.height * self.cfg.get("editor_fraction", 0.78)) if not self._sash_restored
                     else self.paned.sashpos(0))
        self._sash_restored = True
        self.paned.sashpos(0, max(lower, min(requested, upper)))

    def _build_toolbar(self):
        """One compact row; secondary commands stay available in drop-downs."""
        self.toolbar = ttk.Frame(self.root, padding=(8, 4))
        self.toolbar.pack(side="top", fill="x")
        self.toolbar.columnconfigure(6, weight=1)
        self._build_toolbar_icons()
        self.btn_connect = self._toolbar_button(self.toolbar, "بلوتوث", "bluetooth", self.action_connect, 0, 0)
        self.btn_disconnect = self._toolbar_button(self.toolbar, "قطع الاتصال", "disconnect", self.action_disconnect, 0, 1)
        self.btn_send = self._toolbar_button(self.toolbar, "إرسال وتشغيل", "play", self.action_send_run, 0, 2,
                                             style="Primary.Toolbar.TButton")
        self.btn_stop = self._toolbar_button(self.toolbar, "إيقاف", "stop", self.action_stop, 0, 3,
                                             style="Stop.Toolbar.TButton")
        self.options_button = ttk.Menubutton(self.toolbar, text=to_visual("خيارات"), style="Toolbar.TMenubutton")
        self.options_button.grid(row=0, column=4, padx=3, sticky="ns")
        self.tools_button = ttk.Menubutton(self.toolbar, text=to_visual("أدوات"), style="Toolbar.TMenubutton")
        self.tools_button.grid(row=0, column=5, padx=3, sticky="ns")
        self.options_menu = tk.Menu(self.options_button, tearoff=0)
        self.tools_menu = tk.Menu(self.tools_button, tearoff=0)
        self.options_button.configure(menu=self.options_menu)
        self.tools_button.configure(menu=self.tools_menu)
        self._action_entries = {}
        for key, title, icon, command, shortcut in (
                ("usb", "اتصال USB", "usb", self.action_usb_connect, ""),
                ("serial", "استقبال COM", "serial", self.action_serial_toggle, ""),
                ("sendwait", "إرسال مع الانتظار", "wait", self.action_send_wait, "Ctrl+F5"),
                ("dl", "تحميل فقط", "download", self.action_download_only, ""),
                ("stored", "تشغيل المخزن", "stored", self.action_run_stored, "")):
            self._add_toolbar_action(self.options_menu, key, title, icon, command, shortcut)
        for key, title, icon, command, shortcut in (
                ("race", "توليد ملف سباق", "race", self.action_race_gen, "Ctrl+G"),
                ("clear", "جلسة جديدة / مسح", "new", self.action_clear_console, "Ctrl+L"),
                ("export", "تصدير السجل", "export", self.action_export_log, "")):
            self._add_toolbar_action(self.tools_menu, key, title, icon, command, shortcut)
        target = ttk.Frame(self.toolbar)
        target.grid(row=0, column=6, sticky="ew", padx=(8, 0))
        ttk.Label(target, text=to_visual("الروبوت:"), style="Header.TLabel").pack(side="right", padx=4)
        self.target_var = tk.StringVar(value=self.cfg.get("hub_name", ""))
        self.target_entry = ttk.Entry(target, textvariable=self.target_var, width=1)
        self.target_entry.pack(side="left", fill="x", expand=True)
        self.toolbar_buttons = (self.btn_connect, self.btn_disconnect, self.btn_send, self.btn_stop)

    def _add_toolbar_action(self, menu, key, title, icon, command, shortcut):
        menu.add_command(label=to_visual(title), image=self._toolbar_images[icon][0], compound="left",
                         command=command, accelerator=shortcut)
        self._action_entries[key] = (menu, menu.index("end"))

    def _configure_action(self, key, **options):
        menu, index = self._action_entries[key]
        menu.entryconfigure(index, **options)

    def _build_toolbar_icons(self):
        # Pixel glyphs avoid missing emoji/symbol fonts and scale with Tk's DPI.
        size = max(16, min(32, round(18 * float(self.root.tk.call("tk", "scaling")) / (96 / 72))))
        self._toolbar_images = {}
        for name in ICON_PATHS:
            pair = []
            for color in ("#f1f4fc", "#70788d"):
                image = tk.PhotoImage(master=self.root, width=size, height=size)
                for x, y in icon_pixels(name, size):
                    image.put(color, (x, y))
                pair.append(image)
            # Hold Python references and provide a muted disabled-state image.
            self._toolbar_images[name] = (pair[0], "disabled", pair[1])

    def _toolbar_button(self, parent, title, icon, command, row, column, *, span=1,
                        style="Toolbar.TButton"):
        button = ttk.Button(parent, text=to_visual(title), image=self._toolbar_images[icon],
                            compound="left", width=-1, style=style, command=command)
        button.grid(row=row, column=column, columnspan=span, sticky="ew", padx=3, pady=3)
        return button

    def _sync_serial_button(self):
        title = "إيقاف COM" if self.serial_running else "استقبال COM"
        icon = "stop" if self.serial_running else "serial"
        self._configure_action("serial", label=to_visual(title), image=self._toolbar_images[icon][0])

    def _ts_var(self):
        if not hasattr(self, "_v_ts"):
            self._v_ts = tk.BooleanVar(value=bool(self.cfg.get("timestamps", True)))
        return self._v_ts

    def _al_var(self):
        if not hasattr(self, "_v_al"):
            self._v_al = tk.BooleanVar(value=bool(self.cfg.get("autolog", True)))
        return self._v_al

    def _toggle_ts(self):
        self.cfg["timestamps"] = bool(self._ts_var().get())

    def _toggle_al(self):
        self.cfg["autolog"] = bool(self._al_var().get())
        if not self.cfg["autolog"] and self.session_file:
            try:
                self.session_file.close()
            except OSError:
                traceback.print_exc()
            finally:
                self.session_file = None

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="جديد", accelerator="Ctrl+N", command=self.action_new)
        m_file.add_command(label="فتح...", accelerator="Ctrl+O", command=self.action_open)
        m_file.add_command(label="حفظ", accelerator="Ctrl+S", command=self.action_save)
        m_file.add_command(label="حفظ باسم...", accelerator="Ctrl+Shift+S",
                           command=self.action_save_as)
        m_file.add_command(label="إغلاق التبويب", accelerator="Ctrl+W", command=self.action_close_tab)
        self.m_recent = tk.Menu(m_file, tearoff=0)
        m_file.add_cascade(label="آخر الملفات", menu=self.m_recent)
        self._refresh_recent_menu()
        m_file.add_separator()
        m_file.add_command(label="فتح مجلد السجلات", command=self.action_open_logs)
        m_file.add_separator()
        m_file.add_command(label="خروج", command=self._on_close)
        menubar.add_cascade(label="الملف", menu=m_file)

        m_edit = tk.Menu(menubar, tearoff=0)
        m_edit.add_command(label="تراجع", command=lambda: self.editor.edit_undo())
        m_edit.add_command(label="إعادة", command=lambda: self.editor.edit_redo())
        m_edit.add_separator()
        m_edit.add_command(label="قص", accelerator="Ctrl+X",
                           command=lambda: self.editor.event_generate("<<Cut>>"))
        m_edit.add_command(label="نسخ", accelerator="Ctrl+C",
                           command=lambda: self.editor.event_generate("<<Copy>>"))
        m_edit.add_command(label="لصق", accelerator="Ctrl+V",
                           command=lambda: self.editor.event_generate("<<Paste>>"))
        m_edit.add_command(label="تحديد الكل", accelerator="Ctrl+A",
                           command=lambda: self.editor.tag_add("sel", "1.0", "end"))
        m_edit.add_separator()
        m_edit.add_command(label="نسخ المحدد من الكونسول", accelerator="Ctrl+Shift+C",
                           command=self._copy_console)
        m_edit.add_command(label="تحديد كل الكونسول",
                           command=self._select_all_console)
        menubar.add_cascade(label="التحرير", menu=m_edit)

        m_ins = tk.Menu(menubar, tearoff=0)
        m_ins.add_command(label="قالب إعدادات الروبوت",
                          command=lambda: self._insert_at_cursor(SETUP_TEMPLATE))
        m_ins.add_command(label="قالب حلقة تكرار (while True)",
                          command=lambda: self._insert_at_cursor(
                              "while True:\n    # ...\n    wait(100)\n"))
        m_ins.add_command(label="قراءة الحساس الأيسر",
                          command=lambda: self._insert_at_cursor(
                              "print(left_sensor.color(), left_sensor.reflection())\n"))
        menubar.add_cascade(label="إدراج", menu=m_ins)

        m_tools = tk.Menu(menubar, tearoff=0)
        m_tools.add_command(label="توليد ملف سباق من المخرجات ⚙",
                            accelerator="Ctrl+G", command=self.action_race_gen)
        m_tools.add_command(label="جلسة جديدة / مسح الكونسول", accelerator="Ctrl+L",
                            command=self.action_clear_console)
        m_tools.add_command(label="تصدير السجل", command=self.action_export_log)
        menubar.add_cascade(label="أدوات", menu=m_tools)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="شرح الاستخدام", accelerator="F1", command=self.action_help)
        m_help.add_command(label="عن التطبيق", command=lambda: messagebox.showinfo(
            "عن التطبيق",
            "Hyena Control 1.0\nتطبيق تحكم بروبوت SPIKE Prime (Pybricks) عبر البلوتوث\n"
            "يعتمد على pybricksdev + bleak"))
        menubar.add_cascade(label="مساعدة", menu=m_help)

        self.root.config(menu=menubar)

    def _shortcut_bindings(self):
        bindings = {
            "<Control-n>": self.action_new, "<Control-o>": self.action_open,
            "<Control-s>": self.action_save, "<Control-S>": self.action_save_as,
            "<Control-w>": self.action_close_tab,
            "<Control-Tab>": lambda: self._cycle_document(1),
            "<Control-Shift-Tab>": lambda: self._cycle_document(-1),
            "<Control-g>": self.action_race_gen, "<Control-l>": self.action_clear_console,
            "<F5>": self.action_send_run, "<Control-F5>": self.action_send_wait,
            "<Escape>": self.action_stop, "<F1>": self.action_help,
        }

        # ISO_Left_Tab is an X11 alias; some Windows Tk builds reject it at bind time.
        if self.root.tk.call("tk", "windowingsystem") == "x11":
            bindings["<Control-ISO_Left_Tab>"] = lambda: self._cycle_document(-1)
        return bindings

    def _run_shortcut(self, action):
        if not self._closing and not self._confirming_close:
            action()
        return "break"

    def _bind_shortcuts(self):
        for sequence, action in self._shortcut_bindings().items():
            self.root.bind_all(sequence, lambda e, a=action: self._run_shortcut(a))
        self.root.bind_all("<Control-Shift-C>", lambda e: self._run_shortcut(self._copy_console))

    # ---------------- التبويبات ----------------
    def _build_tab_style(self):
        style = ttk.Style(self.root)
        images = []
        for color in (self.FG, self.ERR, self.WARN):
            image = tk.PhotoImage(master=self.root, width=12, height=12)
            for offset in range(3, 9):
                image.put(color, (offset, offset))
                image.put(color, (11 - offset, offset))
            images.append(image)
        self._tab_close_images = images  # Tk images must remain referenced.
        style.element_create("Documents.close", "image", images[0],
                             ("pressed", images[2]), ("active", images[1]), sticky="")
        style.layout("Documents.TNotebook.Tab", [
            ("Notebook.tab", {"sticky": "nswe", "children": [
                ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                    ("Notebook.focus", {"side": "top", "sticky": "nswe", "children": [
                        ("Notebook.label", {"side": "left", "sticky": ""}),
                        ("Documents.close", {"side": "right", "sticky": ""}),
                    ]}),
                ]}),
            ]}),
        ])

    def _create_editor_tab(self, content, path, saved, number, suggested_name):
        frame = ttk.Frame(self.notebook)
        horizontal = ttk.Scrollbar(frame, orient="horizontal")
        horizontal.pack(side="bottom", fill="x")
        box = tk.Frame(frame, bg=self.BG)
        box.pack(fill="both", expand=True)
        gutter = tk.Text(box, width=4, padx=6, pady=6, takefocus=0, border=0,
                         state="disabled", wrap="none", bg=self.PANEL, fg="#5c6373",
                         font=("Consolas", 12), cursor="arrow")
        gutter.pack(side="left", fill="y")
        editor = tk.Text(box, wrap="none", undo=True, border=0,
                         bg=self.ENTRY_BG, fg=self.FG, insertbackground=self.FG,
                         font=("Consolas", 12), padx=8, pady=6,
                         selectbackground=self.ACCENT)
        vertical = ttk.Scrollbar(box, orient="vertical")
        vertical.pack(side="right", fill="y")
        editor.pack(side="left", fill="both", expand=True)
        document = EditorDocument(frame, editor, gutter, vertical, path,
                                  content if saved else "", number, suggested_name)
        vertical.configure(command=lambda *args: self._editor_scroll(*args, document=document))
        editor.configure(yscrollcommand=lambda first, last: self._editor_yview(first, last, document),
                         xscrollcommand=horizontal.set)
        horizontal.configure(command=editor.xview)
        editor.tag_configure("kw", foreground="#79b8ff")
        editor.tag_configure("str", foreground="#a5d6a7")
        editor.tag_configure("com", foreground="#6a7385", font=("Consolas", 12, "italic"))
        editor.tag_configure("num", foreground="#f2a73b")
        editor.insert("1.0", content)
        editor.edit_reset()
        editor.edit_modified(False)
        editor.bind("<<Modified>>", lambda e: self._on_editor_modified(e, document))
        editor.bind("<Button-3>", self._editor_menu)
        for sequence, action in self._shortcut_bindings().items():
            editor.bind(sequence, lambda e, a=action: self._run_shortcut(a))
        return document

    def _new_document(self, content=STARTER_TEXT, path=None, saved=True, suggested_name=None):
        if self._closing or self._confirming_close:
            return None
        if path is None:
            self._untitled_counter += 1
        document = self._create_editor_tab(content, path, saved, self._untitled_counter, suggested_name)
        self.documents.append(document)
        self.notebook.add(document.frame, text=to_visual(document.name))
        self._select_document(document)
        self._update_lineno(document)
        document.highlight_job = self.root.after_idle(lambda: self._rehighlight(document))
        return document

    def _select_document(self, document):
        if document not in self.documents:
            return
        self.active_document = document
        self.notebook.select(document.frame)
        self._set_file_label()
        document.editor.focus_set()

    def _on_tab_changed(self, _event=None):
        if self._closing:
            return
        selected = self.notebook.select()
        for document in self.documents:
            if str(document.frame) == selected:
                if document is not self.active_document:
                    self.active_document = document
                    self._set_file_label()
                    document.editor.focus_set()
                break

    def _cycle_document(self, direction):
        if self.documents:
            index = self.documents.index(self.active_document)
            self._select_document(self.documents[(index + direction) % len(self.documents)])

    def _document_at(self, x, y):
        try:
            return self.documents[self.notebook.index(f"@{x},{y}")]
        except (tk.TclError, IndexError):
            return None

    def _tab_close_press(self, event):
        if self.notebook.identify(event.x, event.y).endswith(".close"):
            self._pressed_close = self._document_at(event.x, event.y)
            return "break"

    def _tab_close_release(self, event):
        document, self._pressed_close = self._pressed_close, None
        if document is not None:
            if (self.notebook.identify(event.x, event.y).endswith(".close")
                    and self._document_at(event.x, event.y) is document):
                self._close_document(document)
            return "break"

    def _close_document(self, document):
        if self._closing or self._confirming_close or document not in self.documents:
            return False
        self._confirming_close = True
        try:
            if not self._confirm_discard(document):
                return False
        finally:
            self._confirming_close = False
        index = self.documents.index(document)
        if document.highlight_job:
            self.root.after_cancel(document.highlight_job)
            document.highlight_job = None
        self.notebook.forget(document.frame)
        self.documents.remove(document)
        document.frame.destroy()
        if not self.documents:
            self.active_document = None
            self._new_document()
        elif self.active_document is document:
            self._select_document(self.documents[min(index, len(self.documents) - 1)])
        else:
            self._set_file_label()
        return True

    def action_close_tab(self):
        return self._close_document(self.active_document)

    # ---------------- محرر: أرقام الأسطر وتلوين الصيغة ----------------
    def _editor_yview(self, first, last, document=None):
        document = document or self.active_document
        document.gutter.yview_moveto(first)
        document.scrollbar.set(first, last)

    def _editor_scroll(self, *args, document=None):
        document = document or self.active_document
        document.editor.yview(*args)
        document.gutter.yview(*args)

    def _update_lineno(self, document=None):
        document = document or self.active_document
        if document not in self.documents:
            return
        try:
            count = int(document.editor.index("end-1c").split(".")[0])
            first = document.editor.yview()[0]
        except Exception:
            return
        gutter = document.gutter
        gutter.configure(state="normal", width=max(4, len(str(count))))
        gutter.delete("1.0", "end")
        gutter.insert("1.0", "\n".join(str(i) for i in range(1, count + 1)))
        gutter.configure(state="disabled")
        gutter.yview_moveto(first)

    def _on_editor_modified(self, _event=None, document=None):
        document = document or self.active_document
        if self._closing or document not in self.documents:
            return
        if document.editor.edit_modified():
            document.editor.edit_modified(False)
            self._update_lineno(document)
            self._set_file_label()
            if document.highlight_job:
                self.root.after_cancel(document.highlight_job)
            document.highlight_job = self.root.after_idle(lambda: self._rehighlight(document))

    KW = ["from", "import", "def", "class", "return", "if", "elif", "else", "for",
          "while", "try", "except", "finally", "with", "as", "in", "not", "and",
          "or", "True", "False", "None", "break", "continue", "pass", "lambda",
          "global", "nonlocal", "del", "raise", "yield", "assert", "is", "await",
          "async"]

    def _rehighlight(self, document=None) -> None:
        document = document or self.active_document
        if self._closing or document not in self.documents:
            return
        document.highlight_job = None
        t = document.editor
        for tag in ("kw", "str", "com", "num"):
            t.tag_remove(tag, "1.0", "end")
        text = t.get("1.0", "end-1c")

        def idx(pos):
            return "1.0+{}c".format(pos)

        for m in re.finditer(r"\b(?:%s)\b" % "|".join(self.KW), text):
            t.tag_add("kw", idx(m.start()), idx(m.end()))
        for m in re.finditer(r"\b\d+(?:\.\d+)?\b", text):
            t.tag_add("num", idx(m.start()), idx(m.end()))
        for m in re.finditer(r"'[^'\n]*'|\"[^\"\n]*\"", text):
            t.tag_add("str", idx(m.start()), idx(m.end()))
        for m in re.finditer(r"#[^\n]*", text):
            t.tag_add("com", idx(m.start()), idx(m.end()))

    # ---------------- أدوات التحديد والنسخ ----------------
    def _console_key(self, event) -> str | None:
        """Handle application shortcuts before Tk Text's class bindings."""
        if self._closing or self._confirming_close:
            return "break"
        ctrl = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        key = event.keysym.lower()
        action = None
        if key == "escape":
            action = self.action_stop
        elif key == "f1":
            action = self.action_help
        elif key == "f5":
            action = self.action_send_wait if ctrl else self.action_send_run
        elif ctrl:
            if key in ("tab", "iso_left_tab"):
                self._cycle_document(-1 if shift or key == "iso_left_tab" else 1)
                return "break"
            if key == "c":
                if shift:
                    self._copy_console()
                else:
                    self._on_console_copy()
                return "break"
            if key == "a":
                self._select_all_console()
                return "break"
            action_name = {
                "n": "action_new", "o": "action_open", "w": "action_close_tab",
                "s": "action_save_as" if shift else "action_save",
                "g": "action_race_gen", "l": "action_clear_console",
            }.get(key)
            if action_name:
                action = getattr(self, action_name)
        if action:
            action()
            return "break"  # do not execute the same shortcut again via bind_all
        if not ctrl and event.keysym in (
                "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
                "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return None
        return "break"

    def _on_console_copy(self, event=None):
        """Copy selected output, excluding only the app's tagged timestamps.

        Do not match clock-shaped strings: they may belong to robot output.
        Dump just the selection and retain its newlines and partial lines.
        """
        w = self.console_w
        selection = w.tag_ranges("sel")
        if not selection:
            return "break"
        parts = []
        for start, end in zip(selection[::2], selection[1::2]):
            timestamp = "ts" in w.tag_names(start)
            for kind, value, _ in w.dump(start, end, text=True, tag=True):
                if value == "ts" and kind in ("tagon", "tagoff"):
                    timestamp = kind == "tagon"
                elif kind == "text" and not timestamp:
                    parts.append(value)
        w.clipboard_clear()
        w.clipboard_append("".join(parts))
        return "break"  # Never let Tk's class binding copy the timestamps again.

    def _copy_console(self) -> None:
        sel = self.console_w.tag_ranges("sel")
        if not sel:
            self.console("(حدّد نصاً أولاً بالفأرة ثم اضغط «نسخ المحدد»)", "info")
            return
        self.console_w.event_generate("<<Copy>>")
        self.console("(نُسخ التحديد ✔)", "ok")

    def _select_all_console(self) -> None:
        self.console_w.tag_add("sel", "1.0", "end-1c")
        self.console_w.focus_set()

    def _popup_copy_items(self, menu, widget) -> None:
        menu.add_command(label="نسخ المحدد", accelerator="Ctrl+C",
                         command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="تحديد الكل", accelerator="Ctrl+A",
                         command=lambda: widget.tag_add("sel", "1.0", "end-1c"))
        menu.add_command(label="إلغاء التحديد",
                         command=lambda: widget.tag_remove("sel", "1.0", "end"))

    def _console_menu(self, event) -> None:
        m = tk.Menu(self.root, tearoff=0)
        self._popup_copy_items(m, self.console_w)
        m.add_separator()
        m.add_command(label="جلسة جديدة / مسح الكونسول", command=self.action_clear_console)
        m.add_command(label="تصدير السجل", command=self.action_export_log)
        m.tk_popup(event.x_root, event.y_root)

    def _editor_menu(self, event) -> None:
        widget = event.widget
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="قص", accelerator="Ctrl+X",
                      command=lambda: widget.event_generate("<<Cut>>"))
        m.add_command(label="نسخ", accelerator="Ctrl+C",
                      command=lambda: widget.event_generate("<<Copy>>"))
        m.add_command(label="لصق", accelerator="Ctrl+V",
                      command=lambda: widget.event_generate("<<Paste>>"))
        m.add_separator()
        self._popup_copy_items(m, widget)
        m.tk_popup(event.x_root, event.y_root)

    # ---------------- الكونسول ----------------
    def console(self, text: str, tag: str = "out", source: str = "ble") -> None:
        w = self.console_w
        line = text.rstrip("\n")
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            self.training_overflow = True
            line = "سطر طويل جداً — لن يُستخدم في توليد السباق"
            tag = "err"
        if tag == "out":
            self.training_revision += 1
            for raw_line in line.splitlines():
                if len(self.training_lines) < MAX_LINES:
                    self.training_lines.append(raw_line)
                else:
                    self.training_overflow = True
        display = ("[سلك] " if source == "serial" else "") + line
        stamp = ts()
        self.console_raw.append((stamp + " " + display) if self._ts_var().get() else display)
        if len(self.console_raw) > MAX_LINES:
            del self.console_raw[:5000]
        at_end = w.yview()[1] >= 0.99
        if self._ts_var().get():
            w.insert("end", stamp + " ", "ts")
        # Shape the Arabic source label separately: applying RTL to the whole
        # line can move Python parentheses ahead of the command.
        visual_prefix = to_visual("[سلك]") + " " if source == "serial" else ""
        rendered = visual_prefix + "\n".join(to_visual(part) for part in line.split("\n"))
        w.insert("end", rendered + "\n", tag)
        if int(w.index("end-1c").split(".")[0]) > MAX_LINES:
            w.delete("1.0", "5001.0")
        if at_end:
            w.see("end")
        self.rx_count += 1
        self.lbl_rx.configure(text=f"{self.rx_count} سطر")
        if self._al_var().get():
            try:
                if self.session_file is None:
                    fd, path = tempfile.mkstemp(prefix="session_" + time.strftime("%Y%m%d_%H%M%S") + "_",
                                                suffix=".txt", dir=LOG_DIR)
                    self.session_file = os.fdopen(fd, "w", encoding="utf-8")
                    self._session_flush = time.monotonic()
                self.session_file.write(f"{stamp} {display}\n")
                if time.monotonic() - self._session_flush >= 1:
                    self.session_file.flush()
                    self._session_flush = time.monotonic()
            except OSError as exc:
                # Do not recursively call console when its own storage fails.
                traceback.print_exc()
                self._al_var().set(False)
                self._toggle_al()
                messagebox.showerror("حفظ السجل", f"تم تعطيل الحفظ التلقائي: {exc}")

    # ---------------- حلقة الرسائل من خيط BLE ----------------
    def _poll(self) -> None:
        if self._closing:
            return
        try:
            dropped = self.queue.take_dropped()
            if dropped:
                self.training_overflow = True
                self.console(f"فقد {dropped} سطر بسبب كثافة الاستقبال؛ ابدأ جلسة جديدة قبل توليد سباق", "err")
            deadline = time.monotonic() + 0.015
            for _ in range(200):
                if time.monotonic() >= deadline:
                    break
                try:
                    msg = self.queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_msg(msg)
                except Exception:
                    traceback.print_exc()
                    self.training_overflow = True
                finally:
                    self.queue.task_done()
        finally:
            if not self._closing:
                self._poll_job = self.root.after(80, self._poll)

    def _reset_upload_progress(self, op_id):
        self._upload_progress_id = op_id
        self._upload_state = "pending"
        self._upload_percent = 0
        self.upload_bar.configure(value=0, style="Upload.Horizontal.TProgressbar")
        self.lbl_upload.configure(text="0% — " + to_visual("تحضير الرفع"))

    def _handle_upload_progress(self, op_id, sent, total, complete):
        if (op_id != self._upload_progress_id or self._upload_state not in ("pending", "uploading")
                or type(sent) is not int or type(total) is not int or total <= 0 or not 0 <= sent <= total):
            return
        percent = 100 if complete is True and sent == total else min(99, sent * 100 // total)
        if percent < self._upload_percent:
            return
        self._upload_percent = percent
        self._upload_state = "complete" if percent == 100 else "uploading"
        self.upload_bar.configure(value=self._upload_percent, style="Upload.Horizontal.TProgressbar")
        text = ("100% — " + to_visual("اكتمل الرفع") if percent == 100
                else f"{self._upload_percent}% | {sent}/{total} B")
        self.lbl_upload.configure(text=text)

    def _finish_progress_operation(self, op_id, error):
        if op_id != self._upload_progress_id:
            return
        if self._upload_state == "complete":
            if error is not None:
                self.lbl_upload.configure(text="100% — " + to_visual("اكتمل الرفع؛ انتهت العملية بخطأ أو إلغاء"))
            return  # Upload completion is separate from starting/waiting for the program.
        if error is None:
            self._upload_state = "unknown"
            self.lbl_upload.configure(text=to_visual("اكتملت العملية؛ نسبة الرفع غير متاحة"))
            return  # Never fabricate a percentage on legacy/unsupported transports.
        cancelled = isinstance(error, (concurrent.futures.CancelledError, asyncio.CancelledError))
        self._upload_state = "cancelled" if cancelled else "failed"
        style = "Cancelled" if cancelled else "Failed"
        self.upload_bar.configure(style=style + ".Upload.Horizontal.TProgressbar")
        self.lbl_upload.configure(text=f"{self._upload_percent}% — " + to_visual("أُلغي الرفع" if cancelled else "فشل الرفع"))

    def _handle_msg(self, msg) -> None:
        kind = msg[0]
        if kind == "upload_progress":
            self._handle_upload_progress(*msg[1:])
        elif kind == "console":
            _, text, tag, *source = msg
            self.console(text, tag, source=source[0] if source else "ble")
        elif kind == "hub_state":
            if msg[1] == "disconnected" and msg[2] is self.hub:
                expected = len(msg) > 3 and msg[3]
                self.connected = False
                self.hub = None
                if not expected:
                    self.training_overflow = True
                    self.console("انقطع الاتصال بالروبوت! تحقق من اكتمال التدريب وابدأ جلسة جديدة.", "err")
                self.lbl_state.configure(text="● غير متصل", foreground=self.ERR)
                self.lbl_prog.configure(text="")
                self._refresh_buttons()
        elif kind == "data_loss":
            self.training_overflow = True
            self.console(msg[1], "err")
        elif kind == "hub_status":
            if msg[2] is not self.hub:
                return
            flags = msg[1]
            parts = []
            parts.append("● البرنامج يعمل" if flags & FLAG_USER_RUN else "○ البرنامج متوقف")
            if flags & FLAG_BATT_LOW:
                parts.append("⚠ البطارية منخفضة!")
            self.lbl_prog.configure(text="   ".join(parts))
        elif kind == "serial_state":
            if msg[2] is not self.serial_stop:
                return
            self.serial_running = bool(msg[1])
            self._sync_serial_button()
        elif kind in ("op_done", "op_error"):
            _, op_id, name, res, err = msg
            self._operations.pop(op_id, None)
            self._finish_progress_operation(op_id, err)
            if op_id == self._send_op_id:
                self._send_op_id = None
                self._sending_path = None
                self._update_send_target()
            self.busy = bool(self._operations)
            if name in ("connect", "usb_connect") and err is None and res is not self._worker_hub:
                self._refresh_buttons()
                return
            if name == "connect" and err is None and res is not None:
                self.hub = res
                self.connected = True
                label = getattr(res, "_app_label", None)
                if not label:
                    dev = getattr(res, "_device", None)
                    label = getattr(dev, "name", None) or "?"
                self.lbl_state.configure(
                    text=f"● متصل: {label} (فرموير {res.fw_version})",
                    foreground=self.OK)
            if name == "usb_connect" and err is None and res is not None:
                self.hub = res
                self.connected = True
                self.lbl_state.configure(
                    text=f"● متصل بالسلك: {getattr(res, '_app_label', 'USB')} "
                         f"(فرموير {res.fw_version})",
                    foreground=self.OK)
            if name == "disconnect" and (err is None or self._worker_hub is None):
                self.hub = None
                self.connected = False
                self.capture = None
                self.lbl_state.configure(text="● غير متصل", foreground=self.ERR)
                self.lbl_prog.configure(text="")
            if name in ("connect", "usb_connect") and err is None and res is not None:
                flags = getattr(getattr(res, "status_observable", None), "value", 0)
                self._handle_msg(("hub_status", int(flags), res))
            if isinstance(err, concurrent.futures.CancelledError):
                self.console(f"تم إلغاء العملية: {name}", "info")
            elif err is not None:
                self.console(f"خطأ في «{name}»: {type(err).__name__}: {err}", "err")
                if self.connected:
                    self.lbl_state.configure(text=f"● متصل (حدث خطأ في {name})",
                                             foreground=self.WARN)
            self._refresh_buttons()

    # ---------------- عمليات BLE (تعمل في خيط المنطق) ----------------
    def _say(self, text: str, tag: str = "info") -> None:
        self.queue.put(("console", text, tag))

    def _hub_or_raise(self):
        if self._worker_hub is None:
            raise RuntimeError("غير متصل بالروبوت — اضغط «بلوتوث» أولاً")
        return self._worker_hub

    async def op_connect(self, target=None):
        if not PB_OK:
            raise RuntimeError(f"pybricksdev غير متوفر: {PB_ERR}")
        scan = int(self.cfg.get("scan_timeout", 20))
        retries = int(self.cfg.get("retries", 6))
        for attempt in range(1, retries + 1):
            self._say(f"المحاولة {attempt}/{retries} — جاري البحث عن الروبوت...")
            try:
                dev = await find_device(target or None, timeout=scan)
            except asyncio.TimeoutError:
                self._say("لم يُعثر على الروبوت — تأكد أنه مضاء ويرمش أزرق", "err")
                continue
            hub = PybricksHubBLE(dev)
            try:
                await asyncio.wait_for(hub.connect(), timeout=60)
            except asyncio.CancelledError:
                await asyncio.wait_for(hub.disconnect(), timeout=5)
                raise
            except Exception as e:
                self._say(f"فشل الاتصال: {type(e).__name__}: {e}", "err")
                try:
                    await asyncio.wait_for(hub.disconnect(), timeout=5)
                except Exception:
                    pass
                continue
            self._say(f"تم الاتصال بـ {dev.name} — الفرموير {hub.fw_version}", "ok")
            hub._app_label = str(dev.name)
            self._worker_hub = hub
            try:
                advertised, effective = configure_ble_upload(hub)
                self._say(f"[TX] BLE write_size={effective} hub_limit={advertised}")
                self._say(f"[TX] {transfer_environment(hub)}")
                self._install_capture(hub)
            except BaseException:
                await asyncio.wait_for(hub.disconnect(), timeout=5)
                self._worker_hub = None
                raise
            return hub
        raise RuntimeError(f"لم يتم الاتصال بعد {retries} محاولات")

    async def op_connect_usb(self):
        """اتصال عبر كابل USB — يفحص دعم الفرموير أولاً"""
        if not PB_OK:
            raise RuntimeError(f"pybricksdev غير متوفر: {PB_ERR}")
        try:
            import usb.core
            import usb.backend.libusb1
            import libusb_package
        except Exception as e:
            raise RuntimeError(f"مكتبات USB غير مكتملة: {e}")

        backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
        if backend is None:
            raise RuntimeError("تعذر تحميل مكتبة libusb")

        dev = usb.core.find(idVendor=0x0694, idProduct=0x0009, backend=backend)
        if dev is None:
            raise RuntimeError("الروبوت غير موصول بالسلك — وصّل كابل USB أولاً")

        # فحص الواجهات: هل يوجد واجهة Pybricks خاصة (0xFF)؟
        classes = []
        try:
            cfgs = dev.configurations()
            if cfgs:
                classes = [i.bInterfaceClass for i in cfgs[0]]
        except Exception as exc:
            raise RuntimeError("تعذر قراءة واجهات USB؛ تحقق من التعريفات والصلاحيات") from exc
        self._say("كابل USB مكتشف — الواجهات: " +
                  (", ".join(hex(c) for c in classes) if classes else "؟"), "info")

        if 0xFF not in classes:
            self._say("فيرومور SPIKE Prime الحالي يعرض على السلك منفذ COM فقط (CDC) —", "err")
            self._say("لا يدعم رفع/تشغيل البرامج بالسلك. الرفع يبقى عبر البلوتوث.", "info")
            self._say("الدعم التجريبي للسلك يتطلب بناء فيرموير مخصصاً من labs.pybricks.com (غير موصى به قبل المسابقة).", "info")
            raise RuntimeError("الكابل يعمل للشحن والاستقبال فقط على فيرمويرك الحالي")

        # واجهة Pybricks موجودة ← مسار كامل ممكن
        hub = PybricksHubUSB(dev)
        try:
            await asyncio.wait_for(hub.connect(), timeout=60)
        except BaseException:
            try:
                await asyncio.wait_for(hub.disconnect(), timeout=5)
            except Exception:
                traceback.print_exc()
            raise
        self._worker_hub = hub
        hub._app_label = "USB سلك"
        self._say(f"اتصال سلك ناجح! {getattr(hub, '_hub_name', '')} — الفرموير {hub.fw_version}", "ok")
        try:
            self._install_capture(hub)
        except BaseException:
            await asyncio.wait_for(hub.disconnect(), timeout=5)
            self._worker_hub = None
            raise
        return hub

    def _install_capture(self, hub) -> None:
        cap = HubCapture(self.queue)
        hub._line_handler = cap.handler
        hub.print_output = False
        hub._enable_line_handler = True

        def on_state(state):
            try:
                if state == ConnectionState.DISCONNECTED:
                    cap.close()
                    if self._worker_hub is hub:
                        self._worker_hub = None
                    self.queue.put(("hub_state", "disconnected", hub, self._disconnecting_hub is hub))
            except Exception:
                traceback.print_exc()

        def on_status(flags):
            try:
                self.queue.put(("hub_status", int(flags), hub))
            except Exception:
                traceback.print_exc()

        try:
            cap.subs.append(hub.connection_state_observable.subscribe(on_state))
            cap.subs.append(hub.status_observable.subscribe(on_status))
        except Exception:
            cap.close()
            raise
        self.capture = cap

    async def _transfer_stage(self, hub, phase, operation):
        self._say(f"[TX] phase={phase}")
        try:
            return await operation()
        except Exception as exc:
            # Record the exception chain BEFORE it is handed back across threads.
            self._say(f"[TX] phase={phase} failed: {type(exc).__name__}: {exc}", "err")
            self._say(f"[TX] {transfer_environment(hub)}", "err")
            for note in getattr(exc, "__notes__", ()):
                if note.startswith("[TX] upload "):
                    self._say(note, "err")
            try:
                with open(os.path.join(LOG_DIR, "transfer_errors.log"), "a", encoding="utf-8") as stream:
                    stream.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} phase={phase} "
                                 f"{transfer_environment(hub)}\n")
                    traceback.print_exc(file=stream)
            except OSError:
                traceback.print_exc()
            if "GATT" in type(exc).__name__ or "GATT" in str(exc):
                self._say("فشل أمر بلوتوث. لم تتم إعادة المحاولة تلقائياً؛ أغلق اتصال Pybricks Code، "
                          "ثم أعد الاتصال وحمّل الملف مجدداً. لا تغيّر الفرموير بناءً على هذا الخطأ وحده.", "err")
            raise TransferFailure(phase, exc) from exc

    async def _quiet_stop(self, hub) -> None:
        async def stop_and_wait():
            await hub.stop_user_program()
            if needs_fresh_status(hub):
                self._say("[TX] waiting_for=fresh-idle-status")
                await wait_hub_idle(hub, require_fresh=True)
            else:
                await wait_hub_idle(hub)
        await self._transfer_stage(hub, "stop-ready", stop_and_wait)

    async def _download_program(self, hub, path):
        # Cancellation or failure may leave only part of the program in hub RAM.
        self._download_uncertain = True
        async def upload():
            with trace_upload_writes(hub):
                await hub.download(path)
        await self._transfer_stage(hub, "compile-upload", upload)
        if needs_fresh_status(hub):
            await self._transfer_stage(hub, "upload-ready",
                                       lambda: wait_hub_idle(hub, require_fresh=True))
        else:
            await self._transfer_stage(hub, "upload-ready", lambda: wait_hub_idle(hub))
        self._download_uncertain = False
        finish_upload_progress()
        self._say("تم رفع الملف كاملاً ✔", "ok")

    async def op_send_run(self, path, wait):
        hub = self._hub_or_raise()
        await self._quiet_stop(hub)
        if getattr(hub, "_mpy_abi_version", 0):
            # Older hubs do not support run(None). Preserve the library fallback.
            self._download_uncertain = True
            await self._transfer_stage(hub, "legacy-upload-run", lambda: hub.run(
                path, wait=wait, print_output=False, line_handler=True))
            self._download_uncertain = False
        else:
            self._say(f"جاري الترجمة والرفع: {os.path.basename(path)} ...")
            await self._download_program(hub, path)
            # A failed upload never reaches start; a failed start is NOT retried.
            await self._transfer_stage(hub, "start-wait" if wait else "start", lambda: hub.run(
                None, wait=wait, print_output=False, line_handler=True))
        if wait:
            self._say("انتهى تنفيذ البرنامج ✔", "ok")
        else:
            self._say("تم الرفع والتشغيل ✔ — يبدأ البرنامج فوراً؛ تأكد من وضع الروبوت الآمن", "ok")

    async def op_download_only(self, path):
        hub = self._hub_or_raise()
        await self._quiet_stop(hub)
        self._say(f"جاري التحميل بدون تشغيل: {os.path.basename(path)} ...")
        await self._download_program(hub, path)
        self._say("تم التحميل ✔ — شغّله لاحقاً بزر «تشغيل المخزن»", "ok")

    async def op_run_stored(self):
        hub = self._hub_or_raise()
        if self._download_uncertain:
            raise RuntimeError("فشل رفع سابق أو تم إلغاؤه؛ أعد تحميل ملف كامل بنجاح قبل تشغيل المخزن")
        await self._quiet_stop(hub)
        self._say("جاري تشغيل البرنامج المخزن ...")
        await self._transfer_stage(hub, "start-stored", lambda: hub.run(
            None, wait=False, print_output=False, line_handler=True))
        self._say("تم تشغيل البرنامج المخزن ✔", "ok")

    async def op_stop(self):
        hub = self._worker_hub
        if hub is None:
            return
        await hub.stop_user_program()
        self._say("تم إيقاف البرنامج ✔", "ok")

    async def op_disconnect(self):
        hub = self._worker_hub
        if hub is not None:
            # Keep ownership after failure so the user can retry disconnecting.
            self._disconnecting_hub = hub
            try:
                await asyncio.wait_for(hub.disconnect(), timeout=5)
            finally:
                self._disconnecting_hub = None
        self._worker_hub = None
        if self.capture:
            self.capture.close()
            self.capture = None
        self._say("تم قطع الاتصال", "info")

    # Each operation owns a task; interrupts await cancellation BEFORE stopping.
    async def _execute_op(self, op_id, factory, interrupt=False):
        task = asyncio.current_task()
        self._async_tasks[op_id] = task
        try:
            if interrupt:
                others = [t for key, t in self._async_tasks.items() if key != op_id]
                for other in others:
                    other.cancel()
                if others:
                    await asyncio.gather(*others, return_exceptions=True)
            if self._op_lock is None:
                self._op_lock = asyncio.Lock()
            async with self._op_lock:
                with upload_progress(lambda sent, total, complete: self.queue.put(
                        ("upload_progress", op_id, sent, total, complete))):
                    return await factory()
        finally:
            self._async_tasks.pop(op_id, None)

    def _start_op(self, name, factory, allow_busy=False):
        if self._closing:
            return False
        if not PB_OK:
            self.console(f"غير جاهز: {PB_ERR}", "err")
            return False
        if not _WORKER.loop.is_running():
            self.console("عامل الاتصال غير متاح؛ أعد تشغيل التطبيق", "err")
            return False
        if (self.busy and not allow_busy) or any(
                entry[0] in ("stop", "disconnect") for entry in self._operations.values()):
            self.console("أكمل العملية الحالية أولاً (Esc يلغي العملية ويوقف البرنامج)", "info")
            return False
        self._next_op += 1
        op_id = self._next_op
        self._operations[op_id] = (name, None)
        self.busy = True
        self._refresh_buttons()
        coro = self._execute_op(op_id, factory, interrupt=allow_busy)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, _WORKER.loop)
        except Exception as exc:
            coro.close()
            self.queue.put(("op_error", op_id, name, None, exc))
            return False
        self._operations[op_id] = (name, fut)

        def _done(f):
            try:
                res = f.result()
                self.queue.put(("op_done", op_id, name, res, None))
            except Exception as exc:
                self.queue.put(("op_error", op_id, name, None, exc))
        fut.add_done_callback(_done)
        return True

    def _refresh_buttons(self) -> None:
        c = self.connected and self.hub is not None
        b = self.busy
        self.btn_connect.configure(state="disabled" if (c or b) else "normal")
        self.btn_disconnect.configure(state="normal" if c else "disabled")
        self._configure_action("usb", state="disabled" if (c or b) else "normal")
        st_send = "normal" if (c and not b) else "disabled"
        self.btn_send.configure(state=st_send)
        self._configure_action("sendwait", state=st_send)
        self._configure_action("dl", state=st_send)
        self._configure_action("stored", state=st_send)
        self.btn_stop.configure(state="normal" if (c or b) else "disabled")

    # ---------------- أفعال الواجهة ----------------
    def action_connect(self):
        if self._closing or self.busy or self.connected:
            return
        target = self.target_var.get().strip()
        if len(target) > 128 or any(ord(c) < 32 for c in target):
            messagebox.showerror("اسم الروبوت", "اسم أو عنوان الروبوت غير صالح")
            return
        if not target and not messagebox.askyesno(
                "اختيار الروبوت", "لم تحدد اسماً أو عنواناً. هل تريد الاتصال بأول روبوت متاح؟",
                default="no"):
            return
        self.cfg["hub_name"] = target
        self.console("جاري الاتصال بالروبوت...", "info")
        self._start_op("connect", lambda: self.op_connect(target))

    def action_disconnect(self):
        self._start_op("disconnect", self.op_disconnect, allow_busy=True)

    def action_usb_connect(self):
        if self.connected and self.hub is not None:
            self.console("اقطع الاتصال الحالي أولاً", "info")
            return
        self.console("جاري فحص كابل USB والاتصال بالسلك...", "info")
        self._start_op("usb_connect", self.op_connect_usb)

    # ---------------- بث السلك (استقبال من COM) ----------------
    def action_serial_toggle(self):
        if self._closing:
            return
        if self.serial_running:
            self.serial_stop.set()
            self.console("إيقاف بث السلك...", "info")
            return
        port = self._find_hub_port()
        if not port:
            self.console("لا يوجد منفذ سلك للروبوت — وصّل الكابل وتأكد من تثبيت pyserial", "err")
            return
        stop = self.serial_stop = threading.Event()
        self.serial_running = True  # reserve before starting; a second click stops it
        self._sync_serial_button()
        self.serial_thread = threading.Thread(target=self._serial_reader, args=(port, stop),
                                              name="serial-reader", daemon=True)
        try:
            self.serial_thread.start()
        except RuntimeError as exc:
            self.serial_running = False
            self._sync_serial_button()
            self.console(f"تعذر بدء قارئ السلك: {exc}", "err")

    def _find_hub_port(self):
        try:
            from serial.tools import list_ports
            for p in list_ports.comports():
                if p.vid == 0x0694 and p.pid == 0x0009:
                    return p.device
        except Exception:
            pass
        return None

    def _serial_reader(self, port: str, stop: threading.Event) -> None:
        s = None
        buf = b""
        discarding = False
        try:
            import serial as ser
            s = ser.Serial(port, 115200, timeout=0.5)
            self.queue.put(("serial_state", True, stop))
            self.queue.put(("console", f"تم فتح {port} — بانتظار بيانات الروبوت", "info"))
            while not stop.is_set():
                data = s.read(256)
                if not data:
                    continue
                if discarding:
                    if b"\n" not in data:
                        continue
                    data = data.split(b"\n", 1)[1]
                    discarding = False
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if len(line) > MAX_LINE_BYTES:
                        self.queue.put(("data_loss", "سطر السلك طويل جداً؛ ابدأ جلسة جديدة"))
                    else:
                        text = line.decode("utf-8", "replace").rstrip("\r")
                        if text:
                            self.queue.put(("console", text, "out", "serial"))
                if len(buf) > MAX_LINE_BYTES:
                    buf = b""
                    discarding = True
                    self.queue.put(("data_loss", "تجاوز مخزن السلك الحد المسموح؛ ابدأ جلسة جديدة"))
        except Exception as exc:
            self.queue.put(("console", f"خطأ في بث {port}: {exc}", "err"))
            self.queue.put(("data_loss", "انقطع بث السلك؛ تحقق من اكتمال بيانات التدريب"))
        finally:
            if buf and not discarding:
                self.queue.put(("console", buf.decode("utf-8", "replace").rstrip("\r"), "out", "serial"))
            if s is not None:
                try:
                    s.close()
                except Exception as exc:
                    self.queue.put(("console", f"تعذر إغلاق {port}: {exc}", "err"))
            self.queue.put(("serial_state", False, stop))

    def _require_saved_file(self, document=None):
        document = document or self.active_document
        if not document.path or document.dirty:
            if not self._save_document(document):
                return None
        return document.path

    def _send_document(self, operation, wait=False):
        if self._closing or self._confirming_close or self.busy:
            return False
        document = self.active_document
        path = self._require_saved_file(document)
        if not path:
            return False
        if operation == "download":
            factory = lambda: self.op_download_only(path)
        else:
            factory = lambda: self.op_send_run(path, wait=wait)
        if self._start_op(operation, factory):
            self._sending_path = path
            self._send_op_id = self._next_op
            self._reset_upload_progress(self._send_op_id)
            self._update_send_target()
            self.console(f"إرسال الملف: {os.path.basename(path)}", "info")
            return True
        return False

    def action_send_run(self):
        return self._send_document("send_run")

    def action_send_wait(self):
        return self._send_document("send_wait", wait=True)

    def action_download_only(self):
        return self._send_document("download")

    def action_run_stored(self):
        self._start_op("run_stored", self.op_run_stored)

    def action_stop(self):
        if self.busy or self.connected:
            self._start_op("stop", self.op_stop, allow_busy=True)

    # ---------------- الملفات ----------------
    def _editor_dirty(self):
        return self.active_document.dirty

    def _confirm_discard(self, document=None):
        document = document or self.active_document
        if not document.dirty:
            return True
        self._select_document(document)
        answer = messagebox.askyesnocancel(
            "تعديلات غير محفوظة", f"هل تريد حفظ التعديلات على {document.name} قبل الإغلاق؟")
        if answer is None:
            return False
        return self._save_document(document) if answer else True

    def action_new(self):
        return self._new_document()

    def action_open(self):
        if self._closing or self._confirming_close:
            return False
        path = filedialog.askopenfilename(
            title="فتح ملف بايثون", filetypes=[("ملفات بايثون", "*.py"), ("الكل", "*.*")],
            initialdir=(os.path.dirname(self.current_path) if self.current_path else APP_DIR))
        return self._open_path(path) if path else False

    def _find_document(self, path):
        key = canonical_path(path)
        return next((document for document in self.documents
                     if document.path and canonical_path(document.path) == key), None)

    def _open_path(self, path):
        if self._closing or self._confirming_close:
            return False
        existing = self._find_document(path)
        if existing:
            self._select_document(existing)
            return True
        try:
            with open(path, "r", encoding="utf-8-sig") as stream:
                text = stream.read()
        except Exception as exc:
            messagebox.showerror("خطأ", f"تعذر فتح الملف:\n{exc}")
            return False
        self._new_document(text, path=os.path.abspath(path))
        self._add_recent(os.path.abspath(path))
        self.console(f"فُتح: {path}", "ok")
        return True

    def action_save(self):
        if self._confirming_close:
            return False
        return self._save_document(self.active_document)

    def action_save_as(self):
        if self._confirming_close:
            return False
        return self._save_document(self.active_document, save_as=True)

    def _save_document(self, document, save_as=False):
        if self._closing or document not in self.documents:
            return False
        path = document.path
        if not path or save_as:
            path = filedialog.asksaveasfilename(
                title="حفظ باسم", defaultextension=".py", filetypes=[("ملفات بايثون", "*.py")],
                initialdir=os.path.dirname(path) if path else APP_DIR,
                initialfile=os.path.basename(path) if path else (
                    document.suggested_name or f"program_{document.number}.py"))
            if not path:
                return False
        return self._save_to(path, document)

    def _save_to(self, path, document=None):
        document = document or self.active_document
        if self._closing or document not in self.documents:
            return False
        owner = self._find_document(path)
        if owner is not None and owner is not document:
            messagebox.showerror("ملف مفتوح", "هذا الملف مفتوح في تبويب آخر؛ احفظ باسم مختلف أو انتقل إلى تبويبه.")
            return False
        try:
            text = document.editor.get("1.0", "end-1c")
            atomic_write(path, text)
        except Exception as exc:
            messagebox.showerror("خطأ", f"تعذر الحفظ:\n{exc}")
            return False
        document.saved_text = text
        document.editor.edit_modified(False)
        document.path = os.path.abspath(path)
        self._add_recent(document.path)
        self._set_file_label()
        self.console(f"حُفظ: {path}", "ok")
        return True

    def _update_send_target(self):
        path = self._sending_path
        label = to_visual("ملف العملية الحالية:") if path else to_visual("سيُرسل:")
        name = (f"{os.path.basename(path)} — {os.path.dirname(os.path.abspath(path))}"
                if path else self.active_document.name)
        self.lbl_send_target.configure(text=f"{label} {to_visual(name)}")

    def _set_file_label(self):
        if self.active_document is None:
            return
        for document, title in tab_names(self.documents).items():
            self.notebook.tab(document.frame, text=to_visual(title) + "  ")
        document = self.active_document
        marker = "* " if document.dirty else ""
        self.lbl_file.configure(text=marker + to_visual(document.name))
        self.file_path_var.set(os.path.abspath(document.path) if document.path else "")
        self._update_send_target()
        if not self._closing:
            self.root.title(f"{marker}{document.name} — Hyena Control")

    def _add_recent(self, p):
        recent = [x for x in self.cfg.get("recent", []) if x != p]
        recent.insert(0, p)
        self.cfg["recent"] = recent[:6]
        self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        if not hasattr(self, "m_recent"):
            return
        self.m_recent.delete(0, "end")
        recent = self.cfg.get("recent", [])
        if not recent:
            self.m_recent.add_command(label="(لا يوجد)", state="disabled")
            return
        for p in recent:
            self.m_recent.add_command(
                label=os.path.basename(p),
                command=lambda pp=p: self._open_path(pp))

    def action_open_logs(self):
        try:
            os.startfile(LOG_DIR)
        except Exception:
            messagebox.showinfo("مجلد السجلات", LOG_DIR)

    # ---------------- أدوات ----------------
    def action_clear_console(self):
        if self.training_lines and not messagebox.askyesno(
                "جلسة تدريب جديدة", "سيتم مسح بيانات التدريب الحالية من الذاكرة. متابعة؟"):
            return
        self.queue.discard_console()
        self.console_w.delete("1.0", "end")
        self.console_raw.clear()
        self.training_lines.clear()
        self.training_overflow = False
        self.rx_count = 0
        self.session_id += 1
        self.lbl_rx.configure(text="0 سطر")

    def action_export_log(self):
        p = filedialog.asksaveasfilename(
            title="تصدير سجل المخرجات", defaultextension=".txt",
            filetypes=[("نص", "*.txt"), ("الكل", "*.*")],
            initialdir=LOG_DIR,
            initialfile="hub_log_" + time.strftime("%Y%m%d_%H%M%S") + ".txt")
        if not p:
            return
        try:
            atomic_write(p, "\n".join(self.console_raw))
            self.console(f"صُدّر السجل: {p}", "ok")
        except Exception as e:
            messagebox.showerror("خطأ", f"تعذر التصدير:\n{e}")

    def _training_snapshot(self):
        revision, pending, lost = self.queue.training_status()
        if self.training_overflow or pending or lost or self._closing:
            return None
        return self.session_id, self.training_revision, revision

    def action_race_gen(self):
        RaceGenWindow(self)

    def action_help(self):
        win = tk.Toplevel(self.root)
        win.title("شرح الاستخدام")
        win.configure(bg=self.BG)
        win.geometry("680x520")
        t = tk.Text(win, wrap="word", bg=self.ENTRY_BG, fg=self.FG,
                    font=("Segoe UI", 11), padx=12, pady=12)
        sb = ttk.Scrollbar(win, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        t.insert("1.0", "\n".join(to_visual(line) for line in HELP_TEXT.split("\n")))
        t.configure(state="disabled")

    def _insert_at_cursor(self, text):
        self.editor.insert("insert", text)
        self.editor.focus_set()

    # ---------------- الإغلاق ----------------
    async def _shutdown(self):
        tasks = list(self._async_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.op_disconnect()
        finally:
            if self.capture:
                self.capture.close()
                self.capture = None

    def _on_close(self):
        if self._closing or self._confirming_close:
            return
        # Resolve every decision before removing any tab or disconnecting the robot.
        # Native dialogs run nested event loops: block reentrant close/new/open shortcuts.
        self._confirming_close = True
        try:
            for document in list(self.documents):
                if not self._confirm_discard(document):
                    return
        finally:
            self._confirming_close = False
        self._save_config()
        self._closing = True
        for document in self.documents:
            document.editor.configure(state="disabled")
            if document.highlight_job:
                self.root.after_cancel(document.highlight_job)
                document.highlight_job = None
        if self._poll_job:
            self.root.after_cancel(self._poll_job)
        if self.serial_stop:
            self.serial_stop.set()
        self.root.title("Hyena Control — جاري إغلاق الاتصال...")
        if _WORKER.loop.is_running():
            self._shutdown_future = asyncio.run_coroutine_threadsafe(self._shutdown(), _WORKER.loop)
        else:
            self._shutdown_future = None
        self._close_started = time.monotonic()
        self._finish_close()

    def _finish_close(self):
        future = self._shutdown_future
        serial_alive = self.serial_thread is not None and self.serial_thread.is_alive()
        if (future is not None and not future.done()) or serial_alive:
            if time.monotonic() - self._close_started > 10:
                # Don't silently kill a connection whose cleanup may still be active.
                self.root.title("Hyena Control — انتظار إغلاق الاتصال؛ تحقق من الكابل")
            self.root.after(50, self._finish_close)
            return
        if future is not None:
            try:
                future.result()
            except Exception:
                traceback.print_exc()
        if self.session_file:
            try:
                self.session_file.close()
            except OSError:
                traceback.print_exc()
        if _WORKER.loop.is_running():
            _WORKER.loop.call_soon_threadsafe(_WORKER.loop.stop)
        else:
            _WORKER.loop.close()
        self.root.destroy()


# ---------------------------------------------------------------------------
# نافذة توليد برنامج السباق
# ---------------------------------------------------------------------------
class RaceGenWindow:
    def __init__(self, app: App):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("توليد ملف سباق من بيانات التدريب")
        self.win.configure(bg=app.BG)
        self.win.geometry("760x600")

        ttk.Label(self.win, text="المخرجات المستخرجة من الكونسول — راجعها ثم احفظها:",
                  style="Header.TLabel").pack(anchor="e", padx=10, pady=(10, 2))

        box = tk.Frame(self.win, bg=app.BG)
        box.pack(fill="both", expand=True, padx=10)
        self.preview = tk.Text(box, wrap="none", bg=app.ENTRY_BG, fg=app.FG,
                               font=("Consolas", 11), padx=8, pady=6)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.preview.pack(side="left", fill="both", expand=True)
        hscroll = ttk.Scrollbar(self.win, orient="horizontal", command=self.preview.xview)
        self.preview.configure(xscrollcommand=hscroll.set)
        hscroll.pack(fill="x", padx=10)

        bar = ttk.Frame(self.win, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="حفظ كملف...", command=self.save).pack(side="left", padx=4)
        ttk.Button(bar, text="فتح في تبويب جديد", command=self.to_editor).pack(side="left", padx=4)
        ttk.Button(bar, text="تحديث من الكونسول", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(bar, text="إغلاق", command=self.win.destroy).pack(side="right", padx=4)

        self.refresh()

    def refresh(self):
        snapshot = self.app._training_snapshot()
        if snapshot is None:
            self.preview.delete("1.0", "end")
            messagebox.showerror("بيانات غير مكتملة", "انتظر اكتمال الاستقبال ومعالجة الرسائل. عند فقد بيانات ابدأ جلسة جديدة وأعد استقبال التدريب كاملاً.")
            return
        kept = []
        errors = []
        for number, line in enumerate(self.app.training_lines, 1):
            try:
                command = training_line(line)
                if command is not None:
                    kept.append(command)
            except ValueError as exc:
                errors.append(f"السطر {number}: {exc}")
        if errors:
            self.preview.delete("1.0", "end")
            messagebox.showerror("أوامر تدريب مرفوضة", "\n".join(errors[:10]))
            return
        if not kept:
            kept = ["# (لا توجد بيانات تدريب في الجلسة الحالية)"]

        head = SETUP_TEMPLATE.split("# --- بداية المسار ---")[0] + \
            "# --- بداية المسار ---\nhub.light.on(Color.GREEN)\n\n"
        tail = "\n# --- نهاية المسار ---\nrobot.stop()\n" \
               "hub.light.on(Color.BLUE)\nhub.speaker.beep(900, 200)\n"
        if snapshot != self.app._training_snapshot():
            self.preview.delete("1.0", "end")
            messagebox.showerror("تغيرت البيانات", "وصلت بيانات جديدة أثناء التوليد؛ انتظر اكتمال الاستقبال ثم حدّث المعاينة.")
            return
        self.preview_snapshot = snapshot
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", head + "\n".join(kept) + "\n" + tail)
        self.app.console(f"توليد سباق — الجلسة {self.app.session_id}: {len(kept)} سطر. راجع المقاسات والحركة قبل التشغيل.", "ok")

    def _preview_current(self):
        snapshot = self.app._training_snapshot()
        if snapshot is None or getattr(self, "preview_snapshot", None) != snapshot:
            messagebox.showerror("معاينة قديمة", "تغيرت بيانات التدريب أو ما زال الاستقبال قيد المعالجة؛ حدّث المعاينة قبل الحفظ أو النسخ.")
            return False
        return bool(self.preview.get("1.0", "end-1c").strip())

    def save(self):
        if self.app._closing or self.app._confirming_close or not self._preview_current():
            return
        p = filedialog.asksaveasfilename(
            title="حفظ برنامج السباق", defaultextension=".py",
            filetypes=[("ملفات بايثون", "*.py")],
            initialdir=(os.path.dirname(self.app.current_path) if self.app.current_path else APP_DIR),
            initialfile="race_" + time.strftime("%H%M%S") + ".py")
        if not p or not self._preview_current():
            return
        if self.app._find_document(p) is not None:
            messagebox.showerror("ملف مفتوح", "هذا الملف مفتوح في تبويب؛ افتح نتيجة السباق في تبويب جديد واحفظها باسم مختلف.")
            return
        try:
            atomic_write(p, self.preview.get("1.0", "end-1c"))
            self.app.console(f"حُفظ برنامج السباق: {p}", "ok")
            self.app._add_recent(p)
        except Exception as e:
            messagebox.showerror("خطأ", f"تعذر الحفظ:\n{e}")

    def to_editor(self):
        if not self._preview_current() or self.app._closing:
            return
        content = self.preview.get("1.0", "end-1c")
        document = self.app._new_document(content, saved=False,
                                           suggested_name="race_" + time.strftime("%H%M%S") + ".py")
        if document is not None:
            self.win.destroy()


# ---------------------------------------------------------------------------
# خيط BLE (حلقة أحداث مستقلة)
# ---------------------------------------------------------------------------
class Worker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = None
        self.ready = threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self._run, name="ble-worker", daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("تعذر بدء عامل الاتصال")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self.ready.set)
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()


_WORKER = Worker()


# ---------------------------------------------------------------------------
# نقطة البداية
# ---------------------------------------------------------------------------
def _run_gui(smoke=False):
    root = None
    app = None
    try:
        if PB_OK:
            _WORKER.start()
        root = tk.Tk()
        app = App(root)
        if smoke:
            root.update()
            app.console("سطر تجريبي 1", "info")
            app.console("robot.straight(100)", "out")
            app.console("تم ✔", "ok")
            root.update()
            app._saved_text = app.editor.get("1.0", "end-1c")
            root.after(10, app._on_close)
        root.mainloop()
    except Exception as exc:
        traceback.print_exc()
        if root is not None:
            try:
                messagebox.showerror("تعذر تشغيل التطبيق", str(exc), parent=root)
            except Exception:
                traceback.print_exc()
            try:
                root.destroy()
            except Exception:
                traceback.print_exc()
        raise SystemExit(1) from exc
    finally:
        # Also clean up when Tk fails or mainloop ends without WM_DELETE_WINDOW.
        if app is not None:
            app._closing = True
            if app.serial_stop:
                app.serial_stop.set()
            if app.serial_thread:
                app.serial_thread.join(timeout=1)
            if _WORKER.loop.is_running():
                try:
                    future = getattr(app, "_shutdown_future", None)
                    if future is None:
                        future = asyncio.run_coroutine_threadsafe(app._shutdown(), _WORKER.loop)
                    future.result(timeout=7)
                except Exception:
                    traceback.print_exc()
            if app.session_file:
                try:
                    app.session_file.close()
                except OSError:
                    traceback.print_exc()
        if _WORKER.loop.is_running():
            _WORKER.loop.call_soon_threadsafe(_WORKER.loop.stop)
        if _WORKER.thread:
            _WORKER.thread.join(timeout=5)
        elif not _WORKER.loop.is_closed():
            _WORKER.loop.close()


def main():
    _run_gui()


def selftest():
    """Build the UI and exercise the normal close path, with guaranteed cleanup."""
    _run_gui(smoke=True)
    print("SELFTEST OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
