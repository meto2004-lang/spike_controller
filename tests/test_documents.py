import asyncio
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from spike_documents import EditorDocument, canonical_path, tab_names
from test_controller import Editor, app_fixture, controller


def tearDownModule():
    if not controller._WORKER.loop.is_closed():
        controller._WORKER.loop.close()


class DocumentModelTests(unittest.TestCase):
    def document(self, text='old', path=None, number=1):
        return EditorDocument(None, Editor(text), None, None, path, 'old', number)

    def test_emptying_saved_content_is_dirty(self):
        self.assertTrue(self.document('').dirty)

    def test_undo_to_saved_text_is_clean(self):
        self.assertFalse(self.document('old').dirty)

    def test_new_names_are_numbered_and_suggestions_are_unsaved(self):
        self.assertEqual(self.document(number=3).name, 'بدون اسم 3')
        document = self.document()
        document.suggested_name = 'race.py'
        self.assertEqual(document.name, 'race.py')
        self.assertIsNone(document.path)

    def test_same_basename_is_disambiguated_by_full_parent_path(self):
        one = self.document(path=os.path.join('one', 'same', 'program.py'))
        two = self.document(path=os.path.join('two', 'same', 'program.py'))
        labels = tab_names([one, two])
        self.assertNotEqual(labels[one], labels[two])
        self.assertIn('program.py', labels[one])
        self.assertIn('one', labels[one])
        self.assertIn('two', labels[two])

    def test_dirty_marker_is_per_document(self):
        one = self.document('changed', path='one.py')
        two = self.document('old', path='two.py')
        labels = tab_names([one, two])
        self.assertEqual(labels[one], '* one.py')
        self.assertEqual(labels[two], 'two.py')

    def test_relative_absolute_and_dot_segments_are_same_identity(self):
        self.assertEqual(canonical_path('some/../program.py'), canonical_path(os.path.abspath('program.py')))


class TabControllerTests(unittest.TestCase):
    def setUp(self):
        self.app = app_fixture()
        self.initial = self.app.active_document
        self.dialog = patch.object(controller, 'messagebox')
        self.box = self.dialog.start()
        self.addCleanup(self.dialog.stop)
        self.box.askyesnocancel.return_value = None

    def test_new_tab_keeps_existing_content_and_dirty_state(self):
        created = self.app.action_new()
        self.assertIs(self.app.active_document, created)
        self.assertIsNot(created.editor, self.initial.editor)
        self.assertEqual(len(self.app.documents), 2)
        self.assertEqual(self.initial.editor.text, 'robot.straight(100)')
        self.assertTrue(self.initial.dirty)
        self.assertFalse(created.dirty)
        self.box.askyesnocancel.assert_not_called()

    def test_open_duplicate_selects_existing_without_reloading_dirty_text(self):
        self.assertTrue(self.app._open_path(os.path.abspath('training.py')))
        self.assertIs(self.app.active_document, self.initial)
        self.assertEqual(len(self.app.documents), 1)
        self.assertEqual(self.initial.editor.text, 'robot.straight(100)')
        self.assertTrue(self.initial.dirty)

    def test_open_same_file_via_symlink_selects_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'program.py'
            alias = Path(directory) / 'alias.py'
            path.write_text('print(1)', encoding='utf-8')
            try:
                alias.symlink_to(path)
            except OSError as exc:
                self.skipTest(f'Symlinks not permitted: {exc}')
            self.app._open_path(str(path))
            first = self.app.active_document
            first.editor.text = 'print(2)'
            self.app._open_path(str(alias))
            self.assertIs(self.app.active_document, first)
            self.assertEqual(first.editor.text, 'print(2)')
            self.assertEqual(len(self.app.documents), 2)

    def test_save_as_cannot_overwrite_file_owned_by_another_tab(self):
        created = self.app._new_document('print(2)', saved=False)
        with patch.object(controller, 'atomic_write') as write:
            self.assertFalse(self.app._save_to(os.path.abspath('training.py'), created))
        write.assert_not_called()
        self.assertTrue(created.dirty)
        self.assertIsNone(created.path)
        self.assertEqual(self.initial.editor.text, 'robot.straight(100)')

    def test_save_dialog_targets_original_document_even_after_tab_switch(self):
        self.initial.path = None
        destination = self.app._new_document('print(2)')
        self.app._select_document(self.initial)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'first.py')
            def choose(**kwargs):
                self.app._select_document(destination)
                return path
            with patch.object(controller.filedialog, 'asksaveasfilename', side_effect=choose):
                self.assertTrue(self.app.action_save())
            self.assertEqual(Path(path).read_text(encoding='utf-8'), 'robot.straight(100)')
            self.assertEqual(self.initial.path, path)
        self.assertIs(self.app.active_document, destination)
        self.assertIsNone(destination.path)
        self.assertTrue(destination.editor.text == 'print(2)')

    def test_close_cancel_keeps_tab_and_does_not_cancel_its_highlight(self):
        self.initial.highlight_job = 'first-job'
        self.assertFalse(self.app.action_close_tab())
        self.assertIn(self.initial, self.app.documents)
        self.initial.frame.destroy.assert_not_called()
        self.app.root.after_cancel.assert_not_called()

    def test_close_discard_cancels_highlight_and_keeps_one_blank_tab(self):
        self.initial.highlight_job = 'first-job'
        self.box.askyesnocancel.return_value = False
        self.assertTrue(self.app.action_close_tab())
        self.assertNotIn(self.initial, self.app.documents)
        self.initial.frame.destroy.assert_called_once()
        self.app.root.after_cancel.assert_called_with('first-job')
        self.assertEqual(len(self.app.documents), 1)
        self.assertFalse(self.app.active_document.dirty)
        self.assertIsNone(self.app.current_path)

    def test_close_save_writes_before_removing_tab(self):
        self.box.askyesnocancel.return_value = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'training.py'
            path.write_text('old', encoding='utf-8')
            self.initial.path = str(path)
            self.assertTrue(self.app.action_close_tab())
            self.assertEqual(path.read_text(encoding='utf-8'), 'robot.straight(100)')
        self.assertNotIn(self.initial, self.app.documents)

    def test_failed_save_prevents_tab_close(self):
        self.box.askyesnocancel.return_value = True
        with patch.object(controller, 'atomic_write', side_effect=OSError('full')):
            self.assertFalse(self.app.action_close_tab())
        self.assertIn(self.initial, self.app.documents)
        self.assertTrue(self.initial.dirty)
        self.initial.frame.destroy.assert_not_called()

    def test_cancelled_save_as_prevents_tab_close(self):
        self.initial.path = None
        self.box.askyesnocancel.return_value = True
        with patch.object(controller.filedialog, 'asksaveasfilename', return_value=''):
            self.assertFalse(self.app.action_close_tab())
        self.assertIn(self.initial, self.app.documents)

    def test_close_clean_tab_does_not_prompt(self):
        second = self.app._new_document('print(2)')
        self.assertTrue(self.app._close_document(second))
        self.box.askyesnocancel.assert_not_called()
        self.assertIs(self.app.active_document, self.initial)

    def test_quit_cancel_does_not_remove_tabs_or_disconnect(self):
        second = self.app._new_document('print(2)', saved=False)
        self.box.askyesnocancel.side_effect = [False, None]
        self.app._save_config = MagicMock()
        self.app._finish_close = MagicMock()
        self.app._on_close()
        self.assertFalse(self.app._closing)
        self.assertEqual(self.app.documents, [self.initial, second])
        self.app._save_config.assert_not_called()
        self.app._finish_close.assert_not_called()
        self.initial.frame.destroy.assert_not_called()
        second.frame.destroy.assert_not_called()

    def test_quit_resolves_all_tabs_and_cancels_each_highlight(self):
        second = self.app._new_document('print(2)', saved=False)
        self.initial.highlight_job, second.highlight_job = 'job-a', 'job-b'
        self.box.askyesnocancel.return_value = False
        self.app._save_config = MagicMock()
        self.app._finish_close = MagicMock()
        worker = types.SimpleNamespace(loop=types.SimpleNamespace(is_running=lambda: False))
        with patch.object(controller, '_WORKER', worker):
            self.app._on_close()
        self.assertTrue(self.app._closing)
        self.assertEqual(self.box.askyesnocancel.call_count, 2)
        self.app.root.after_cancel.assert_any_call('job-a')
        self.app.root.after_cancel.assert_any_call('job-b')
        self.app._finish_close.assert_called_once()

    def test_switch_keeps_robot_console_and_training_session_unchanged(self):
        self.app.training_lines = ['robot.turn(90)']
        self.app.console_raw = ['existing output']
        self.app.serial_running = True
        self.app._start_op = MagicMock()
        second = self.app._new_document('other')
        self.app._select_document(self.initial)
        self.app._select_document(second)
        self.assertEqual(self.app.training_lines, ['robot.turn(90)'])
        self.assertEqual(self.app.console_raw, ['existing output'])
        self.assertTrue(self.app.serial_running)
        self.app._start_op.assert_not_called()

    def test_cycle_wraps_in_both_directions(self):
        second = self.app._new_document('other')
        self.app._cycle_document(1)
        self.assertIs(self.app.active_document, self.initial)
        self.app._cycle_document(-1)
        self.assertIs(self.app.active_document, second)

    def test_notebook_selection_updates_active_document(self):
        self.app._new_document('other')
        self.app.notebook.select(self.initial.frame)
        self.app._on_tab_changed()
        self.assertIs(self.app.active_document, self.initial)
        self.assertIn('training.py', self.app.root.title.call_args.args[0])

    def test_late_highlight_callback_is_bound_to_its_own_tab(self):
        self.initial.editor.tag_add = MagicMock()
        self.initial.editor.dirty = True
        self.app._on_editor_modified(document=self.initial)
        callback = self.app.root.after_idle.call_args.args[0]
        other = self.app._new_document('print(99)')
        other.editor.tag_add = MagicMock()
        callback()
        self.initial.editor.tag_add.assert_called()
        other.editor.tag_add.assert_not_called()
        self.assertIs(self.app.active_document, other)

    def test_closed_tab_callback_is_harmless(self):
        self.initial.editor.tag_add = MagicMock()
        self.box.askyesnocancel.return_value = False
        self.app._close_document(self.initial)
        self.app._rehighlight(self.initial)
        self.initial.editor.tag_add.assert_not_called()

    def test_send_pins_file_across_tab_changes_and_clears_label_on_completion(self):
        self.initial.saved_text = self.initial.editor.text
        captured = []
        def start(name, factory):
            self.app._next_op = 42
            self.app._operations[42] = (name, None)
            captured.append(factory)
            return True
        self.app._start_op = start
        self.app.op_send_run = AsyncMock()
        self.assertTrue(self.app.action_send_run())
        self.app._new_document('another program', path='other.py')
        self.assertEqual(self.app._sending_path, 'training.py')
        self.assertIn('training.py', self.app.lbl_send_target.configure.call_args.kwargs['text'])
        asyncio.run(captured[0]())
        self.app.op_send_run.assert_awaited_once_with('training.py', wait=False)
        self.app._handle_msg(('op_done', 42, 'send_run', None, None))
        self.assertIsNone(self.app._sending_path)
        self.assertIn('other.py', self.app.lbl_send_target.configure.call_args.kwargs['text'])

    def test_failed_save_or_cancel_does_not_schedule_send(self):
        self.initial.path = None
        self.app._start_op = MagicMock()
        with patch.object(controller.filedialog, 'asksaveasfilename', return_value=''):
            self.assertFalse(self.app.action_send_run())
        self.app._start_op.assert_not_called()

    def test_nested_close_or_new_during_quit_prompt_is_blocked(self):
        self.app._save_config = MagicMock()
        def answer(*args):
            self.assertIsNone(self.app.action_new())
            self.assertFalse(self.app.action_close_tab())
            self.assertFalse(self.app.action_save_as())
            self.assertFalse(self.app.action_send_run())
            self.app._on_close()
            return None
        self.box.askyesnocancel.side_effect = answer
        self.app._on_close()
        self.assertEqual(self.app.documents, [self.initial])
        self.assertFalse(self.app._confirming_close)
        self.assertFalse(self.app._closing)
        self.assertEqual(self.box.askyesnocancel.call_count, 1)
        self.app._save_config.assert_not_called()

    def test_download_targets_captured_document(self):
        self.initial.saved_text = self.initial.editor.text
        captured = []
        def start(name, factory):
            self.app._next_op = 41
            captured.append(factory)
            return True
        self.app._start_op = start
        self.app.op_download_only = AsyncMock()
        self.assertTrue(self.app.action_download_only())
        self.app.action_new()
        asyncio.run(captured[0]())
        self.app.op_download_only.assert_awaited_once_with('training.py')

    def test_stale_completion_does_not_clear_new_send_target(self):
        self.app._operations[42] = ('send_run', None)
        self.app._sending_path = 'new.py'
        self.app._send_op_id = 43
        self.app._handle_msg(('op_done', 42, 'send_run', None, None))
        self.assertEqual(self.app._send_op_id, 43)
        self.assertEqual(self.app._sending_path, 'new.py')

    def test_failed_save_blocks_every_send_mode(self):
        self.app._start_op = MagicMock()
        with patch.object(controller, 'atomic_write', side_effect=OSError('full')):
            for action in (self.app.action_send_run, self.app.action_send_wait,
                           self.app.action_download_only):
                with self.subTest(action=action.__name__):
                    self.assertFalse(action())
        self.app._start_op.assert_not_called()
        self.assertIsNone(self.app._sending_path)

    def test_quit_failed_save_keeps_all_tabs_and_connection(self):
        second = self.app._new_document('print(2)', path='second.py', saved=False)
        self.box.askyesnocancel.side_effect = [False, True]
        self.app._save_config = MagicMock()
        self.app._finish_close = MagicMock()
        with patch.object(controller, 'atomic_write', side_effect=OSError('full')):
            self.app._on_close()
        self.assertFalse(self.app._closing)
        self.assertEqual(self.app.documents, [self.initial, second])
        self.app._save_config.assert_not_called()
        self.app._finish_close.assert_not_called()

    def test_race_save_cannot_overwrite_an_open_document(self):
        race = controller.RaceGenWindow.__new__(controller.RaceGenWindow)
        race.app, race.preview, race.win = self.app, Editor('race program'), MagicMock()
        race.preview_snapshot = self.app._training_snapshot()
        with patch.object(controller.filedialog, 'asksaveasfilename', return_value='training.py'), \
                patch.object(controller, 'atomic_write') as write:
            race.save()
        write.assert_not_called()
        self.assertEqual(self.initial.editor.text, 'robot.straight(100)')
        self.box.showerror.assert_called_once()


if __name__ == '__main__':
    unittest.main()
