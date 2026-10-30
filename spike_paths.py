"""Shared locations for the desktop app and the browser app.

Both front-ends use the same user data directory so settings, logs and saved
programs stay in one predictable place, and neither of them needs write access
to the installation folder.
"""
from __future__ import annotations

import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.environ.get("HYENA_CONTROL_DATA_DIR") or os.path.join(
    os.environ.get("LOCALAPPDATA")
    or os.environ.get("XDG_STATE_HOME")
    or os.path.join(os.path.expanduser("~"), ".local", "state"),
    "HyenaControl",
)

CONFIG_FILE = os.path.join(DATA_DIR, "spike_controller_config.json")
WEB_CONFIG_FILE = os.path.join(DATA_DIR, "spike_web_config.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")
WORKSPACE_DIR = os.environ.get("HYENA_WORKSPACE_DIR") or os.path.join(DATA_DIR, "programs")
WEB_DIR = os.path.join(APP_DIR, "web")


def ensure_dirs() -> None:
    for path in (DATA_DIR, LOG_DIR, WORKSPACE_DIR):
        os.makedirs(path, exist_ok=True)
