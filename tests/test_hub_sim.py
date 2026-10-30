"""Headless tests for the browser app's hub controller (simulation transport)."""
import time
import unittest

from spike_hub import HubBusy, HubController, HubError, _scripted_steps
from spike_race import TrainingSession


def wait_until(predicate, timeout=8.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class SimulationHubTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.training = TrainingSession()
        self.hub = HubController(self.events.append, self.training, force_simulation=True)
        self.hub.start()

    def tearDown(self):
        self.hub.close()

    def console(self, level=None):
        return [event for event in self.events
                if event["t"] == "console" and (level is None or event["level"] == level)]

    def test_starts_in_simulation_mode(self):
        self.assertTrue(self.hub.simulation)
        self.assertEqual(self.hub.state()["mode"], "simulation")

    def test_connect_reports_state_and_firmware(self):
        self.hub.connect()
        self.assertTrue(wait_until(lambda: self.hub.state()["connected"]), self.events)
        state = self.hub.state()
        self.assertEqual(state["transport"], "sim")
        self.assertEqual(state["firmware"], "4.1.0-sim")
        self.assertIn("Connected", " ".join(event.get("text", "") for event in self.console()))

    def test_run_streams_program_output_and_finishes(self):
        self.hub.connect()
        self.assertTrue(wait_until(lambda: self.hub.state()["connected"]))
        self.hub.run('print("hello hub")\nrobot.straight(120)\nwait(50)\n', "demo.py")
        self.assertTrue(wait_until(
            lambda: any(event.get("text") == "hello hub" for event in self.console())),
            "program output was not streamed")
        self.assertTrue(wait_until(lambda: not self.hub.state()["busy"]))
        self.assertTrue(wait_until(lambda: not self.hub.state()["running"]))
        self.assertTrue(any("robot.straight(120)" in event.get("text", "")
                            for event in self.console()))

    def test_training_lines_reach_the_session(self):
        self.hub.connect()
        self.assertTrue(wait_until(lambda: self.hub.state()["connected"]))
        for line in ("robot.straight(100)", "robot.turn(90)", "robot.stop()"):
            self.hub.log("out", line, record_training=True)
        snapshot = self.training.snapshot()
        self.assertEqual(snapshot["lines"], 3)
        result = self.training.generate()
        self.assertIn("robot.straight(100)", result["code"])
        self.assertEqual(result["kept"], 3)

    def test_second_operation_is_rejected_while_busy(self):
        self.hub.connect()
        self.assertTrue(wait_until(lambda: self.hub.state()["connected"]))
        self.assertTrue(wait_until(lambda: not self.hub.state()["busy"]))
        self.hub.run('print("a")\n', "a.py")
        with self.assertRaises(HubBusy):
            self.hub.run('print("b")\n', "b.py")
        self.assertTrue(wait_until(lambda: not self.hub.state()["busy"]))

    def test_stop_requires_a_connection(self):
        self.hub.stop()
        self.assertTrue(wait_until(
            lambda: any("not connected" in event.get("text", "").lower()
                        for event in self.console())), self.events)

    def test_download_only_does_not_start_the_program(self):
        self.hub.connect()
        self.assertTrue(wait_until(lambda: self.hub.state()["connected"]))
        self.hub.run('print("stored")\n', "stored.py", mode="download")
        self.assertTrue(wait_until(lambda: not self.hub.state()["busy"]))
        self.assertFalse(self.hub.state()["running"])
        self.assertTrue(self.hub.state()["can_run_stored"])
        self.assertFalse(any(event.get("text") == "stored" for event in self.console()))

    def test_disconnect_clears_the_connection(self):
        self.hub.connect()
        self.assertTrue(wait_until(lambda: self.hub.state()["connected"]))
        self.hub.disconnect()
        self.assertTrue(wait_until(lambda: not self.hub.state()["connected"]))
        self.assertEqual(self.hub.state()["label"], "")


class ScriptedStepTests(unittest.TestCase):
    def test_prints_and_moves_keep_source_order(self):
        steps = _scripted_steps('print("one")\nrobot.turn(90)\nprint("two")\n')
        self.assertEqual([kind for kind, _ in steps], ["print", "move", "print"])
        self.assertEqual(steps[1][1], "robot.turn(90)")

    def test_wait_values_are_capped_by_the_player(self):
        steps = _scripted_steps("wait(10000)\nwait(50)\n")
        self.assertEqual(steps, [("wait", 10000), ("wait", 50)])

    def test_syntax_errors_do_not_raise(self):
        steps = _scripted_steps("if True\n  pass\n")
        self.assertEqual(steps[0][0], "print")
        self.assertIn("syntax error", steps[0][1])

    def test_non_literal_arguments_are_masked(self):
        steps = _scripted_steps("robot.straight(distance)\n")
        self.assertIn("…", steps[0][1])


if __name__ == "__main__":
    unittest.main()
