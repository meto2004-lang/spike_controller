"""Training-session bookkeeping and race program generation (GUI-free).

The browser app streams hub output through this class exactly like the desktop
app did: a session starts clean, every console line is kept, and a race program
may only be generated from a session that received all of its data.
"""
from __future__ import annotations

import threading

from spike_core import training_line
from spike_templates import RACE_PROGRAM_HEAD, RACE_PROGRAM_TAIL

MAX_SESSION_LINES = 20000


class TrainingError(RuntimeError):
    """Translated in the UI; the code keeps an English machine message."""


class TrainingSession:
    def __init__(self, max_lines: int = MAX_SESSION_LINES):
        self._lock = threading.RLock()
        self.max_lines = max_lines
        self.lines: list[str] = []
        self.revision = 0
        self.lost = False
        self.active_since = None

    # -- input ---------------------------------------------------------
    def add_line(self, text: str, *, raw: bool = True) -> None:
        """Store one hub console line.

        `raw=False` is used for lines the application itself injected (for
        example simulation data), which never mark the session as damaged.
        """
        with self._lock:
            if len(self.lines) >= self.max_lines:
                self.lost = True
                return
            self.lines.append(text)
            self.revision += 1
            if self.active_since is None:
                self.active_since = True

    def mark_lost(self, reason: str = "") -> None:
        with self._lock:
            self.lost = True
            self.revision += 1
            if reason:
                self.lines.append(f"[data loss] {reason}")

    def reset(self) -> None:
        with self._lock:
            self.lines.clear()
            self.revision += 1
            self.lost = False
            self.active_since = None

    # -- output --------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "lines": len(self.lines),
                "revision": self.revision,
                "lost": self.lost,
                "available": bool(self.lines) and not self.lost,
            }

    def generate(self) -> dict:
        """Validate the session and build the race program.

        Returns `{code, kept, skipped, revision, total}`. Raises TrainingError
        with an English message and a stable `reason` code for the UI.
        """
        with self._lock:
            revision = self.revision
            lost = self.lost
            lines = list(self.lines)

        if lost:
            raise TrainingError("incomplete session: start a new session and "
                                "record the training again")
        if not lines:
            raise TrainingError("no training data in the current session")

        kept: list[str] = []
        errors: list[str] = []
        skipped = 0
        for number, line in enumerate(lines, 1):
            try:
                command = training_line(line)
            except ValueError as exc:
                errors.append(f"line {number}: {exc}")
                continue
            if command is None:
                skipped += 1
            else:
                kept.append(command)

        if errors:
            raise TrainingError("; ".join(errors[:10]))
        if not kept:
            raise TrainingError("no robot.straight/turn/drive/stop commands were recorded")

        code = RACE_PROGRAM_HEAD + "\n" + "\n".join(kept) + "\n" + RACE_PROGRAM_TAIL
        with self._lock:
            if self.revision != revision or self.lost:
                raise TrainingError("training data changed; generate again")
        return {"code": code, "kept": len(kept), "skipped": skipped,
                "revision": revision, "total": len(lines)}
