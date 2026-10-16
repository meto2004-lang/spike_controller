"""Per-tab document state; importing this module does not require Tkinter."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any


def canonical_path(path: str) -> str:
    """Match relative/absolute paths, symlinks and Windows case aliases."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


@dataclass(eq=False)
class EditorDocument:
    frame: Any
    editor: Any
    gutter: Any
    scrollbar: Any
    path: str | None = None
    saved_text: str = ""
    number: int = 1
    suggested_name: str | None = None
    highlight_job: Any = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path) if self.path else (
            self.suggested_name or f"بدون اسم {self.number}")

    @property
    def dirty(self) -> bool:
        return self.editor.get("1.0", "end-1c") != self.saved_text


def tab_names(documents: list[EditorDocument]) -> dict[EditorDocument, str]:
    """Disambiguate equal basenames without putting full paths on every tab."""
    names = {}
    for document in documents:
        duplicates = [other for other in documents
                      if os.path.normcase(other.name) == os.path.normcase(document.name)]
        name = document.name
        if len(duplicates) > 1:
            # Full parent paths also distinguish directories with identical names.
            parent = os.path.dirname(os.path.abspath(document.path)) if document.path else f"#{document.number}"
            name = f"{name} [{parent}]"
        names[document] = ("* " if document.dirty else "") + name
    return names
