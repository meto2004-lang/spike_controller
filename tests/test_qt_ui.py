"""Real Qt widgets and native Unicode layouts, without connecting a robot."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from test_controller import controller

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QInputMethodEvent, QTextCursor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from spike_qt import MainWindow, QtApp
    from spike_qt_text import raw_text
    QT_ERROR = None
except (ImportError, OSError) as exc:
    QT_ERROR = str(exc)


def tearDownModule():
    if not controller._WORKER.loop.is_closed():
        controller._WORKER.loop.close()


def x_at(line, position):
    result = line.cursorToX(position)
    return result[0] if isinstance(result, tuple) else result


@unittest.skipIf(QT_ERROR is not None, 'Qt runtime unavailable: ' + str(QT_ERROR))
class QtArabicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        config = dict(controller.DEFAULTS, autolog=False, autoconnect=False)
        with patch.object(controller.App, '_load_config', return_value=config), \
                patch.object(controller, 'LOG_DIR', str(self.directory)), \
                patch.object(controller, 'PB_OK', False):
            self.window = MainWindow()
            self.app = QtApp(self.window)
        self.window.show()
        self.qt.processEvents()
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        self.app._closing = True
        self.app.root.destroy()
        self.window.deleteLater()
        self.qt.processEvents()

    def new_source(self, source):
        document = self.app._new_document(source)
        self.qt.processEvents()
        return document, document.editor.widget

    def console_sample(self, text, source='ble'):
        self.app.console_view.clear()
        self.app._ts_var().set(True)
        with patch.object(controller, 'ts', return_value='12:34:56'):
            self.app.console(text, source=source)
        self.app.console_view.selectAll()
        self.qt.processEvents()
        return self.app.console_view

    def test_native_editor_source_roundtrip_with_arabic_diacritics_emoji_and_nbsp(self):
        source = '# تعليق عربي\nprint("السَّلَامُ عليكم 👋")\ntext = "غير\u00a0منفصل"\n'
        doc, editor = self.new_source(source)
        self.assertEqual(doc.editor.get('1.0', 'end-1c'), source)
        self.assertFalse(doc.dirty)
        self.assertIsInstance(editor, __import__('spike_qt_text').CodeEditor)
        self.assertNotIn('\u202e', doc.editor.get())

    def test_native_arabic_layout_runs_right_to_left(self):
        _, editor = self.new_source('مرحبا بالعالم')
        line = editor.document().begin().layout().lineAt(0)
        self.assertGreater(x_at(line, 1), x_at(line, 3))

    def test_arabic_editor_paragraphs_are_right_aligned_not_only_shaped(self):
        _, editor = self.new_source('مرحبا بالعالم\nprint("hello")\n# تعليق عربي')
        for number in (0, 2):
            block = editor.document().findBlockByNumber(number)
            cursor = QTextCursor(block)
            self.assertGreater(editor.cursorRect(cursor).x(), editor.viewport().width() // 2)

    def test_native_python_structure_ltr_and_inline_arabic_rtl(self):
        source = 'print("مرحبا بالعالم")'
        _, editor = self.new_source(source)
        line = editor.document().begin().layout().lineAt(0)
        self.assertLess(x_at(line, 0), x_at(line, 4))
        start = source.index('مرحبا')
        self.assertGreater(x_at(line, start + 1), x_at(line, start + 3))
        self.assertEqual(raw_text(editor.document()), source)

    def test_direction_switch_only_changes_layout_not_source_dirty_state_or_undo(self):
        source = 'print("مرحبا")\n# تعليق عربي'
        doc, editor = self.new_source(source)
        for index in (2, 1, 0):
            self.app.direction_combo.setCurrentIndex(index)
            self.qt.processEvents()
            self.assertEqual(doc.editor.get(), source)
            self.assertFalse(doc.dirty)
            self.assertFalse(editor.document().isUndoAvailable())

    def test_ime_commit_arabic_inside_python_and_native_undo_redo(self):
        source = 'print("")'
        doc, editor = self.new_source(source)
        cursor = editor.textCursor()
        cursor.setPosition(len('print("'))
        editor.setTextCursor(cursor)
        editor.setFocus()
        event = QInputMethodEvent()
        event.setCommitString('مرحبا بالعالم')
        QApplication.sendEvent(editor, event)
        expected = 'print("مرحبا بالعالم")'
        self.assertEqual(doc.editor.get(), expected)
        self.assertTrue(doc.dirty)
        editor.undo()
        self.assertEqual(doc.editor.get(), source)
        self.assertFalse(doc.dirty)
        editor.redo()
        self.assertEqual(doc.editor.get(), expected)

    def test_save_and_reopen_arabic_file_is_exact_utf8_not_visual_forms(self):
        source = '# تعليق عربي\nprint("مرحبا 👋")\n'
        doc, _ = self.new_source(source)
        path = self.directory / 'برنامج.py'
        self.assertTrue(self.app._save_to(str(path), doc))
        self.assertEqual(path.read_bytes(), source.encode('utf-8'))
        self.assertFalse(doc.dirty)
        self.assertTrue(self.app._open_path(str(path)))
        self.assertIs(self.app.active_document, doc)
        self.assertIn('برنامج.py', self.window.windowTitle())

    def test_tabs_keep_separate_arabic_sources_undo_and_direction(self):
        first, one = self.new_source('print("الأول")')
        self.app.direction_combo.setCurrentIndex(2)
        second, two = self.new_source('print("الثاني")')
        two.insertPlainText('# x')
        self.app._select_document(first)
        self.assertEqual(self.app.direction_combo.currentIndex(), 2)
        self.assertEqual(first.editor.get(), 'print("الأول")')
        self.assertNotEqual(second.editor.get(), 'print("الثاني")')
        self.assertIsNot(one.document(), two.document())

    def test_editor_clipboard_preserves_logical_arabic_and_nbsp(self):
        source = 'print("مرحبا\u00a0👋")\n'
        _, editor = self.new_source(source)
        editor.selectAll()
        editor.copy()
        self.assertEqual(self.qt.clipboard().text(), source)

    def test_console_raw_copy_omits_only_app_timestamp_not_robot_clock(self):
        text = 'مرحبا بالعالم\nوقت الروبوت 01:02:03\nrobot.turn(90)'
        view = self.console_sample(text)
        view.copy()
        self.assertEqual(self.qt.clipboard().text(), text + '\n')
        self.assertIn('12:34:56', raw_text(view.document()))
        self.assertEqual(self.app.training_lines[-3:], text.splitlines())
        self.assertIn('12:34:56 ' + text, self.app.console_raw[-1])

    def test_console_ctrl_c_and_button_both_copy_unshaped_logical_arabic(self):
        text = 'مرحبا 👋 — Python 3'
        view = self.console_sample(text)
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.qt.clipboard().text(), text + '\n')
        self.qt.clipboard().setText('old')
        self.app._copy_console()
        self.assertEqual(self.qt.clipboard().text(), text + '\n')

    def test_partial_timestamp_and_partial_arabic_selection(self):
        view = self.console_sample('مرحبا')
        cursor = QTextCursor(view.document())
        cursor.setPosition(4)
        cursor.setPosition(len('12:34:56 ') + 3, QTextCursor.MoveMode.KeepAnchor)
        view.setTextCursor(cursor)
        view.copy()
        self.assertEqual(self.qt.clipboard().text(), 'مرح')
        cursor.setPosition(1)
        cursor.setPosition(4, QTextCursor.MoveMode.KeepAnchor)
        view.setTextCursor(cursor)
        view.copy()
        self.assertEqual(self.qt.clipboard().text(), '')

    def test_console_paragraph_direction_ignores_serial_label_and_timestamp(self):
        view = self.console_sample('robot.straight(100)\nمرحبا بالعالم', source='serial')
        first = view.document().begin()
        self.assertEqual(first.blockFormat().layoutDirection(), Qt.LayoutDirection.LeftToRight)
        self.assertEqual(first.next().blockFormat().layoutDirection(), Qt.LayoutDirection.RightToLeft)
        view.copy()
        self.assertEqual(self.qt.clipboard().text(), '[سلك] robot.straight(100)\nمرحبا بالعالم\n')

    def test_arabic_console_is_actually_right_aligned_and_latin_remains_left(self):
        view = self.console_sample('مرحبا بالعالم\nrobot.turn(90)')
        first = view.document().begin()
        line = first.layout().lineAt(0)
        self.assertGreater(x_at(line, 10), x_at(line, 12))
        self.assertGreater(line.naturalTextRect().right(), view.viewport().width() // 2)
        latin = first.next().layout().lineAt(0)
        self.assertLess(latin.naturalTextRect().left(), 20)

    def test_console_is_read_only_and_paste_cannot_modify_it(self):
        view = self.console_sample('مرحبا')
        before = raw_text(view.document())
        self.qt.clipboard().setText('injected')
        view.paste()
        QTest.keyClick(view, Qt.Key.Key_Backspace)
        self.assertEqual(raw_text(view.document()), before)

    def test_no_removed_arabic_preview_is_reintroduced(self):
        self.assertFalse(hasattr(self.app, 'arabic_preview'))
        self.assertEqual(self.app.tabs.count(), len(self.app.documents))

    def test_document_close_cancel_preserves_dirty_source(self):
        doc, editor = self.new_source('print("مرحبا")')
        editor.insertPlainText('# changed')
        with patch.object(self.app.dialogs, 'askyesnocancel', return_value=None):
            self.assertFalse(self.app._close_document(doc))
        self.assertIn(doc, self.app.documents)
        self.assertTrue(doc.dirty)

    def test_close_inactive_tab_does_not_switch_or_replace_active_source(self):
        first, _ = self.new_source('print("أول")')
        second, _ = self.new_source('print("ثان")')
        self.assertTrue(self.app._close_document(first))
        self.assertIs(self.app.active_document, second)
        self.assertEqual(second.editor.get(), 'print("ثان")')

    def test_upload_and_stop_states_match_existing_safety_rules(self):
        self.assertFalse(self.app.btn_send.widget.isEnabled())
        self.app.connected, self.app.hub, self.app.busy = True, object(), True
        self.app._refresh_buttons()
        self.assertFalse(self.app.btn_send.widget.isEnabled())
        self.assertTrue(self.app.btn_stop.widget.isEnabled())
        for key in ('usb', 'sendwait', 'dl', 'stored'):
            self.assertFalse(self.app._action_entries[key].isEnabled())

    def test_progress_is_six_pixels_and_never_100_before_completion(self):
        self.app._reset_upload_progress(42)
        self.app._handle_msg(('upload_progress', 42, 10, 10, False))
        self.assertEqual(self.app.upload_bar.widget.height(), 6)
        self.assertEqual(self.app.upload_bar.widget.value(), 99)
        self.app._handle_msg(('upload_progress', 42, 10, 10, True))
        self.assertEqual(self.app.upload_bar.widget.value(), 100)
        self.assertIn('اكتمل', self.app.lbl_upload.widget.text())

    def test_toolbar_and_editor_fit_small_medium_and_large_windows(self):
        for width, height in ((900, 560), (1220, 780), (1600, 900)):
            self.window.resize(width, height)
            self.qt.processEvents()
            self.assertLess(self.app.toolbar.height(), 55)
            self.assertGreater(self.app.target_entry.width(), 80)
            self.assertGreater(self.app.editor.widget.viewport().height(), 150)
            self.assertEqual(self.app.upload_bar.widget.height(), 6)

    def test_upload_captures_exact_arabic_path_and_original_file_bytes(self):
        source = 'print("مرحبا")\n'
        doc, _ = self.new_source(source)
        path = self.directory / 'تدريب.py'
        self.app._save_to(str(path), doc)
        self.app._start_op = MagicMock(return_value=True)
        self.assertTrue(self.app.action_send_run())
        self.assertEqual(self.app._sending_path, str(path))
        self.assertEqual(path.read_bytes(), source.encode('utf-8'))
        self.app._new_document('print("آخر")')
        self.assertIn('تدريب.py', self.app.lbl_send_target.widget.text())

    def test_serial_toggle_label_is_native_arabic(self):
        self.app.serial_running = True
        self.app._sync_serial_button()
        self.assertEqual(self.app._action_entries['serial'].text(), 'إيقاف COM')

    def test_training_generator_reuses_raw_lines_and_opens_native_editor(self):
        self.app.console('robot.straight(100)')
        self.app.action_race_gen()
        race = self.app.race_windows[-1]
        self.assertIn('robot.straight(100)', race.preview.get())
        before = len(self.app.documents)
        race.to_editor()
        self.assertEqual(len(self.app.documents), before + 1)
        self.assertIn('robot.straight(100)', self.app.editor.get())
        self.assertTrue(self.app.active_document.dirty)

    def test_stale_training_preview_does_not_replace_editor(self):
        self.app.console('robot.straight(100)')
        self.app.action_race_gen()
        race = self.app.race_windows[-1]
        before = len(self.app.documents)
        self.app.console('robot.turn(90)')
        with patch.object(self.app.dialogs, 'showerror') as error:
            race.to_editor()
        error.assert_called_once()
        self.assertEqual(len(self.app.documents), before)
        race.win.close()

    def test_shared_backend_keeps_transport_methods_unchanged(self):
        for name in ('op_send_run', 'op_download_only', '_quiet_stop', '_execute_op', '_start_op'):
            self.assertIs(getattr(QtApp, name), getattr(controller.App, name))

    def test_closing_main_window_closes_owned_dialogs_and_stops_poll_timers(self):
        self.app.action_help()
        self.app.console('robot.straight(100)')
        self.app.action_race_gen()
        dialog = self.app.help_dialog
        race = self.app.race_windows[-1]
        self.app._closing = True
        self.app.root.destroy()
        self.assertFalse(dialog.isVisible())
        self.assertFalse(race.win.isVisible())
        self.assertFalse(self.app.root.timers)

    def test_import_and_default_entry_use_qt_without_requesting_tk(self):
        with patch('spike_qt.run', return_value=0) as run:
            self.assertEqual(controller.main(), 0)
        run.assert_called_once_with()
