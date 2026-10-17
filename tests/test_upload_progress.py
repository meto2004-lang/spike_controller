"""Byte-accurate progress without hardware, timers, or Tk worker-thread access."""
import asyncio
import concurrent.futures
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_controller import app_fixture, controller
from spike_core import validated_config
from spike_transfer import (TransferFailure, finish_upload_progress,
                            trace_upload_writes, upload_progress)

UUID = 'c5f50002-8280-46da-89f4-6d8051e4aeef'


def tearDownModule():
    if not controller._WORKER.loop.is_closed():
        controller._WORKER.loop.close()


class ModernHub:
    """Same framing as pybricksdev; the real library is also exercised below."""
    def __init__(self, program=bytes(range(100)), ble=True):
        self.program = program
        self._app_ble_upload_configured = ble
        self._mpy_abi_version = 0
        self.status_observable = SimpleNamespace(value=0)
        self.write_gatt_char = AsyncMock()

    async def download(self, path):
        await self.download_user_program(self.program)

    async def download_user_program(self, program):
        await self.write_gatt_char(UUID, struct.pack('<BI', 3, 0), response=True)
        for offset in range(0, len(program), 15):
            await self.write_gatt_char(UUID, struct.pack('<BI', 4, offset) + program[offset:offset + 15], response=True)
        await self.write_gatt_char(UUID, struct.pack('<BI', 3, len(program)), response=True)


class ProgressTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        log_dir = patch.object(controller, 'LOG_DIR', directory.name)
        log_dir.start()
        self.addCleanup(log_dir.stop)

    async def test_only_acknowledged_payload_excluding_headers_and_metadata(self):
        hub, events = ModernHub(), []
        original = hub.write_gatt_char
        with upload_progress(lambda *event: events.append(event)):
            with trace_upload_writes(hub):
                await hub.download('source.py')
            self.assertEqual(events, [(0, 100, False)] + [(n, 100, False) for n in (15, 30, 45, 60, 75, 90, 100)])
            finish_upload_progress()
            self.assertEqual(events[-1], (100, 100, True))
        self.assertIs(hub.write_gatt_char, original)
        self.assertNotIn('download_user_program', vars(hub))
        self.assertEqual(original.await_count, 9)
        self.assertTrue(all(call.kwargs == {'response': True} for call in original.call_args_list))
        self.assertTrue(all(len(call.args[1]) <= 20 for call in original.call_args_list))

    async def test_failed_begin_data_or_final_write_never_reports_completion_or_retries(self):
        for successful_writes in (0, 1, 2, 8):
            with self.subTest(successful_writes=successful_writes):
                hub, events = ModernHub(), []
                error = OSError('failed ACK')
                original = hub.write_gatt_char
                original.side_effect = [None] * successful_writes + [error]
                with upload_progress(lambda *event: events.append(event)):
                    with self.assertRaises(OSError) as caught, trace_upload_writes(hub):
                        await hub.download('source.py')
                    finish_upload_progress()
                self.assertIs(caught.exception, error)
                self.assertEqual(original.await_count, successful_writes + 1)
                self.assertEqual(events[-1][0], min(100, max(0, successful_writes - 1) * 15))
                self.assertFalse(any(event[2] for event in events))
                self.assertIs(hub.write_gatt_char, original)
                self.assertNotIn('download_user_program', vars(hub))

    async def test_cancellation_during_write_restores_exact_instance_attributes(self):
        hub, events = ModernHub(), []
        original_download = AsyncMock(side_effect=hub.download_user_program)
        hub.download_user_program = original_download
        original_write = hub.write_gatt_char
        original_write.side_effect = [None, None, asyncio.CancelledError()]
        with upload_progress(lambda *event: events.append(event)):
            with self.assertRaises(asyncio.CancelledError), trace_upload_writes(hub):
                await hub.download('source.py')
            finish_upload_progress()
        self.assertEqual(events[-1], (15, 100, False))
        self.assertIs(hub.download_user_program, original_download)
        self.assertIs(hub.write_gatt_char, original_write)
        finish_upload_progress()  # No leaked reporter after the context exits.
        self.assertFalse(any(event[2] for event in events))

    async def test_usb_gets_real_progress_without_ble_configuration_or_packet_changes(self):
        hub, events = ModernHub(ble=False), []
        with upload_progress(lambda *event: events.append(event)):
            with trace_upload_writes(hub):
                await hub.download('source.py')
            finish_upload_progress()
        self.assertEqual(events[-1], (100, 100, True))
        self.assertFalse(hub._app_ble_upload_configured)

    async def test_reporter_exception_never_changes_successful_robot_io(self):
        hub, callback = ModernHub(), Mock(side_effect=RuntimeError('UI closed'))
        with upload_progress(callback):
            with trace_upload_writes(hub):
                await hub.download('source.py')
            finish_upload_progress()
        callback.assert_called_once_with(0, 100, False)
        self.assertEqual(hub.write_gatt_char.await_count, 9)

    async def test_updates_are_bounded_to_integer_percentages_for_large_bundle(self):
        hub, events = ModernHub(b'x' * 30000), []
        with upload_progress(lambda *event: events.append(event)):
            with trace_upload_writes(hub):
                await hub.download('source.py')
            finish_upload_progress()
        self.assertEqual(len(events), 101)
        self.assertEqual(events[-1], (30000, 30000, True))

    async def test_nested_and_concurrent_task_contexts_do_not_mix(self):
        outer, inner = [], []
        async def transfer(events):
            hub = ModernHub()
            with upload_progress(lambda *event: events.append(event)):
                with trace_upload_writes(hub):
                    await asyncio.sleep(0)
                    await hub.download('source.py')
                finish_upload_progress()
        with upload_progress(lambda *event: outer.append(event)):
            await asyncio.gather(transfer(inner), transfer(inner))
            finish_upload_progress()
        self.assertEqual(outer, [])
        self.assertEqual(len(inner), 18)
        self.assertEqual(sum(event[2] for event in inner), 2)

    async def test_unknown_empty_or_nonsequential_upload_never_fakes_completion(self):
        for program, bad_offset in ((b'', None), (b'x', 1)):
            with self.subTest(program=program):
                hub, events = ModernHub(program), []
                if bad_offset is not None:
                    async def malformed(program):
                        await hub.write_gatt_char(UUID, struct.pack('<BI', 4, bad_offset) + program, response=True)
                        await hub.write_gatt_char(UUID, struct.pack('<BI', 3, len(program)), response=True)
                    hub.download_user_program = malformed
                with upload_progress(lambda *event: events.append(event)):
                    with trace_upload_writes(hub):
                        await hub.download('source.py')
                    finish_upload_progress()
                self.assertFalse(any(event[2] for event in events))

    async def test_real_library_ble_and_usb_framing_and_method_restoration(self):
        try:
            from pybricksdev.connections.pybricks import PybricksHubBLE, PybricksHubUSB
        except ImportError:
            self.skipTest('Runtime dependencies not installed')
        for cls, limit, ble in ((PybricksHubBLE, 20, True), (PybricksHubUSB, 64, False)):
            with self.subTest(transport=cls.__name__):
                hub = cls.__new__(cls)
                hub._max_write_size, hub._max_user_program_size = limit, 4096
                hub._app_ble_upload_configured = ble
                hub.write_gatt_char = AsyncMock()
                program, events = bytes(range(100)), []
                with patch('pybricksdev.connections.pybricks.tqdm'):
                    await hub.download_user_program(program)
                    baseline = hub.write_gatt_char.call_args_list[:]
                    hub.write_gatt_char.reset_mock()
                    with upload_progress(lambda *event: events.append(event)):
                        with trace_upload_writes(hub):
                            await hub.download_user_program(program)
                        finish_upload_progress()
                self.assertEqual(hub.write_gatt_char.call_args_list, baseline)
                self.assertEqual(events[-1], (len(program), len(program), True))
                self.assertNotIn('download_user_program', vars(hub))

    async def test_operation_only_enqueues_progress_and_completes_after_readiness(self):
        app, hub = app_fixture(), ModernHub()
        app._reset_upload_progress(7)
        async def ready(*args, **kwargs):
            self.assertEqual(app._upload_state, 'pending')  # No Tk mutation in worker.
            self.assertTrue(app._download_uncertain)
        with patch.object(controller, 'wait_hub_idle', side_effect=ready) as wait:
            await app._execute_op(7, lambda: app._download_program(hub, 'source.py'))
        wait.assert_awaited_once_with(hub, require_fresh=True)
        while not app.queue.empty():
            msg = app.queue.get_nowait()
            if msg[0] == 'upload_progress':
                app._handle_msg(msg)
        self.assertEqual(app._upload_state, 'complete')
        self.assertEqual(app._upload_percent, 100)
        self.assertFalse(app._download_uncertain)

    async def test_readiness_error_or_cancellation_after_final_ack_still_never_reaches_100(self):
        for error in (TimeoutError('no fresh status'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                app, hub = app_fixture(), ModernHub()
                app._reset_upload_progress(7)
                with patch.object(controller, 'wait_hub_idle', side_effect=error):
                    with self.assertRaises((TransferFailure, asyncio.CancelledError)):
                        await app._execute_op(7, lambda: app._download_program(hub, 'source.py'))
                while not app.queue.empty():
                    msg = app.queue.get_nowait()
                    if msg[0] == 'upload_progress':
                        app._handle_msg(msg)
                app._finish_progress_operation(7, error)
                self.assertEqual(app._upload_percent, 99)
                self.assertIn(app._upload_state, ('failed', 'cancelled'))
                self.assertTrue(app._download_uncertain)


class ProgressViewTests(unittest.TestCase):
    def setUp(self):
        self.app = app_fixture()
        self.app._reset_upload_progress(10)

    def test_byte_count_at_total_stays_99_until_explicit_success(self):
        self.app._handle_msg(('upload_progress', 10, 100, 100, False))
        self.assertEqual(self.app._upload_percent, 99)
        self.app._handle_msg(('upload_progress', 10, 100, 100, True))
        self.assertEqual(self.app._upload_percent, 100)

    def test_failure_and_cancellation_keep_confirmed_percentage_and_terminal_state(self):
        for error in (OSError(), asyncio.CancelledError(), concurrent.futures.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.app._reset_upload_progress(10)
                self.app._handle_upload_progress(10, 43, 100, False)
                self.app._finish_progress_operation(10, error)
                self.app._handle_upload_progress(10, 100, 100, True)
                self.assertEqual(self.app._upload_percent, 43)
                self.assertIn(self.app._upload_state, ('failed', 'cancelled'))

    def test_new_operation_rejects_old_progress_and_old_completion(self):
        self.app._reset_upload_progress(11)
        self.app._handle_upload_progress(10, 100, 100, True)
        self.app._finish_progress_operation(10, OSError())
        self.assertEqual(self.app._upload_percent, 0)
        self.assertEqual(self.app._upload_state, 'pending')

    def test_malformed_messages_do_not_advance_bar(self):
        for sent, total in ((True, 1), (0, False), (0, 0), (2, 1), (-1, 10), ('1', 10)):
            self.app._handle_upload_progress(10, sent, total, True)
            self.assertEqual(self.app._upload_percent, 0)

    def test_progress_is_monotonic_and_incomplete_complete_flag_is_not_100(self):
        self.app._handle_upload_progress(10, 50, 100, False)
        self.app._handle_upload_progress(10, 25, 100, True)
        self.assertEqual(self.app._upload_percent, 50)
        self.assertEqual(self.app._upload_state, 'uploading')

    def test_legacy_or_unmeasurable_success_never_invents_100(self):
        self.app._finish_progress_operation(10, None)
        self.assertEqual(self.app._upload_percent, 0)
        self.assertEqual(self.app._upload_state, 'unknown')

    def test_start_or_wait_error_does_not_undo_a_completed_upload(self):
        self.app._handle_upload_progress(10, 100, 100, True)
        self.app._finish_progress_operation(10, OSError('start failed'))
        self.assertEqual(self.app._upload_percent, 100)
        self.assertEqual(self.app._upload_state, 'complete')

    def test_restart_clears_previous_fill_and_failure_color(self):
        self.app._handle_upload_progress(10, 43, 100, False)
        self.app._finish_progress_operation(10, OSError())
        self.app._reset_upload_progress(11)
        self.app.upload_bar.configure.assert_called_with(value=0, style='Upload.Horizontal.TProgressbar')
        self.assertEqual(self.app._upload_percent, 0)

    def test_saved_editor_fraction_is_bounded_and_rejects_corrupt_preferences(self):
        defaults = {'editor_fraction': 0.78}
        self.assertEqual(validated_config({'editor_fraction': 0.65}, defaults)['editor_fraction'], 0.65)
        for value in (True, '0.6', None, -1, 0.1, 0.95, float('nan'), float('inf'), 10 ** 1000):
            with self.subTest(value=str(value)[:20]):
                self.assertEqual(validated_config({'editor_fraction': value}, defaults)['editor_fraction'], 0.78)
