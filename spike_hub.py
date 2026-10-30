"""Hub connection service for the browser front-end.

Pybricks Code talks to the hub from the browser. A web page cannot open a
Bluetooth LE link to a SPIKE Prime by itself, so Hyena Control keeps the proven
`pybricksdev` transport in a local Python process and streams everything to the
page over Server-Sent Events.

Two transports live here:

* :class:`PybricksTransport` - real BLE (and USB when the firmware supports it).
* :class:`SimulatedTransport` - a deterministic stand-in used for demos, tests
  and for the live preview when no robot or `pybricksdev` is installed.

The controller serialises operations, publishes small JSON-ready events and
keeps the training session in :mod:`spike_race`.
"""
from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import importlib
import os
import platform
import re
import sys
import threading
import time
import traceback
from typing import Any, Callable

from spike_core import MAX_HUB_LOG_BYTES, MAX_LINE_BYTES
from spike_paths import LOG_DIR
from spike_race import TrainingSession

USER_PROGRAM_RUNNING = 1 << 6
FILE_IO_IN_PROGRESS = 1 << 13
BATTERY_LOW = (1 << 0) | (1 << 1)


class HubError(RuntimeError):
    """Raised when an operation cannot start; the message is user-facing."""


class HubBusy(HubError):
    """Another hub operation is still running."""


def _now() -> str:
    return time.strftime("%H:%M:%S")


class HubController:
    """One background asyncio loop owning the hub connection."""

    def __init__(self, emit: Callable[[dict], None], training: TrainingSession,
                 *, force_simulation: bool = False, data_dir: str | None = None):
        self._emit = emit
        self.training = training
        self._data_dir = data_dir
        self._log_dir = os.path.join(data_dir, "logs") if data_dir else LOG_DIR
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            "connected": False,
            "label": "",
            "firmware": "",
            "transport": None,
            "running": False,
            "battery": None,
            "busy": False,
            "operation": None,
            "mode": "simulation",
            "can_run_stored": False,
            "last_error": None,
        }
        self._task: asyncio.Task | None = None
        self._active: set[asyncio.Task] = set()
        self._busy_flag = False
        self._future: concurrent.futures.Future | None = None
        self._stop_requested = False
        self._transport: Any = None
        self._pending_upload_ok = False
        self._loop = asyncio.new_event_loop()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closing = False

        self._force_simulation = bool(force_simulation)
        self._import_error: str | None = None
        self._pybricks = None
        if not force_simulation:
            available, error = self._probe_pybricks()
            self._pybricks = True if available else None
            self._import_error = error
        if self._pybricks is None:
            self._state["mode"] = "simulation"

    # -- infrastructure -------------------------------------------------
    @staticmethod
    def _load_pybricks() -> bool:
        available, _error = HubController._probe_pybricks()
        return available

    @staticmethod
    def _probe_pybricks() -> tuple[bool, str | None]:
        """Import the BLE stack once and remember why it failed, if it did."""
        try:
            from pybricksdev.ble import find_device  # noqa: F401
            from pybricksdev.connections.pybricks import (  # noqa: F401
                PybricksHubBLE, PybricksHubUSB,
            )
            return True, None
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _module_version(name: str) -> str | None:
        try:
            module = importlib.import_module(name)
        except Exception:
            return None
        return str(getattr(module, "__version__", None) or "unknown")

    def diagnostics(self) -> dict:
        """Why Bluetooth is (or is not) available, for the connect dialog."""
        reason = None
        if self.simulation:
            reason = "simulate_flag" if self._force_simulation else "missing_pybricksdev"
        return {
            "simulation": self.simulation,
            "forced_simulation": self._force_simulation,
            "reason": reason,
            "reason_detail": self._import_error,
            "pybricksdev": self._module_version("pybricksdev"),
            "bleak": self._module_version("bleak"),
            "serial": self._module_version("serial"),
            "python": platform.python_version(),
            "platform": sys.platform,
        }

    @property
    def simulation(self) -> bool:
        return self._pybricks is None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_loop, name="hub-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("could not start the hub worker")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        try:
            self._loop.run_forever()
        finally:
            pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def close(self) -> None:
        self._closing = True
        if self._thread is None:
            return
        async def shutdown():
            await self._stop_transport(silent=True)
        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=8)
        except Exception:
            pass
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._thread = None

    # -- state & events -------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            snapshot = dict(self._state)
            snapshot["mode"] = "simulation" if self.simulation else "hardware"
            snapshot["training"] = self.training.snapshot()
            return snapshot

    def _publish(self, event: dict) -> None:
        try:
            self._emit(event)
        except Exception:  # pragma: no cover - a listener must not break I/O
            traceback.print_exc()

    def _set_state(self, **changes) -> None:
        with self._lock:
            self._state.update(changes)
            snapshot = dict(self._state)
            snapshot["mode"] = "simulation" if self.simulation else "hardware"
            snapshot["training"] = self.training.snapshot()
        self._publish({"t": "state", "state": snapshot})

    def log(self, level: str, text: str, *, key: str | None = None,
            params: dict | None = None, record_training: bool = False) -> None:
        event = {"t": "console", "level": level, "text": text, "ts": _now()}
        if key:
            event["key"] = key
            event["params"] = params or {}
        self._publish(event)
        if record_training:
            self.training.add_line(text)
            self._publish({"t": "training", **self.training.snapshot()})

    def toast(self, level: str, text: str, *, key: str | None = None,
              params: dict | None = None) -> None:
        event = {"t": "toast", "level": level, "text": text}
        if key:
            event["key"] = key
            event["params"] = params or {}
        self._publish(event)

    def progress(self, phase: str, sent: int = 0, total: int = 0,
                 percent: int | None = None) -> None:
        self._publish({"t": "progress", "phase": phase, "sent": sent,
                       "total": total, "percent": percent})

    # -- scheduling -----------------------------------------------------
    def _schedule(self, name: str, factory, *, force: bool = False):
        with self._lock:
            if self._closing:
                raise HubError("server is shutting down")
            if not force and self._busy_flag:
                raise HubBusy("another hub operation is already running")
            if not force:
                self._busy_flag = True
        self._set_state(busy=True, operation=name)
        future = asyncio.run_coroutine_threadsafe(
            self._guarded(name, factory, force=force), self._loop)
        with self._lock:
            self._future = future
        return future

    async def _guarded(self, name: str, factory, *, force: bool = False) -> Any:
        task = asyncio.current_task()
        with self._lock:
            self._task = task
            self._active.add(task)
            self._stop_requested = False
        try:
            return await factory()
        except asyncio.CancelledError:
            self.log("warn", f"operation cancelled: {name}", key="log.cancelled",
                     params={"name": name})
            raise
        except HubError as exc:
            self.toast("error", str(exc))
            self.log("err", str(exc))
            self._set_state(last_error=str(exc), running=False)
        except Exception as exc:  # unexpected: keep the full trace in the log
            detail = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            self._write_transfer_error(name, exc)
            self.toast("error", detail)
            self.log("err", detail)
            self._set_state(last_error=detail, running=False)
        finally:
            with self._lock:
                self._active.discard(task)
                if self._task is task:
                    self._task = None
                if not force:
                    self._busy_flag = False
                still_busy = self._busy_flag
            self._set_state(busy=still_busy, operation=None if not still_busy else self._state.get("operation"))

    def _write_transfer_error(self, name: str, exc: Exception) -> None:
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            with open(os.path.join(self._log_dir, "web_transfer_errors.log"), "a",
                      encoding="utf-8") as stream:
                stream.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} operation={name}\n")
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=stream)
        except OSError:
            traceback.print_exc()

    def wait(self, timeout: float = 30) -> None:
        """Block until the current operation finishes (tests, scripts)."""
        with self._lock:
            future = self._future
        if future is not None:
            try:
                future.result(timeout=timeout)
            except concurrent.futures.CancelledError:
                pass
            except Exception:
                pass

    # -- public operations ---------------------------------------------
    def scan(self, target: str | None = None, timeout: float = 6.0) -> None:
        self._schedule("scan", lambda: self._op_scan(target, timeout))

    def connect(self, target: str | None = None, *, retries: int = 6,
                scan_timeout: float = 20.0) -> None:
        self._schedule("connect", lambda: self._op_connect(target, retries, scan_timeout))

    def disconnect(self) -> None:
        self._schedule("disconnect", self._op_disconnect)

    def stop(self) -> None:
        """Cancel a transfer if needed, then stop the running program."""
        self._schedule("stop", self._op_stop, force=True)

    def run(self, code: str, name: str, *, mode: str = "run",
            wait: bool = False) -> None:
        self._schedule("run", lambda: self._op_run(code, name, mode, wait))

    # -- operations -----------------------------------------------------
    async def _op_scan(self, target: str | None, timeout: float) -> None:
        self.progress("scan", percent=None)
        found: list[dict] = []
        if self.simulation:
            names = ("SPIKE Prime (simulation)", "Technic Hub (simulation)")
            for index, name in enumerate(names):
                await asyncio.sleep(0.25)
                hub = {"name": name, "address": f"SIM-{index + 1:02d}",
                       "rssi": -42 - index * 9, "simulated": True}
                found.append(hub)
                self._publish({"t": "scan", "hubs": [hub], "done": False})
            self._publish({"t": "scan", "hubs": found, "done": True})
            self.log("info", f"Found {len(found)} hub(s)", key="log.scan_found",
                     params={"count": len(found)})
            self.progress("idle", percent=0)
            return

        try:
            from bleak import BleakScanner
            discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        except Exception as exc:
            self.progress("idle", percent=0)
            self._publish({"t": "scan", "hubs": [], "done": True, "failed": True})
            raise HubError(f"Bluetooth scan failed: {type(exc).__name__}: {exc}") from exc
        for device, adv in discovered.values():
            name = device.name or adv.local_name or "(unnamed)"
            hub = {"name": name, "address": device.address,
                   "rssi": getattr(adv, "rssi", None), "simulated": False}
            found.append(hub)
            self._publish({"t": "scan", "hubs": [hub], "done": False})
        self._publish({"t": "scan", "hubs": found, "done": True, "empty": not found})
        if found:
            self.log("info", f"Found {len(found)} Bluetooth device(s)", key="log.scan_found",
                     params={"count": len(found)})
        else:
            # An empty scan is the most common "Bluetooth does not work" report:
            # say what to check instead of leaving a silent, blank list.
            self.log("warn", "No Bluetooth device was found", key="log.no_hub_found")
            self.toast("warn", "No hub found nearby", key="toast.no_hub_found")
        self.progress("idle", percent=0)

    async def _op_connect(self, target: str | None, retries: int,
                          scan_timeout: float) -> None:
        await self._stop_transport(silent=True)
        if self.simulation:
            transport = SimulatedTransport(self)
            self.log("info", "Connecting to the simulated hub…",
                     key="log.connecting", params={"target": transport.label})
            await transport.connect()
            self._transport = transport
            self._set_state(connected=True, label=transport.label,
                            firmware=transport.firmware, transport="sim",
                            battery=transport.battery, last_error=None,
                            can_run_stored=True)
            self.log("ok", f"Connected to {transport.label} (firmware {transport.firmware})",
                     key="log.connected",
                     params={"name": transport.label, "firmware": transport.firmware})
            self.toast("success", "Simulation hub connected", key="toast.connected")
            await self._emit_welcome()
            return

        retries = max(1, min(int(retries or 1), 10))
        last_error = ""
        for attempt in range(1, retries + 1):
            self.log("info", f"Attempt {attempt}/{retries} — scanning for a hub…",
                     key="log.searching", params={"attempt": attempt, "retries": retries})
            transport = PybricksTransport(self, target=target)
            try:
                await transport.connect(timeout=scan_timeout)
            except asyncio.CancelledError:
                await transport.disconnect()
                raise
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self.log("err", f"Connection failed: {last_error}",
                         key="log.connect_failed", params={"error": str(exc)})
                await transport.disconnect()
                continue
            self._transport = transport
            self._set_state(connected=True, label=transport.label,
                            firmware=transport.firmware, transport="ble",
                            battery=None, last_error=None, can_run_stored=False)
            self.log("ok", f"Connected to {transport.label} (firmware {transport.firmware})",
                     key="log.connected",
                     params={"name": transport.label, "firmware": transport.firmware})
            self.toast("success", transport.label, key="toast.connected")
            await self._emit_welcome()
            return
        raise HubError(
            f"no hub answered after {retries} attempts"
            + (f" (last error: {last_error})" if last_error else ""))

    async def _emit_welcome(self) -> None:
        if self.simulation:
            self.log("info", "Simulation mode — no Bluetooth is used",
                     key="log.sim_env")
            self.log("info", "The simulated hub is ready: press Download and run (F5)",
                     key="log.sim_ready")
        else:
            self.log("info", await self._environment())
        self._publish({"t": "training", **self.training.snapshot()})

    async def _environment(self) -> str:
        await asyncio.sleep(0)
        try:
            from spike_transfer import transfer_environment
            hub = getattr(self._transport, "hub", None)
            return transfer_environment(hub) if hub is not None else "pybricks-dev"
        except Exception:
            return "pybricks-dev"

    async def _op_disconnect(self) -> None:
        await self._stop_transport(silent=False)
        self._set_state(connected=False, label="", firmware="", transport=None,
                        running=False, battery=None, can_run_stored=False)
        self.log("info", "Disconnected", key="log.disconnected")
        self.progress("idle", percent=0)

    async def _stop_transport(self, silent: bool) -> None:
        transport, self._transport = self._transport, None
        if transport is None:
            return
        try:
            await transport.disconnect()
        except Exception as exc:
            if not silent:
                self.log("warn", f"disconnect reported: {exc}", key="log.disconnect_warn",
                         params={"error": str(exc)})
        self._pending_upload_ok = False
        if not silent:
            self.progress("idle", percent=0)

    async def _op_stop(self) -> None:
        others = self._other_active_tasks()
        if others:
            self.log("info", "Cancelling the current transfer…", key="log.cancelling")
            for task in others:
                task.cancel()
            await asyncio.gather(*others, return_exceptions=True)
        transport = self._transport
        if transport is None:
            if others:
                # Cancelling a pending transfer/connection is a valid stop.
                self.progress("idle", percent=0)
                return
            raise HubError("not connected to a hub")
        await transport.stop()
        self._set_state(running=False)
        self.log("ok", "Program stopped", key="log.stopped")
        self.progress("idle", percent=0)

    def _other_active_tasks(self) -> list[asyncio.Task]:
        current = asyncio.current_task()
        with self._lock:
            tasks = list(self._active)
        return [task for task in tasks if task is not current and not task.done()]

    async def _op_run(self, code: str, name: str, mode: str, wait: bool) -> None:
        transport = self._transport
        if transport is None:
            raise HubError("not connected to a hub")

        if mode == "stored":
            if not self._pending_upload_ok:
                raise HubError("no successful upload in this session; upload "
                               "a program before running the stored one")
            self.log("info", "Starting the stored program…", key="log.stored_start")
            await transport.start(wait=wait)
            self._set_state(running=True)
            self.log("ok", "Stored program started", key="log.stored_ok")
            return

        self.log("info", f"Stopping any running program…", key="log.stop_first")
        await transport.stop()
        self._set_state(running=False)

        path = await asyncio.get_running_loop().run_in_executor(
            None, self._write_program, code, name)
        try:
            self.progress("upload", 0, 100, 0)
            self.log("info", f"Compiling and uploading {name}…",
                     key="log.uploading", params={"name": name})
            await transport.upload(path, code)
            self._pending_upload_ok = True
            self._set_state(can_run_stored=True)
            self.progress("upload", 100, 100, 100)
            self.log("ok", "Upload complete ✔", key="log.upload_ok")
            if mode == "download":
                self.log("ok", "Saved on the hub — use “Run stored program” later",
                         key="log.download_ok")
                return
            self.log("info", "Starting the program…", key="log.starting")
            await transport.start(wait=wait)
            self._set_state(running=True)
            if wait:
                self.log("ok", "Program finished ✔", key="log.finished")
                self._set_state(running=False)
            else:
                self.log("ok", "Running ✔ — make sure the robot is on a safe path",
                         key="log.running")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _write_program(self, code: str, name: str) -> str:
        """Write the upload to a private temp file; the hub API wants a path."""
        import tempfile
        directory = os.path.join(self._data_dir or self._log_dir, "tmp")
        os.makedirs(directory, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(name or "program.py"))
        if not safe.endswith(".py"):
            safe += ".py"
        fd, path = tempfile.mkstemp(prefix="upload_", suffix="_" + safe, dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(code)
        return path

    # -- transport callbacks -------------------------------------------
    def _hub_line(self, line) -> None:
        raw = bytes(line) if isinstance(line, (bytes, bytearray)) else str(line).encode("utf-8", "replace")
        if len(raw) > MAX_LINE_BYTES:
            self.training.mark_lost("hub line exceeded the size limit")
            self._publish({"t": "training", **self.training.snapshot()})
            self.log("err", "A hub line exceeded the size limit",
                     key="log.line_too_long")
            return
        text = raw.decode("utf-8", "replace")
        prefix = next((p for p in (b"PB_OF:", b"_file_begin_ ") if raw.startswith(p)), None)
        if prefix:
            name = raw[len(prefix):].decode("utf-8", "replace").strip()
            self._start_file_log(name)
            return
        if raw.strip() in (b"PB_EOF", b"_file_end_"):
            self._close_file_log()
            return
        self._write_file_log(text)
        self.log("out", text, record_training=True)
        self._publish({"t": "training", **self.training.snapshot()})

    def _start_file_log(self, name: str) -> None:
        self._close_file_log()
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", os.path.basename(name.replace("\\", "/")))[:48]
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            self._file_path = os.path.join(
                self._log_dir, f"hub_{safe}_{time.strftime('%Y%m%d_%H%M%S')}.txt")
            self._file = open(self._file_path, "w", encoding="utf-8")
            self._file_bytes = 0
            self.log("ok", f"Hub log file: {self._file_path}", key="log.file_log",
                     params={"path": self._file_path})
        except OSError as exc:
            self._file = None
            self.log("err", f"Could not open the hub log: {exc}", key="log.file_error",
                     params={"error": str(exc)})

    def _write_file_log(self, text: str) -> None:
        stream = getattr(self, "_file", None)
        if not stream:
            return
        try:
            size = len((text + "\n").encode("utf-8"))
            if self._file_bytes + size > MAX_HUB_LOG_BYTES:
                raise OSError("hub log reached the size limit")
            stream.write(text + "\n")
            self._file_bytes += size
        except OSError as exc:
            self._close_file_log()
            self.log("err", f"Could not save the hub log: {exc}", key="log.file_error",
                     params={"error": str(exc)})

    def _close_file_log(self) -> None:
        stream, self._file = getattr(self, "_file", None), None
        if stream:
            try:
                stream.close()
                self.log("ok", "Hub log finished", key="log.file_done")
            except OSError:
                traceback.print_exc()

    def _hub_status(self, flags: int) -> None:
        running = bool(flags & USER_PROGRAM_RUNNING)
        self._set_state(running=running, busy_flags=bool(flags & FILE_IO_IN_PROGRESS))

    def _hub_battery(self, value) -> None:
        self._set_state(battery=value)

    def _hub_disconnected(self) -> None:
        self._transport = None
        self._set_state(connected=False, running=False, transport=None,
                        can_run_stored=False)


# ---------------------------------------------------------------------------
# Real transport
# ---------------------------------------------------------------------------
class PybricksTransport:
    """Thin adapter around pybricksdev, mirroring the desktop app's sequence."""

    def __init__(self, controller: HubController, target: str | None = None):
        self.controller = controller
        self.target = (target or "").strip() or None
        self.hub = None
        self.label = ""
        self.firmware = ""
        self._subscriptions = []
        self._file = None

    async def connect(self, timeout: float = 20.0) -> None:
        from pybricksdev.ble import find_device
        from pybricksdev.connections.pybricks import PybricksHubBLE

        dev = await find_device(self.target, timeout=timeout)
        self.label = str(getattr(dev, "name", None) or self.target or "Pybricks hub")
        hub = PybricksHubBLE(dev)
        try:
            await asyncio.wait_for(hub.connect(), timeout=60)
        except BaseException:
            try:
                await asyncio.wait_for(hub.disconnect(), timeout=5)
            except Exception:
                pass
            raise
        self.hub = hub
        self.firmware = getattr(hub, "fw_version", "?")

        from spike_transfer import (configure_ble_upload, transfer_environment)
        advertised, effective = configure_ble_upload(hub)
        self.controller.log("info", f"[TX] BLE write_size={effective} hub_limit={advertised}",
                            key="log.write_size",
                            params={"effective": effective, "limit": advertised})
        self.controller.log("info", transfer_environment(hub))

        hub._line_handler = self.controller._hub_line
        hub.print_output = False
        hub._enable_line_handler = True

        from pybricksdev.connections import ConnectionState
        try:
            self._subscriptions.append(hub.connection_state_observable.subscribe(
                lambda state: self.controller._hub_disconnected()
                if state == ConnectionState.DISCONNECTED else None))
            self._subscriptions.append(hub.status_observable.subscribe(
                lambda flags: self.controller._hub_status(int(flags))))
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        for subscription in self._subscriptions:
            try:
                subscription.dispose()
            except Exception:
                traceback.print_exc()
        self._subscriptions = []
        await self._close_file()
        hub, self.hub = self.hub, None
        if hub is not None:
            await asyncio.wait_for(hub.disconnect(), timeout=5)

    async def _close_file(self) -> None:
        stream, self._file = self._file, None
        if stream:
            stream.close()

    async def upload(self, path: str, code: str) -> None:
        from spike_transfer import (finish_upload_progress, needs_fresh_status,
                                    trace_upload_writes, upload_progress,
                                    wait_hub_idle)
        hub = self._require()
        await self._wait_idle()
        loop = asyncio.get_running_loop()

        def report(sent: int, total: int, complete: bool) -> None:
            percent = 100 if complete else min(99, sent * 100 // max(1, total))
            loop.call_soon_threadsafe(
                self.controller.progress, "upload", sent, total, percent)

        with upload_progress(report):
            with trace_upload_writes(hub):
                await hub.download(path)
        self.controller.log("info", "[TX] phase=upload-ready", key="log.phase",
                            params={"phase": "upload-ready"})
        if needs_fresh_status(hub):
            await wait_hub_idle(hub, require_fresh=True)
        else:
            await wait_hub_idle(hub)
        finish_upload_progress()

    async def start(self, *, wait: bool = False) -> None:
        hub = self._require()
        await hub.run(None, wait=wait, print_output=False, line_handler=True)

    async def stop(self) -> None:
        hub = self.hub
        if hub is None:
            return
        await hub.stop_user_program()

    async def _wait_idle(self) -> None:
        from spike_transfer import needs_fresh_status, wait_hub_idle
        hub = self._require()
        if needs_fresh_status(hub):
            await wait_hub_idle(hub, require_fresh=True)
        else:
            await wait_hub_idle(hub)

    def _require(self):
        if self.hub is None:
            raise HubError("hub connection is closed")
        return self.hub


# ---------------------------------------------------------------------------
# Simulation transport
# ---------------------------------------------------------------------------
class SimulatedTransport:
    """Deterministic hub stand-in for demos and headless tests."""

    label = "SPIKE Prime (simulation)"
    firmware = "4.1.0-sim"
    battery = 87

    def __init__(self, controller: HubController):
        self.controller = controller
        self.started = False
        self._task: asyncio.Task | None = None

    async def connect(self) -> None:
        await asyncio.sleep(0.4)

    async def disconnect(self) -> None:
        await self.stop()

    async def upload(self, path: str, code: str) -> None:
        self._code = code
        total = max(1024, len(code.encode("utf-8")) * 64)
        steps = 14
        for step in range(1, steps + 1):
            await asyncio.sleep(0.045)
            sent = int(total * step / steps)
            self.controller.progress("upload", sent, total,
                                     min(99, sent * 100 // total))
        self.controller.progress("upload", total, total, 99)

    async def start(self, *, wait: bool = False) -> None:
        self.started = True
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._play(wait))

    async def stop(self) -> None:
        task, self._task = self._task, None
        self.started = False
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # -- fake program execution ---------------------------------------
    async def _play(self, wait: bool) -> None:
        code = getattr(self, "_code", "")
        steps = _scripted_steps(code)
        try:
            for kind, value in steps:
                await asyncio.sleep(0.16)
                if kind == "print":
                    self.controller.log("out", value, record_training=True)
                elif kind == "wait":
                    await asyncio.sleep(min(value / 1000.0, 0.4))
                else:
                    self.controller.log("info", f"» {value}")
            if steps:
                await asyncio.sleep(0.15)
            self.controller.progress("upload", 1, 1, 100)
            self.controller.log("ok", "Program finished ✔", key="log.finished")
        except asyncio.CancelledError:
            self.controller.log("warn", "Program stopped", key="log.stopped")
            raise
        finally:
            self.started = False
            self.controller._set_state(running=False)


def _scripted_steps(code: str) -> list[tuple[str, Any]]:
    """Turn a program into believable console lines without executing it."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return [("print", "syntax error: the program was not executed")]
    steps: list[tuple[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "print" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    steps.append((0, node.lineno, "print", first.value))
            elif node.func.id == "wait" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, (int, float)):
                    steps.append((0, node.lineno, "wait", first.value))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "robot"):
            steps.append((0, node.lineno, "move",
                          f"robot.{node.func.attr}(" +
                          ", ".join(str(int(a.value)) if isinstance(a, ast.Constant)
                                    and isinstance(a.value, (int, float)) else "…"
                                    for a in node.args) + ")"))
    steps.sort(key=lambda item: item[1])
    return [(kind, value) for _, _, kind, value in steps][:200]
