"""Small transfer safeguards for the pybricksdev 2.x API.

No automatic retries: starting a robot program twice is not a safe recovery
strategy when the outcome of a Bluetooth write is unknown.
"""
from __future__ import annotations

import asyncio
from importlib.metadata import PackageNotFoundError, version
import sys

USER_PROGRAM_RUNNING = 1 << 6
# Public Pybricks protocol status bit, present in firmware 4.1.0. pybricksdev
# 2.3.2's StatusFlag enum does not yet name it, but retains unknown bits.
FILE_IO_IN_PROGRESS = 1 << 13
BLE_SAFE_WRITE_SIZE = 20  # ATT MTU 23 minus the three-byte ATT header


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
    return advertised, effective


async def wait_hub_idle(hub, timeout: float = 5.0) -> None:
    """Wait for observed program/file-I/O state, not a fixed 500 ms delay."""
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


def transfer_environment(hub) -> str:
    parts = [f"firmware={getattr(hub, 'fw_version', '?')}", f"platform={sys.platform}"]
    for package in ("pybricksdev", "bleak"):
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = "unknown"
        parts.append(f"{package}={installed}")
    return " ".join(parts)
