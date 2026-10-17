"""Import actual application code; replace only GUI/hardware at their boundary."""
import asyncio
import importlib
from pathlib import Path
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from spike_core import EventQueue, MAX_LINE_BYTES
from spike_documents import EditorDocument
# Keep this shared module cached when the no-Tk import mock restores sys.modules.
import spike_transfer  # noqa: F401

try:
    import tkinter  # noqa: F401
except ImportError:
    tkinter = types.ModuleType('tkinter')
    tkinter.filedialog = MagicMock()
    tkinter.messagebox = MagicMock()
    tkinter.ttk = MagicMock()
    with patch.dict(sys.modules, {'tkinter': tkinter}):
        controller = importlib.import_module('spike_controller')
else:
    controller = importlib.import_module('spike_controller')


def tearDownModule():
    if not controller._WORKER.loop.is_closed():
        controller._WORKER.loop.close()


class Editor:
    def __init__(self, text='robot.straight(100)', dirty=True):
        self.text, self.dirty = text, dirty
    def get(self, *args):
        if len(args) == 2 and str(args[1]).endswith('.end'):
            line = int(str(args[0]).split('.')[0])
            lines = self.text.split('\n')
            return lines[line - 1] if line <= len(lines) else ''
        return self.text
    def edit_modified(self, value=None):
        if value is not None: self.dirty = value
        return self.dirty
    def edit_reset(self): pass
    def delete(self, *args): self.text = ''
    def insert(self, index, value, *tags): self.text += value
    def focus_set(self): pass
    def yview(self): return (0.0, 1.0)
    def index(self, *args):
        if args == ('insert',): return getattr(self, 'cursor', '1.0')
        return f'{len(self.text.splitlines()) + 1}.0'
    def see(self, *args): pass
    def configure(self, **kwargs): pass
    def yview_moveto(self, *args): pass
    def tag_remove(self, *args): pass
    def tag_add(self, *args): pass


class Notebook:
    def __init__(self):
        self.frames = []
        self.selected = None
        self.options = {}
    def add(self, frame, **kwargs):
        self.frames.append(frame)
        self.options[frame] = kwargs
        if self.selected is None: self.selected = frame
    def select(self, frame=None):
        if frame is not None: self.selected = frame
        return str(self.selected) if self.selected is not None else ''
    def tab(self, frame, **kwargs): self.options[frame].update(kwargs)
    def forget(self, frame):
        self.frames.remove(frame)
        self.options.pop(frame)
        if self.selected is frame:
            self.selected = self.frames[0] if self.frames else None


def app_fixture():
    app = controller.App.__new__(controller.App)
    app.root = MagicMock()
    app.documents = []
    app.active_document = None
    app._untitled_counter = 1
    app._sending_path = None
    app._send_op_id = None
    app._pressed_close = None
    app.notebook = Notebook()
    def create_tab(content, path, saved, number, suggested_name):
        return EditorDocument(MagicMock(), Editor(content, False), MagicMock(), MagicMock(),
                              path, content if saved else '', number, suggested_name)
    app._create_editor_tab = create_tab
    initial = EditorDocument(MagicMock(), Editor(), MagicMock(), MagicMock(), 'training.py', 'old')
    app.documents.append(initial)
    app.active_document = initial
    app.notebook.add(initial.frame)
    app.console_w = Editor('')
    app.lbl_file = MagicMock()
    app.lbl_send_target = MagicMock()
    app.upload_bar = MagicMock()
    app.lbl_upload = MagicMock()
    app._upload_progress_id = None
    app._upload_state = 'idle'
    app._upload_percent = 0
    app.options_menu = MagicMock()
    app.tools_menu = MagicMock()
    app._action_entries = {name: (app.options_menu, index) for index, name in
                           enumerate(('usb', 'serial', 'sendwait', 'dl', 'stored'))}
    app.file_path_var = MagicMock()
    app._toolbar_images = {'serial': ('serial-image',), 'stop': ('stop-image',)}
    app.lbl_rx = MagicMock()
    app.lbl_state = MagicMock()
    app.lbl_prog = MagicMock()
    app.btn_serial = MagicMock()
    app.queue = EventQueue()
    app.console_raw = []
    app.training_lines = []
    app.training_overflow = False
    app.training_revision = 0
    app._disconnecting_hub = None
    app.session_id = 1
    app.rx_count = 0
    app._saved_text = 'old'
    app.current_path = 'training.py'
    app._hl_job = None
    app._poll_job = None
    app._closing = False
    app._confirming_close = False
    app.serial_stop = threading.Event()
    app.serial_running = False
    app.serial_thread = None
    app.session_file = None
    app.cfg = {'recent': []}
    app._v_ts = MagicMock()
    app._v_ts.get.return_value = False
    app._v_al = MagicMock()
    app._v_al.get.return_value = False
    app._refresh_buttons = MagicMock()
    app._update_lineno = MagicMock()
    app._operations = {}
    app._next_op = 0
    app._async_tasks = {}
    app._op_lock = None
    app.busy = False
    app.connected = False
    app.hub = None
    app._worker_hub = None
    app._download_uncertain = False
    app.capture = None
    app.ERR = 'red'
    app.OK = 'green'
    app.WARN = 'yellow'
    return app


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.app = app_fixture()
        self.dialog = patch.object(controller, 'messagebox')
        self.box = self.dialog.start()
        self.addCleanup(self.dialog.stop)

    def test_modified_callback_does_not_erase_document_dirty_state(self):
        self.app._on_editor_modified()
        self.assertFalse(self.app.editor.edit_modified())
        self.assertTrue(self.app._editor_dirty())
        self.assertTrue(self.app.lbl_file.configure.call_args.kwargs['text'].startswith('*'))

    def test_empty_document_is_dirty_when_saved_content_was_not_empty(self):
        self.app.editor.text = ''
        self.assertTrue(self.app._editor_dirty())

    def test_undo_to_saved_text_is_clean(self):
        self.app.editor.text = 'old'
        self.assertFalse(self.app._editor_dirty())

    def test_save_before_upload_matches_visible_text(self):
        with tempfile.TemporaryDirectory() as d:
            self.app.current_path = str(Path(d) / 'training.py')
            Path(self.app.current_path).write_text('old')
            path = self.app._require_saved_file()
            self.assertEqual(Path(path).read_text(encoding="utf-8"), self.app.editor.text)
            self.assertFalse(self.app._editor_dirty())

    def test_failed_save_prevents_upload(self):
        with patch.object(controller, 'atomic_write', side_effect=OSError('full')):
            self.assertIsNone(self.app._require_saved_file())
        self.assertTrue(self.app._editor_dirty())

    def test_cancel_save_as_prevents_upload(self):
        self.app.current_path = None
        with patch.object(controller.filedialog, 'asksaveasfilename', return_value=''):
            self.assertIsNone(self.app._require_saved_file())

    def test_open_creates_tab_without_discarding_current_edits(self):
        previous = self.app.active_document
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'other.py'
            path.write_text('print(123)', encoding='utf-8')
            self.assertTrue(self.app._open_path(str(path)))
        self.assertIn(previous, self.app.documents)
        self.assertEqual(previous.editor.text, 'robot.straight(100)')
        self.assertTrue(previous.dirty)
        self.assertEqual(len(self.app.documents), 2)
        self.box.askyesnocancel.assert_not_called()

    def test_copy_race_becomes_unsaved_document(self):
        race = controller.RaceGenWindow.__new__(controller.RaceGenWindow)
        race.app = self.app
        race.preview_snapshot = self.app._training_snapshot()
        race.preview = Editor('robot.turn(90)')
        race.win = MagicMock()
        self.box.askyesno.return_value = True
        race.to_editor()
        self.assertIsNone(self.app.current_path)
        self.assertTrue(self.app._editor_dirty())
        self.assertEqual(self.app.editor.text, 'robot.turn(90)')

    def test_copy_race_preserves_existing_file_without_discard_prompt(self):
        previous = self.app.active_document
        race = controller.RaceGenWindow.__new__(controller.RaceGenWindow)
        race.app, race.preview, race.win = self.app, Editor('new'), MagicMock()
        race.preview_snapshot = self.app._training_snapshot()
        race.to_editor()
        self.assertEqual(previous.path, 'training.py')
        self.assertEqual(previous.editor.text, 'robot.straight(100)')
        self.assertIn(previous, self.app.documents)
        self.assertIsNone(self.app.current_path)
        self.box.askyesnocancel.assert_not_called()

    def test_open_file_updates_header_window_title_and_copyable_path(self):
        self.box.askyesno.return_value = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'training.py'
            path.write_text('print("ready")', encoding='utf-8')
            self.assertTrue(self.app._open_path(str(path)))
        self.app.lbl_file.configure.assert_called_with(text='training.py')
        self.app.root.title.assert_called_with('training.py — Hyena Control')
        self.app.file_path_var.set.assert_called_with(os.path.abspath(path))

    def test_open_arabic_filename_shapes_header_but_not_title_or_path(self):
        self.box.askyesno.return_value = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'تدريب الروبوت.py'
            path.write_text('print("ready")', encoding='utf-8')
            self.assertTrue(self.app._open_path(str(path)))
        self.app.lbl_file.configure.assert_called_with(text=controller.to_visual(path.name))
        self.app.root.title.assert_called_with(f'{path.name} — Hyena Control')
        self.app.file_path_var.set.assert_called_with(os.path.abspath(path))

    def test_saving_as_new_filename_updates_all_document_displays(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'new-name.py'
            self.assertTrue(self.app._save_to(str(path)))
        self.app.lbl_file.configure.assert_called_with(text='new-name.py')
        self.app.root.title.assert_called_with('new-name.py — Hyena Control')
        self.app.file_path_var.set.assert_called_with(os.path.abspath(path))

    def test_dirty_marker_is_shown_in_header_and_window_title(self):
        self.app._set_file_label()
        self.app.lbl_file.configure.assert_called_with(text='* training.py')
        self.app.root.title.assert_called_with('* training.py — Hyena Control')

    def test_new_document_clears_previous_filename_and_path(self):
        self.box.askyesno.return_value = True
        self.app.action_new()
        self.app.lbl_file.configure.assert_called_with(text=controller.to_visual('بدون اسم 2'))
        self.app.root.title.assert_called_with('بدون اسم 2 — Hyena Control')
        self.app.file_path_var.set.assert_called_with('')

    def test_cancelled_open_keeps_document_identity(self):
        self.app._set_file_label()
        self.app.root.title.reset_mock()
        self.app.file_path_var.set.reset_mock()
        with patch.object(controller.filedialog, 'askopenfilename', return_value=''):
            self.app.action_open()
        self.app.root.title.assert_not_called()
        self.app.file_path_var.set.assert_not_called()
        self.assertEqual(self.app.current_path, 'training.py')

    def test_open_failure_keeps_previous_document_identity(self):
        self.box.askyesno.return_value = True
        with patch('builtins.open', side_effect=OSError('unreadable')):
            self.assertFalse(self.app._open_path('missing.py'))
        self.app.root.title.assert_not_called()
        self.app.file_path_var.set.assert_not_called()
        self.assertEqual(self.app.current_path, 'training.py')

    def test_copy_race_replaces_document_identity_with_unsaved_file(self):
        race = controller.RaceGenWindow.__new__(controller.RaceGenWindow)
        race.app, race.preview, race.win = self.app, Editor('robot.turn(90)'), MagicMock()
        race.preview_snapshot = self.app._training_snapshot()
        self.box.askyesno.return_value = True
        race.to_editor()
        self.app.lbl_file.configure.assert_called_with(text='* ' + controller.to_visual(self.app.active_document.name))
        self.app.root.title.assert_called_with(f'* {self.app.active_document.name} — Hyena Control')
        self.app.file_path_var.set.assert_called_with('')


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.app = app_fixture()
        self.dialog = patch.object(controller, 'messagebox')
        self.box = self.dialog.start()
        self.addCleanup(self.dialog.stop)

    def preview(self):
        race = controller.RaceGenWindow.__new__(controller.RaceGenWindow)
        race.app, race.preview = self.app, Editor('stale preview')
        race.refresh()
        return race.preview.text

    def test_clear_resets_data_count_and_pending_output(self):
        self.app.console('robot.straight(200)')
        self.app.queue.put(('console', 'robot.turn(90)', 'out'))
        self.box.askyesno.return_value = True
        self.app.action_clear_console()
        self.assertEqual(self.app.console_raw, [])
        self.assertEqual(self.app.training_lines, [])
        self.assertEqual(self.app.rx_count, 0)
        self.assertEqual(self.app.session_id, 2)
        self.assertTrue(self.app.queue.empty())

    def test_clear_can_be_cancelled(self):
        self.app.training_lines = ['robot.turn(90)']
        self.box.askyesno.return_value = False
        self.app.action_clear_console()
        self.assertEqual(self.app.training_lines, ['robot.turn(90)'])

    def test_serial_label_only_affects_display_not_training(self):
        self.app._handle_msg(('console', 'robot.straight(100)', 'out', 'serial'))
        self.assertIn('[سلك]', self.app.console_raw[-1])
        self.assertEqual(self.app.training_lines, ['robot.straight(100)'])
        self.assertIn('robot.straight(100)', self.preview())

    def test_arabic_serial_label_does_not_reorder_python_punctuation(self):
        self.app.console('robot.straight(100)', 'out', source='serial')
        self.assertIn('robot.straight(100)', self.app.console_w.text)
        self.assertTrue(self.app.console_w.text.endswith('robot.straight(100)\n'))

    def test_app_messages_do_not_become_training(self):
        self.app.console('robot.straight(200)', 'info')
        self.assertEqual(self.app.training_lines, [])

    def test_rejected_command_clears_stale_preview(self):
        self.app.training_lines = ["robot.straight(100); print('bad')"]
        self.assertEqual(self.preview(), '')
        self.box.showerror.assert_called_once()

    def test_stale_preview_cannot_be_saved_after_starting_new_session(self):
        race = controller.RaceGenWindow.__new__(controller.RaceGenWindow)
        race.app, race.preview = self.app, Editor('old race')
        race.preview_snapshot = (self.app.session_id - 1, 0, 0)
        self.assertFalse(race._preview_current())
        self.box.showerror.assert_called_once()

    def test_lost_data_blocks_race(self):
        self.app.training_lines = ['robot.turn(90)']
        self.app.training_overflow = True
        self.assertEqual(self.preview(), '')
        self.box.showerror.assert_called_once()

    def test_intentional_repeated_commands_are_preserved(self):
        self.app.training_lines = ['robot.turn(90)', 'robot.turn(90)']
        self.assertEqual(self.preview().count('robot.turn(90)'), 2)

    def test_poll_continues_after_bad_message(self):
        self.app.queue.put(('console',))
        self.app.queue.put(('console', 'robot.turn(90)', 'out'))
        with patch.object(controller.traceback, 'print_exc'):
            self.app._poll()
        self.assertTrue(self.app.training_overflow)
        self.assertEqual(self.app.training_lines, ['robot.turn(90)'])
        self.app.root.after.assert_called_once()

    def test_overload_invalidates_training_but_keeps_completion(self):
        self.app.queue = EventQueue(limit=1)
        self.app.queue.put(('console', 'robot.turn(90)', 'out'))
        self.app.queue.put(('console', 'dropped', 'out'))
        self.app._poll()
        self.assertTrue(self.app.training_overflow)

    def make_race(self):
        self.app.console('robot.turn(90)', 'out')
        race = controller.RaceGenWindow.__new__(controller.RaceGenWindow)
        race.app, race.preview, race.win = self.app, Editor(''), MagicMock()
        race.refresh()
        return race

    def test_preview_invalidated_by_new_output_in_same_session(self):
        race = self.make_race()
        self.app.console('robot.straight(100)', 'out')
        self.assertFalse(race._preview_current())

    def test_unpolled_output_invalidates_preview(self):
        race = self.make_race()
        self.app.queue.put(('console', 'robot.straight(100)', 'out'))
        self.assertFalse(race._preview_current())

    def test_unpolled_loss_blocks_generation(self):
        self.app.training_lines = ['robot.turn(90)']
        self.app.queue.put(('data_loss', 'lost'))
        self.assertEqual(self.preview(), '')

    def test_pending_output_blocks_generation_until_processed(self):
        self.app.queue.put(('console', 'robot.turn(90)', 'out'))
        self.assertEqual(self.preview(), '')
        self.app._poll()
        self.assertIn('robot.turn(90)', self.preview())

    def test_save_dialog_receiving_new_data_does_not_write_stale_race(self):
        race = self.make_race()
        def choose_path(**kwargs):
            self.app.console('robot.straight(100)', 'out')
            return 'race.py'
        with patch.object(controller.filedialog, 'asksaveasfilename', side_effect=choose_path), \
                patch.object(controller, 'atomic_write') as write:
            race.save()
        write.assert_not_called()

    def test_race_new_tab_keeps_training_editor_intact(self):
        race = self.make_race()
        original_document = self.app.active_document
        original = original_document.editor.text
        race.to_editor()
        self.assertEqual(original_document.editor.text, original)
        self.assertIn(original_document, self.app.documents)
        self.assertIsNot(self.app.active_document, original_document)
        self.box.askyesnocancel.assert_not_called()

    def test_expected_disconnect_preserves_complete_training(self):
        hub = self.app.hub = object()
        self.app.connected = True
        self.app._handle_msg(('hub_state', 'disconnected', hub, True))
        self.assertFalse(self.app.training_overflow)
        self.assertFalse(self.app.connected)

    def test_unexpected_disconnect_invalidates_training(self):
        hub = self.app.hub = object()
        self.app.connected = True
        self.app._handle_msg(('hub_state', 'disconnected', hub, False))
        self.assertTrue(self.app.training_overflow)


class SerialAndCaptureTests(unittest.TestCase):
    def run_serial(self, reads):
        app = app_fixture()
        port = MagicMock()
        def read(size):
            value = reads.pop(0)
            if not reads:
                app.serial_stop.set()
            if isinstance(value, Exception):
                raise value
            return value
        port.read.side_effect = read
        module = types.SimpleNamespace(Serial=MagicMock(return_value=port))
        with patch.dict(sys.modules, {'serial': module}):
            app._serial_reader('MOCK', app.serial_stop)
        port.close.assert_called_once()
        return app, list(app.queue.queue)

    def test_serial_announces_start_stop_and_keeps_partial_line(self):
        app, messages = self.run_serial([b'robot.straight(100)\nrobot.stop()'])
        self.assertIn(('serial_state', True, app.serial_stop), messages)
        self.assertIn(('serial_state', False, app.serial_stop), messages)
        self.assertIn(('console', 'robot.stop()', 'out', 'serial'), messages)

    def test_read_failure_is_reported_and_port_closed(self):
        app, messages = self.run_serial([OSError('disconnected')])
        self.assertTrue(any(m[0] == 'data_loss' for m in messages))
        self.assertIn(('serial_state', False, app.serial_stop), messages)

    def test_long_serial_lines_are_bounded_and_rejected(self):
        chunks = [b'x' * 256] * (MAX_LINE_BYTES // 256 + 1)
        chunks.append(b'robot.turn(1)\nrobot.stop()\n')
        app, messages = self.run_serial(chunks)
        self.assertTrue(any(m[0] == 'data_loss' for m in messages))
        self.assertFalse(any(m[0] == 'console' and 'robot.turn(1)' in m[1] for m in messages))
        self.assertIn(('console', 'robot.stop()', 'out', 'serial'), messages)

    def test_second_click_stops_starting_reader_without_starting_another(self):
        app = app_fixture()
        app._find_hub_port = lambda: 'MOCK'
        with patch.object(controller.threading, 'Thread') as thread:
            app.action_serial_toggle()
            app.action_serial_toggle()
            thread.assert_called_once()
            self.assertTrue(app.serial_stop.is_set())

    def test_old_serial_completion_cannot_reset_new_reader(self):
        app = app_fixture()
        app.serial_running = True
        app._handle_msg(('serial_state', False, threading.Event()))
        self.assertTrue(app.serial_running)

    def test_robot_file_does_not_overwrite_existing_log(self):
        with tempfile.TemporaryDirectory() as d, patch.object(controller, 'LOG_DIR', d):
            path = Path(d) / 'important.txt'
            path.write_text('keep')
            cap = controller.HubCapture(EventQueue())
            cap.handler(b'PB_OF:important.txt')
            cap.handler(b'new')
            cap.handler(b'PB_EOF')
            self.assertEqual(path.read_text(encoding="utf-8"), 'keep')
            self.assertNotEqual(cap.log_path, str(path))
            self.assertEqual(Path(cap.log_path).read_text(encoding="utf-8"), 'new\n')

    def test_new_robot_file_closes_previous_and_protocol_requires_prefix(self):
        with tempfile.TemporaryDirectory() as d, patch.object(controller, 'LOG_DIR', d):
            out = EventQueue()
            cap = controller.HubCapture(out)
            cap.handler(b'PB_OF:first.txt')
            first = cap.log_file
            cap.handler(b'PB_OF:second.txt')
            self.assertTrue(first.closed)
            cap.handler(b'print PB_EOF inside text')
            self.assertIsNotNone(cap.log_file)
            cap.close()
            cap.close()
            self.assertIsNone(cap.log_file)

    def test_disconnect_closes_capture_in_worker_callback(self):
        app = app_fixture()
        hub = MagicMock()
        states = types.SimpleNamespace(DISCONNECTED='disconnected')
        with patch.object(controller, 'ConnectionState', states, create=True):
            app._install_capture(hub)
            cap = app.capture
            cap.log_file = MagicMock()
            app._worker_hub = hub
            callback = hub.connection_state_observable.subscribe.call_args.args[0]
            callback('disconnected')
            self.assertIsNone(cap.log_file)
            self.assertIsNone(app._worker_hub)
            self.assertEqual(cap.subs, [])

    def test_robot_log_size_limit_closes_file_but_keeps_live_output(self):
        with tempfile.TemporaryDirectory() as d, patch.object(controller, 'LOG_DIR', d):
            out = EventQueue()
            cap = controller.HubCapture(out)
            cap.handler(b'PB_OF:test.txt')
            stream = cap.log_file
            with patch.object(controller, 'MAX_HUB_LOG_BYTES', 1):
                cap.handler(b'robot.stop()')
            self.assertTrue(stream.closed)
            self.assertIsNone(cap.log_file)
            self.assertIn(('console', 'robot.stop()', 'out'), list(out.queue))

    def test_robot_write_error_is_reported_not_raised_into_transport(self):
        out = EventQueue()
        cap = controller.HubCapture(out)
        stream = cap.log_file = MagicMock()
        stream.write.side_effect = OSError('full')
        cap.handler(b'robot.stop()')
        stream.close.assert_called_once()
        self.assertIsNone(cap.log_file)
        self.assertTrue(any(m[0] == 'console' and m[2] == 'err' for m in out.queue))


class OperationUITests(unittest.TestCase):
    def test_one_completion_keeps_busy_while_other_operation_exists(self):
        app = app_fixture()
        app._operations = {1: ('send_wait', None), 2: ('stop', None)}
        app.busy = True
        app._handle_msg(('op_done', 2, 'stop', None, None))
        self.assertTrue(app.busy)
        self.assertIn(1, app._operations)

    def test_rejected_operation_never_constructs_coroutine(self):
        app = app_fixture()
        factory = MagicMock()
        with patch.object(controller, 'PB_OK', False):
            self.assertFalse(app._start_op('test', factory))
        factory.assert_not_called()

    def test_busy_operation_never_constructs_coroutine(self):
        app = app_fixture()
        app.busy = True
        factory = MagicMock()
        running_worker = types.SimpleNamespace(loop=types.SimpleNamespace(is_running=lambda: True))
        with patch.object(controller, 'PB_OK', True), patch.object(controller, '_WORKER', running_worker):
            self.assertFalse(app._start_op('test', factory))
        factory.assert_not_called()

    def test_stale_hub_notifications_are_ignored(self):
        app = app_fixture()
        app.connected = True
        app.hub = object()
        app._handle_msg(('hub_state', 'disconnected', object()))
        self.assertTrue(app.connected)


class ConsoleShortcutTests(unittest.TestCase):
    def test_shortcuts_are_dispatched_once_before_text_class_bindings(self):
        cases = [
            ('F5', 0, 'action_send_run'), ('F5', 4, 'action_send_wait'),
            ('s', 4, 'action_save'), ('S', 5, 'action_save_as'),
            ('n', 4, 'action_new'), ('o', 4, 'action_open'),
            ('g', 4, 'action_race_gen'), ('l', 4, 'action_clear_console'),
            ('Escape', 0, 'action_stop'), ('F1', 0, 'action_help'),
            ('C', 5, '_copy_console'),
        ]
        for key, state, method in cases:
            with self.subTest(key=key, state=state):
                app = app_fixture()
                action = MagicMock()
                setattr(app, method, action)
                event = types.SimpleNamespace(keysym=key, state=state)
                self.assertEqual(app._console_key(event), 'break')
                action.assert_called_once_with()

    def test_editing_shortcuts_remain_blocked(self):
        app = app_fixture()
        for key in ('v', 'x', 'BackSpace', 'Delete'):
            self.assertEqual(app._console_key(types.SimpleNamespace(keysym=key, state=4)), 'break')

    def test_no_console_actions_while_closing(self):
        app = app_fixture()
        app._closing = True
        app.action_send_run = MagicMock()
        self.assertEqual(app._console_key(types.SimpleNamespace(keysym='F5', state=0)), 'break')
        app.action_send_run.assert_not_called()


class StartupTests(unittest.TestCase):
    def test_startup_failure_returns_nonzero_and_closes_worker_loop(self):
        for entry_point in (controller.main, controller.selftest):
            with self.subTest(entry_point=entry_point.__name__):
                worker = controller.Worker()
                root = MagicMock()
                with patch.object(controller, '_WORKER', worker), \
                        patch.object(controller, 'PB_OK', False), \
                        patch.object(controller.tk, 'Tk', return_value=root, create=True), \
                        patch.object(controller, 'App', side_effect=OSError('not writable')), \
                        patch.object(controller, 'messagebox'), \
                        patch.object(controller.traceback, 'print_exc'):
                    with self.assertRaises(SystemExit) as result:
                        entry_point()
                self.assertEqual(result.exception.code, 1)
                root.destroy.assert_called_once()
                self.assertTrue(worker.loop.is_closed())

    def test_display_failure_closes_running_worker(self):
        worker = controller.Worker()
        with patch.object(controller, '_WORKER', worker), \
                patch.object(controller, 'PB_OK', True), \
                patch.object(controller.tk, 'Tk', side_effect=RuntimeError('no display'), create=True), \
                patch.object(controller.traceback, 'print_exc'):
            with self.assertRaises(SystemExit):
                controller.selftest()
        self.assertFalse(worker.thread.is_alive())
        self.assertTrue(worker.loop.is_closed())

    def test_stopped_worker_rejects_operation_without_creating_task(self):
        app = app_fixture()
        factory = MagicMock()
        worker = controller.Worker()
        try:
            with patch.object(controller, '_WORKER', worker), patch.object(controller, 'PB_OK', True):
                self.assertFalse(app._start_op('test', factory))
        finally:
            worker.loop.close()
        factory.assert_not_called()
        self.assertFalse(app.busy)
        self.assertEqual(app._operations, {})


class ConnectionSelectionTests(unittest.TestCase):
    def test_unnamed_connection_requires_explicit_confirmation(self):
        app = app_fixture()
        app.target_var = MagicMock()
        app.target_var.get.return_value = ''
        app._start_op = MagicMock()
        with patch.object(controller, 'messagebox') as box:
            box.askyesno.return_value = False
            app.action_connect()
        app._start_op.assert_not_called()

    def test_named_connection_passes_target_to_transport(self):
        app = app_fixture()
        app.target_var = MagicMock()
        app.target_var.get.return_value = ' Hyena '
        app._start_op = MagicMock()
        app.action_connect()
        self.assertEqual(app.cfg['hub_name'], 'Hyena')
        self.assertEqual(app._start_op.call_args.args[0], 'connect')
        self.assertTrue(callable(app._start_op.call_args.args[1]))


class WorkerIntegrationTests(unittest.TestCase):
    def test_real_worker_dispatch_completion_and_shutdown(self):
        app = app_fixture()
        worker = controller.Worker()
        worker.start()
        result_ready = threading.Event()
        async def operation():
            result_ready.set()
            return 123
        try:
            with patch.object(controller, '_WORKER', worker), patch.object(controller, 'PB_OK', True):
                self.assertTrue(app._start_op('test', operation))
                self.assertTrue(result_ready.wait(2))
                # Blocking queue wait, not timing-dependent polling.
                message = app.queue.get(timeout=2)
                self.assertEqual(message[0], 'op_done')
                self.assertEqual(message[3], 123)
                app._handle_msg(message)
                app.queue.task_done()
                self.assertFalse(app.busy)
        finally:
            worker.loop.call_soon_threadsafe(worker.loop.stop)
            worker.thread.join(timeout=5)
        self.assertFalse(worker.thread.is_alive())
        self.assertTrue(worker.loop.is_closed())


class AsyncOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.logs = patch.object(controller, 'LOG_DIR', directory.name)
        self.logs.start()
        self.addCleanup(self.logs.stop)

    async def test_interrupt_waits_for_cancellation_cleanup_before_stopping(self):
        app = app_fixture()
        entered = asyncio.Event()
        trace = []
        async def upload():
            entered.set()
            try:
                await asyncio.Event().wait()
                trace.append('program started')
            finally:
                await asyncio.sleep(0)
                trace.append('upload cancelled')
        async def stop():
            trace.append('stop sent')
        first = asyncio.create_task(app._execute_op(1, upload))
        await entered.wait()
        await app._execute_op(2, stop, interrupt=True)
        self.assertTrue(first.cancelled())
        self.assertEqual(trace, ['upload cancelled', 'stop sent'])
        self.assertEqual(app._async_tasks, {})

    async def test_regular_operations_are_serialized(self):
        app = app_fixture()
        trace = []
        async def first():
            trace.append('first starts')
            await asyncio.sleep(0)
            trace.append('first ends')
        async def second():
            trace.append('second starts')
        await asyncio.gather(app._execute_op(1, first), app._execute_op(2, second))
        self.assertEqual(trace, ['first starts', 'first ends', 'second starts'])

    async def test_failed_pre_stop_aborts_upload(self):
        app = app_fixture()
        hub = MagicMock()
        async def fail(): raise OSError('not connected')
        hub.stop_user_program = fail
        app._worker_hub = hub
        with self.assertRaises(RuntimeError):
            await app.op_send_run('program.py', False)
        hub.run.assert_not_called()

    async def test_shutdown_cancels_operations_and_closes_capture(self):
        app = app_fixture()
        entered = asyncio.Event()
        async def operation():
            entered.set()
            await asyncio.Event().wait()
        first = asyncio.create_task(app._execute_op(1, operation))
        await entered.wait()
        cap = app.capture = MagicMock()
        await app._shutdown()
        self.assertTrue(first.cancelled())
        cap.close.assert_called_once()
        self.assertIsNone(app.capture)

    async def test_disconnect_failure_preserves_connection_for_retry(self):
        app = app_fixture()
        hub = MagicMock()
        async def fail(): raise OSError('busy')
        hub.disconnect = fail
        app._worker_hub = hub
        app.capture = MagicMock()
        with self.assertRaises(OSError):
            await app.op_disconnect()
        self.assertIs(app._worker_hub, hub)
        app.capture.close.assert_not_called()

    async def test_disconnect_callback_reports_intentional_disconnect(self):
        app = app_fixture()
        hub = MagicMock()
        states = types.SimpleNamespace(DISCONNECTED='disconnected')
        app._worker_hub = hub
        with patch.object(controller, 'ConnectionState', states, create=True):
            app._install_capture(hub)
            callback = hub.connection_state_observable.subscribe.call_args.args[0]
            async def disconnect():
                callback('disconnected')
            hub.disconnect = disconnect
            await app.op_disconnect()
        self.assertIn(('hub_state', 'disconnected', hub, True), list(app.queue.queue))
        self.assertIsNone(app._disconnecting_hub)

    async def test_connected_ble_hub_gets_safe_write_limit_and_version_log(self):
        app = app_fixture()
        hub = types.SimpleNamespace(
            _max_write_size=244, fw_version='4.1.0', connect=AsyncMock(), disconnect=AsyncMock())
        app._install_capture = MagicMock()
        device = types.SimpleNamespace(name='Hyena')
        with patch.object(controller, 'PB_OK', True), \
                patch.object(controller, 'find_device', AsyncMock(return_value=device), create=True), \
                patch.object(controller, 'PybricksHubBLE', lambda dev: hub, create=True):
            result = await app.op_connect('Hyena')
        self.assertIs(result, hub)
        self.assertEqual(hub._max_write_size, 20)
        app._install_capture.assert_called_once_with(hub)
        messages = '\n'.join(m[1] for m in app.queue.queue if m[0] == 'console')
        self.assertIn('BLE write_size=20 hub_limit=244', messages)
        self.assertIn('firmware=4.1.0', messages)

    async def test_connect_cancellation_disconnects_partial_hub(self):
        app = app_fixture()
        entered = asyncio.Event()
        disconnected = []
        class Hub:
            async def connect(self):
                entered.set()
                await asyncio.Event().wait()
            async def disconnect(self):
                disconnected.append(True)
        async def find(target, timeout):
            self.assertEqual(target, 'Hyena')
            return object()
        with patch.object(controller, 'PB_OK', True), \
                patch.object(controller, 'find_device', find, create=True), \
                patch.object(controller, 'PybricksHubBLE', lambda dev: Hub(), create=True):
            task = asyncio.create_task(app.op_connect('Hyena'))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(disconnected, [True])
        self.assertIsNone(app._worker_hub)


class TransferPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.logs = patch.object(controller, 'LOG_DIR', self.directory.name)
        self.logs.start()
        self.addCleanup(self.logs.stop)
        self.app = app_fixture()
        self.hub = types.SimpleNamespace(
            fw_version='4.1.0', _mpy_abi_version=0,
            status_observable=types.SimpleNamespace(value=0),
            stop_user_program=AsyncMock(), download=AsyncMock(), run=AsyncMock())
        self.app._worker_hub = self.hub

    async def test_modern_upload_finishes_before_start(self):
        order = []
        self.hub.stop_user_program.side_effect = lambda: order.append('stop')
        self.hub.download.side_effect = lambda path: order.append('download')
        self.hub.run.side_effect = lambda *a, **kw: order.append('start')
        await self.app.op_send_run('program.py', False)
        self.assertEqual(order, ['stop', 'download', 'start'])
        self.hub.download.assert_awaited_once_with('program.py')
        self.hub.run.assert_awaited_once_with(None, wait=False, print_output=False, line_handler=True)
        self.assertFalse(self.app._download_uncertain)

    async def test_wait_mode_is_forwarded_without_uploading_twice(self):
        await self.app.op_send_run('program.py', True)
        self.hub.download.assert_awaited_once_with('program.py')
        self.hub.run.assert_awaited_once_with(None, wait=True, print_output=False, line_handler=True)

    async def test_gatt_upload_failure_is_not_retried_or_started(self):
        class BleakGATTProtocolError(Exception):
            pass
        original = BleakGATTProtocolError(14, 'GATT Protocol Error: Unlikely Error')
        self.hub.download.side_effect = original
        with self.assertRaises(controller.TransferFailure) as result:
            await self.app.op_send_run('program.py', False)
        self.assertEqual(result.exception.phase, 'compile-upload')
        self.assertIs(result.exception.__cause__, original)
        self.hub.download.assert_awaited_once()
        self.hub.run.assert_not_awaited()
        self.assertTrue(self.app._download_uncertain)
        text = (Path(self.directory.name) / 'transfer_errors.log').read_text(encoding='utf-8')
        self.assertIn('firmware=4.1.0', text)
        self.assertIn('phase=compile-upload', text)
        self.assertIn('BleakGATTProtocolError', text)
        with self.assertRaises(RuntimeError):
            await self.app.op_run_stored()
        self.hub.run.assert_not_awaited()

    async def test_start_error_is_distinct_and_not_retried(self):
        self.hub.run.side_effect = OSError('ack lost')
        with self.assertRaises(controller.TransferFailure) as result:
            await self.app.op_send_run('program.py', False)
        self.assertEqual(result.exception.phase, 'start')
        self.hub.download.assert_awaited_once()
        self.hub.run.assert_awaited_once()
        self.assertFalse(self.app._download_uncertain)

    async def test_busy_hub_prevents_upload(self):
        with patch.object(controller, 'wait_hub_idle', side_effect=TimeoutError('still busy')):
            with self.assertRaises(controller.TransferFailure) as result:
                await self.app.op_send_run('program.py', False)
        self.assertEqual(result.exception.phase, 'stop-ready')
        self.hub.download.assert_not_awaited()
        self.hub.run.assert_not_awaited()

    async def test_cancelled_upload_cannot_run_stored_or_start_late(self):
        entered = asyncio.Event()
        async def upload(path):
            entered.set()
            await asyncio.Event().wait()
        self.hub.download.side_effect = upload
        task = asyncio.create_task(self.app.op_send_run('program.py', False))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.hub.run.assert_not_awaited()
        self.assertTrue(self.app._download_uncertain)
        with self.assertRaises(RuntimeError):
            await self.app.op_run_stored()

    async def test_download_only_does_not_start_and_clears_previous_failure(self):
        self.app._download_uncertain = True
        await self.app.op_download_only('program.py')
        self.hub.download.assert_awaited_once_with('program.py')
        self.hub.run.assert_not_awaited()
        self.assertFalse(self.app._download_uncertain)

    async def test_legacy_run_fallback_remains_available(self):
        self.hub._mpy_abi_version = 6
        await self.app.op_send_run('program.py', False)
        self.hub.download.assert_not_awaited()
        self.hub.run.assert_awaited_once_with('program.py', wait=False, print_output=False, line_handler=True)

    async def test_trace_file_failure_does_not_mask_original_error(self):
        original = OSError('transport failed')
        self.hub.download.side_effect = original
        with patch.object(controller, 'LOG_DIR', str(Path(self.directory.name) / 'missing')), \
                patch.object(controller.traceback, 'print_exc'):
            with self.assertRaises(controller.TransferFailure) as result:
                await self.app.op_send_run('program.py', False)
        self.assertIs(result.exception.__cause__, original)


if __name__ == '__main__':
    unittest.main()
