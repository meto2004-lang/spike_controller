"""Fail clearly (including Checks API annotations) if the native Qt UI cannot import."""
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from PySide6.QtWidgets import QApplication  # noqa: F401
    import spike_qt  # noqa: F401
except Exception:
    detail = traceback.format_exc()
    print('::error::' + detail.replace('%', '%25').replace('\n', '%0A').replace('\r', '%0D'))
    raise
print('Qt interface imports OK')
