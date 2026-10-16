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

    def test_arabic_preview_tracks_cursor_and_keeps_source_undo_and_save_intact(self):
        source = 'robot.straight(100)  # التدريب قبل السباق\nprint("مرحبا بالعالم")\n'
        document = self.app._new_document(source)
        document.editor.mark_set('insert', '1.4')
        document.editor.event_generate('<<Selection>>')
        self.root.update()
        self.assertEqual(self.app.arabic_preview.get('1.0', 'end-1c'),
                         controller.to_visual(source.splitlines()[0]))
        document.editor.mark_set('insert', '2.3')
        document.editor.event_generate('<<Selection>>')
        self.root.update()
        self.assertEqual(self.app.arabic_preview.get('1.0', 'end-1c'),
                         controller.to_visual(source.splitlines()[1]))
        self.assertEqual(str(self.app.arabic_preview.cget('state')), 'disabled')
        self.assertFalse(document.dirty)
        self.assertFalse(document.editor.edit_modified())
        self.assertEqual(document.editor.get('1.0', 'end-1c'), source)
        with self.assertRaises(tk.TclError):
            document.editor.edit_undo()
        path = self.directory / 'arabic-source.py'
        self.assertTrue(self.app._save_to(str(path)))
        self.assertEqual(path.read_bytes(), source.encode('utf-8'))

    def test_preview_copy_exports_logical_line_not_presentation_forms(self):
        source = 'message = "مرحبا بالعالم"  # تعليق عربي'
        document = self.app._new_document(source)
        self.root.update()
        self.app.arabic_preview.tag_add('sel', '1.0', 'end')
        self.app.arabic_preview.event_generate('<<Copy>>')
        self.root.update()
        self.assertEqual(self.root.clipboard_get(), source)
        self.assertFalse(self.app.arabic_preview.cget('exportselection'))
        self.assertEqual(document.editor.get('1.0', 'end-1c'), source)
        self.assertFalse(document.dirty)

    def test_preview_toggle_and_tab_switch_restore_correct_source(self):
        first = self.app._new_document('# التعليق الأول')
        second = self.app._new_document('# التعليق الثاني')
        self.root.update()
        self.assertEqual(self.app._arabic_preview_source, second.editor.get('1.0', 'end-1c'))
        self.app._toggle_arabic_preview()
        self.root.update()
        self.assertFalse(self.app.arabic_preview_frame.winfo_ismapped())
        self.app._select_document(first)
        self.app._toggle_arabic_preview()
        self.root.update()
        self.assertTrue(self.app.arabic_preview_frame.winfo_ismapped())
        self.assertGreater(self.app.arabic_preview.winfo_width(), 500)
        self.assertEqual(self.app._arabic_preview_source, first.editor.get('1.0', 'end-1c'))


if __name__ == '__main__':
    unittest.main()
