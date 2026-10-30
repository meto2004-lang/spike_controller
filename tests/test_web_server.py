"""End-to-end HTTP tests for the browser app (simulation, no robot needed)."""
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest

import spike_server


def request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = None
    base = {"X-Hyena-Client": "web"}
    if headers is not None:
        base.update(headers)
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        base["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=base)
    response = connection.getresponse()
    data = response.read()
    content_type = response.getheader("Content-Type") or ""
    connection.close()
    if "application/json" in content_type:
        return response.status, json.loads(data.decode("utf-8"))
    return response.status, data


class WebServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = spike_server.create_server("127.0.0.1", 0, simulate=True,
                                                data_dir=cls.tmp.name)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.app.shutdown()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    # -- basics --------------------------------------------------------
    def test_health_reports_simulation(self):
        status, data = request(self.port, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["simulation"])
        self.assertEqual(data["version"], spike_server.VERSION)

    def test_bootstrap_contains_workspace_and_examples(self):
        status, data = request(self.port, "GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertIn("settings", data)
        self.assertIn("starter", data)
        self.assertTrue(data["examples"])
        self.assertEqual(data["workspace"]["dir"], os.path.join(self.tmp.name, "programs"))

    def test_bootstrap_explains_the_bluetooth_transport(self):
        status, data = request(self.port, "GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        details = data["transport"]
        self.assertTrue(details["simulation"])
        self.assertIn(details["reason"], ("simulate_flag", "missing_pybricksdev"))
        self.assertIn("pybricksdev", details)
        self.assertIn("bleak", details)

    def test_index_is_served(self):
        status, body = request(self.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Hyena Control", body)

    def test_path_traversal_is_rejected(self):
        status, _ = request(self.port, "GET", "/../spike_server.py")
        self.assertIn(status, (403, 404))

    def test_post_without_client_header_is_forbidden(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("POST", "/api/files/save",
                           body=json.dumps({"name": "x.py", "content": ""}),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        connection.close()

    # -- files ---------------------------------------------------------
    def test_file_lifecycle(self):
        status, saved = request(self.port, "POST", "/api/files/save",
                                {"name": "alpha.py", "content": "print('a')\n"})
        self.assertEqual(status, 200)
        self.assertTrue(os.path.isfile(saved["path"]))
        names = [item["name"] for item in saved["files"]]
        self.assertIn("alpha.py", names)

        status, opened = request(self.port, "POST", "/api/files/open", {"name": "alpha.py"})
        self.assertEqual(opened["content"], "print('a')\n")

        status, renamed = request(self.port, "POST", "/api/files/rename",
                                  {"name": "alpha.py", "new_name": "beta.py"})
        self.assertEqual(status, 200)
        self.assertEqual(renamed["name"], "beta.py")

        status, deleted = request(self.port, "POST", "/api/files/delete", {"name": "beta.py"})
        self.assertEqual(status, 200)
        self.assertNotIn("beta.py", [item["name"] for item in deleted["files"]])

    def test_workspace_rejects_bad_names_and_escapes(self):
        for name in ("../escape.py", "/etc/passwd", "program.exe", "") :
            status, data = request(self.port, "POST", "/api/files/save",
                                   {"name": name, "content": "x"})
            self.assertEqual(status, 400, (name, data))

    def test_settings_round_trip(self):
        status, data = request(self.port, "POST", "/api/settings", {"lang": "en", "theme": "dark"})
        self.assertEqual(status, 200)
        self.assertEqual(data["settings"]["lang"], "en")
        self.assertEqual(data["settings"]["theme"], "dark")
        status, data = request(self.port, "POST", "/api/settings", {"lang": "xx", "retries": 99})
        self.assertEqual(data["settings"]["lang"], "en")       # invalid values are ignored
        self.assertEqual(data["settings"]["retries"], 6)
        request(self.port, "POST", "/api/settings", {"lang": "ar", "theme": "light"})

    # -- lint ----------------------------------------------------------
    def test_lint_reports_syntax_errors(self):
        status, data = request(self.port, "POST", "/api/lint", {"code": "if True\n  pass\n"})
        self.assertEqual(status, 200)
        self.assertEqual(data["diagnostics"][0]["line"], 1)
        self.assertEqual(data["diagnostics"][0]["severity"], "error")

    def test_lint_accepts_micropython_const(self):
        status, data = request(self.port, "POST", "/api/lint",
                               {"code": "from pybricks.tools import wait\nconst(2)\n"})
        self.assertEqual(data["diagnostics"], [])

    # -- training and race --------------------------------------------
    def test_race_generation_requires_data(self):
        request(self.port, "POST", "/api/training/reset", {})
        status, data = request(self.port, "POST", "/api/race/preview", {})
        self.assertEqual(status, 409)
        self.assertEqual(data["kind"], "training")

    def test_simulated_training_produces_a_race_program(self):
        request(self.port, "POST", "/api/training/reset", {})
        status, data = request(self.port, "POST", "/api/program/simulate-training", {})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["lines"], 3)
        status, race = request(self.port, "POST", "/api/race/preview", {})
        self.assertEqual(status, 200)
        self.assertIn("robot.straight(172)", race["code"])
        self.assertIn("DriveBase", race["code"])
        self.assertGreater(race["kept"], 0)

    # -- hub -----------------------------------------------------------
    def test_connect_run_and_stop(self):
        status, _ = request(self.port, "POST", "/api/hub/connect", {"target": None})
        self.assertIn(status, (200, 202))
        self.assertTrue(self.wait_for(lambda: request(self.port, "GET", "/api/state")[1]["state"]["connected"]))
        state = request(self.port, "GET", "/api/state")[1]["state"]
        self.assertEqual(state["mode"], "simulation")

        request(self.port, "POST", "/api/training/reset", {})
        status, _ = request(self.port, "POST", "/api/program/run", {
            "code": 'print("hello from the test")\n', "name": "test.py", "mode": "run"})
        self.assertEqual(status, 202)
        self.assertTrue(self.wait_for(
            lambda: any(entry.get("text") == "hello from the test"
                        for entry in request(self.port, "GET", "/api/logs")[1]["logs"])))
        self.assertTrue(self.wait_for(
            lambda: not request(self.port, "GET", "/api/state")[1]["state"]["busy"]))

        status, _ = request(self.port, "POST", "/api/hub/stop", {})
        self.assertEqual(status, 202)
        self.assertTrue(self.wait_for(
            lambda: not request(self.port, "GET", "/api/state")[1]["state"]["running"]))

        status, _ = request(self.port, "POST", "/api/hub/disconnect", {})
        self.assertEqual(status, 202)
        self.assertTrue(self.wait_for(
            lambda: not request(self.port, "GET", "/api/state")[1]["state"]["connected"]))

    def test_console_log_can_be_exported(self):
        status, body = request(self.port, "GET", "/api/logs/download")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, bytes)

    # -- events --------------------------------------------------------
    def test_event_stream_is_server_sent_events(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/api/events")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue((response.getheader("Content-Type") or "").startswith("text/event-stream"))
        first = response.readline()
        self.assertIn(b"retry:", first)
        connection.close()


if __name__ == "__main__":
    unittest.main()
