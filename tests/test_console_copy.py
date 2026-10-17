"""Console-only clipboard filtering: app timestamps are tags, not text patterns."""
import types
import unittest
from unittest.mock import MagicMock

from test_controller import app_fixture, controller


def tearDownModule():
    if not controller._WORKER.loop.is_closed():
        controller._WORKER.loop.close()


class ConsoleCopyTests(unittest.TestCase):
    def setUp(self):
        self.app = app_fixture()
        self.widget = self.app.console_w = MagicMock()
        self.widget.tag_ranges.return_value = ('1.0', '3.0')
        self.widget.tag_names.return_value = ('sel', 'ts')

    def copy(self, events):
        self.widget.dump.return_value = events
        self.assertEqual(self.app._on_console_copy(), 'break')
        self.widget.clipboard_clear.assert_called_once_with()
        return self.widget.clipboard_append.call_args.args[0]

    def test_removes_only_tagged_timestamps_across_lines_and_message_types(self):
        text = self.copy([
            ('text', '[12:30:00] ', '1.0'),
            ('tagoff', 'ts', '1.11'),
            ('tagon', 'out', '1.11'),
            ('text', 'robot.straight(100)\n', '1.11'),
            ('tagoff', 'out', '2.0'),
            ('tagon', 'ts', '2.0'),
            ('text', '[12:30:01] ', '2.0'),
            ('tagoff', 'ts', '2.11'),
            ('tagon', 'info', '2.11'),
            ('text', 'ready\n', '2.11'),
        ])
        self.assertEqual(text, 'robot.straight(100)\nready\n')
        self.widget.dump.assert_called_once_with('1.0', '3.0', text=True, tag=True)

    def test_keeps_clock_shaped_robot_output_and_untagged_text(self):
        self.widget.tag_names.return_value = ('out', 'sel')
        expected = '[12:34:56] robot timestamp\n    robot.drive(100, 0)\n'
        self.assertEqual(self.copy([('text', expected, '1.0')]), expected)

    def test_partial_timestamp_at_selection_start_is_removed(self):
        self.widget.tag_ranges.return_value = ('1.4', '1.24')
        self.assertEqual(self.copy([
            ('text', '30:00] ', '1.4'),
            ('tagoff', 'ts', '1.11'),
            ('text', 'robot.turn(90)', '1.11'),
        ]), 'robot.turn(90)')

    def test_partial_timestamp_at_selection_end_is_removed_without_losing_newline(self):
        self.widget.tag_names.return_value = ('out', 'sel')
        self.assertEqual(self.copy([
            ('text', 'selected text\n', '1.11'),
            ('tagon', 'ts', '2.0'),
            ('text', '[12:', '2.0'),
        ]), 'selected text\n')

    def test_selection_only_in_timestamp_clears_old_clipboard_content(self):
        self.assertEqual(self.copy([('text', '[12:30:00]', '1.0')]), '')

    def test_no_selection_does_not_touch_clipboard_or_fall_back_to_native_copy(self):
        self.widget.tag_ranges.return_value = ()
        self.assertEqual(self.app._on_console_copy(), 'break')
        self.widget.clipboard_clear.assert_not_called()
        self.widget.clipboard_append.assert_not_called()
        self.widget.dump.assert_not_called()

    def test_copy_does_not_change_visible_output_raw_logs_or_training(self):
        self.app.console_raw = ['[12:30:00] robot.turn(90)']
        self.app.training_lines = ['robot.turn(90)']
        self.copy([('tagoff', 'ts', '1.11'), ('text', 'robot.turn(90)', '1.11')])
        self.assertEqual(self.app.console_raw, ['[12:30:00] robot.turn(90)'])
        self.assertEqual(self.app.training_lines, ['robot.turn(90)'])
        self.widget.delete.assert_not_called()
        self.widget.insert.assert_not_called()

    def test_ctrl_c_dispatches_filtered_copy_and_stops_native_binding(self):
        self.app._on_console_copy = MagicMock(return_value='break')
        self.assertEqual(self.app._console_key(types.SimpleNamespace(keysym='c', state=4)), 'break')
        self.app._on_console_copy.assert_called_once_with()

    def test_button_and_popup_share_the_filtered_virtual_copy_path(self):
        self.app.console = MagicMock()
        self.app._copy_console()
        self.widget.event_generate.assert_called_once_with('<<Copy>>')
        self.widget.event_generate.reset_mock()
        menu = MagicMock()
        self.app._popup_copy_items(menu, self.widget)
        menu.add_command.call_args_list[0].kwargs['command']()
        self.widget.event_generate.assert_called_once_with('<<Copy>>')
