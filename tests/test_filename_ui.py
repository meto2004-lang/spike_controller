"""Optional real-Tk layout regression tests (Linux CI runs these under Xvfb)."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

try:
    import tkinter as tk
except ImportError:
    tk = None

from test_controller import controller


def tearDownModule():
    if not controller._WORKER.loop.is_closed():
        controller._WORKER.loop.close()


@unittest.skipIf(tk is None, 'Tkinter is not installed')
class FilenameLayoutTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'No display available: {exc}')
        self.addCleanup(self.root.destroy)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        cfg = dict(controller.DEFAULTS, geometry='900x560', autolog=False, autoconnect=False)
        with patch.object(controller, 'PB_OK', False), \
                patch.object(controller, 'LOG_DIR', directory.name), \
                patch.object(controller.App, '_load_config', return_value=cfg):
            self.app = controller.App(self.root)
        self.addCleanup(self.cancel_callbacks)
        self.root.update()

    def cancel_callbacks(self):
        self.app._closing = True
        for job in [self.app._poll_job] + [doc.highlight_job for doc in self.app.documents]:
            if job:
                self.root.after_cancel(job)

    def open_file(self, name):
        path = self.directory / name
        path.write_text('print("ready")', encoding='utf-8')
        self.assertTrue(self.app._open_path(str(path)))
        self.root.update()
        return path

    def test_filename_is_visible_above_editor_at_minimum_window_size(self):
        path = self.open_file('program.py')
        self.assertTrue(self.app.lbl_file.winfo_ismapped())
        self.assertGreater(self.app.lbl_file.winfo_width(), 100)
        self.assertEqual(self.app.lbl_file.cget('text'), path.name)
        self.assertIn(path.name, self.root.title())
        self.assertLess(self.app.lbl_file.winfo_rooty(), self.app.editor.winfo_rooty())
        # Do not resolve away Windows 8.3 aliases in the copyable source path.
        self.assertEqual(self.app.file_path_var.get(), os.path.abspath(path))
        self.assertEqual(str(self.app.file_path_entry.cget('state')), 'readonly')

    def test_long_connection_status_does_not_hide_filename(self):
        path = self.open_file('long_' + 'program_' * 16 + '.py')
        self.app.lbl_state.configure(text='Connected: ' + 'hub-' * 100)
        self.root.geometry('900x560')
        self.root.update()
        self.assertTrue(self.app.lbl_file.winfo_ismapped())
        self.assertGreater(self.app.lbl_file.winfo_width(), 100)
        self.assertEqual(self.app.lbl_file.cget('text'), path.name)
        self.assertIn(path.name, self.root.title())
        self.assertEqual(self.app.file_path_var.get(), os.path.abspath(path))
        self.assertTrue(self.app.lbl_state.winfo_ismapped())

    def test_arabic_filename_uses_visual_header_and_logical_native_title(self):
        path = self.open_file('برنامج التدريب.py')
        self.assertEqual(self.app.lbl_file.cget('text'), controller.to_visual(path.name))
        self.assertEqual(self.root.title(), f'{path.name} — Hyena Control')
        self.assertEqual(self.app.file_path_entry.get(), os.path.abspath(path))

    def test_editors_have_independent_undo_cursor_and_dirty_flags(self):
        first = self.app._new_document('print(1)\n')
        first.editor.edit_separator()
        first.editor.insert('end-1c', '# first\n')
        first.editor.mark_set('insert', '2.2')
        second = self.app._new_document('print(2)\n')
        second.editor.edit_separator()
        second.editor.insert('end-1c', '# second\n')
        self.root.update()
        second.editor.edit_undo()
        self.root.update()
        self.assertEqual(second.editor.get('1.0', 'end-1c'), 'print(2)\n')
        self.assertFalse(second.dirty)
        self.assertTrue(first.dirty)
        self.assertIn('# first', first.editor.get('1.0', 'end-1c'))
        self.app._select_document(first)
        self.root.update()
        self.assertEqual(first.editor.index('insert'), '2.2')
        first.editor.edit_undo()
        self.root.update()
        self.assertFalse(first.dirty)
        self.assertFalse(second.dirty)

    def test_scroll_and_syntax_tags_stay_with_the_document(self):
        first = self.app._new_document('for n in range(200):\n' + '    print(123)\n' * 200)
        self.root.update()
        first.editor.yview_moveto(0.65)
        self.root.update()
        original_scroll = first.editor.yview()[0]
        self.assertGreater(original_scroll, 0.5)
        self.assertTrue(first.editor.tag_ranges('kw'))
        second = self.app._new_document('"hello"\n' * 200)
        self.root.update()
        second.editor.yview_moveto(0.1)
        self.root.update()
        second_scroll = second.editor.yview()[0]
        self.assertFalse(second.editor.tag_ranges('kw'))
        self.assertTrue(second.editor.tag_ranges('str'))
        self.app._select_document(first)
        self.root.update()
        self.assertAlmostEqual(first.editor.yview()[0], original_scroll, delta=0.02)
        self.assertAlmostEqual(first.gutter.yview()[0], original_scroll, delta=0.02)
        self.assertAlmostEqual(second.editor.yview()[0], second_scroll, delta=0.02)
        self.assertTrue(first.editor.tag_ranges('kw'))

    def close_icon_point(self, document):
        self.root.update()
        notebook = self.app.notebook
        expected = notebook.index(document.frame)
        for y in range(2, min(60, notebook.winfo_height()), 2):
            for x in range(notebook.winfo_width()):
                if notebook.identify(x, y).endswith('.close'):
                    if notebook.index(f'@{x},{y}') == expected:
                        return x, y
        self.fail('No close icon found on the document tab')

    def test_close_icon_closes_only_the_clicked_inactive_tab(self):
        self.open_file('one.py')
        first = self.app.active_document
        self.open_file('two.py')
        second = self.app.active_document
        x, y = self.close_icon_point(first)
        with patch.object(controller.messagebox, 'askyesnocancel') as question:
            self.app.notebook.event_generate('<ButtonPress-1>', x=x, y=y)
            self.app.notebook.event_generate('<ButtonRelease-1>', x=x, y=y)
            self.root.update()
        question.assert_not_called()
        self.assertNotIn(first, self.app.documents)
        self.assertIn(second, self.app.documents)
        self.assertIs(self.app.active_document, second)

    def test_close_icon_cancel_keeps_dirty_tab(self):
        self.open_file('dirty.py')
        document = self.app.active_document
        document.editor.insert('end-1c', '# changed\n')
        self.root.update()
        x, y = self.close_icon_point(document)
        with patch.object(controller.messagebox, 'askyesnocancel', return_value=None) as question:
            self.app.notebook.event_generate('<ButtonPress-1>', x=x, y=y)
            self.app.notebook.event_generate('<ButtonRelease-1>', x=x, y=y)
            self.root.update()
        question.assert_called_once()
        self.assertIn(document, self.app.documents)
        self.assertTrue(document.dirty)

    def test_ctrl_tab_in_editor_cycles_once(self):
        first = self.app.active_document
        self.app._new_document('print(2)')
        self.root.update()
        self.root.focus_force()
        self.app.editor.focus_set()
        self.root.update()
        self.app.editor.event_generate('<Control-KeyPress-Tab>')
        self.root.update()
        self.assertIs(self.app.active_document, first)

    def test_in_flight_filename_has_own_visible_row_after_switching_tabs(self):
        first = self.open_file('upload.py')
        self.app._start_op = MagicMock(return_value=True)
        self.assertTrue(self.app.action_send_run())
        second = self.open_file('other.py')
        self.app.lbl_state.configure(text='Connected: ' + 'hub-' * 100)
        self.root.update()
        self.assertEqual(self.app.current_path, str(second))
        self.assertEqual(self.app._sending_path, str(first))
        self.assertIn(first.name, self.app.lbl_send_target.cget('text'))
        self.assertTrue(self.app.lbl_send_target.winfo_ismapped())
        self.assertGreater(self.app.lbl_send_target.winfo_width(), 700)

    def test_toolbar_is_one_compact_row_and_no_controls_overlap_or_leave_the_window(self):
        self.assertEqual(len(self.app.toolbar_buttons), 4)
        self.assertEqual(set(self.app._action_entries), {'usb', 'serial', 'sendwait', 'dl', 'stored', 'race', 'clear', 'export'})
        for width, height in ((900, 560), (1220, 780), (1600, 900)):
            self.root.geometry(f'{width}x{height}')
            self.root.update()
            rectangles = []
            self.assertLessEqual(self.app.toolbar.winfo_height(), 58)
            for button in (*self.app.toolbar_buttons, self.app.options_button, self.app.tools_button):
                self.assertTrue(button.winfo_ismapped())
                x = button.winfo_rootx() - self.root.winfo_rootx()
                y = button.winfo_rooty() - self.root.winfo_rooty()
                w, h = button.winfo_width(), button.winfo_height()
                self.assertGreaterEqual(w, 40)
                self.assertGreaterEqual(h, 28)
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + w, self.root.winfo_width())
                self.assertLessEqual(y + h, self.root.winfo_height())
                self.assertEqual(button.grid_info()['row'], 0)
                rectangles.append((x, y, x + w, y + h))
            for index, (left, top, right, bottom) in enumerate(rectangles):
                for other_left, other_top, other_right, other_bottom in rectangles[index + 1:]:
                    self.assertFalse(left < other_right and right > other_left and top < other_bottom and bottom > other_top)
            self.assertGreater(self.app.target_entry.winfo_width(), 100,
                               [(str(w), w.winfo_reqwidth(), w.winfo_width()) for w in self.app.toolbar.winfo_children()])
            self.assertGreater(self.app.editor.winfo_height(), 210)
            self.assertGreater(self.app.editor.winfo_height(), self.app.console_w.winfo_height())

    def test_toolbar_labels_fit_and_danger_action_is_distinct(self):
        from tkinter import font
        style = controller.ttk.Style(self.root)
        button_font = font.Font(root=self.root, font=style.lookup('Toolbar.TButton', 'font'))
        for button in self.app.toolbar_buttons:
            image_name = self.root.tk.splitlist(button.cget('image'))[0]
            image_width = int(self.root.tk.call('image', 'width', image_name))
            required = button_font.measure(button.cget('text')) + image_width + 16
            self.assertGreaterEqual(button.winfo_width(), required, button.cget('text'))
        self.assertEqual(self.app.btn_send.cget('style'), 'Primary.Toolbar.TButton')
        self.assertEqual(self.app.btn_stop.cget('style'), 'Stop.Toolbar.TButton')
        self.assertNotEqual(style.lookup('Primary.Toolbar.TButton', 'background'),
                            style.lookup('Stop.Toolbar.TButton', 'background'))

    def test_toolbar_invokes_existing_send_download_and_stop_commands(self):
        self.open_file('toolbar.py')
        self.app.connected = True
        self.app.hub = object()
        self.app._refresh_buttons()
        self.app._start_op = MagicMock(return_value=True)
        for button, operation in ((self.app.btn_send, 'send_run'), (self.app.btn_stop, 'stop')):
            with self.subTest(operation=operation):
                self.app._start_op.reset_mock()
                button.invoke()
                self.assertEqual(self.app._start_op.call_args.args[0], operation)
        for key, operation in (('sendwait', 'send_wait'), ('dl', 'download'), ('stored', 'run_stored')):
            with self.subTest(operation=operation):
                self.app._start_op.reset_mock()
                menu, index = self.app._action_entries[key]
                menu.invoke(index)
                self.assertEqual(self.app._start_op.call_args.args[0], operation)

    def test_disabled_upload_cannot_be_invoked_and_stop_stays_available_when_busy(self):
        self.app._start_op = MagicMock()
        self.app.btn_send.invoke()
        self.app._start_op.assert_not_called()
        self.app.connected, self.app.hub, self.app.busy = True, object(), True
        self.app._refresh_buttons()
        self.assertIn('disabled', self.app.btn_send.state())
        self.assertNotIn('disabled', self.app.btn_stop.state())
        self.app.btn_stop.invoke()
        self.assertEqual(self.app._start_op.call_args.args[0], 'stop')

    def add_copy_sample(self):
        self.app._ts_var().set(True)
        with patch.object(controller, 'ts', return_value='[12:34:56]'):
            self.app.console('robot.straight(100)', 'out')
            self.app.console('[01:02:03] robot time\nrobot.turn(90)', 'info')
        self.app._select_all_console()
        self.root.update()
        return 'robot.straight(100)\n[01:02:03] robot time\nrobot.turn(90)\n'

    def test_console_copy_event_excludes_only_app_time_and_keeps_logs_unchanged(self):
        expected = self.add_copy_sample()
        displayed = self.app.console_w.get('1.0', 'end-1c')
        raw, training = self.app.console_raw[:], self.app.training_lines[:]
        self.app.console_w.event_generate('<<Copy>>')
        self.root.update()
        self.assertEqual(self.root.clipboard_get(), expected)
        self.assertEqual(self.app.console_w.get('1.0', 'end-1c'), displayed)
        self.assertEqual(self.app.console_raw, raw)
        self.assertEqual(self.app.training_lines, training)

    def test_console_ctrl_c_and_copy_button_use_filtered_clipboard(self):
        expected = self.add_copy_sample()
        self.root.focus_force()
        self.app.console_w.focus_set()
        self.root.update()
        self.app.console_w.event_generate('<Control-KeyPress-c>')
        self.root.update()
        self.assertEqual(self.root.clipboard_get(), expected)
        self.root.clipboard_clear()
        self.app._copy_console()  # Also the header button / Ctrl+Shift+C callback.
        self.root.update()
        self.assertEqual(self.root.clipboard_get(), expected)

    def test_console_copy_handles_partial_timestamp_selection_and_empty_output(self):
        self.add_copy_sample()
        widget = self.app.console_w
        widget.tag_remove('sel', '1.0', 'end')
        widget.tag_add('sel', '1.4', '2.4')
        widget.event_generate('<<Copy>>')
        self.assertEqual(self.root.clipboard_get(), 'robot.straight(100)\n')
        widget.tag_remove('sel', '1.0', 'end')
        widget.tag_add('sel', '1.1', '1.5')
        widget.event_generate('<<Copy>>')
        self.assertEqual(self.root.clipboard_get(), '')

    def test_console_copy_after_timestamp_toggle_and_clear(self):
        self.add_copy_sample()
        widget = self.app.console_w
        widget.delete('1.0', 'end')
        self.app._ts_var().set(False)
        self.app.console('[12:34:56] from robot')
        self.app._ts_var().set(True)
        self.app.console('next')
        self.app._select_all_console()
        widget.event_generate('<<Copy>>')
        self.assertEqual(self.root.clipboard_get(), '[12:34:56] from robot\nnext\n')

    def test_editor_copy_keeps_clock_text_and_is_not_console_filtered(self):
        source = 'print("[12:34:56]")\n'
        self.app.editor.insert('1.0', source)
        self.app.editor.tag_add('sel', '1.0', 'end-1c')
        self.app.editor.event_generate('<<Copy>>')
        self.root.update()
        self.assertEqual(self.root.clipboard_get(), source)

    def test_upload_bar_is_thin_green_and_at_the_very_bottom(self):
        bar = self.app.upload_bar
        self.assertTrue(bar.winfo_ismapped())
        self.assertLessEqual(bar.winfo_height(), 10)
        self.assertEqual(bar.winfo_width(), self.root.winfo_width() - 16)
        self.assertLessEqual(self.root.winfo_rooty() + self.root.winfo_height() - bar.winfo_rooty(), 14)
        self.app._reset_upload_progress(123)
        self.app._handle_msg(('upload_progress', 123, 43, 100, False))
        self.root.update()
        self.assertEqual(float(bar.cget('value')), 43)
        style = controller.ttk.Style(self.root)
        self.assertEqual(style.lookup(bar.cget('style'), 'background'), self.app.OK)
        self.app._handle_msg(('upload_progress', 123, 100, 100, False))
        self.assertEqual(float(bar.cget('value')), 99)
        self.app._handle_msg(('upload_progress', 123, 100, 100, True))
        self.assertEqual(float(bar.cget('value')), 100)

    def test_secondary_upload_menu_is_disabled_during_transfer(self):
        self.app.connected, self.app.hub, self.app.busy = True, object(), True
        self.app._refresh_buttons()
        self.app._start_op = MagicMock()
        for key in ('usb', 'sendwait', 'dl', 'stored'):
            menu, index = self.app._action_entries[key]
            self.assertEqual(menu.entrycget(index, 'state'), 'disabled')
            menu.invoke(index)
        self.app._start_op.assert_not_called()
        self.assertNotIn('disabled', self.app.btn_stop.state())

    def test_only_file_tabs_remain_and_arabic_source_is_unmodified(self):
        self.assertFalse(hasattr(self.app, 'arabic_preview'))
        self.assertFalse(hasattr(self.app, 'arabic_preview_frame'))
        source = 'print("مرحبا بالعالم")  # النص الأصلي\n'
        document = self.app._new_document(source)
        self.root.update()
        self.assertEqual(document.editor.get('1.0', 'end-1c'), source)
        self.assertFalse(document.dirty)
        self.assertEqual(len(self.app.notebook.tabs()), len(self.app.documents))
        path = self.directory / 'arabic.py'
        self.assertTrue(self.app._save_to(str(path)))
        self.assertEqual(path.read_bytes(), source.encode('utf-8'))


if __name__ == '__main__':
    unittest.main()
