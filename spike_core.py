"""GUI-independent validation and persistence helpers for Hyena Control."""
from __future__ import annotations

import ast
import copy
import math
import os
import queue
import tempfile

MAX_LINES = 20000
MAX_LINE_BYTES = 65536
MAX_HUB_LOG_BYTES = 10 * 1024 * 1024


def atomic_write(path: str, text: str) -> None:
    """Replace only after a complete, flushed write in the destination directory."""
    path = os.path.abspath(path)
    fd, temporary = tempfile.mkstemp(prefix=".hyena-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validated_config(data, defaults: dict) -> dict:
    cfg = copy.deepcopy(defaults)
    if not isinstance(data, dict):
        return cfg
    geometry = data.get("geometry")
    if isinstance(geometry, str):
        import re
        if re.fullmatch(r"\d{2,5}x\d{2,5}(?:[+-]\d{1,6}){0,2}", geometry):
            cfg["geometry"] = geometry
    for key, lower, upper in (("sash", 100, 5000), ("scan_timeout", 1, 120), ("retries", 1, 10)):
        value = data.get(key)
        if type(value) is int and lower <= value <= upper:
            cfg[key] = value
    for key in ("timestamps", "autolog", "autoconnect"):
        if type(data.get(key)) is bool:
            cfg[key] = data[key]
    name = data.get("hub_name")
    if isinstance(name, str) and len(name) <= 128 and all(ord(c) >= 32 for c in name):
        cfg["hub_name"] = name.strip()
    recent = data.get("recent")
    if isinstance(recent, list):
        cfg["recent"] = list(dict.fromkeys(x for x in recent if isinstance(x, str) and x and "\0" not in x))[:6]
    return cfg


def training_line(text: str) -> str | None:
    """Accept one literal DriveBase call, never executable expressions.

    Limits are defensive input bounds, NOT a guarantee of physical safety.
    straight: millimetres; turn: degrees; drive: mm/s and degrees/s.
    Other methods/keywords must be reviewed manually in the editor.
    """
    s = text.strip()
    if "\n" in s or "\r" in s or "\0" in s:
        raise ValueError("يسمح بسطر تدريب واحد دون محارف تحكم")
    if s.startswith("#") and ("sensor" in s.lower() or "حساس" in s):
        return s
    if not s.startswith("robot."):
        return None
    if len(s.encode("utf-8")) > MAX_LINE_BYTES:
        raise ValueError("سطر حركة طويل جداً")
    try:
        tree = ast.parse(s, mode="exec")
    except (SyntaxError, ValueError) as exc:
        raise ValueError("صياغة أمر الحركة غير صالحة") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        raise ValueError("يسمح بأمر حركة واحد فقط في السطر")
    call = tree.body[0].value
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name) and call.func.value.id == "robot"):
        raise ValueError("أمر حركة غير مسموح")
    bounds = {"straight": (10000,), "turn": (3600,), "stop": (), "drive": (1000, 1000)}
    limits = bounds.get(call.func.attr)
    if limits is None or call.keywords or len(call.args) != len(limits):
        raise ValueError("دالة أو معاملات حركة غير مدعومة")
    values = []
    for node, limit in zip(call.args, limits):
        sign = 1
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            sign = -1 if isinstance(node.op, ast.USub) else 1
            node = node.operand
        if not isinstance(node, ast.Constant) or type(node.value) not in (int, float):
            raise ValueError("معاملات الحركة يجب أن تكون أرقاماً ثابتة")
        value = sign * node.value
        if abs(value) > limit or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError("قيمة الحركة تتجاوز الحدود المسموحة")
        values.append(repr(value))
    # Reconstruct from validated primitives; discard comments and formatting.
    return f"robot.{call.func.attr}({', '.join(values)})"


class EventQueue(queue.Queue):
    """Bound console backlog without ever dropping control/completion messages.

    Loss is explicit: the UI invalidates the training session rather than
    silently generating an incomplete race. Producers never block shutdown.
    """
    def __init__(self, limit=2000):
        super().__init__()
        self.limit = limit
        self.console_pending = 0
        self.dropped = 0
        self.revision = 0

    @staticmethod
    def _training_event(item):
        return (item[0] == "data_loss"
                or (item[0] == "console" and len(item) > 2 and item[2] == "out")
                or (item[0] == "hub_state" and item[1] == "disconnected"
                    and not (len(item) > 3 and item[3])))

    def training_status(self):
        """Atomic input revision + pending/loss flags for preview validation."""
        with self.mutex:
            return self.revision, any(self._training_event(item) for item in self.queue), bool(self.dropped)

    def put(self, item, block=True, timeout=None):
        with self.not_full:
            if self._training_event(item):
                self.revision += 1
            is_console = item[0] == "console"
            if is_console and self.console_pending >= self.limit:
                self.dropped += 1
                return
            if is_console:
                self.console_pending += 1
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def _get(self):
        item = super()._get()
        if item[0] == "console":
            self.console_pending -= 1
        return item

    def take_dropped(self):
        with self.mutex:
            count, self.dropped = self.dropped, 0
            return count

    def discard_console(self):
        """Start a session at an atomic queue boundary; retain control messages."""
        with self.mutex:
            retained = [item for item in self.queue if item[0] not in ("console", "data_loss")]
            removed = len(self.queue) - len(retained)
            self.queue.clear()
            self.queue.extend(retained)
            self.console_pending = 0
            self.dropped = 0
            self.unfinished_tasks -= removed
            if self.unfinished_tasks == 0:
                self.all_tasks_done.notify_all()
            self.not_full.notify_all()
