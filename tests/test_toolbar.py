"""Font-independent toolbar glyphs and unchanged command-state behavior."""
import unittest
from unittest.mock import MagicMock, patch

from spike_icons import ICON_PATHS, icon_pixels
from test_controller import app_fixture, controller


def tearDownModule():
    if not controller._WORKER.loop.is_closed():
        controller._WORKER.loop.close()


class IconTests(unittest.TestCase):
    def test_all_icons_are_nonempty_and_inside_canvas_at_each_scale(self):
        for name in ICON_PATHS:
            for size in (12, 16, 18, 24, 32, 64):
                with self.subTest(name=name, size=size):
                    pixels = icon_pixels(name, size)
                    self.assertGreater(len(pixels), 10)
                    self.assertTrue(all(0 <= x < size and 0 <= y < size for x, y in pixels))

    def test_all_actions_have_distinct_glyphs(self):
        glyphs = [frozenset(icon_pixels(name)) for name in ICON_PATHS]
        self.assertEqual(len(set(glyphs)), len(ICON_PATHS))

    def test_stop_is_a_filled_square(self):
        pixels = icon_pixels('stop', 18)
        self.assertTrue(all((x, y) in pixels for x in range(4, 14) for y in range(4, 14)))

    def test_scaling_increases_opaque_pixels(self):
        for name in ICON_PATHS:
            self.assertGreater(len(icon_pixels(name, 36)), len(icon_pixels(name, 18)))

    def test_invalid_sizes_and_unknown_icon_fail_explicitly(self):
        for size in (0, 11, 65, True, None, 18.0):
            with self.subTest(size=size), self.assertRaises(ValueError):
                icon_pixels('stop', size)
        with self.assertRaises(KeyError):
            icon_pixels('missing')


class ToolbarStateTests(unittest.TestCase):
    def test_connection_busy_and_stop_states_are_unchanged(self):
        for connected, busy in ((False, False), (True, False), (True, True), (False, True)):
            with self.subTest(connected=connected, busy=busy):
                app = app_fixture()
                app.connected, app.busy = connected, busy
                app.hub = object() if connected else None
                for name in ('connect', 'disconnect', 'usb', 'send', 'sendwait', 'dl', 'stored', 'stop'):
                    setattr(app, 'btn_' + name, MagicMock())
                controller.App._refresh_buttons(app)
                app.btn_connect.configure.assert_called_with(state='disabled' if connected or busy else 'normal')
                app.btn_usb.configure.assert_called_with(state='disabled' if connected or busy else 'normal')
                app.btn_disconnect.configure.assert_called_with(state='normal' if connected else 'disabled')
                for button in (app.btn_send, app.btn_sendwait, app.btn_dl, app.btn_stored):
                    button.configure.assert_called_with(state='normal' if connected and not busy else 'disabled')
                app.btn_stop.configure.assert_called_with(state='normal' if connected or busy else 'disabled')

    def test_serial_label_and_icon_follow_running_state(self):
        app = app_fixture()
        for running, title, icon in ((False, 'استقبال COM', 'serial'), (True, 'إيقاف COM', 'stop')):
            app.serial_running = running
            app._sync_serial_button()
            app.btn_serial.configure.assert_called_with(text=controller.to_visual(title), image=app._toolbar_images[icon])

    def test_serial_completion_updates_button_without_resetting_training(self):
        app = app_fixture()
        app.training_lines = ['robot.turn(90)']
        app.serial_running = True
        app._handle_msg(('serial_state', False, app.serial_stop))
        self.assertFalse(app.serial_running)
        self.assertEqual(app.training_lines, ['robot.turn(90)'])
        app.btn_serial.configure.assert_called_with(text=controller.to_visual('استقبال COM'), image=app._toolbar_images['serial'])

    def test_source_saving_preserves_raw_arabic_without_preview(self):
        app = app_fixture()
        source = 'message = "مرحبا بالعالم"  # التعليق الأصلي\n'
        app.editor.text = source
        with patch.object(controller, 'atomic_write') as write:
            self.assertTrue(app._save_to('arabic.py'))
        write.assert_called_once_with('arabic.py', source)
        self.assertFalse(app._editor_dirty())
        self.assertFalse(hasattr(app, 'arabic_preview'))

    def test_preview_implementation_has_been_removed(self):
        for name in ('_build_arabic_preview', '_toggle_arabic_preview', '_update_arabic_preview', '_copy_arabic_preview'):
            self.assertFalse(hasattr(controller.App, name))

    def test_small_window_reserves_useful_editor_and_console_space(self):
        app = app_fixture()
        app._sash_restored = False
        app.paned = MagicMock()
        app.cfg['sash'] = 440
        event = MagicMock(height=310)
        app._layout_panes(event)
        app.paned.sashpos.assert_called_with(0, 210)
        self.assertTrue(app._sash_restored)

    def test_resize_keeps_user_divider_position_when_it_fits(self):
        app = app_fixture()
        app._sash_restored = True
        app.paned = MagicMock()
        app.paned.sashpos.return_value = 260
        app.cfg['sash'] = 440
        app._layout_panes(MagicMock(height=700))
        app.paned.sashpos.assert_called_with(0, 260)
        self.assertEqual(app.cfg['sash'], 440)


if __name__ == '__main__':
    unittest.main()
