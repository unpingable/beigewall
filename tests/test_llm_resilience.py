import json
import os
import tempfile
import unittest

from blackwall.agent.drivers import LLMDriver, ScriptedDriver
from blackwall.llm_experiment import run_llm_experiment


class TestRunnerResumability(unittest.TestCase):
    def test_sealed_runs_are_reused_not_rerun(self):
        factory = lambda task: ScriptedDriver(task.script, task.narrative)
        with tempfile.TemporaryDirectory() as d:
            run_llm_experiment(factory, d, label="rs", model="scripted",
                               base_url="n/a", runs=1)
            sealed_path = os.path.join(d, "run-1", "metrics.json")
            sealed_before = open(sealed_path).read()
            result = run_llm_experiment(factory, d, label="rs",
                                        model="scripted", base_url="n/a",
                                        runs=2)
            # run-1 untouched; run-2 created; aggregate spans both
            self.assertEqual(open(sealed_path).read(), sealed_before)
            self.assertTrue(os.path.exists(os.path.join(d, "run-2",
                                                        "metrics.json")))
            self.assertEqual(result["aggregate"]["suite"]["runs"], 2)
            self.assertEqual(result["meta"]["runs_completed"], [1, 2])
            self.assertEqual(result["meta"]["runs_reused"], [1])


class TestRetryBackoff(unittest.TestCase):
    def test_multiple_transient_failures_absorbed(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class TwiceFlaky(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.server.request_count += 1
                if self.server.request_count <= 2:
                    payload = b'{"error": "transient"}'
                    self.send_response(500)
                else:
                    payload = json.dumps({"choices": [{"message": {
                        "role": "assistant", "content": "ok"}}]}).encode()
                    self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), TwiceFlaky)
        server.request_count = 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            driver = LLMDriver(f"http://127.0.0.1:{server.server_port}", "k", "m")
            driver._sleep = lambda s: None
            driver.begin({"task_id": "t", "prompt": "p", "prompt_hash": "h"})
            move = driver.act([])
            self.assertEqual(move, {"op": "final", "answer": "ok"})
            self.assertEqual(driver.telemetry()["transport_retries"], 2)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
