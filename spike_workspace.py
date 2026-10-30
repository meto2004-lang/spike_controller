"""Program workspace for the browser app.

The page never touches the file system directly: it lists, opens, saves,
renames and deletes programs through these helpers, which keep every path
inside the workspace directory and reuse the desktop app's atomic writer.
"""
from __future__ import annotations

import os
import time

from spike_core import atomic_write

MAX_FILE_BYTES = 2 * 1024 * 1024
ALLOWED_SUFFIXES = (".py", ".txt", ".json", ".csv", ".md", ".blocks")


class WorkspaceError(RuntimeError):
    pass


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or "\0" in name:
        raise WorkspaceError("invalid file name")
    # A program name is a bare file name: refuse paths instead of flattening
    # them, so a request can never look like it escaped the workspace.
    if "/" in name or "\\" in name:
        raise WorkspaceError("invalid file name")
    if os.path.splitext(name)[1].lower() not in ALLOWED_SUFFIXES:
        raise WorkspaceError("only .py, .txt, .json, .csv, .md and .blocks files are allowed")
    return name


class Workspace:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    # -- paths ---------------------------------------------------------
    def path(self, name: str) -> str:
        """Resolve an existing file name, refusing anything outside the root."""
        resolved = os.path.realpath(os.path.join(self.root, name or ""))
        root = os.path.realpath(self.root)
        if resolved != root and not resolved.startswith(root + os.sep):
            raise WorkspaceError("path escapes the workspace")
        return resolved

    # -- queries -------------------------------------------------------
    def list(self) -> list[dict]:
        entries = []
        for entry in sorted(os.scandir(self.root), key=lambda e: e.name.lower()):
            if not entry.is_file():
                continue
            if os.path.splitext(entry.name)[1].lower() not in ALLOWED_SUFFIXES:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            entries.append({
                "name": entry.name,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
            })
        entries.sort(key=lambda item: item["mtime"], reverse=True)
        return entries

    def read(self, name: str) -> dict:
        path = self.path(_safe_name(name))
        if not os.path.isfile(path):
            raise WorkspaceError("file not found")
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise WorkspaceError("file is too large to open")
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as stream:
            content = stream.read()
        return {"name": os.path.basename(path), "path": path, "content": content,
                "mtime": int(os.path.getmtime(path))}

    # -- mutations -----------------------------------------------------
    def write(self, name: str, content: str) -> dict:
        name = _safe_name(name)
        if not isinstance(content, str):
            raise WorkspaceError("content must be text")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise WorkspaceError("file is too large to save")
        path = os.path.join(self.root, name)
        atomic_write(path, content)
        return {"name": name, "path": path, "mtime": int(os.path.getmtime(path))}

    def rename(self, old: str, new: str) -> dict:
        source = self.path(_safe_name(old))
        if not os.path.isfile(source):
            raise WorkspaceError("file not found")
        target = os.path.join(self.root, _safe_name(new))
        if os.path.exists(target):
            raise WorkspaceError("a file with that name already exists")
        os.replace(source, target)
        return {"name": os.path.basename(target), "path": target}

    def delete(self, name: str) -> dict:
        path = self.path(_safe_name(name))
        if not os.path.isfile(path):
            raise WorkspaceError("file not found")
        os.unlink(path)
        return {"name": os.path.basename(path)}
