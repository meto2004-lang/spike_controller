"""The Arabic preview is a projection, never the document's source of truth."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_controller import app_fixture, controller


def tearDownModule():
    if not controller._WORKER.loop.is_closed():
        controller._WORKER.loop.close()


class ArabicPreviewTests(unittest.TestCase):
    def setUp(self):
        self.app = app_fixture()
        self.document = self.app.active_document
        self.source = 'robot.straight(100)  # التدريب قبل السباق 123'
        self.document.editor.text = self.source
        self.document.editor.cursor = '1.4'
        self.document.saved_text = self.source
        self.document.editor.edit_modified(False)

    def test_display_is_shaped_but_document_baseline_and_dirty_flag_are_untouched(self):
        with patch.object(controller, 'to_visual', side_effect=lambda text: 'visual:' + text):
            self.app._update_arabic_preview()
        self.assertEqual(self.app.arabic_preview.text, 'visual:' + self.source)
        self.assertEqual(self.document.editor.text, self.source)
        self.assertEqual(self.document.saved_text, self.source)
        self.assertFalse(self.document.dirty)
        self.assertFalse(self.document.editor.edit_modified())

    def test_actual_shaping_with_runtime_dependencies(self):
        if not controller.RTL_OK:
            self.skipTest('Arabic shaping dependencies are not installed')
        self.app._update_arabic_preview()
        self.assertNotEqual(self.app.arabic_preview.text, self.source)
        self.assertEqual(self.app.arabic_preview.text, controller.to_visual(self.source))
        self.assertIn('robot.straight(100)', self.app.arabic_preview.text)
        self.assertEqual(self.document.editor.text, self.source)

    def test_cursor_selects_only_current_logical_line(self):
        self.document.editor.text = self.source + '\nprint("مرحبا بالعالم")\n'
        self.app._update_arabic_preview()
        self.assertEqual(self.app._arabic_preview_source, self.source)
        self.document.editor.cursor = '2.3'
        self.app._update_arabic_preview()
        self.assertEqual(self.app._arabic_preview_source, 'print("مرحبا بالعالم")')
        self.assertEqual(self.app.arabic_preview.text, controller.to_visual('print("مرحبا بالعالم")'))

    def test_modified_event_refreshes_preview_without_rewriting_code(self):
        self.app._update_arabic_preview()
        changed = 'message = "نص جديد"'
        self.document.editor.text = changed
        self.document.editor.edit_modified(True)
        self.app._on_editor_modified(document=self.document)
        self.assertEqual(self.app._arabic_preview_source, changed)
        self.assertEqual(self.document.editor.text, changed)
        self.assertTrue(self.document.dirty)

    def test_switch_tabs_restores_each_current_line(self):
        self.app._update_arabic_preview()
        second = self.app._new_document('print("الثاني")')
        self.assertEqual(self.app._arabic_preview_source, 'print("الثاني")')
        self.app._select_document(self.document)
        self.assertEqual(self.app._arabic_preview_source, self.source)
        self.assertIsNot(second, self.document)

    def test_late_event_from_inactive_or_closed_document_does_not_replace_preview(self):
        second = self.app._new_document('print("الثاني")')
        self.app._update_arabic_preview(self.document)
        self.assertEqual(self.app._arabic_preview_source, second.editor.text)
        self.app.documents.remove(self.document)
        self.app._update_arabic_preview(self.document)
        self.assertEqual(self.app._arabic_preview_source, second.editor.text)

    def test_copy_returns_complete_logical_source_with_trailing_spaces(self):
        self.document.editor.text = self.source + '  '
        self.app._update_arabic_preview()
        self.assertEqual(self.app._copy_arabic_preview(), 'break')
        self.app.root.clipboard_clear.assert_called_once()
        self.app.root.clipboard_append.assert_called_once_with(self.source + '  ')

    def test_long_preview_is_bounded_but_copy_is_not_truncated(self):
        source = '# التدريب ' * controller.ARABIC_PREVIEW_LIMIT
        self.document.editor.text = source
        self.app._update_arabic_preview()
        self.assertLessEqual(len(self.app.arabic_preview.text), controller.ARABIC_PREVIEW_LIMIT + 100)
        self.app._copy_arabic_preview()
        self.app.root.clipboard_append.assert_called_with(source)
        self.assertEqual(self.document.editor.text, source)

    def test_empty_line_does_not_clear_clipboard(self):
        self.document.editor.text = ''
        self.app._update_arabic_preview()
        self.assertEqual(self.app.arabic_preview.text, '')
        self.app._copy_arabic_preview()
        self.app.root.clipboard_clear.assert_not_called()
        self.app.root.clipboard_append.assert_not_called()

    def test_ascii_line_remains_ascii(self):
        self.document.editor.text = 'print("hello", 123)'
        self.app._update_arabic_preview()
        self.assertEqual(self.app.arabic_preview.text, self.document.editor.text)

    def test_missing_shaping_dependencies_are_explicit(self):
        with patch.object(controller, 'RTL_OK', False):
            self.app._update_arabic_preview()
        self.assertEqual(self.app.arabic_preview.text, self.source)
        self.assertIn('requirements.txt', self.app.arabic_preview_heading.configure.call_args.kwargs['text'])
        self.assertFalse(self.document.dirty)

    def test_same_line_cursor_move_does_not_render_again(self):
        self.app._update_arabic_preview()
        with patch.object(controller, 'to_visual') as render:
            self.document.editor.cursor = '1.9'
            self.app._update_arabic_preview()
        render.assert_not_called()

    def test_toggle_hides_and_restores_current_line_without_changing_document(self):
        self.app._update_arabic_preview()
        self.app._toggle_arabic_preview()
        self.app.arabic_preview_frame.pack_forget.assert_called_once()
        self.document.editor.text = '# تعليق جديد'
        self.app._toggle_arabic_preview()
        self.assertTrue(self.app._arabic_preview_visible)
        self.assertEqual(self.app._arabic_preview_source, '# تعليق جديد')
        self.assertEqual(self.document.saved_text, self.source)

    def test_shutdown_does_not_touch_preview_widgets(self):
        self.app._closing = True
        self.app._update_arabic_preview()
        self.app.arabic_preview_frame.pack.assert_not_called()

    def test_save_writes_original_arabic_not_display_forms(self):
        self.app._update_arabic_preview()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'arabic.py'
            self.assertTrue(self.app._save_to(str(path)))
            self.assertEqual(path.read_bytes(), self.source.encode('utf-8'))
        self.assertEqual(self.document.saved_text, self.source)


if __name__ == '__main__':
    unittest.main()
