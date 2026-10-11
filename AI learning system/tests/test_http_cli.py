"""Real local HTTP transport + subprocess CLI; no remote model or credentials."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from learning_system.model import ChatModel, ModelError, TruncatedOutput, structured
from support import PROBLEM, RESOURCE_ONE, RESOURCE_TWO, analysis_for, answer_for


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def server(response_factory=None):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "body": body})
            payload = json.loads(body["messages"][1]["content"])["input"]
            if response_factory:
                response = response_factory(body)
            else:
                output = analysis_for(payload) if "kind" in payload else answer_for(payload)
                response = {"choices": [{"finish_reason": "stop", "message": {
                    "content": json.dumps(output, ensure_ascii=False)}}]}
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", received
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join()


class HTTPCLITests(unittest.TestCase):
    def test_complete_cli_workflow_through_local_chat_endpoint(self):
        with tempfile.TemporaryDirectory() as folder, server() as (url, received):
            folder = Path(folder)
            resources, problems = folder / "resources", folder / "problems"
            resources.mkdir()
            problems.mkdir()
            (resources / "a.md").write_text(RESOURCE_ONE)
            (resources / "b.md").write_text(RESOURCE_TWO)
            (problems / "q.md").write_text(PROBLEM)
            env = {**os.environ, "LLM_BASE_URL": url, "LLM_MODEL": "fixture-model", "LLM_API_KEY": "",
                   "DEEPSEEK_API_KEY": "", "LLM_JSON_MODE": "1", "LLM_MAX_TOKENS": "6000"}
            db = folder / "demo.sqlite3"
            def run(*args):
                return subprocess.run([sys.executable, "-m", "learning_system", *args, "--db", str(db)],
                                      cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
            for kind, path in [("resource", resources), ("problem", problems)]:
                result = run("ingest", str(path), "--kind", kind)
                self.assertEqual(0, result.returncode, result.stderr)
            detail = json.loads(run("point", "矩阵秩").stdout)
            self.assertEqual(1, len(detail["problems"]))
            self.assertEqual("秩计算", detail["problems"][0]["question_type"])
            answer = json.loads(run("ask", "线性方程组什么时候唯一可解？", "--json").stdout)
            self.assertEqual("answered", answer["status"])
            self.assertTrue(answer["citations"][0]["quote"])
            count = len(received)
            self.assertEqual(0, run("ingest", str(resources), "--kind", "resource").returncode)
            self.assertEqual(count, len(received))
            unsupported = json.loads(run("ask", "矩阵与张量分解有什么关系？", "--json").stdout)
            self.assertEqual("insufficient", unsupported["status"])
            self.assertEqual(2, len(json.loads(run("history").stdout)))
            self.assertEqual(3, len(json.loads(run("plan").stdout)))
            self.assertTrue(all(r["path"] == "/chat/completions" for r in received))
            self.assertTrue(all(r["body"]["response_format"] == {"type": "json_object"} for r in received))

    def test_invalid_model_response_and_transport_error_are_not_silently_answered(self):
        with self.assertRaises(ModelError):
            ChatModel(base_url="http://external.example", model="m")
        with self.assertRaises(ModelError):
            ChatModel(base_url="https://api.deepseek.com", model="m", api_key="").complete("JSON", {}, {})

    def test_truncation_is_not_retried_and_role_budgets_are_sent(self):
        def truncated(body):
            return {"choices": [{"finish_reason": "length", "message": {"content": "{"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 256,
                              "completion_tokens_details": {"reasoning_tokens": 0}}}
        with patch.dict(os.environ, {"LLM_QA_MAX_TOKENS": "256", "LLM_ANALYSIS_MAX_TOKENS": "768",
                                     "LLM_THINKING": "disabled", "LLM_LOG_STATS": "0"}), server(truncated) as (url, received):
            model = ChatModel(url, "fixture")
            with self.assertRaises(TruncatedOutput):
                structured(model, "JSON", {"question": "测试"}, {}, lambda result: None)
            self.assertEqual(1, len(received))
            self.assertEqual(256, received[0]["body"]["max_tokens"])
            self.assertEqual({"type": "disabled"}, received[0]["body"]["thinking"])
            self.assertEqual(0, model.last_stats["reasoning_tokens"])
            with self.assertRaises(TruncatedOutput):
                model.complete("JSON", {"kind": "resource"}, {})
            self.assertEqual(768, received[1]["body"]["max_tokens"])


if __name__ == "__main__":
    unittest.main()
