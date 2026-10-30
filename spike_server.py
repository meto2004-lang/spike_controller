#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hyena Control Web — a Pybricks Code style IDE served by a local Python process.

Why a local server instead of browser Bluetooth? A web page cannot drive the
Pybricks BLE protocol by itself, and Pybricks Code solves that with a Chromium
only Web Bluetooth stack. This app keeps `pybricksdev` in Python (the same code
the desktop app uses) and talks to the page over HTTP + Server-Sent Events, so
it works in any browser while still flashing and running real programs.

Run:
    python spike_server.py            # http://127.0.0.1:8000
    python spike_server.py --simulate # no robot / no pybricksdev needed
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import spike_paths
from spike_hub import HubBusy, HubController, HubError
from spike_race import TrainingError, TrainingSession
from spike_templates import EXAMPLES, STARTER_PROGRAM
from spike_workspace import Workspace, WorkspaceError

VERSION = "2.1.0"
MAX_BODY_BYTES = 8 * 1024 * 1024
LOG_BUFFER = 4000

DEFAULT_SETTINGS = {
    "lang": "ar",
    "theme": "light",
    "scan_timeout": 20,
    "retries": 6,
    "autoconnect": False,
    "hub_name": "",
    "timestamps": True,
    "auto_scroll": True,
    "font_size": 14,
    "last_file": None,
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}


RANGES = {"scan_timeout": (1, 120), "retries": (1, 10), "font_size": (10, 28)}


def normalize_field(key: str, value):
    """Return (is_valid, normalized). Unknown or malformed values are dropped."""
    if key == "lang":
        return value in ("ar", "en"), value
    if key == "theme":
        return value in ("light", "dark"), value
    if key in RANGES:
        low, high = RANGES[key]
        if type(value) is int and low <= value <= high:
            return True, value
        return False, None
    if key in ("autoconnect", "timestamps", "auto_scroll"):
        return type(value) is bool, value
    if key == "hub_name":
        if isinstance(value, str) and len(value) <= 128 and all(ord(c) >= 32 for c in value):
            return True, value.strip()
        return False, None
    if key == "last_file":
        if value is None:
            return True, None
        if isinstance(value, str) and 0 < len(value) <= 255 and "\0" not in value:
            return True, value
        return False, None
    return False, None


def validated_settings(raw) -> dict:
    """Start from the defaults and accept only well-formed known fields."""
    cfg = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return cfg
    for key, value in raw.items():
        if key not in DEFAULT_SETTINGS:
            continue
        ok, clean = normalize_field(key, value)
        if ok:
            cfg[key] = clean
    return cfg


class WebApp:
    """Server state: settings, log buffer, SSE subscribers and the hub."""

    def __init__(self, *, simulate: bool = False, workspace: str | None = None,
                 data_dir: str | None = None):
        self.data_dir = os.path.abspath(data_dir or spike_paths.DATA_DIR)
        self.log_dir = os.path.join(self.data_dir, "logs")
        self.config_file = os.path.join(self.data_dir, "spike_web_config.json")
        os.makedirs(self.log_dir, exist_ok=True)
        self.workspace = Workspace(workspace or os.path.join(self.data_dir, "programs"))
        self.settings = self._load_settings()
        self._lock = threading.RLock()
        self._subscribers: set[queue.Queue] = set()
        self._logs: list[dict] = []
        self._seq = 0
        self._closing = False
        self.training = TrainingSession()
        self.hub = HubController(self.publish, self.training,
                                 force_simulation=simulate,
                                 data_dir=self.data_dir)
        self.hub.start()

    # -- settings ------------------------------------------------------
    def _load_settings(self) -> dict:
        try:
            with open(self.config_file, "r", encoding="utf-8") as stream:
                return validated_settings(json.load(stream))
        except (OSError, ValueError):
            return dict(DEFAULT_SETTINGS)

    def save_settings(self, changes: dict) -> dict:
        """Apply valid fields only; one bad value must not reset the rest."""
        if not isinstance(changes, dict):
            return self.settings
        merged = dict(self.settings)
        for key, value in changes.items():
            if key not in DEFAULT_SETTINGS:
                continue
            ok, clean = normalize_field(key, value)
            if ok:
                merged[key] = clean
        self.settings = validated_settings(merged)
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as stream:
                json.dump(self.settings, stream, ensure_ascii=False, indent=2)
        except OSError:
            traceback.print_exc()
        return self.settings

    # -- event bus -----------------------------------------------------
    def publish(self, event: dict) -> None:
        if event.get("t") == "console":
            with self._lock:
                self._seq += 1
                event = {**event, "seq": self._seq}
                self._logs.append(event)
                if len(self._logs) > LOG_BUFFER:
                    del self._logs[: len(self._logs) - LOG_BUFFER]
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subscribers.add(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    def logs(self, since: int = 0) -> list[dict]:
        with self._lock:
            return [entry for entry in self._logs if entry.get("seq", 0) > since]

    def log_text(self) -> str:
        with self._lock:
            entries = list(self._logs)
        lines = []
        for entry in entries:
            stamp = f"[{entry['ts']}] " if entry.get("ts") else ""
            lines.append(f"{stamp}{entry.get('text', '')}")
        return "\n".join(lines) + ("\n" if lines else "")

    # -- lifecycle -----------------------------------------------------
    def shutdown(self) -> None:
        self._closing = True
        for channel in list(self._subscribers):
            try:
                channel.put_nowait({"t": "server", "closing": True})
            except queue.Full:
                pass
        self.hub.close()

    # -- snapshots -----------------------------------------------------
    def bootstrap(self) -> dict:
        state = self.hub.state()
        return {
            "version": VERSION,
            "simulation": self.hub.simulation,
            "pybricksdev": not self.hub.simulation,
            "transport": self.hub.diagnostics(),
            "settings": self.settings,
            "state": state,
            "workspace": {"dir": self.workspace.root, "files": self.workspace.list()},
            "starter": STARTER_PROGRAM,
            "examples": EXAMPLES,
            "logs": self.logs(0),
            "log_seq": self._seq,
            "paths": {"data": self.data_dir, "logs": self.log_dir},
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


class HyenaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, app: WebApp):
        super().__init__(address, handler)
        self.app = app


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"HyenaControl/{VERSION}"

    # -- plumbing ------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the console readable
        if self.path.startswith("/api/events"):
            return
        print(f"[web] {self.address_string()} {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str, *, kind: str = "error") -> None:
        self._json({"ok": False, "error": message, "kind": kind}, status)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _authorised(self) -> bool:
        """Block cross-site writes. A custom header cannot be sent by a form."""
        return self.headers.get("X-Hyena-Client") == "web" and \
            self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none", "")

    # -- routing -------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            return self._api_get(path, urllib.parse.parse_qs(parsed.query))
        return self._serve_static(path)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "HEAD is not supported")
        return self._serve_static(parsed.path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if not self._authorised():
            # Drain and close: an unread body would be parsed as a new request.
            self.close_connection = True
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if 0 < length <= MAX_BODY_BYTES:
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
            return self._error(HTTPStatus.FORBIDDEN, "missing client header")
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._api_post(parsed.path, body, urllib.parse.parse_qs(parsed.query))

    # -- static files --------------------------------------------------
    def _serve_static(self, path: str) -> None:
        app: WebApp = self.server.app
        if path in ("", "/"):
            path = "/index.html"
        relative = urllib.parse.unquote(path).lstrip("/")
        root = os.path.realpath(spike_paths.WEB_DIR)
        target = os.path.realpath(os.path.join(root, relative))
        if target != root and not target.startswith(root + os.sep):
            return self._error(HTTPStatus.FORBIDDEN, "path escapes the web root")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            return self._error(HTTPStatus.NOT_FOUND, f"not found: {relative}")
        try:
            with open(target, "rb") as stream:
                body = stream.read()
        except OSError as exc:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
        suffix = os.path.splitext(target)[1].lower()
        content_type = CONTENT_TYPES.get(suffix) or mimetypes.guess_type(target)[0] \
            or "application/octet-stream"
        self._send(HTTPStatus.OK, body, content_type)

    # -- GET API -------------------------------------------------------
    def _api_get(self, path: str, query: dict) -> None:
        app: WebApp = self.server.app
        try:
            if path == "/api/bootstrap":
                return self._json(app.bootstrap())
            if path == "/api/state":
                return self._json({"ok": True, "state": app.hub.state()})
            if path == "/api/logs":
                since = int((query.get("since") or ["0"])[0] or 0)
                return self._json({"ok": True, "logs": app.logs(since), "seq": app._seq})
            if path == "/api/logs/download":
                body = app.log_text().encode("utf-8")
                return self._send(HTTPStatus.OK, body, "text/plain; charset=utf-8", {
                    "Content-Disposition": 'attachment; filename="hyena-console-'
                                           + time.strftime("%Y%m%d-%H%M%S") + '.txt"'})
            if path == "/api/files":
                return self._json({"ok": True, "dir": app.workspace.root,
                                   "files": app.workspace.list()})
            if path == "/api/files/download":
                name = (query.get("name") or [""])[0]
                data = app.workspace.read(name)
                return self._send(HTTPStatus.OK, data["content"].encode("utf-8"),
                                  "text/plain; charset=utf-8", {
                                      "Content-Disposition":
                                          f'attachment; filename="{data["name"]}"'})
            if path == "/api/examples":
                return self._json({"ok": True, "examples": EXAMPLES,
                                   "starter": STARTER_PROGRAM})
            if path == "/api/events":
                return self._stream_events()
            if path == "/api/health":
                return self._json({"ok": True, "version": VERSION,
                                   "simulation": app.hub.simulation})
            return self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
        except WorkspaceError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))

    # -- POST API ------------------------------------------------------
    def _api_post(self, path: str, body: dict, query: dict) -> None:
        app: WebApp = self.server.app
        try:
            if path == "/api/hub/scan":
                app.hub.scan(body.get("target") or None,
                             float(body.get("timeout") or 6))
                return self._json({"ok": True}, HTTPStatus.ACCEPTED)
            if path == "/api/hub/connect":
                app.hub.connect(body.get("target") or app.settings.get("hub_name") or None,
                                retries=int(body.get("retries") or app.settings["retries"]),
                                scan_timeout=float(body.get("timeout")
                                                   or app.settings["scan_timeout"]))
                return self._json({"ok": True}, HTTPStatus.ACCEPTED)
            if path == "/api/hub/disconnect":
                app.hub.disconnect()
                return self._json({"ok": True}, HTTPStatus.ACCEPTED)
            if path == "/api/hub/stop":
                app.hub.stop()
                return self._json({"ok": True}, HTTPStatus.ACCEPTED)
            if path == "/api/program/run":
                code = body.get("code")
                if not isinstance(code, str) or not code.strip():
                    return self._error(HTTPStatus.BAD_REQUEST, "the program is empty")
                if len(code.encode("utf-8")) > 1024 * 1024:
                    return self._error(HTTPStatus.BAD_REQUEST, "the program is too large")
                mode = body.get("mode") or "run"
                if mode not in ("run", "download", "stored"):
                    return self._error(HTTPStatus.BAD_REQUEST, "unknown run mode")
                name = str(body.get("name") or "program.py")
                app.hub.run(code, name, mode=mode, wait=bool(body.get("wait")))
                return self._json({"ok": True}, HTTPStatus.ACCEPTED)
            if path == "/api/lint":
                return self._json({"ok": True, "diagnostics": lint(body.get("code") or "")})
            if path == "/api/files/open":
                return self._json({"ok": True, **app.workspace.read(str(body.get("name") or ""))})
            if path == "/api/files/save":
                saved = app.workspace.write(str(body.get("name") or ""),
                                            body.get("content") or "")
                app.save_settings({"last_file": saved["name"]})
                return self._json({"ok": True, **saved,
                                   "files": app.workspace.list()})
            if path == "/api/files/rename":
                moved = app.workspace.rename(str(body.get("name") or ""),
                                             str(body.get("new_name") or ""))
                return self._json({"ok": True, **moved, "files": app.workspace.list()})
            if path == "/api/files/delete":
                removed = app.workspace.delete(str(body.get("name") or ""))
                return self._json({"ok": True, **removed, "files": app.workspace.list()})
            if path == "/api/race/preview":
                try:
                    result = app.training.generate()
                except TrainingError as exc:
                    return self._error(HTTPStatus.CONFLICT, str(exc), kind="training")
                return self._json({"ok": True, **result})
            if path == "/api/training/reset":
                app.training.reset()
                app.hub._publish({"t": "training", **app.training.snapshot()})
                app.hub.log("info", "Training session cleared", key="log.session_reset")
                return self._json({"ok": True, **app.training.snapshot()})
            if path == "/api/program/simulate-training":
                if not app.hub.simulation:
                    return self._error(HTTPStatus.BAD_REQUEST,
                                       "simulated data is only available in simulation mode")
                lines = body.get("lines") or demo_training_lines()
                if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
                    return self._error(HTTPStatus.BAD_REQUEST, "lines must be a list of strings")
                for line in lines[:2000]:
                    app.hub.log("out", line, record_training=True)
                return self._json({"ok": True, **app.training.snapshot()})
            if path == "/api/settings":
                return self._json({"ok": True, "settings": app.save_settings(body)})
            return self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
        except (HubBusy, HubError) as exc:
            return self._error(HTTPStatus.CONFLICT, str(exc), kind="hub")
        except WorkspaceError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc), kind="workspace")
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))

    # -- SSE -----------------------------------------------------------
    def _stream_events(self) -> None:
        app: WebApp = self.server.app
        channel = app.subscribe()
        self.close_connection = True
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 1500\n: connected\n\n")
            self.wfile.flush()
            last_id = self.headers.get("Last-Event-ID")
            if last_id and last_id.isdigit():
                for entry in app.logs(int(last_id)):
                    self._sse(entry, entry.get("seq"))
            while not app._closing:
                try:
                    event = channel.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse(event, event.get("seq"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            app.unsubscribe(channel)

    def _sse(self, event: dict, event_id=None) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        chunk = (f"id: {event_id}\n" if event_id else "") + f"data: {payload}\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()


def lint(code: str) -> list[dict]:
    """Cheap syntax diagnostics; no execution and no imports of the program.

    CPython's parser is used as an approximation of MicroPython's. Pybricks
    programs may use MicroPython-only `const(...)` calls, so those are blanked
    out first to avoid false errors (the columns on such lines shift slightly).
    Diagnostics are advisory: they never block an upload, because the hub's
    compiler remains the authority.
    """
    diagnostics: list[dict] = []
    if not isinstance(code, str):
        return [{"severity": "error", "line": 1, "column": 1,
                 "message": "code must be text"}]
    try:
        compile(_micropython_parseable(code), "<editor>", "exec")
    except SyntaxError as exc:
        diagnostics.append({
            "severity": "error",
            "line": int(exc.lineno or 1),
            "column": int(exc.offset or 1),
            "message": exc.msg or "invalid syntax",
        })
    except (ValueError, OverflowError) as exc:
        diagnostics.append({"severity": "error", "line": 1, "column": 1,
                            "message": str(exc)})
    if len(code.splitlines()) > 20000:
        diagnostics.append({"severity": "warning", "line": 1, "column": 1,
                            "message": "the program is very long"})
    return diagnostics


def _micropython_parseable(code: str) -> str:
    """Blank `const(...)` so the CPython parser accepts common Pybricks code."""
    import re
    return re.sub(r"\bconst\s*\(", "(", code)


def demo_training_lines() -> list[str]:
    return [
        "robot.straight(172)",
        "# LEFT=38 RIGHT=41 (colour sensors)",
        "robot.turn(32)",
        "robot.straight(410)",
        "robot.turn(-90)",
        "robot.straight(265)",
        "robot.stop()",
    ]


def create_server(host: str = "127.0.0.1", port: int = 8000, *,
                  simulate: bool = False, workspace: str | None = None,
                  data_dir: str | None = None) -> HyenaServer:
    app = WebApp(simulate=simulate, workspace=workspace, data_dir=data_dir)
    server = HyenaServer((host, port), Handler, app)
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (use 0.0.0.0 to reach it from another device)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workspace", default=None,
                        help="folder that holds the .py programs")
    parser.add_argument("--simulate", action="store_true",
                        help="never open Bluetooth; use the simulated hub")
    parser.add_argument("--data-dir", default=None,
                        help="override the settings/log folder")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the browser automatically")
    args = parser.parse_args(argv)

    try:
        server = create_server(args.host, args.port, simulate=args.simulate,
                               workspace=args.workspace, data_dir=args.data_dir)
    except OSError as exc:
        print(f"Cannot start the server on {args.host}:{args.port}: {exc}")
        return 1

    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{host}:{server.server_address[1]}/"
    mode = "SIMULATION" if server.app.hub.simulation else "HARDWARE"
    print(f"Hyena Control Web {VERSION} — {mode}", flush=True)
    if server.app.hub.simulation:
        details = server.app.hub.diagnostics()
        if details["reason"] == "simulate_flag":
            print("Bluetooth is off because --simulate was given.", flush=True)
            print("Restart without --simulate to talk to a real hub.", flush=True)
        else:
            print("Bluetooth is off because pybricksdev could not be imported:", flush=True)
            print(f"  {details['reason_detail']}", flush=True)
            print("Install it with:  python -m pip install -r requirements.txt", flush=True)
            print("Then restart this server.", flush=True)
    print(f"Open {url}  (Ctrl+C to stop)", flush=True)
    print(f"Workspace: {server.app.workspace.root}", flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        server.app.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
