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
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# مسار التطبيق أولاً (نحتاجه لتهيئة سجل المخرجات)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "spike_controller_config.json")
LOG_DIR = os.path.join(APP_DIR, "logs")

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

DEFAULTS = {
    "geometry": "1220x780",
    "sash": 440,
    "timestamps": True,
    "autolog": True,
    "scan_timeout": 20,
    "retries": 6,
    "recent": [],
    "autoconnect": False,
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
   - اضغط «اتصال» — يعيد المحاولة تلقائياً حتى يظهر الروبوت
   - عند الاتصال تظهر الفرموير في شريط الحالة

2) إرسال وتشغيل:
   - افتح ملف .py أو اكتبه في المحرر
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
   - بعد استقبال بيانات التدريب اضغط «توليد ملف سباق»
   - يستخرج الأسطر المسجّلة (robot.straight / robot.turn)
   - راجع المعاينة ثم «حفظ كملف» وأرسله للروبوت

5) الحفظ:
   - كل مخرجات الروبوت تُحفظ تلقائياً في مجلد logs
   - «تصدير السجل» يحفظ نسخة مختارة يدوياً

6) اختصارات:
   F5 إرسال وتشغيل | Ctrl+F5 إرسال مع الانتظار
   Esc إيقاف البرنامج | F1 مساعدة
   Ctrl+N/O/S فتح/حفظ/جديد | Ctrl+L مسح الكونسول
   Ctrl+Shift+C نسخ المحدد من الكونسول (أو زر يمين في الكونسول)

7) الكونسول:
   - حدّد أي نص بالفأرة وانسخه (Ctrl+C أو قائمة يمين)
   - الكونسول قراءة فقط — لا يُمسح بمجرد الضغط على المفاتيح
"""

# ---------------------------------------------------------------------------
# استيراد pybricksdev (اختياري — رسالة خطأ واضحة إن لم يُثبَّت)
# ---------------------------------------------------------------------------
try:
    from bleak import BleakClient
    from pybricksdev.ble import find_device
    from pybricksdev.ble.pybricks import StatusFlag
    from pybricksdev.connections import ConnectionState
    from pybricksdev.connections.pybricks import PybricksHubBLE, PybricksHubUSB

    # كرت CSR بطيء في اكتشاف GATT — نمدّ مهلة الاتصال الافتراضية
    _orig_connect = BleakClient.connect

    async def _connect_long(self, *args, **kwargs):
        kwargs.setdefault("timeout", 60)
        return await _orig_connect(self, *args, **kwargs)

    BleakClient.connect = _connect_long
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
        self.subs = []

    def handler(self, line) -> None:
        raw = line if isinstance(line, (bytes, bytearray)) else str(line).encode("utf-8", "replace")
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:
            text = repr(raw)

        # الروبوت يطلب فتح ملف سجل محلي
        if b"PB_OF:" in raw or b"_file_begin_ " in raw:
            start = len(b"PB_OF:") if b"PB_OF:" in raw else len(b"_file_begin_ ")
            name = raw[start:].decode("utf-8", "replace").strip()
            safe = os.path.basename(name.replace("\\", "/")) or "hub_log.txt"
            self.log_path = os.path.join(LOG_DIR, safe)
            try:
                self.log_file = open(self.log_path, "w", encoding="utf-8")
                self.out.put(("console", f"[روبوت] بدأ حفظ سجل: {self.log_path}", "ok"))
            except Exception as e:
                self.out.put(("console", f"[روبوت] تعذر فتح ملف السجل: {e}", "err"))
            return

        if b"PB_EOF" in raw or b"_file_end_" in raw:
            if self.log_file:
                try:
                    self.log_file.close()
                except Exception:
                    pass
                self.log_file = None
                self.out.put(("console", "[روبوت] انتهى حفظ السجل", "ok"))
            return

        if self.log_file:
            self.log_file.write(text + "\n")
            self.log_file.flush()
            return

        self.out.put(("console", text, "out"))

    def close(self) -> None:
        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
            self.log_file = None
        for s in self.subs:
            try:
                s.dispose()
            except Exception:
                pass
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

        self.queue: "queue.Queue" = queue.Queue()
        self.hub = None                 # كائن PybricksHubBLE/USB (يُنشئ في خيط BLE)
        self.capture = None
        self.busy = False
        self.connected = False
        self.current_path = None
        self.rx_count = 0
        self.session_file = None
        self._hl_job = None
        self.serial_running = False
        self.serial_stop = None

        os.makedirs(LOG_DIR, exist_ok=True)

        self._build_style()
        self._build_ui()
        self._bind_shortcuts()
        self._refresh_buttons()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()

        if not PB_OK:
            self.console(f"تعذر تحميل pybricksdev: {PB_ERR}", "err")
            self.console("ثبّته بـ: pip install pybricksdev bleak", "info")

        self.console("مرحباً — اضغط «اتصال» بعد تشغيل الروبوت (F1 للمساعدة)", "info")

        if self.cfg.get("autoconnect"):
            self.root.after(500, self.action_connect)

    # ---------------- إعدادات ----------------
    def _load_config(self) -> dict:
        cfg = dict(DEFAULTS)
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
        return cfg

    def _save_config(self) -> None:
        try:
            self.cfg["geometry"] = self.root.geometry()
            try:
                self.cfg["sash"] = self.paned.sashpos(0)
            except Exception:
                pass
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

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

    def _build_ui(self) -> None:
        self.root.title("Hyena Control — تحكم روبوت SPIKE Prime (Pybricks)")
        try:
            self.root.geometry(self.cfg.get("geometry", DEFAULTS["geometry"]))
        except Exception:
            self.root.geometry("1220x780")
        self.root.minsize(900, 560)

        self._build_menu()

        # ---- شريط الأدوات ----
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side="top", fill="x")

        self.btn_connect = ttk.Button(bar, text="اتصال", command=self.action_connect)
        self.btn_connect.pack(side="left", padx=2)
        self.btn_disconnect = ttk.Button(bar, text="قطع الاتصال", command=self.action_disconnect)
        self.btn_disconnect.pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        self.btn_usb = ttk.Button(bar, text="اتصال بالسلك ▶", command=self.action_usb_connect)
        self.btn_usb.pack(side="left", padx=2)
        self.btn_serial = ttk.Button(bar, text="بث السلك ▶", command=self.action_serial_toggle)
        self.btn_serial.pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        self.btn_send = ttk.Button(bar, text="إرسال وتشغيل ▶", style="Accent.TButton",
                                   command=self.action_send_run)
        self.btn_send.pack(side="left", padx=2)
        self.btn_sendwait = ttk.Button(bar, text="إرسال مع الانتظار ⏳",
                                       command=self.action_send_wait)
        self.btn_sendwait.pack(side="left", padx=2)
        self.btn_dl = ttk.Button(bar, text="تحميل فقط ⤓", command=self.action_download_only)
        self.btn_dl.pack(side="left", padx=2)
        self.btn_stored = ttk.Button(bar, text="تشغيل المخزن ↺", command=self.action_run_stored)
        self.btn_stored.pack(side="left", padx=2)
        self.btn_stop = ttk.Button(bar, text="إيقاف ■", style="Danger.TButton",
                                   command=self.action_stop)
        self.btn_stop.pack(side="left", padx=2)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)

        self.btn_race = ttk.Button(bar, text="توليد ملف سباق ⚙", command=self.action_race_gen)
        self.btn_race.pack(side="left", padx=2)
        self.btn_clear = ttk.Button(bar, text="مسح الكونسول", command=self.action_clear_console)
        self.btn_clear.pack(side="left", padx=2)
        self.btn_export = ttk.Button(bar, text="تصدير السجل", command=self.action_export_log)
        self.btn_export.pack(side="left", padx=2)

        # ---- منطقة رئيسية: محرر + كونسول ----
        self.paned = ttk.PanedWindow(self.root, orient="vertical")
        self.paned.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        # محرر
        ed_frame = ttk.Frame(self.paned)
        self.paned.add(ed_frame, weight=3)
        ttk.Label(ed_frame, text="المحرر", style="Header.TLabel").pack(anchor="e")

        ed_box = tk.Frame(ed_frame, bg=self.BG)
        ed_box.pack(fill="both", expand=True)
        self.ln = tk.Text(ed_box, width=4, padx=6, takefocus=0, border=0,
                          state="disabled", bg=self.PANEL, fg="#5c6373",
                          font=("Consolas", 11), cursor="arrow")
        self.ln.pack(side="left", fill="y")
        self.editor = tk.Text(ed_box, wrap="none", undo=True, border=0,
                              bg=self.ENTRY_BG, fg=self.FG, insertbackground=self.FG,
                              font=("Consolas", 12), padx=8, pady=6,
                              selectbackground=self.ACCENT)
        self.sb_ed = ttk.Scrollbar(ed_box, orient="vertical",
                                   command=self._editor_scroll)
        self.editor.configure(yscrollcommand=self._editor_yview)
        self.sb_ed.pack(side="right", fill="y")
        self.editor.pack(side="left", fill="both", expand=True)

        self.editor.insert("1.0", STARTER_TEXT)
        self.editor.edit_reset()

        # كونسول
        cs_frame = ttk.Frame(self.paned)
        self.paned.add(cs_frame, weight=2)

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
        self.console_w.bind("<Button-3>", self._console_menu)
        self.editor.bind("<Button-3>", self._editor_menu)

        # محرر: تلوين الصيغة + أرقام الأسطر
        self.editor.tag_configure("kw", foreground="#79b8ff")
        self.editor.tag_configure("str", foreground="#a5d6a7")
        self.editor.tag_configure("com", foreground="#6a7385", font=("Consolas", 12, "italic"))
        self.editor.tag_configure("num", foreground="#f2a73b")
        self.editor.bind("<<Modified>>", self._on_editor_modified)
        self.editor.bind("<MouseWheel>", lambda e: self.root.after_idle(self._update_lineno))
        self._update_lineno()
        try:
            self.paned.sashpos(0, int(self.cfg.get("sash", 440)))
        except Exception:
            pass

        # ---- شريط الحالة ----
        sb = ttk.Frame(self.root, padding=(10, 4))
        sb.pack(side="bottom", fill="x")
        self.lbl_state = ttk.Label(sb, text="● غير متصل", foreground=self.ERR)
        self.lbl_state.pack(side="left")
        self.lbl_prog = ttk.Label(sb, text="", style="Header.TLabel")
        self.lbl_prog.pack(side="left", padx=14)
        self.chk_ts = tk.Checkbutton(sb, text="طوابع زمنية", variable=self._ts_var(),
                                     command=self._toggle_ts, bg=self.BG, fg="#9aa3b5",
                                     selectcolor=self.PANEL, activebackground=self.BG,
                                     activeforeground=self.FG)
        self.chk_ts.pack(side="left", padx=6)
        self.chk_al = tk.Checkbutton(sb, text="حفظ تلقائي", variable=self._al_var(),
                                     command=self._toggle_al, bg=self.BG, fg="#9aa3b5",
                                     selectcolor=self.PANEL, activebackground=self.BG,
                                     activeforeground=self.FG)
        self.chk_al.pack(side="left", padx=6)
        self.lbl_file = ttk.Label(sb, text="ملف جديد", style="Header.TLabel")
        self.lbl_file.pack(side="right", padx=8)
        self.lbl_rx = ttk.Label(sb, text="0 سطر", style="Header.TLabel")
        self.lbl_rx.pack(side="right")

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

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="جديد", accelerator="Ctrl+N", command=self.action_new)
        m_file.add_command(label="فتح...", accelerator="Ctrl+O", command=self.action_open)
        m_file.add_command(label="حفظ", accelerator="Ctrl+S", command=self.action_save)
        m_file.add_command(label="حفظ باسم...", accelerator="Ctrl+Shift+S",
                           command=self.action_save_as)
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
        m_tools.add_command(label="مسح الكونسول", accelerator="Ctrl+L",
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

    def _bind_shortcuts(self) -> None:
        r = self.root
        r.bind_all("<Control-n>", lambda e: self.action_new())
        r.bind_all("<Control-o>", lambda e: self.action_open())
        r.bind_all("<Control-s>", lambda e: self.action_save())
        r.bind_all("<Control-S>", lambda e: self.action_save_as())
        r.bind_all("<Control-g>", lambda e: self.action_race_gen())
        r.bind_all("<Control-l>", lambda e: self.action_clear_console())
        r.bind_all("<Control-Shift-C>", lambda e: self._copy_console())
        r.bind_all("<F5>", lambda e: self.action_send_run())
        r.bind_all("<Control-F5>", lambda e: self.action_send_wait())
        r.bind_all("<Escape>", lambda e: self.action_stop())
        r.bind_all("<F1>", lambda e: self.action_help())

    # ---------------- محرر: أرقام الأسطر وتلوين الصيغة ----------------
    def _editor_yview(self, first, last):
        # مزامنة أرقام الأسطر مع التمرير
        self.ln.yview_moveto(first)
        self.sb_ed.set(first, last)

    def _editor_scroll(self, *args):
        self.editor.yview(*args)
        self.ln.yview(*args)

    def _update_lineno(self) -> None:
        try:
            count = int(self.editor.index("end-1c").split(".")[0])
        except Exception:
            return
        self.ln.configure(state="normal")
        self.ln.delete("1.0", "end")
        self.ln.insert("1.0", "\n".join(str(i) for i in range(1, count + 1)))
        self.ln.configure(state="disabled")

    def _on_editor_modified(self, _event=None) -> None:
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            self._update_lineno()
            if self._hl_job:
                self.root.after_cancel(self._hl_job)
            self._hl_job = self.root.after_idle(self._rehighlight)

    KW = ["from", "import", "def", "class", "return", "if", "elif", "else", "for",
          "while", "try", "except", "finally", "with", "as", "in", "not", "and",
          "or", "True", "False", "None", "break", "continue", "pass", "lambda",
          "global", "nonlocal", "del", "raise", "yield", "assert", "is", "await",
          "async"]

    def _rehighlight(self) -> None:
        self._hl_job = None
        t = self.editor
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
        """الكونسول قراءة فقط: نسمح بالتنقل والتحديد والنسخ فقط"""
        ctrl = bool(event.state & 0x4)
        if ctrl and event.keysym.lower() in ("c", "a"):
            return None  # نسخ / تحديد الكل
        if ctrl:
            return "break"  # منع لصق/قص وغيرها
        if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End",
                            "Prior", "Next", "Shift_L", "Shift_R",
                            "Control_L", "Control_R", "Escape"):
            return None
        return "break"  # منع أي كتابة أو حذف

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
        m.add_command(label="مسح الكونسول", command=self.action_clear_console)
        m.add_command(label="تصدير السجل", command=self.action_export_log)
        m.tk_popup(event.x_root, event.y_root)

    def _editor_menu(self, event) -> None:
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="قص", accelerator="Ctrl+X",
                      command=lambda: self.editor.event_generate("<<Cut>>"))
        m.add_command(label="نسخ", accelerator="Ctrl+C",
                      command=lambda: self.editor.event_generate("<<Copy>>"))
        m.add_command(label="لصق", accelerator="Ctrl+V",
                      command=lambda: self.editor.event_generate("<<Paste>>"))
        m.add_separator()
        self._popup_copy_items(m, self.editor)
        m.tk_popup(event.x_root, event.y_root)

    # ---------------- الكونسول ----------------
    def console(self, text: str, tag: str = "out") -> None:
        w = self.console_w
        if self._ts_var().get():
            w.insert("end", ts() + " ", "ts")
        w.insert("end", text.rstrip("\n") + "\n", tag)
        w.see("end")
        self.rx_count += 1
        self.lbl_rx.configure(text=f"{self.rx_count} سطر")
        if self._al_var().get():
            try:
                if self.session_file is None:
                    self.session_file = open(
                        os.path.join(LOG_DIR, "session_" +
                                     time.strftime("%Y%m%d_%H%M%S") + ".txt"),
                        "a", encoding="utf-8")
                self.session_file.write(f"{ts()} {text.rstrip()}\n")
                self.session_file.flush()
            except Exception:
                pass

    # ---------------- حلقة الرسائل من خيط BLE ----------------
    def _poll(self) -> None:
        try:
            while True:
                msg = self.queue.get_nowait()
                self._handle_msg(msg)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _handle_msg(self, msg) -> None:
        kind = msg[0]
        if kind == "console":
            _, text, tag = msg
            self.console(text, tag)
        elif kind == "hub_state":
            if msg[1] == "disconnected" and self.connected:
                self.connected = False
                self.hub = None
                self.capture = None
                self.console("انقطع الاتصال بالروبوت!", "err")
                self.lbl_state.configure(text="● غير متصل", foreground=self.ERR)
                self.lbl_prog.configure(text="")
                self._refresh_buttons()
        elif kind == "hub_status":
            flags = msg[1]
            parts = []
            parts.append("● البرنامج يعمل" if flags & FLAG_USER_RUN else "○ البرنامج متوقف")
            if flags & FLAG_BATT_LOW:
                parts.append("⚠ البطارية منخفضة!")
            self.lbl_prog.configure(text="   ".join(parts))
        elif kind == "serial_state":
            self.serial_running = bool(msg[1])
            self.btn_serial.configure(
                text="بث السلك ■" if self.serial_running else "بث السلك ▶")
        elif kind in ("op_done", "op_error"):
            _, name, res, err = msg
            self.busy = False
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
            if name == "disconnect":
                if self.capture:
                    self.capture.close()
                self.hub = None
                self.connected = False
                self.capture = None
                self.lbl_state.configure(text="● غير متصل", foreground=self.ERR)
                self.lbl_prog.configure(text="")
            if err is not None:
                self.console(f"خطأ في «{name}»: {type(err).__name__}: {err}", "err")
                if self.connected:
                    self.lbl_state.configure(text=f"● متصل (حدث خطأ في {name})",
                                             foreground=self.WARN)
            self._refresh_buttons()

    # ---------------- عمليات BLE (تعمل في خيط المنطق) ----------------
    def _say(self, text: str, tag: str = "info") -> None:
        self.queue.put(("console", text, tag))

    def _hub_or_raise(self):
        if self.hub is None:
            raise RuntimeError("غير متصل بالروبوت — اضغط «اتصال» أولاً")
        return self.hub

    async def op_connect(self):
        if not PB_OK:
            raise RuntimeError(f"pybricksdev غير متوفر: {PB_ERR}")
        scan = int(self.cfg.get("scan_timeout", 20))
        retries = int(self.cfg.get("retries", 6))
        for attempt in range(1, retries + 1):
            self._say(f"المحاولة {attempt}/{retries} — جاري البحث عن الروبوت...")
            try:
                dev = await find_device(None, timeout=scan)
            except asyncio.TimeoutError:
                self._say("لم يُعثر على الروبوت — تأكد أنه مضاء ويرمش أزرق", "err")
                continue
            hub = PybricksHubBLE(dev)
            try:
                await hub.connect()
            except Exception as e:
                self._say(f"فشل الاتصال: {type(e).__name__}: {e}", "err")
                try:
                    await hub.disconnect()
                except Exception:
                    pass
                continue
            self._say(f"تم الاتصال بـ {dev.name} — الفرموير {hub.fw_version}", "ok")
            hub._app_label = str(dev.name)
            self._install_capture(hub)
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
        except Exception:
            pass
        self._say("كابل USB مكتشف — الواجهات: " +
                  (", ".join(hex(c) for c in classes) if classes else "؟"), "info")

        if 0xFF not in classes:
            self._say("فيرومور SPIKE Prime الحالي يعرض على السلك منفذ COM فقط (CDC) —", "err")
            self._say("لا يدعم رفع/تشغيل البرامج بالسلك. الرفع يبقى عبر البلوتوث.", "info")
            self._say("الدعم التجريبي للسلك يتطلب بناء فيرموير مخصصاً من labs.pybricks.com (غير موصى به قبل المسابقة).", "info")
            raise RuntimeError("الكابل يعمل للشحن والاستقبال فقط على فيرمويرك الحالي")

        # واجهة Pybricks موجودة ← مسار كامل ممكن
        hub = PybricksHubUSB(dev)
        await hub.connect()
        hub._app_label = "USB سلك"
        self._say(f"اتصال سلك ناجح! {getattr(hub, '_hub_name', '')} — الفرموير {hub.fw_version}", "ok")
        self._install_capture(hub)
        return hub

    def _install_capture(self, hub) -> None:
        cap = HubCapture(self.queue)
        hub._line_handler = cap.handler
        hub.print_output = False
        hub._enable_line_handler = True

        def on_state(state):
            try:
                if state == ConnectionState.DISCONNECTED:
                    self.queue.put(("hub_state", "disconnected"))
            except Exception:
                pass

        def on_status(flags):
            try:
                self.queue.put(("hub_status", int(flags)))
            except Exception:
                pass

        try:
            cap.subs.append(hub.connection_state_observable.subscribe(on_state))
            cap.subs.append(hub.status_observable.subscribe(on_status))
        except Exception:
            pass
        self.capture = cap

    async def _quiet_stop(self, hub) -> None:
        try:
            await hub.stop_user_program()
            await asyncio.sleep(0.5)
        except Exception as e:
            self._say(f"(إيقاف البرنامج السابق تم تخطيه: {type(e).__name__})", "info")

    async def op_send_run(self, path, wait):
        hub = self._hub_or_raise()
        await self._quiet_stop(hub)
        self._say(f"جاري الترجمة والرفع: {os.path.basename(path)} ...")
        await hub.run(path, wait=wait, print_output=False, line_handler=True)
        if wait:
            self._say("انتهى تنفيذ البرنامج ✔", "ok")
        else:
            self._say("تم الرفع والتشغيل ✔ — شغّل الروبوت عند نقطة البداية", "ok")

    async def op_download_only(self, path):
        hub = self._hub_or_raise()
        await self._quiet_stop(hub)
        self._say(f"جاري التحميل بدون تشغيل: {os.path.basename(path)} ...")
        await hub.download(path)
        self._say("تم التحميل ✔ — شغّله لاحقاً بزر «تشغيل المخزن»", "ok")

    async def op_run_stored(self):
        hub = self._hub_or_raise()
        await self._quiet_stop(hub)
        self._say("جاري تشغيل البرنامج المخزن ...")
        await hub.run(None, wait=False, print_output=False, line_handler=True)
        self._say("تم تشغيل البرنامج المخزن ✔", "ok")

    async def op_stop(self):
        hub = self._hub_or_raise()
        await hub.stop_user_program()
        self._say("تم إيقاف البرنامج ✔", "ok")

    async def op_disconnect(self):
        hub = self.hub
        if hub is None:
            return
        try:
            await hub.disconnect()
        except Exception as e:
            self._say(f"(قطع الاتصال: {type(e).__name__})", "info")
        self._say("تم قطع الاتصال", "info")

    # ---------------- إرسال الأوامر من الواجهة ----------------
    def _start_op(self, name, coro, allow_busy=False):
        if not PB_OK:
            self.console(f"غير جاهز: {PB_ERR}", "err")
            return
        if self.busy and not allow_busy:
            self.console("أكمل العملية الحالية أولاً (أو اضغط Esc للإيقاف)", "info")
            return
        if not self.busy:
            self.busy = True
            self._refresh_buttons()
        fut = asyncio.run_coroutine_threadsafe(coro, _WORKER.loop)

        def _done(f):
            try:
                res = f.result()
                self.queue.put(("op_done", name, res, None))
            except Exception as e:
                self.queue.put(("op_error", name, None, e))

        fut.add_done_callback(_done)

    def _refresh_buttons(self) -> None:
        c = self.connected and self.hub is not None
        b = self.busy
        self.btn_connect.configure(state="disabled" if (c or b) else "normal")
        self.btn_disconnect.configure(state="normal" if c else "disabled")
        self.btn_usb.configure(state="disabled" if (c or b) else "normal")
        st_send = "normal" if (c and not b) else "disabled"
        self.btn_send.configure(state=st_send)
        self.btn_sendwait.configure(state=st_send)
        self.btn_dl.configure(state=st_send)
        self.btn_stored.configure(state=st_send)
        self.btn_stop.configure(state="normal" if c else "disabled")

    # ---------------- أفعال الواجهة ----------------
    def action_connect(self):
        self.console("جاري الاتصال بالروبوت...", "info")
        self._start_op("connect", self.op_connect())

    def action_disconnect(self):
        self._start_op("disconnect", self.op_disconnect(), allow_busy=True)

    def action_usb_connect(self):
        if self.connected and self.hub is not None:
            self.console("اقطع الاتصال الحالي أولاً", "info")
            return
        self.console("جاري فحص كابل USB والاتصال بالسلك...", "info")
        self._start_op("usb_connect", self.op_connect_usb())

    # ---------------- بث السلك (استقبال من COM) ----------------
    def action_serial_toggle(self):
        if self.serial_running:
            if self.serial_stop:
                self.serial_stop.set()
            self.console("إيقاف بث السلك...", "info")
            return
        port = self._find_hub_port()
        if not port:
            self.console("لا يوجد منفذ سلك للروبوت — وصّل كابل USB أولاً", "err")
            return
        self.serial_stop = threading.Event()
        t = threading.Thread(target=self._serial_reader, args=(port,),
                             name="serial-reader", daemon=True)
        t.start()
        self.console(f"فتح {port} — بانتظار بيانات الروبوت عبر السلك...", "info")

    def _find_hub_port(self):
        try:
            from serial.tools import list_ports
            for p in list_ports.comports():
                if p.vid == 0x0694 and p.pid == 0x0009:
                    return p.device
        except Exception:
            pass
        return None

    def _serial_reader(self, port: str) -> None:
        try:
            import serial as ser
            s = ser.Serial(port, 115200, timeout=0.5)
        except Exception as e:
            self.queue.put(("console", f"تعذر فتح {port}: {e}", "err"))
            self.queue.put(("serial_state", False))
            return
        buf = b""
        got_any = False
        try:
            while not self.serial_stop.is_set():
                data = s.read(256)
                if data:
                    got_any = True
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("utf-8", "replace").rstrip("\r")
                        if text:
                            self.queue.put(("console", "[سلك] " + text, "out"))
        finally:
            try:
                s.close()
            except Exception:
                pass
            if not got_any:
                self.queue.put(("console",
                                "[سلك] أُغلق المنفذ — لم تصل بيانات (قد لا يبث الفرموير على السلك)",
                                "info"))
            self.queue.put(("serial_state", False))

    def _require_saved_file(self) -> str | None:
        if not self.current_path:
            self.console("احفظ الملف أولاً (Ctrl+S)", "info")
            self.action_save()
        if self.current_path and not self.editor.edit_modified():
            return self.current_path
        if self.current_path:
            self.action_save()
        return self.current_path

    def action_send_run(self):
        path = self._require_saved_file()
        if not path:
            return
        self.console(f"إرسال وتشغيل: {os.path.basename(path)}", "info")
        self._start_op("send_run", self.op_send_run(path, wait=False))

    def action_send_wait(self):
        path = self._require_saved_file()
        if not path:
            return
        self.console(f"إرسال مع الانتظار: {os.path.basename(path)}", "info")
        self._start_op("send_wait", self.op_send_run(path, wait=True))

    def action_download_only(self):
        path = self._require_saved_file()
        if not path:
            return
        self._start_op("download", self.op_download_only(path))

    def action_run_stored(self):
        self._start_op("run_stored", self.op_run_stored())

    def action_stop(self):
        if self.connected and self.hub is not None:
            self._start_op("stop", self.op_stop(), allow_busy=True)

    # ---------------- الملفات ----------------
    def _editor_dirty(self) -> bool:
        return bool(self.editor.get("1.0", "end-1c").strip()) and self.editor.edit_modified()

    def _confirm_discard(self) -> bool:
        if not self._editor_dirty():
            return True
        return messagebox.askyesno("تعديلات غير محفوظة",
                                   "هناك تعديلات غير محفوظة. هل تريد المتابعة دون حفظ؟")

    def action_new(self):
        if not self._confirm_discard():
            return
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", STARTER_TEXT)
        self.editor.edit_reset()
        self.current_path = None
        self._set_file_label()

    def action_open(self):
        if not self._confirm_discard():
            return
        p = filedialog.askopenfilename(
            title="فتح ملف بايثون",
            filetypes=[("ملفات بايثون", "*.py"), ("الكل", "*.*")],
            initialdir=os.path.dirname(self.current_path or APP_DIR))
        if not p:
            return
        self._open_path(p)

    def _open_path(self, p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            messagebox.showerror("خطأ", f"تعذر فتح الملف:\n{e}")
            return
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.editor.edit_reset()
        self.current_path = p
        self._add_recent(p)
        self._set_file_label()
        self.console(f"فُتح: {p}", "ok")

    def action_save(self):
        if not self.current_path:
            self.action_save_as()
            return
        self._save_to(self.current_path)

    def action_save_as(self):
        p = filedialog.asksaveasfilename(
            title="حفظ باسم", defaultextension=".py",
            filetypes=[("ملفات بايثون", "*.py")],
            initialdir=os.path.dirname(self.current_path or APP_DIR),
            initialfile=os.path.basename(self.current_path or "program.py"))
        if not p:
            return
        self._save_to(p)

    def _save_to(self, p):
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.editor.get("1.0", "end-1c"))
        except Exception as e:
            messagebox.showerror("خطأ", f"تعذر الحفظ:\n{e}")
            return
        self.editor.edit_modified(False)
        self.current_path = p
        self._add_recent(p)
        self._set_file_label()
        self.console(f"حُفظ: {p}", "ok")

    def _set_file_label(self):
        name = os.path.basename(self.current_path) if self.current_path else "ملف جديد"
        self.lbl_file.configure(text=name)

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
        self.console_w.delete("1.0", "end")

    def action_export_log(self):
        p = filedialog.asksaveasfilename(
            title="تصدير سجل المخرجات", defaultextension=".txt",
            filetypes=[("نص", "*.txt"), ("الكل", "*.*")],
            initialdir=LOG_DIR,
            initialfile="hub_log_" + time.strftime("%Y%m%d_%H%M%S") + ".txt")
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.console_w.get("1.0", "end-1c"))
            self.console(f"صُدّر السجل: {p}", "ok")
        except Exception as e:
            messagebox.showerror("خطأ", f"تعذر التصدير:\n{e}")

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
        t.insert("1.0", HELP_TEXT)
        t.configure(state="disabled")

    def _insert_at_cursor(self, text):
        self.editor.insert("insert", text)
        self.editor.focus_set()

    # ---------------- الإغلاق ----------------
    def _on_close(self):
        if self._editor_dirty():
            if not messagebox.askyesno("خروج", "تعديلات غير محفوظة — الخروج بدون حفظ؟"):
                return
        self._save_config()
        if self.serial_stop:
            self.serial_stop.set()
        if self.session_file:
            try:
                self.session_file.close()
            except Exception:
                pass
        if self.connected and self.hub is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.op_disconnect(), _WORKER.loop)
                time.sleep(1.2)
            except Exception:
                pass
        try:
            _WORKER.loop.call_soon_threadsafe(_WORKER.loop.stop)
        except Exception:
            pass
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

        bar = ttk.Frame(self.win, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="حفظ كملف...", command=self.save).pack(side="left", padx=4)
        ttk.Button(bar, text="نسخ للمحرر", command=self.to_editor).pack(side="left", padx=4)
        ttk.Button(bar, text="تحديث من الكونسول", command=self.refresh).pack(side="left", padx=4)
        ttk.Button(bar, text="إغلاق", command=self.win.destroy).pack(side="right", padx=4)

        self.refresh()

    def refresh(self):
        console_text = self.app.console_w.get("1.0", "end-1c")
        kept = []
        for line in console_text.splitlines():
            # إزالة الطابع الزمني إن وُجد (مثل 10:22:33)
            s = re.sub(r"^\d{2}:\d{2}:\d{2}\s+", "", line.strip())
            # أوامر الحركة المسجّلة
            if re.match(r"^robot\.(straight|turn|stop|straight_time|drive)\b", s):
                kept.append(s)
            # تعليقات قراءات الحساسات
            elif s.startswith("#") and ("sensor" in s or "حساس" in s):
                kept.append(s)
        if not kept:
            kept = ["# (لا توجد بيانات تدريب في الكونسول — درّب الروبوت أولاً)"]

        head = SETUP_TEMPLATE.split("# --- بداية المسار ---")[0] + \
            "# --- بداية المسار ---\nhub.light.on(Color.GREEN)\n\n"
        tail = "\n# --- نهاية المسار ---\nrobot.stop()\n" \
               "hub.light.on(Color.BLUE)\nhub.speaker.beep(900, 200)\n"
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", head + "\n".join(kept) + "\n" + tail)
        self.app.console(f"توليد سباق: {len(kept)} سطر مسجّل", "ok")

    def save(self):
        p = filedialog.asksaveasfilename(
            title="حفظ برنامج السباق", defaultextension=".py",
            filetypes=[("ملفات بايثون", "*.py")],
            initialdir=os.path.join(os.path.dirname(self.app.current_path or APP_DIR)),
            initialfile="race_" + time.strftime("%H%M%S") + ".py")
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.preview.get("1.0", "end-1c"))
            self.app.console(f"حُفظ برنامج السباق: {p}", "ok")
            self.app._add_recent(p)
        except Exception as e:
            messagebox.showerror("خطأ", f"تعذر الحفظ:\n{e}")

    def to_editor(self):
        content = self.preview.get("1.0", "end-1c")
        self.app.editor.delete("1.0", "end")
        self.app.editor.insert("1.0", content)
        self.app.editor.edit_modified(True)
        self.win.destroy()
        self.app.editor.focus_set()


# ---------------------------------------------------------------------------
# خيط BLE (حلقة أحداث مستقلة)
# ---------------------------------------------------------------------------
class Worker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()

    def start(self):
        t = threading.Thread(target=self._run, name="ble-worker", daemon=True)
        t.start()
        while not self.loop.is_running():
            time.sleep(0.01)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()


_WORKER = Worker()


# ---------------------------------------------------------------------------
# نقطة البداية
# ---------------------------------------------------------------------------
def main():
    if PB_OK:
        _WORKER.start()
    root = tk.Tk()
    App(root)
    root.mainloop()


def selftest():
    """اختبار بناء الواجهة بدون تشغيل طويل"""
    root = tk.Tk()
    app = App(root)
    root.update()
    app.console("سطر تجريبي 1", "info")
    app.console("robot.straight(100)", "out")
    app.console("تم ✔", "ok")
    root.update()
    root.destroy()
    print("SELFTEST OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
