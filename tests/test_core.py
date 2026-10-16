from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from spike_core import EventQueue, atomic_write, training_line, validated_config


class CoreTests(unittest.TestCase):
    def test_safe_commands_are_rebuilt(self):
        for text, expected in [
            ('robot.straight(100)', 'robot.straight(100)'),
            (' robot.turn(-32.5) # turn', 'robot.turn(-32.5)'),
            ('robot.stop()', 'robot.stop()'),
            ('robot.drive(+200, -90)', 'robot.drive(200, -90)'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(training_line(text), expected)

    def test_unsafe_commands_rejected(self):
        for text in [
            "robot.straight(100); print('bad')", 'robot.turn(__import__("os"))',
            'robot.straight(1 + 2)', 'robot.straight(True)', 'robot.straight(1e999)',
            'robot.straight(10001)', 'robot.turn(-3601)', 'robot.drive(1001, 0)',
            'robot.stop(1)', 'robot.straight(*[100])', 'robot.turn(angle=90)',
            'robot.turn(90)\nrobot.stop()', 'robot.straight_time(10)',
            'robot.turn(0).anything()', 'robot.straight(100) if True else None',
            'robot.turn = 90', 'robot.turn(',
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                training_line(text)

    def test_unrelated_lines_ignored_and_comments_preserved(self):
        self.assertIsNone(training_line('battery: 100%'))
        self.assertEqual(training_line('# حساس أيسر: 20'), '# حساس أيسر: 20')
        self.assertEqual(training_line('# sensor: black'), '# sensor: black')

    def test_atomic_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'program.py'
            path.write_text('old')
            with patch('spike_core.os.replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    atomic_write(str(path), 'new')
            self.assertEqual(path.read_text(encoding="utf-8"), 'old')
            self.assertEqual(list(Path(d).iterdir()), [path])

    def test_atomic_write_unicode(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'program.py'
            atomic_write(str(path), '# حساس\nrobot.turn(10)\n')
            self.assertEqual(path.read_text(encoding="utf-8"), '# حساس\nrobot.turn(10)\n')

    def test_config_validates_types_bounds_and_does_not_share_lists(self):
        defaults = dict(geometry='1220x780', sash=440, retries=6, scan_timeout=20,
                        recent=[], timestamps=True, autolog=True, autoconnect=False)
        result = validated_config(dict(geometry='broken', recent=[None, 'a', 'a', 2, 'b'],
                                       retries=True, scan_timeout=-1, timestamps='false'), defaults)
        self.assertEqual(result['recent'], ['a', 'b'])
        self.assertEqual(result['retries'], 6)
        self.assertEqual(result['scan_timeout'], 20)
        self.assertTrue(result['timestamps'])
        self.assertEqual(result['geometry'], '1220x780')
        result['recent'].append('c')
        self.assertEqual(defaults['recent'], [])
        self.assertEqual(validated_config([], defaults), defaults)

    def test_queue_never_drops_control_and_reports_loss(self):
        q = EventQueue(limit=1)
        q.put(('console', 'first', 'out'))
        q.put(('console', 'dropped', 'out'))
        q.put(('op_done', 1))
        self.assertEqual(q.qsize(), 2)
        self.assertEqual(q.take_dropped(), 1)
        self.assertEqual(q.take_dropped(), 0)
        self.assertEqual(q.get_nowait()[1], 'first')
        q.task_done()
        self.assertEqual(q.get_nowait()[0], 'op_done')
        q.task_done()
        self.assertEqual(q.console_pending, 0)
        q.join()

    def test_session_boundary_discards_pending_data_not_control(self):
        q = EventQueue()
        q.put(('console', 'old', 'out'))
        q.put(('data_loss', 'old loss'))
        q.put(('op_done', 1))
        q.discard_console()
        self.assertEqual(q.qsize(), 1)
        self.assertEqual(q.get_nowait()[0], 'op_done')
        q.task_done()
        q.join()
        self.assertEqual(q.console_pending, 0)
        with self.assertRaises(queue.Empty):
            q.get_nowait()


if __name__ == '__main__':
    unittest.main()
