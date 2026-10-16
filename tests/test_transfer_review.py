"""Regressions for firmware 4.1 suppressing status while receiving a file."""
import asyncio
import struct
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spike_transfer import (FILE_IO_IN_PROGRESS, USER_PROGRAM_RUNNING,
                            configure_ble_upload, needs_fresh_status,
                            trace_upload_writes, wait_hub_idle)
from test_controller import app_fixture, controller

UUID = 'c5f50002-8280-46da-89f4-6d8051e4aeef'


def tearDownModule():
    if not controller._WORKER.loop.is_closed():
        controller._WORKER.loop.close()


class ReplayStatus:
    """Synchronous cached replay, like Rx BehaviorSubject, plus real reports."""
    def __init__(self, value=0):
        self.value = value
        self.listeners = []
        self.subscriptions = asyncio.Queue()
        self.disposed = 0

    def subscribe(self, callback):
        self.listeners.append(callback)
        callback(self.value)
        self.subscriptions.put_nowait(True)
        def dispose():
            self.listeners.remove(callback)
            self.disposed += 1
        return SimpleNamespace(dispose=dispose)

    def emit(self, value):
        self.value = value
        for callback in list(self.listeners):
            callback(value)


class FreshReadyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_zero_is_not_a_fresh_idle_report(self):
        status = ReplayStatus()
        task = asyncio.create_task(wait_hub_idle(SimpleNamespace(status_observable=status), require_fresh=True))
        await status.subscriptions.get()
        self.assertFalse(task.done())
        # Firmware may remain silent while an earlier transfer is still active.
        status.emit(FILE_IO_IN_PROGRESS)
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        status.emit(0)
        await task
        self.assertEqual(status.listeners, [])
        self.assertEqual(status.disposed, 1)

    async def test_silent_firmware_times_out_even_if_cached_status_is_idle(self):
        status = ReplayStatus()
        with self.assertRaisesRegex(TimeoutError, 'fresh idle status'):
            await wait_hub_idle(SimpleNamespace(status_observable=status), timeout=0, require_fresh=True)
        self.assertEqual(status.disposed, 1)

    async def test_new_idle_report_can_ignore_unrelated_battery_flags(self):
        status = ReplayStatus(USER_PROGRAM_RUNNING)
        task = asyncio.create_task(wait_hub_idle(SimpleNamespace(status_observable=status), require_fresh=True))
        await status.subscriptions.get()
        status.emit(1)
        await task
        self.assertEqual(status.disposed, 1)

    async def test_latest_busy_report_invalidates_earlier_idle_candidate(self):
        status = ReplayStatus()
        task = asyncio.create_task(wait_hub_idle(SimpleNamespace(status_observable=status), require_fresh=True))
        await status.subscriptions.get()
        status.emit(0)
        status.emit(USER_PROGRAM_RUNNING)
        for _ in range(4):
            await asyncio.sleep(0)
        self.assertFalse(task.done())
        status.emit(0)
        await task

    async def test_invalid_new_report_fails_closed(self):
        for value in (None, False, '0'):
            with self.subTest(value=value):
                status = ReplayStatus()
                task = asyncio.create_task(wait_hub_idle(SimpleNamespace(status_observable=status), require_fresh=True))
                await status.subscriptions.get()
                status.emit(value)
                with self.assertRaises(RuntimeError):
                    await task
                self.assertEqual(status.disposed, 1)

    async def test_cancellation_disposes_subscription(self):
        status = ReplayStatus()
        task = asyncio.create_task(wait_hub_idle(SimpleNamespace(status_observable=status), require_fresh=True))
        await status.subscriptions.get()
        late_callback = status.listeners[0]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(status.disposed, 1)
        status.emit(0)  # A subsequent notification cannot resurrect the upload.
        with patch.object(asyncio.get_running_loop(), 'call_soon_threadsafe') as schedule:
            late_callback(0)  # A backend callback already in flight during disposal.
        schedule.assert_not_called()

    async def test_notification_from_other_thread_is_safe(self):
        status = ReplayStatus()
        task = asyncio.create_task(wait_hub_idle(SimpleNamespace(status_observable=status), require_fresh=True))
        await status.subscriptions.get()
        await asyncio.to_thread(status.emit, 0)
        await task

    async def test_real_behavior_subject_cached_replay_is_skipped(self):
        try:
            from reactivex.subject import BehaviorSubject
        except ImportError:
            self.skipTest('Runtime dependencies not installed')
        subject = BehaviorSubject(0)
        with self.assertRaises(TimeoutError):
            await wait_hub_idle(SimpleNamespace(status_observable=subject), timeout=0, require_fresh=True)
        self.assertEqual(len(subject.observers), 0)
        loop = asyncio.get_running_loop()
        loop.call_soon(subject.on_next, 0)
        await wait_hub_idle(SimpleNamespace(status_observable=subject), require_fresh=True)
        self.assertEqual(len(subject.observers), 0)
        subject.dispose()

    async def test_only_configured_modern_ble_uses_fresh_mode(self):
        modern = SimpleNamespace(_max_write_size=244, _mpy_abi_version=0)
        usb = SimpleNamespace(_max_write_size=512, _mpy_abi_version=0)
        legacy = SimpleNamespace(_max_write_size=20, _mpy_abi_version=6)
        configure_ble_upload(modern)
        configure_ble_upload(legacy)
        self.assertTrue(needs_fresh_status(modern))
        self.assertFalse(needs_fresh_status(usb))
        self.assertFalse(needs_fresh_status(legacy))
        self.assertEqual(usb._max_write_size, 512)


class WriteTraceTests(unittest.IsolatedAsyncioTestCase):
    def hub(self):
        return SimpleNamespace(_app_ble_upload_configured=True,
                               status_observable=SimpleNamespace(value=0),
                               write_gatt_char=AsyncMock())

    async def test_success_preserves_exact_calls_and_restores_override(self):
        hub = self.hub()
        original = hub.write_gatt_char
        packets = [struct.pack('<BI', 3, 0), struct.pack('<BI3s', 4, 0, b'abc'), struct.pack('<BI', 3, 3)]
        with trace_upload_writes(hub):
            for packet in packets:
                await hub.write_gatt_char(UUID, packet, response=True)
        self.assertIs(hub.write_gatt_char, original)
        self.assertEqual([call.args for call in original.await_args_list], [(UUID, p) for p in packets])
        self.assertTrue(all(call.kwargs == {'response': True} for call in original.await_args_list))

    async def test_error_note_identifies_begin_data_or_finalize_and_original_cause(self):
        for failed in range(3):
            with self.subTest(failed=failed):
                hub = self.hub()
                original = hub.write_gatt_char
                error = OSError('GATT 0x0e')
                original.side_effect = [None] * failed + [error]
                packets = [struct.pack('<BI', 3, 0), struct.pack('<BI3s', 4, 0, b'abc'), struct.pack('<BI', 3, 3)]
                with self.assertRaises(OSError) as result:
                    with trace_upload_writes(hub):
                        for packet in packets:
                            await hub.write_gatt_char(UUID, packet, response=True)
                self.assertIs(result.exception, error)
                self.assertIs(hub.write_gatt_char, original)
                self.assertEqual(original.await_count, failed + 1)
                note = error.__notes__[0]
                self.assertIn(f'command={ ("begin", "data", "finalize")[failed]}', note)
                self.assertIn('offset=0' if failed == 1 else f'size={0 if failed == 0 else 3}', note)
                self.assertIn(f'acked_bytes={3 if failed == 2 else 0}', note)
                self.assertIn('cached_flags=0x00000000', note)
                self.assertNotIn('abc', note)  # No code payload in diagnostics.

    async def test_long_gap_and_write_timings_are_recorded_without_sleep_or_retry(self):
        hub = self.hub()
        error = OSError('GATT 0x0e')
        hub.write_gatt_char.side_effect = [None, error]
        with patch('spike_transfer.monotonic', side_effect=[1.0, 1.1, 2.2, 3.3]):
            with self.assertRaises(OSError):
                with trace_upload_writes(hub):
                    await hub.write_gatt_char(UUID, struct.pack('<BI', 3, 0), response=True)
                    await hub.write_gatt_char(UUID, struct.pack('<BI1s', 4, 0, b'x'), response=True)
        self.assertIn('gap_ms=1100.0', error.__notes__[0])
        self.assertIn('write_ms=1100.0', error.__notes__[0])
        self.assertEqual(hub.write_gatt_char.await_count, 2)

    async def test_other_characteristic_and_command_are_passthrough(self):
        hub = self.hub()
        original = hub.write_gatt_char
        with trace_upload_writes(hub):
            await hub.write_gatt_char('other-uuid', b'payload', False)
            await hub.write_gatt_char(UUID, b'\x01', response=True)
        self.assertEqual(original.await_args_list[0].args, ('other-uuid', b'payload', False))
        self.assertEqual(original.await_args_list[1].args, (UUID, b'\x01'))
        self.assertIs(hub.write_gatt_char, original)

    async def test_usb_has_no_wrapper(self):
        hub = self.hub()
        del hub._app_ble_upload_configured
        original = hub.write_gatt_char
        with trace_upload_writes(hub):
            self.assertIs(hub.write_gatt_char, original)

    async def test_inherited_method_is_restored_without_instance_override(self):
        class Hub:
            _app_ble_upload_configured = True
            async def write_gatt_char(self, *args, **kwargs): pass
        hub = Hub()
        self.assertNotIn('write_gatt_char', vars(hub))
        with trace_upload_writes(hub):
            await hub.write_gatt_char(UUID, b'\x01', response=True)
        self.assertNotIn('write_gatt_char', vars(hub))
        self.assertIs(hub.write_gatt_char.__func__, Hub.write_gatt_char)

    async def test_cancellation_restores_writer_and_does_not_retry(self):
        hub = self.hub()
        original = hub.write_gatt_char
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        original.side_effect = blocked
        async def upload():
            with trace_upload_writes(hub):
                await hub.write_gatt_char(UUID, struct.pack('<BI', 3, 0), response=True)
        task = asyncio.create_task(upload())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIs(hub.write_gatt_char, original)
        self.assertEqual(original.await_count, 1)

    async def test_real_library_framing_is_unchanged_with_trace(self):
        try:
            from pybricksdev.connections.pybricks import PybricksHubBLE
        except ImportError:
            self.skipTest('Runtime dependencies not installed')
        hub = PybricksHubBLE.__new__(PybricksHubBLE)
        hub._max_write_size, hub._max_user_program_size = 244, 4096
        hub.write_gatt_char = AsyncMock()
        original = hub.write_gatt_char
        program = bytes(range(100))
        configure_ble_upload(hub)
        with patch('pybricksdev.connections.pybricks.tqdm'):
            await hub.download_user_program(program)
            expected = list(original.await_args_list)
            original.reset_mock()
            with trace_upload_writes(hub):
                await hub.download_user_program(program)
        self.assertEqual(original.await_args_list, expected)
        self.assertIs(hub.write_gatt_char, original)
        self.assertTrue(all(len(call.args[1]) <= 20 for call in expected))


class FreshPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_and_start_wait_for_real_idle_reports_outside_transfer(self):
        app = app_fixture()
        status = ReplayStatus(0)  # Cached idle hides an active prior transfer.
        hub = SimpleNamespace(_max_write_size=244, _mpy_abi_version=0,
                              status_observable=status, stop_user_program=AsyncMock(),
                              write_gatt_char=AsyncMock(), download=AsyncMock(), run=AsyncMock())
        configure_ble_upload(hub)
        app._worker_hub = hub
        async def download(path):
            self.assertEqual(status.listeners, [])  # Never wait for notifications mid-upload.
        hub.download.side_effect = download
        task = asyncio.create_task(app.op_send_run('program.py', False))
        await status.subscriptions.get()
        hub.download.assert_not_awaited()
        hub.run.assert_not_awaited()
        status.emit(0)  # Firmware resumes reports after the old transfer expires.
        await status.subscriptions.get()
        hub.download.assert_awaited_once_with('program.py')
        hub.run.assert_not_awaited()
        status.emit(0)  # Fresh idle after finalization, not the earlier cached value.
        await task
        hub.run.assert_awaited_once_with(None, wait=False, print_output=False, line_handler=True)
        self.assertFalse(app._download_uncertain)
        self.assertEqual(status.disposed, 2)

    async def test_packet_context_is_logged_without_start_or_retry(self):
        app = app_fixture()
        status = ReplayStatus()
        error = OSError('GATT 0x0e')
        writer = AsyncMock(side_effect=error)
        hub = SimpleNamespace(_max_write_size=244, _mpy_abi_version=0, fw_version='4.1.0',
                              status_observable=status, stop_user_program=AsyncMock(),
                              write_gatt_char=writer, run=AsyncMock())
        async def download(path):
            await hub.write_gatt_char(UUID, struct.pack('<BI', 3, 0), response=True)
        hub.download = download
        configure_ble_upload(hub)
        app._worker_hub = hub
        with tempfile.TemporaryDirectory() as directory, patch.object(controller, 'LOG_DIR', directory):
            task = asyncio.create_task(app.op_send_run('program.py', False))
            await status.subscriptions.get()
            status.emit(0)
            with self.assertRaises(controller.TransferFailure) as caught:
                await task
            self.assertIs(caught.exception.__cause__, error)
            log = (Path(directory) / 'transfer_errors.log').read_text(encoding='utf-8')
        self.assertIn('command=begin size=0', log)
        self.assertIn('cached_flags=0x00000000', log)
        self.assertIn('firmware=4.1.0', log)
        self.assertEqual(writer.await_count, 1)
        self.assertIs(hub.write_gatt_char, writer)
        self.assertTrue(app._download_uncertain)
        hub.run.assert_not_awaited()
        messages = []
        while not app.queue.empty():
            message = app.queue.get_nowait()
            if message[0] == 'console':
                messages.append(message[1])
        self.assertTrue(any('command=begin' in message for message in messages))

    async def test_cancelled_post_upload_ready_keeps_stored_program_blocked(self):
        app = app_fixture()
        status = ReplayStatus()
        hub = SimpleNamespace(_max_write_size=244, _mpy_abi_version=0,
                              status_observable=status, stop_user_program=AsyncMock(),
                              write_gatt_char=AsyncMock(), download=AsyncMock(), run=AsyncMock())
        configure_ble_upload(hub)
        app._worker_hub = hub
        task = asyncio.create_task(app.op_send_run('program.py', False))
        await status.subscriptions.get()
        status.emit(0)
        await status.subscriptions.get()
        hub.download.assert_awaited_once()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        status.emit(0)
        self.assertEqual(status.disposed, 2)
        self.assertTrue(app._download_uncertain)
        hub.run.assert_not_awaited()
        with self.assertRaises(RuntimeError):
            await app.op_run_stored()


if __name__ == '__main__':
    unittest.main()
