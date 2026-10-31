"""LEGO SPIKE / MINDSTORMS Robot Inventor — official firmware support.

Provides a BLE connection path (LWP3.0 / LEGO Hub GATT) and a USB-serial
(raw MicroPython REPL) path so Hyena Control can talk to a Hub that is still
running the stock LEGO firmware, in addition to Pybricks.

Public surface mirrors what ``spike_controller.py`` expects from a hub object:

* ``connect()`` / ``disconnect()`` async methods
* ``fw_version`` attribute (populated after connect)
* ``status_observable`` (RX Subject-like exposing integer status flags)
* ``connection_state_observable`` (RX Subject-like)
* ``line_handler`` attribute (callable receiving bytes/str output lines)
* ``run(path, wait, print_output, line_handler)`` — sends + runs a program
* ``download(path)`` — uploads without starting
* ``stop_user_program()`` — interrupts the running program

On official firmware there is no BLE path for arbitrary Python upload; we send
scripts over the USB CDC/serial raw REPL. BLE connections are useful for status,
battery, and starting/stopping the already-loaded program via LWP3.

References:
* LEGO Wireless Protocol 3.0.00 public document set
* Pybricks ``pybricksdev`` sources for BLE patterns (BLE service discovery, etc.)
* MicroPython raw REPL protocol (CTRL-A / CTRL-B / CTRL-C / CTRL-D)
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable, Any

try:
    from bleak import BleakClient, BleakScanner
    OFFICIAL_BLEAK_OK = True
    OFFICIAL_BLEAK_ERR = ""
except Exception as _e:  # pragma: no cover
    OFFICIAL_BLEAK_OK = False
    OFFICIAL_BLEAK_ERR = str(_e)

try:
    import serial as _serial  # pyserial
    OFFICIAL_SERIAL_OK = True
    OFFICIAL_SERIAL_ERR = ""
except Exception as _e:  # pragma: no cover
    OFFICIAL_SERIAL_OK = False
    OFFICIAL_SERIAL_ERR = str(_e)

# Backwards-compatible aliases (used internally and by some callers).
BLEAK_OK = OFFICIAL_BLEAK_OK
BLEAK_ERR = OFFICIAL_BLEAK_ERR
SERIAL_OK = OFFICIAL_SERIAL_OK
SERIAL_ERR = OFFICIAL_SERIAL_ERR


# ---------------------------------------------------------------------------
# BLE service/characteristic UUIDs (LEGO Wireless Protocol 3.0)
# ---------------------------------------------------------------------------
# The LEGO Hub advertises the Hub Service and exposes two characteristics on it:
# a UART-like inbox (write) and outbox (notify). All LWP3 messages are framed
# with 2-byte little-endian length over these characteristics.
LEGO_HUB_SERVICE_UUID = "00001623-1212-efde-1623-785feabcd123"
LEGO_HUB_CHAR_INBOX   = "00001624-1212-efde-1623-785feabcd123"  # write (hub -> us via notify too on some hubs)
LEGO_HUB_CHAR_OUTBOX  = "00001625-1212-efde-1623-785feabcd123"  # notify (hub -> us)

# Fallback: some official-firmware builds also advertise the Pybricks-like
# Nordic UART Service. We prefer the LEGO Hub service above.
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_UUID      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


# ---------------------------------------------------------------------------
# Minimal RX-like Subject (drop-in for rx.subjects.BehaviorSubject)
# ---------------------------------------------------------------------------
class _BehaviorSubject:
    def __init__(self, initial: int = 0):
        self.value: int = initial
        self._subs: list[Callable[[int], Any]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback):
        with self._lock:
            self._subs.append(callback)
        try:
            callback(self.value)
        except Exception:
            pass

        class _Sub:
            def __init__(self, outer, cb):
                self.outer = outer
                self.cb = cb
            def dispose(self):
                with self.outer._lock:
                    if self.cb in self.outer._subs:
                        self.outer._subs.remove(self.cb)

        return _Sub(self, callback)

    def on_next(self, value: int):
        with self._lock:
            self.value = value
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(value)
            except Exception:
                pass


class _ConnectionSubject(_BehaviorSubject):
    # Values mirror pybricksdev.connections.ConnectionState integers:
    # 0 = DISCONNECTED, 1 = CONNECTING, 2 = CONNECTED
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2


# ---------------------------------------------------------------------------
# LWP3 message types we actually care about
# ---------------------------------------------------------------------------
MSG_HUB_PROPERTIES = 0x01       # Hub Properties (battery, button, fw version...)
MSG_HUB_ACTIONS    = 0x02       # Hub Actions (disconnect, switch off, …)
MSG_FW_UPDATE      = 0x03
MSG_BOOTLOADER     = 0x04
MSG_PORT_INFO      = 0x04
MSG_ERROR          = 0x05
MSG_HW_NET_CMDS    = 0x08
MSG_PORT_POWER     = 0x10       # Port Input Format Single / port data
MSG_PORT_OUTPUT    = 0x81

# Hub property identifiers
PROP_NAME        = 0x01
PROP_BUTTON      = 0x02
PROP_FW_VERSION  = 0x03
PROP_HW_VERSION  = 0x04
PROP_RSSI        = 0x05
PROP_BATTERY_VOLTAGE = 0x06
PROP_BATTERY_TYPE    = 0x07
PROP_MFR_NAME    = 0x08
PROP_RADIO_FW    = 0x09
PROP_LEGO_WIRELESS_PROTO = 0x0A
PROP_SYSTEM_TYPE_ID = 0x0B
PROP_HW_NET_ID   = 0x0C
PROP_PRIMARY_MAC = 0x0D
PROP_SECONDARY_MAC = 0x0E
PROP_HW_NET_FAMILY = 0x0F


def _lwp_frame(payload: bytes) -> bytes:
    """Frame an LWP3 message with its 2-byte little-endian length header."""
    return len(payload).to_bytes(2, "little") + payload


def _lwp_hub_property_request(prop: int, operation: int = 0x05) -> bytes:
    """Request a hub property value. operation 0x05 = RequestUpdate."""
    return _lwp_frame(bytes([MSG_HUB_PROPERTIES, prop, operation]))


def _lwp_parse_hub_property(data: bytes):
    """Parse a Hub Properties message; returns (prop_id, value_bytes) or None."""
    if len(data) < 3 or data[0] != MSG_HUB_PROPERTIES:
        return None
    prop = data[1]
    op   = data[2]
    # op 0x06 = Update (push / reply). We also accept 0x05 (request) for robustness.
    if op not in (0x05, 0x06):
        return None
    return prop, data[3:]


# ---------------------------------------------------------------------------
# Official-firmware hub over BLE (LWP3)
# ---------------------------------------------------------------------------
@dataclass
class OfficialBLEHub:
    """LEGO SPIKE / Robot Inventor hub connected over official BLE protocol.

    Status flags (for the UI status bar): we synthesise a Pybricks-compatible
    flag word so the existing UI/consumers behave naturally:

    * bit 0/1 battery-low warning
    * bit 6 user-program-running (set while we have pushed a program via USB
      and haven't stopped it; over pure BLE this stays 0).
    """
    target_name: Optional[str] = None
    scan_timeout: float = 20.0

    def __post_init__(self):
        if not BLEAK_OK:
            raise RuntimeError(f"مكتبة bleak غير متوفرة: {BLEAK_ERR}")
        self._client: Optional[BleakClient] = None
        self._tx_uuid: Optional[str] = None
        self._rx_uuid: Optional[str] = None
        self.fw_version: str = "?"
        self._fw_parts: tuple = ()
        self._battery_pct: int = 0
        self._button_pressed: bool = False
        self._name: str = self.target_name or "LEGO Hub"
        self._app_label: str = self.target_name or "LEGO Hub"
        self.status_observable = _BehaviorSubject(0)
        self.connection_state_observable = _ConnectionSubject(
            _ConnectionSubject.DISCONNECTED)
        self.line_handler: Optional[Callable[[bytes], None]] = None
        self.print_output: bool = False
        self._enable_line_handler: bool = True
        # USB-serial session, if attached, for running Python programs
        self._serial: Optional["OfficialUSBREPL"] = None
        self._program_running = False
        self._mpy_abi_version = 1  # pretend legacy so pybricksdev legacy path isn't taken
        # The controller uses these to know it should not apply pybricksdev-only
        # transfer tuning/monitoring.
        self._app_is_official = True

    # ---- connection lifecycle ----
    async def connect(self) -> None:
        self.connection_state_observable.on_next(_ConnectionSubject.CONNECTING)
        dev = await find_official_device(name=self.target_name, timeout=self.scan_timeout)
        if dev is None:
            raise asyncio.TimeoutError(
                "لم يُعثر على أي روبوت LEGO يعمل بالفيرموير الرسمي عبر البلوتوث"
            )
        self._name = dev.name or self._name
        # Windows backend: use cached services to avoid re-pairing prompts.
        client = BleakClient(dev, timeout=30.0,
                             disconnected_callback=lambda c: None)
        await asyncio.wait_for(client.connect(), timeout=30.0)
        self._client = client

        # بعض خلفيات ويندوز/BlueZ لا تُنهي اكتشاف الخدمات فوراً بعد connect().
        # نطلبها صراحة ثم ننتظر قليلاً.
        try:
            await asyncio.wait_for(client.get_services(), timeout=15.0)
        except Exception:
            pass
        await asyncio.sleep(1.0)

        services = client.services
        tx_uuid = rx_uuid = None
        # احصر جميع الخدمات والخصائص في بنية آمنة قبل البحث.
        svc_info = [svc.uuid for svc in services]
        all_characteristics = []
        for svc in services:
            for ch in svc.characteristics:
                all_characteristics.append(
                    (svc.uuid, ch.uuid.lower(), list(ch.properties or []), ch))

        # 1) خدمة LEGO Hub الرسمية 1623 مع خصائص Inbox (1624) و Outbox (1625).
        for _svc, cl, props, ch in all_characteristics:
            if _svc.lower() != LEGO_HUB_SERVICE_UUID:
                continue
            if ("notify" in props or "indicate" in props) and tx_uuid is None \
                    and LEGO_HUB_CHAR_OUTBOX in cl:
                tx_uuid = ch.uuid
            if ("write" in props or "write-without-response" in props) and rx_uuid is None \
                    and LEGO_HUB_CHAR_INBOX in cl:
                rx_uuid = ch.uuid


        # 2) ابحث في كل الخدمات عن خصائص تنتمي لعائلة LEGO 162x إن لم نجد 1624/1625 مباشرة.
        if not (tx_uuid and rx_uuid):
            for _svc, cl, props, ch in all_characteristics:
                if "162" in cl:
                    if ("notify" in props or "indicate" in props) and tx_uuid is None \
                            and ("1625" in cl or "1626" in cl or "1627" in cl):
                        tx_uuid = ch.uuid
                    if ("write" in props or "write-without-response" in props) and rx_uuid is None \
                            and ("1624" in cl or "1626" in cl):
                        rx_uuid = ch.uuid

        # 3) Nordic UART fallback
        if not (tx_uuid and rx_uuid):
            for _svc, cl, props, ch in all_characteristics:
                if NUS_TX_UUID in cl and ("notify" in props or "indicate" in props):
                    tx_uuid = ch.uuid
                if NUS_RX_UUID in cl and ("write" in props or "write-without-response" in props):
                    rx_uuid = ch.uuid

        # 4) Last resort: أي زوج notify+write من خدمة LEGO (تنتهي بـ aeef)
        if not (tx_uuid and rx_uuid):
            any_notify_lego = any_write_lego = None
            any_notify = any_write = None
            for _svc, cl, props, ch in all_characteristics:
                is_lego = "efde-1623-785feabcd123" in cl or "aeef" in cl
                if ("notify" in props or "indicate" in props):
                    if any_notify is None:
                        any_notify = ch.uuid
                    if is_lego and any_notify_lego is None:
                        any_notify_lego = ch.uuid
                if ("write" in props or "write-without-response" in props):
                    if any_write is None:
                        any_write = ch.uuid
                    if is_lego and any_write_lego is None:
                        any_write_lego = ch.uuid
            if any_notify_lego and any_write_lego:
                tx_uuid, rx_uuid = any_notify_lego, any_write_lego
            elif any_notify and any_write:
                tx_uuid, rx_uuid = any_notify, any_write

        if not (tx_uuid and rx_uuid):
            await client.disconnect()
            diag = "; ".join(str(s) for s in svc_info[:8])
            if len(svc_info) > 8:
                diag += f" ... (+{len(svc_info)-8})"
            raise RuntimeError(
                "تعذر العثور على خصائص الكتابة والإشعار لدى الروبوت. "
                f"عدد الخدمات المكتشفة: {len(svc_info)}. "
                "قد يكون الروبوت متصلاً بتطبيق آخر (LEGO SPIKE / Powered Up) "
                "أو ما زال في وضع الإقلاع. أغلق التطبيقات الأخرى، "
                "افصل البلوتوث ثم أعد تشغيله، وأعد المحاولة.")
        self._tx_uuid, self._rx_uuid = tx_uuid, rx_uuid

        await client.start_notify(tx_uuid, self._on_notify)
        # استفسر عن إصدار الفرموير واسم الجهاز والبطارية (بمهلة بين الأوامر).
        try:
            await self._write(_lwp_hub_property_request(PROP_FW_VERSION))
            await asyncio.sleep(0.3)
            await self._write(_lwp_hub_property_request(PROP_NAME))
            await asyncio.sleep(0.3)
            await self._write(_lwp_hub_property_request(PROP_BATTERY_VOLTAGE))
            await asyncio.sleep(0.3)
            await self._write(_lwp_hub_property_request(PROP_BUTTON))
            await asyncio.sleep(0.2)
        except Exception as e:
            # حتى لو فشلت هذه الاستفسارات، الاتصال ناجح ونكمل.
            pass

        self._app_label = self._name or "LEGO Hub"
        self.connection_state_observable.on_next(_ConnectionSubject.CONNECTED)
        self._emit_status()

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self._stop_program_flag()
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self.connection_state_observable.on_next(_ConnectionSubject.DISCONNECTED)

    # ---- I/O ----
    async def _write(self, data: bytes) -> None:
        if self._client is None or self._rx_uuid is None:
            raise RuntimeError("الروبوت غير متصل")
        try:
            await self._client.write_gatt_char(self._rx_uuid, data, response=False)
        except Exception:
            # Some backends/characteristics require response=True.
            await self._client.write_gatt_char(self._rx_uuid, data, response=True)

    def _on_notify(self, _sender, data: bytearray) -> None:
        try:
            self._handle_lwp(bytes(data))
        except Exception:
            pass

    def _handle_lwp(self, frame: bytes) -> None:
        # The first two bytes are the length (LE). Length INCLUDES the two header
        # bytes in some LEGO docs, but the hub implementation that arrives here
        # already drops them on some adapters — be tolerant.
        if len(frame) < 3:
            return
        if frame[0] == MSG_HUB_PROPERTIES:
            parsed = _lwp_parse_hub_property(frame)
            if not parsed:
                return
            prop, val = parsed
            if prop == PROP_FW_VERSION and len(val) >= 4:
                # BCD style: major.minor.bugfix.build (4 bytes)
                self._fw_parts = tuple(val[:4])
                self.fw_version = ".".join(str(b) for b in val[:4]) + " (official)"
            elif prop == PROP_NAME and val:
                try:
                    hub_name = val.decode("utf-8", "replace").rstrip("\x00").strip()
                    if hub_name:
                        self._name = hub_name
                        self._app_label = hub_name
                except Exception:
                    pass
            elif prop == PROP_BATTERY_VOLTAGE and val:
                # Single byte percentage, or voltage in some modes; clamp to 0-100
                self._battery_pct = max(0, min(100, int(val[0])))
            elif prop == PROP_BUTTON and val:
                self._button_pressed = bool(val[0])
            self._emit_status()

    # ---- status ----
    def _emit_status(self) -> None:
        flags = 0
        if self._program_running:
            flags |= (1 << 6)
        if self._battery_pct and self._battery_pct < 15:
            flags |= (1 << 0)
        if self._battery_pct and self._battery_pct < 5:
            flags |= (1 << 1)
        self.status_observable.on_next(flags)

    def _set_program_running(self, running: bool) -> None:
        self._program_running = running
        self._emit_status()

    def _stop_program_flag(self) -> None:
        self._program_running = False
        self._emit_status()

    # ---- program management over BLE ----
    # On pure BLE with official firmware, "run/donwload" of arbitrary Python is
    # NOT possible without a custom bootloader mode.  We therefore surface a
    # clear error telling the user to use USB if they want to send scripts.
    # We DO support stopping the running program via a Hub Action "Stop current
    # program" message (0x02 / 0x01) and starting the "stored" slot via the
    # "Start program" action.

    async def stop_user_program(self) -> None:
        # Try serial-side stop first.
        if self._serial is not None:
            self._serial.interrupt()
        # Note: the LWP3 public subset does not expose a generic "kill the running
        # MicroPython script" action; many programs stop via the left button on
        # the hub. Over pure BLE official firmware we do not send a VM kill
        # command (that requires an encrypted/authenticated channel on newer
        # firmware). We just update our UI flag.
        self._stop_program_flag()

    async def run(self, path, *, wait=False, print_output=False, line_handler=True) -> None:
        if path is not None:
            raise RuntimeError(
                "الفيرموير الرسمي عبر البلوتوث لا يدعم رفع بايثون تعسفي. "
                "استخدم «اتصال USB» لإرسال البرامج عبر منفذ REPL، "
                "أو ثبّت فيرموير Pybricks لرفع البرامج عبر البلوتوث."
            )
        # path=None ("start stored program") is not implementable over LWP3
        # public spec for arbitrary Python slots; tell the user.
        if self._serial is None:
            raise RuntimeError(
                "تشغيل البرنامج المخزن عبر بلوتوث الفيرموير الرسمي غير مدعوم في هذا الإصدار. "
                "استخدم «إرسال وتشغيل» عبر USB."
            )
        self._set_program_running(True)

    async def download(self, path) -> None:
        raise RuntimeError(
            "لا يمكن رفع البرامج إلى الفيرموير الرسمي عبر البلوتوث. "
            "استخدم «اتصال USB» (منفذ REPL) للرفع."
        )


# ---------------------------------------------------------------------------
# Official-firmware hub over USB (CDC serial — MicroPython raw REPL)
# ---------------------------------------------------------------------------
class OfficialUSBREPL:
    """Send Python programs over the SPIKE Prime USB CDC raw REPL.

    The hub's USB serial (VID 0x0694, PID 0x0009) exposes MicroPython REPL at
    115200 baud. Using CTRL-A (raw mode), we can paste a script, execute it
    with CTRL-D, and stream its stdout/stderr back. We also support soft
    interrupt (CTRL-C) to stop a running program.
    """
    CTRL_A = b"\x01"
    CTRL_B = b"\x02"
    CTRL_C = b"\x03"
    CTRL_D = b"\x04"
    PROMPT_RAW = b"raw REPL; CTRL-B to exit\r\n>"
    OK = b"OK"

    def __init__(self, port: str,
                 on_line: Optional[Callable[[bytes], None]] = None,
                 on_state: Optional[Callable[[int], None]] = None,
                 on_status: Optional[Callable[[int], None]] = None,
                 fw_version_callback: Optional[Callable[[str], None]] = None):
        if not SERIAL_OK:
            raise RuntimeError(f"مكتبة pyserial غير متوفرة: {SERIAL_ERR}")
        self.port = port
        self._line_cb = on_line
        self._state_cb = on_state
        self._status_cb = on_status
        self._fw_cb = fw_version_callback
        self._ser = None  # type: ignore
        self._reader_task: Optional[asyncio.Task] = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        # pyserial opens in a blocking way; do it in an executor.
        self._ser = await loop.run_in_executor(
            None, lambda: _serial.Serial(self.port, 115200, timeout=0.2))
        # Enter raw REPL
        await self._drain_input()
        self._ser.write(self.CTRL_C)
        await asyncio.sleep(0.1)
        await self._drain_input()
        self._ser.write(self.CTRL_A)
        await asyncio.sleep(0.2)
        head = await self._read_until(self.PROMPT_RAW, timeout=2.0)
        if self.PROMPT_RAW not in head:
            # Try leaving raw-paste mode if the hub was left in it.
            self._ser.write(self.CTRL_B)
            await asyncio.sleep(0.2)
            await self._drain_input()
            self._ser.write(self.CTRL_C + self.CTRL_A)
            await asyncio.sleep(0.3)
            head = await self._read_until(self.PROMPT_RAW, timeout=2.0)
            if self.PROMPT_RAW not in head:
                raise RuntimeError(
                    f"لم أستطع الدخول إلى raw REPL على {self.port}. "
                    "تأكد أن المنفذ هو REPL الخاص بالروبوت (الفيرموير الرسمي)."
                )
        if self._state_cb:
            self._state_cb(2)  # CONNECTED
        # Query firmware version (MicroPython banner or sys.implementation).
        await self._exec_snippet(
            "import sys,os\n"
            "try:\n"
            " print('FW:'+getattr(os,'uname',lambda:None)().version)\n"
            "except Exception as e:\n"
            " print('FW:? '+repr(e))\n",
            quiet=True,
        )
        # Start reader
        self._reader_task = asyncio.create_task(self._reader())

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except Exception:
                pass
            self._reader_task = None
        # Leave raw REPL
        if self._ser is not None:
            try:
                self._ser.write(self.CTRL_B)
            except Exception:
                pass
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        if self._state_cb:
            self._state_cb(0)

    def close(self) -> None:
        # Sync-safe helper for cleanup from non-async paths
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def interrupt(self) -> None:
        """Best-effort interrupt: send CTRL-C on the serial port."""
        if self._ser is not None:
            try:
                self._ser.write(self.CTRL_C)
            except Exception:
                pass

    # ---- reading ----
    async def _drain_input(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._ser.reset_input_buffer())

    async def _read_some(self, timeout: float = 0.2) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._ser.read(4096) if self._ser else b"")

    async def _read_until(self, marker: bytes, timeout: float = 2.0) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        buf = b""
        while deadline - loop.time() > 0 and marker not in buf:
            chunk = await self._read_some(0.2)
            if chunk:
                buf += chunk
            else:
                await asyncio.sleep(0.02)
        return buf

    async def _reader(self) -> None:
        loop = asyncio.get_running_loop()
        self._rx_buf = b""
        try:
            while not self._stop_event.is_set():
                chunk = await loop.run_in_executor(
                    None, lambda: self._ser.read(512) if self._ser else b"")
                if not chunk:
                    await asyncio.sleep(0.02)
                    continue
                self._feed_to_delivery(chunk)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            if getattr(self, "_rx_buf", b""):
                tail = self._rx_buf.rstrip(b"\r\n")
                if tail:
                    self._deliver_line_tail(tail)
                self._rx_buf = b""

    def _deliver_line_tail(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").strip()
        if not text or text in ("OK", ">"):
            return
        if self._line_cb:
            try:
                self._line_cb(text.encode("utf-8"))
            except Exception:
                pass

    # ---- executing code ----
    async def run_program(self, path: str, wait: bool = False) -> None:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        await self._exec_snippet(source, wait=wait)

    async def _exec_snippet(self, source: str, *, wait: bool = False,
                            quiet: bool = False) -> None:
        if self._ser is None:
            raise RuntimeError("منفذ السلك غير مفتوح")
        loop = asyncio.get_running_loop()
        # Interrupt any running program first.
        self._ser.write(self.CTRL_C)
        await asyncio.sleep(0.15)
        await self._drain_input()
        # Enter raw REPL
        self._ser.write(self.CTRL_A)
        await asyncio.sleep(0.2)
        prompt = await self._read_until(self.PROMPT_RAW, timeout=3.0)
        if self.PROMPT_RAW not in prompt:
            raise RuntimeError("تعذر الدخول إلى raw REPL (لم نرَ رسالة البدء)")
        # Clear accumulated reader state by draining the input buffer once more
        await loop.run_in_executor(None, lambda: self._ser.reset_input_buffer())

        # Send the script line-by-line; in raw REPL MicroPython accepts each
        # line ending with \r, then compiles+executes on CTRL-D.
        async with self._write_lock:
            for line in source.split("\n"):
                chunk = (line + "\r").encode("utf-8")
                self._ser.write(chunk)
                await asyncio.sleep(0.003)
            self._ser.write(self.CTRL_D)

        self._running = True
        if self._status_cb:
            self._status_cb(1 << 6)
        try:
            if wait:
                # After CTRL-D, MicroPython prints stdout/stderr followed by OK
                # and a fresh ">" raw prompt. Poll for ">" with preceding OK.
                deadline = loop.time() + 300.0
                tail = b""
                while loop.time() < deadline:
                    chunk = await loop.run_in_executor(
                        None, lambda: self._ser.read(4096) if self._ser else b"")
                    if chunk:
                        self._feed_to_delivery(chunk)
                        tail = (tail + chunk)[-32:]
                        # Finished when we see the raw prompt marker ">".
                        if b">" in tail:
                            break
                    else:
                        await asyncio.sleep(0.03)
                else:
                    raise TimeoutError("انتهت مهلة انتظار انتهاء البرنامج على الفيرموير الرسمي")
        finally:
            self._running = False
            if self._status_cb:
                self._status_cb(0)

    def _feed_to_delivery(self, chunk: bytes) -> None:
        """Split incoming raw bytes at newlines and pass to the line handler."""
        self._rx_buf = getattr(self, "_rx_buf", b"") + chunk
        while b"\n" in self._rx_buf:
            line, self._rx_buf = self._rx_buf.split(b"\n", 1)
            line = line.rstrip(b"\r")
            # Drop raw REPL control chatter: OK and lone ">"
            if line in (b"OK", b">", b""):
                continue
            # Some hubs emit \x04 or other control chars after command end; strip them
            line = bytes(b for b in line if b >= 0x20 or b in (0x09,))
            if not line:
                continue
            text = line.decode("utf-8", "replace")
            m = re.match(r"^FW:(.*)$", text.strip())
            if m and self._fw_cb:
                version = m.group(1).strip()
                if version and version != "?":
                    self._fw_cb(version + " (official USB)")
            if self._line_cb:
                try:
                    self._line_cb(text.encode("utf-8"))
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# High-level hub objects used by the controller
# ---------------------------------------------------------------------------
@dataclass
class OfficialUSBCDC:
    """Wraps OfficialUSBREPL with the same surface PybricksHubUSB exposes.

    This is what the controller creates when the user clicks «اتصال USB»
    and the device is running the OFFICIAL firmware (no 0xFF interface class,
    but the LEGO USB CDC serial port is present).
    """
    port: str

    def __post_init__(self):
        self.fw_version: str = "? (official USB)"
        self._app_label: str = "USB سلك (رسمي)"
        self._repl: Optional[OfficialUSBREPL] = None
        self.status_observable = _BehaviorSubject(0)
        self.connection_state_observable = _ConnectionSubject(
            _ConnectionSubject.DISCONNECTED)
        self.line_handler: Optional[Callable[[bytes], None]] = None
        self.print_output: bool = False
        self._enable_line_handler: bool = True
        self._app_is_official = True
        self._mpy_abi_version = 1
        self._device = None
        self._running = False

    async def connect(self) -> None:
        def on_line(raw: bytes) -> None:
            if self.line_handler:
                try:
                    self.line_handler(raw)
                except Exception:
                    pass

        def on_status(flags: int) -> None:
            self._running = bool(flags & (1 << 6))
            self.status_observable.on_next(flags)

        def on_fw(v: str) -> None:
            self.fw_version = v

        self._repl = OfficialUSBREPL(
            self.port,
            on_line=on_line,
            on_state=lambda s: self.connection_state_observable.on_next(s),
            on_status=on_status,
            fw_version_callback=on_fw,
        )
        await self._repl.connect()

    async def disconnect(self) -> None:
        if self._repl is not None:
            try:
                await self._repl.disconnect()
            except Exception:
                self._repl.close()
        self._repl = None
        self.connection_state_observable.on_next(_ConnectionSubject.DISCONNECTED)
        self.status_observable.on_next(0)

    async def stop_user_program(self) -> None:
        if self._repl is not None:
            self._repl.interrupt()
            self._running = False
            self.status_observable.on_next(0)

    async def run(self, path, *, wait=False, print_output=False, line_handler=True) -> None:
        if self._repl is None:
            raise RuntimeError("المنفذ غير متصل")
        if path is None:
            # Start currently-loaded program: on official firmware via USB we
            # simply re-enter the REPL; there is no separate "stored" slot
            # without filesystem writes.
            raise RuntimeError(
                "لا يوجد برنامج محمّل مسبقاً عبر هذا المسار؛ استخدم «إرسال وتشغيل»."
            )
        self.status_observable.on_next(1 << 6)
        try:
            await self._repl.run_program(path, wait=wait)
        finally:
            self.status_observable.on_next(0)

    async def download(self, path) -> None:
        # On official firmware there is no separate "download only" flash slot
        # we can write to via REPL without extra tooling; treat download as a
        # soft no-op (exec to a placeholder that exits immediately) would hide
        # user intent, so we upload-and-do-not-start via a mechanism that
        # compiles the file to .mpy on the hub filesystem. For simplicity,
        # we inform the user and execute nothing.
        raise RuntimeError(
            "الخيار «تحميل فقط» غير مدعوم على الفيرموير الرسمي. "
            "استخدم «إرسال وتشغيل»."
        )

    def write_gatt_char(self, *args, **kwargs):
        # No BLE here; the controller's transfer tracing checks for this method.
        raise RuntimeError("هذا الاتصال سلكي على الفيرموير الرسمي — لا GATT")


# ---------------------------------------------------------------------------
# Scanning helper: find an official-firmware hub over BLE
# ---------------------------------------------------------------------------
# Patterns in the local name commonly advertised by official firmware:
#   * "SPIKE"          (SPIKE Prime default pairing name: SPIKE Prime, Technic SMALL Hub)
#   * "HUB-"           (MINDSTORMS Robot Inventor default name prefix)
#   * "TrainHub"       (Powered Up hub)
#   * "Move Hub"       (Boost)
# We also fall back to manufacturer-data ID 0x0397 (LEGO System A/S).
_LEGO_NAME_PREFIXES = ("SPIKE", "HUB-", "HUB ", "TRAIN", "TRAINHUB", "MOVE", "CITY",
                       "DUPL", "TECHN", "LEGO", "MPY", "CONTROL", "INVENT", "WEDO",
                       "VIDIYO", "SUPER", "BATMAN", "MARIO")
_LEGO_MANUFACTURER_ID = 0x0397  # LEGO System A/S


async def find_official_device(name: Optional[str] = None, timeout: float = 20.0,
                               name_hint: Optional[str] = None,
                               debug_cb=None):
    """Return a bleak BLEDevice for an official LEGO hub, or None.

    يستخدم BleakScanner.discover(return_adv=True) — الواجهة الأكثر موثوقية
    عبر الأنظمة (تعمل على ويندوز/لينكس/ماك مع كل إصدارات Bleak).
    يطبّق نظام نقاط على كل جهة يجدها، ويُعيد أعلاها نقاطاً.
    """
    if not BLEAK_OK:
        raise RuntimeError(f"bleak غير متوفر: {BLEAK_ERR}")

    target_name = (name or name_hint or "").strip()

    def _get_name(dev, adv):
        for attr in ("local_name", "_local_name"):
            v = getattr(adv, attr, None)
            if v:
                return v
        return getattr(dev, "name", None) or ""

    def _hub_type_from_md(md_bytes: bytes) -> str:
        # First byte of LEGO manufacturer payload (after the 2-byte company ID)
        # is the Hub Type / system type ID per LEGO Wireless Protocol 3:
        #   0x41 WeDo 2.0 (tilt)
        #   0x42 Boost / Move Hub
        #   0x43 Powered Up 2-port Hub (Train)
        #   0x44 Hubs with handset
        #   0x45 Super Mario / Luigi
        #   0x46 Luigi standalone?
        #   0x59 Control+ (Technic Large Hub)
        #   0x60 SPIKE Prime Hub (SOMBRERO)
        #   0x61 MINDSTORMS Robot Inventor / SPIKE Prime (converged FW)
        #   0x80 Remote Control Handset
        if not md_bytes:
            return ""
        b = md_bytes[0]
        return {
            0x41: "WeDo", 0x42: "Move/Boost", 0x43: "Train Hub",
            0x44: "Handset", 0x45: "Mario", 0x46: "Luigi",
            0x59: "Technic L", 0x60: "SPIKE Prime",
            0x61: "SPIKE/Inventor", 0x80: "Remote",
        }.get(b, f"type=0x{b:02x}")

    def _make_entry(dev, adv):
        local = _get_name(dev, adv)
        uuids = list(getattr(adv, "service_uuids", []) or [])
        md = getattr(adv, "manufacturer_data", None) or {}
        rssi = getattr(adv, "rssi", None)
        txp = getattr(adv, "tx_power", None)
        lego_md = md.get(_LEGO_MANUFACTURER_ID)
        hub_type = _hub_type_from_md(lego_md) if lego_md else ""
        return {
            "name": local,
            "uuids": uuids,
            "has_lego_md": _LEGO_MANUFACTURER_ID in md,
            "hub_type": hub_type,
            "rssi": rssi if rssi is not None else txp,
        }

    # مسح واحد يُعيد كل الأجهزة وبيانات إعلانها.
    try:
        discovered = await BleakScanner.discover(
            timeout=timeout, return_adv=True,
            scanning_mode="active",
        )
    except TypeError:
        # إصدارات أقدم من bleak لا تقبل scanning_mode.
        discovered = await BleakScanner.discover(
            timeout=timeout, return_adv=True)

    # discovered is dict: address -> (BLEDevice, AdvertisementData)
    seen = {}
    for addr, (dev, adv) in discovered.items():
        entry = _make_entry(dev, adv)
        seen[addr] = entry
        if debug_cb is not None:
            try:
                debug_cb(addr, entry)
            except Exception:
                pass

    # مطابقة بالاسم إذا طُلب
    if target_name:
        tn_up = target_name.upper()
        for addr, entry in seen.items():
            nm = (entry.get("name") or "").upper()
            if nm == tn_up or (tn_up and nm.startswith(tn_up)):
                dev = discovered[addr][0]
                return dev
            if addr.replace(":", "").upper() == tn_up.replace(":", "").upper():
                return discovered[addr][0]

    # أنواع الروبوتات القابلة للبرمجة (بايثون) التي نهتم بالاتصال بها.
    _PROGRAMMABLE_HUB_TYPES = {"SPIKE Prime", "SPIKE/Inventor", "Technic L"}

    def _score(entry):
        nm = (entry.get("name") or "").upper()
        uuids_lc = [u.lower() for u in entry.get("uuids", [])]
        hub_type = entry.get("hub_type") or ""
        score = 0
        if LEGO_HUB_SERVICE_UUID in uuids_lc:
            score += 300
        if any(u.startswith("0000162") for u in uuids_lc):
            score += 100
        if entry.get("has_lego_md"):
            score += 80
        # الأفضلية للروبوتات القابلة للبرمجة (SPIKE/Inventor/Technic L).
        if hub_type in _PROGRAMMABLE_HUB_TYPES:
            score += 500
        # ألعاب/أجهزة تحكم — لا نريدها هدفاً للاتصال.
        if hub_type in ("Mario", "Luigi", "Handset", "Remote"):
            score -= 500
        elif hub_type in ("Move/Boost", "Train Hub", "WeDo"):
            score -= 50
        if any(nm.startswith(p) for p in _LEGO_NAME_PREFIXES):
            score += 40
        for frag in ("SPIKE", "PRIME", "INVENT", "TECHN"):
            if frag in nm:
                score += 30
        for frag in ("LEGO", "MOVE", "BOOST", "CITY", "POWERED",
                     "TRAIN", "WEDO", "VIDI"):
            if frag in nm:
                score += 10
        if "HUB" in nm and score > 50:
            score += 5
        if "PYBRICK" in nm:
            score -= 500
        if NUS_SERVICE_UUID in uuids_lc and score > 0:
            score += 20
        return score

    best = None
    best_score = 0
    for addr, entry in seen.items():
        s = _score(entry)
        if s > best_score:
            best_score = s
            best = (addr, entry)

    if best is None:
        return None
    return discovered[best[0]][0]



def diagnostic_scan(timeout: float = 8.0):
    """Perform a raw BLE scan and return a list of
    (address, name, has_lego_md, uuids, rssi). Uses BleakScanner.discover
    for best cross-platform reliability.
    """
    async def _run():
        if not BLEAK_OK:
            raise RuntimeError(f"bleak غير متوفر: {BLEAK_ERR}")
        try:
            discovered = await BleakScanner.discover(
                timeout=timeout, return_adv=True, scanning_mode="active")
        except TypeError:
            discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        results = []
        for addr, (dev, adv) in discovered.items():
            local = None
            for attr in ("local_name", "_local_name"):
                v = getattr(adv, attr, None)
                if v:
                    local = v; break
            if local is None:
                local = getattr(dev, "name", None) or ""
            uuids = list(getattr(adv, "service_uuids", []) or [])
            md = getattr(adv, "manufacturer_data", None) or {}
            rssi = getattr(adv, "rssi", None)
            txp = getattr(adv, "tx_power", None)
            results.append((addr, local, _LEGO_MANUFACTURER_ID in md, uuids,
                            rssi if rssi is not None else txp))
        return results
    return _run()


def is_official_firmware_usb(dev) -> bool:
    """Heuristic: a LEGO SPIKE hub whose interfaces DO NOT contain the
    Pybricks vendor-specific 0xFF class. That means it is on stock firmware
    (which exposes CDC-ACM only) or another non-Pybricks build.
    """
    try:
        cfgs = dev.configurations()
        if not cfgs:
            return False
        classes = {i.bInterfaceClass for i in cfgs[0]}
    except Exception:
        return False
    # Must look like a LEGO hub and NOT have Pybricks' 0xFF interface.
    return dev.idVendor == 0x0694 and dev.idProduct == 0x0009 and 0xFF not in classes
