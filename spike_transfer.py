"""Small transfer safeguards for the pybricksdev 2.x API.

No automatic retries: starting a robot program twice is not a safe recovery
strategy when the outcome of a Bluetooth write is unknown.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from time import monotonic
from importlib.metadata import PackageNotFoundError, version
import sys

USER_PROGRAM_RUNNING = 1 << 6
# Public Pybricks protocol status bit, present in firmware 4.1.0. pybricksdev
# 2.3.2's StatusFlag enum does not yet name it, but retains unknown bits.
FILE_IO_IN_PROGRESS = 1 << 13
BLE_SAFE_WRITE_SIZE = 20  # ATT MTU 23 minus the three-byte ATT header
PYBRICKS_COMMAND_UUID = "c5f50002-8280-46da-89f4-6d8051e4aeef"
WRITE_PROGRAM_META = 3
WRITE_USER_RAM = 4


class TransferFailure(RuntimeError):
    def __init__(self, phase: str, error: Exception):
        self.phase = phase
        super().__init__(f"[TX phase={phase}] {type(error).__name__}: {error}")


def configure_ble_upload(hub) -> tuple[int, int]:
    """Limit library-generated upload packets to fit a baseline BLE ATT write.

    `_max_write_size` controls download_user_program()'s five-byte-header chunks
    in pybricksdev 2.x. Scope the change to this BLE hub, not USB or Bleak globals.
    This trades throughput for avoiding long/fragmented writes on slow adapters.
    It cannot fix every cause of GATT 0x0E.
    """
    advertised = hub._max_write_size
    if type(advertised) is not int or advertised < 6:
        raise RuntimeError("حجم كتابة BLE غير صالح؛ تحقق من إصدار pybricksdev")
    effective = min(advertised, BLE_SAFE_WRITE_SIZE)
    hub._max_write_size = effective
    hub._app_ble_upload_configured = True
    return advertised, effective


async def wait_hub_idle(hub, timeout: float = 5.0, *, require_fresh: bool = False) -> None:
    """Wait for idle, optionally requiring a new report rather than cached state.

    Use require_fresh for configured modern BLE hubs outside the upload itself.
    The default polling mode is retained for USB/legacy callers.
    """
    if require_fresh:
        await _wait_fresh_idle(hub, timeout)
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        flags = hub.status_observable.value
        if not isinstance(flags, int):
            raise RuntimeError("تعذر قراءة حالة الروبوت؛ أعد الاتصال قبل الإرسال")
        if not flags & (USER_PROGRAM_RUNNING | FILE_IO_IN_PROGRESS):
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("الروبوت ما زال ينفذ برنامجاً أو عملية نقل؛ لم يبدأ الإرسال أو التشغيل")
        await asyncio.sleep(min(0.05, remaining))


async def _wait_fresh_idle(hub, timeout: float) -> None:
    """Ignore BehaviorSubject's cached value; require an actual new idle report.

    Firmware 4.1 suppresses status notifications while FILE_IO_IN_PROGRESS is
    set (sys/host.c). A cached zero is therefore NOT evidence of current idle.
    Its periodic reports resume after completion or the storage idle timeout.
    Call this only outside an active upload, never between data packets.
    """
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    deadline = loop.time() + timeout
    subscribing = True
    listening = True

    def accept(flags):
        if ready.done():
            return
        if not isinstance(flags, int) or isinstance(flags, bool):
            ready.set_exception(RuntimeError("Invalid fresh hub status; reconnect before upload"))
        elif not flags & (USER_PROGRAM_RUNNING | FILE_IO_IN_PROGRESS):
            ready.set_result(None)

    def observe(flags):
        # Rx BehaviorSubject synchronously replays its cached value in subscribe().
        if not subscribing and listening:
            loop.call_soon_threadsafe(accept, flags)

    subscription = hub.status_observable.subscribe(observe)
    subscribing = False
    try:
        while True:
            await asyncio.wait_for(ready, timeout=max(0, deadline - loop.time()))
            latest = hub.status_observable.value
            if not isinstance(latest, int) or isinstance(latest, bool):
                raise RuntimeError("Invalid current hub status; reconnect before upload")
            if not latest & (USER_PROGRAM_RUNNING | FILE_IO_IN_PROGRESS):
                return
            # Another busy report may have arrived before the waiter resumed.
            ready = loop.create_future()
    except asyncio.TimeoutError as exc:
        raise TimeoutError("No fresh idle status report; refusing to continue") from exc
    finally:
        listening = False
        subscription.dispose()


def needs_fresh_status(hub) -> bool:
    """BLE modern-profile hubs only; leave USB and legacy behavior unchanged."""
    return (getattr(hub, "_app_ble_upload_configured", False) is True
            and getattr(hub, "_mpy_abi_version", None) == 0)


class UploadWriteTrace:
    """Our instrumentation adds no console/file I/O on successful writes."""
    def __init__(self):
        self.acked_bytes = 0
        self.last_ack = None
        self.max_write_ms = 0.0
        self.max_gap_ms = 0.0

    async def write(self, original, hub, uuid, data, *args, **kwargs):
        # Only the modern Pybricks command characteristic, not NUS/other services.
        if str(uuid).lower() != PYBRICKS_COMMAND_UUID or len(data) < 5:
            return await original(uuid, data, *args, **kwargs)
        command, value = data[0], int.from_bytes(data[1:5], "little")
        if command not in (WRITE_PROGRAM_META, WRITE_USER_RAM):
            return await original(uuid, data, *args, **kwargs)
        label = "data" if command == WRITE_USER_RAM else ("begin" if value == 0 else "finalize")
        payload = len(data) - 5 if command == WRITE_USER_RAM else 0
        response = kwargs.get("response", args[0] if args else None)
        started = monotonic()
        gap_ms = (started - self.last_ack) * 1000 if self.last_ack is not None else 0.0
        self.max_gap_ms = max(self.max_gap_ms, gap_ms)
        try:
            result = await original(uuid, data, *args, **kwargs)
        except Exception as exc:
            elapsed_ms = (monotonic() - started) * 1000
            self.max_write_ms = max(self.max_write_ms, elapsed_ms)
            flags = getattr(getattr(hub, "status_observable", None), "value", None)
            flags_text = f"0x{flags:08x}" if isinstance(flags, int) else "unknown"
            position = f"offset={value}" if command == WRITE_USER_RAM else f"size={value}"
            exc.add_note(
                f"[TX] upload command={label} {position} payload={payload} packet={len(data)} "
                f"response={response} acked_bytes={self.acked_bytes} cached_flags={flags_text} "
                f"write_ms={elapsed_ms:.1f} gap_ms={gap_ms:.1f} "
                f"max_write_ms={self.max_write_ms:.1f} max_gap_ms={self.max_gap_ms:.1f}")
            raise
        self.last_ack = monotonic()
        self.max_write_ms = max(self.max_write_ms, (self.last_ack - started) * 1000)
        self.acked_bytes += payload
        return result


@contextmanager
def trace_upload_writes(hub):
    """Temporarily instrument this BLE instance, preserving wire bytes and calls.

    The library still owns compilation/framing, and no error is retried. Restore
    the exact instance attribute (or inherited method) on success, error/cancel.
    """
    if getattr(hub, "_app_ble_upload_configured", False) is not True:
        yield
        return
    original = hub.write_gatt_char
    attributes = vars(hub)
    had_override = "write_gatt_char" in attributes
    previous_override = attributes.get("write_gatt_char")
    trace = UploadWriteTrace()

    async def write(uuid, data, *args, **kwargs):
        return await trace.write(original, hub, uuid, data, *args, **kwargs)

    hub.write_gatt_char = write
    try:
        yield
    finally:
        if had_override:
            hub.write_gatt_char = previous_override
        else:
            del hub.write_gatt_char


def transfer_environment(hub) -> str:
    parts = [f"firmware={getattr(hub, 'fw_version', '?')}", f"platform={sys.platform}"]
    for package in ("pybricksdev", "bleak"):
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = "unknown"
        parts.append(f"{package}={installed}")
    return " ".join(parts)
