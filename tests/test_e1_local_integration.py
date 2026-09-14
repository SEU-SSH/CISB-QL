"""Opt-in real CodeQL pipeline check, with mock HTTP model responses only."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx

from agent_backend import ResponsesBackend
import run


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("RUN_E1_TESTS") == "1", "set RUN_E1_TESTS=1 for real local CodeQL")
class E1LocalTests(unittest.IsolatedAsyncioTestCase):
    async def test_format_then_real_ql_error_then_compile_success(self):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        out = f"runs/e1-responses-local-{timestamp}"
        good = {"qll_code": (ROOT / "codeql-pack/query.qll").read_text(),
                "ql_code": (ROOT / "codeql-pack/query.ql").read_text()}
        bad = {**good, "qll_code": good["qll_code"].replace("Function", "MissingTypeForE1")}
        replies = iter(["invalid JSON", json.dumps(bad), json.dumps(good)])
        requests = []

        def handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            body = json.loads(request.content)
            requests.append(body)
            content = next(replies)
            return httpx.Response(200, json={
                "id": "resp_e1_offline", "object": "response", "created_at": 0, "status": "completed",
                "model": "OFFLINE_HTTP_MOCK_NOT_A_REAL_MODEL",
                "output": [{"id": "msg_e1_offline", "type": "message", "status": "completed", "role": "assistant",
                            "content": [{"type": "output_text", "text": content, "annotations": []}]}],
                "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}})

        def factory(config, key, url):
            return ResponsesBackend(config, key, url, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        args = run.arguments(["--spec", "specs/12051b318b_spec.md", "--mcp", "off", "--out", out])
        with patch.dict(os.environ, {"QL_API_KEY": "offline-test-key", "QL_BASE_URL": "https://offline.invalid/v1"}):
            code = await run.run_batch(ROOT, args, factory)
        experiment_path = ROOT / out / "experiment.json"
        experiment = json.loads(experiment_path.read_text())
        experiment.update(validation_only=True, model_transport="httpx.MockTransport", not_research_result=True)
        experiment_path.write_text(json.dumps(experiment, indent=2) + "\n")
        self.assertEqual(code, 0)
        directory = ROOT / out / "12051b318b"
        summary = json.loads((directory / "summary.json").read_text())
        self.assertEqual(summary["successful_attempt"], 2)
        self.assertEqual(summary["model_calls"], 3)
        self.assertEqual(summary["compile_calls"], 2)
        self.assertFalse((directory / "attempt_0/query.ql").exists())
        self.assertEqual((directory / "attempt_1/query.qll").read_text(), bad["qll_code"])
        self.assertEqual((directory / "attempt_2/query.qll").read_text(), good["qll_code"])
        self.assertEqual((directory / "spec.md").read_bytes(), (ROOT / "specs/12051b318b_spec.md").read_bytes())
        feedback = json.loads(requests[2]["input"][0]["content"])
        self.assertIn("MissingTypeForE1", feedback["previous_attempt"]["diagnostics"]["cli"]["stdout"])
        self.assertEqual(summary["semantic_status"], "not_evaluated")
        self.assertEqual(summary["api_format"], "responses")
        self.assertEqual(summary["tokens"], {"input_tokens": 30, "output_tokens": 30, "total_tokens": 60})
        self.assertEqual(experiment["api_format"], "responses")
        self.assertTrue(all(request["instructions"] == experiment["prompt"] for request in requests))
        print(f"E1 LOCAL TOOL CHECK ONLY (mock model): {ROOT / out}", flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
