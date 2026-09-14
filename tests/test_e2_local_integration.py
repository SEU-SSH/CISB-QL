"""Opt-in real MCP/LSP/CLI checks; every model response is offline or mock HTTP."""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import httpx

from agent_backend import ResponsesBackend
from codeql_tools import MCPError, mcp_session, write_pack
from harness import run_sample, save_json
import run
from test_e1 import CONFIG, GOOD, PACK, SPEC, FakeCompiler, generated, parse_spec
from test_e2 import tool_response


ROOT = Path(__file__).resolve().parents[1]
CODEQL = os.environ.get("CODEQL_PATH", "/usr/local/codeql/codeql")
ENTRY = Path(os.environ.get("CODEQL_MCP_ENTRY", str(Path.home() / "codeql-lsp-mcp/dist/index.js")))


@unittest.skipUnless(os.environ.get("RUN_E2_TESTS") == "1", "set RUN_E2_TESTS=1 for real local MCP/LSP/CLI")
class E2LocalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.out = ROOT / "runs" / f"e2-local-{timestamp}"

    async def assert_processes_closed(self, stderr_path):
        pids = re.findall(r"CodeQL (?:MCP|LSP) pid=(\d+)", stderr_path.read_text())
        self.assertGreaterEqual(len(pids), 2, stderr_path.read_text())
        for pid in map(int, pids):
            for _ in range(40):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.1)
            else:
                self.fail(f"MCP/LSP process {pid} survived shutdown")

    async def test_real_tools_format_error_repair_and_clean_candidate(self):
        bad = {**GOOD, "qll_code": GOOD["qll_code"] + "\npredicate bad(MissingTypeForE2 f) { any() }\n"}
        requests = []

        def handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            body = json.loads(request.content)
            requests.append(body)
            self.assertFalse(body["parallel_tool_calls"])
            self.assertEqual({tool["name"] for tool in body["tools"]},
                             {"codeql_hover", "codeql_definition", "codeql_complete"})
            index = len(requests) - 1
            if index == 0:
                raw = generated("invalid JSON")
            elif index == 1:
                raw = tool_response(1)
            elif index == 2:
                self.assertEqual(body["input"][1:-1], tool_response(1)["output"])
                self.assertIn("Function", body["input"][-1]["output"])
                raw = generated(json.dumps(bad))
            elif index == 3:
                payload = json.loads(body["input"][0]["content"])
                self.assertEqual(payload["attempt"], 2)
                self.assertIn("MissingTypeForE2", json.dumps(payload["previous_attempt"]["diagnostics"]))
                self.assertTrue(payload["tool_context"]["directory"].endswith("attempt_1"))
                raw = tool_response(3, name="codeql_definition")
                completion = tool_response(4, name="codeql_complete",
                    arguments={"file": "query.ql", "line": 5, "character": 12})
                raw["output"].append(completion["output"][-1])
            elif index == 4:
                definition = json.loads(body["input"][-2]["output"])["result"]
                completion = json.loads(body["input"][-1]["output"])["result"]
                self.assertTrue(definition["sources"])
                self.assertIn("cpp-all/7.0.0", json.dumps(definition))
                self.assertTrue(completion["items"])
                self.assertLessEqual(len(completion["items"]), 20)
                raw = generated()
            else:
                raise AssertionError("unexpected extra model request")
            raw.update(model="OFFLINE_HTTP_MOCK_NOT_A_REAL_MODEL", usage={
                "input_tokens": 10, "output_tokens": 10, "total_tokens": 20,
                "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 2}})
            return httpx.Response(200, json=raw)

        def factory(config, key, url):
            return ResponsesBackend(config, key, url, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        args = run.arguments(["--spec", "specs/12051b318b_spec.md", "--mcp", "on", "--out", str(self.out)])
        try:
            with patch.dict(os.environ, {"QL_API_KEY": "offline-test-key", "QL_BASE_URL": "https://offline.invalid/v1"}):
                code = await run.run_batch(ROOT, args, factory)
        finally:
            path = self.out / "experiment.json"
            if path.exists():
                experiment = json.loads(path.read_text())
                experiment.update(validation_only=True, not_research_result=True, model_transport="httpx.MockTransport")
                save_json(path, experiment)
        self.assertEqual(code, 0)
        directory = self.out / "12051b318b"
        summary = json.loads((directory / "summary.json").read_text())
        self.assertEqual(summary["successful_attempt"], 2)
        self.assertEqual(summary["model_calls"], 5)
        self.assertEqual(summary["model_tool_calls"], 3)
        self.assertEqual(summary["compile_calls"], 2)
        self.assertEqual(summary["mcp_sessions"], 3)
        self.assertEqual(summary["mcp_calls"], 18)
        self.assertEqual(summary["mcp_failures"], 0)
        self.assertEqual(summary["tokens"]["total_tokens"], 100)
        self.assertFalse((directory / "attempt_0/query.ql").exists())
        self.assertEqual((directory / "attempt_1/query.qll").read_text(), bad["qll_code"])
        self.assertEqual((directory / "attempt_2/query.qll").read_text(), GOOD["qll_code"])
        clean = json.loads((directory / "attempt_2/diagnostics.json").read_text())
        self.assertEqual(clean["mcp"]["diagnostics"], {"query.qll": [], "query.ql": []})
        for path in (directory / "mcp").glob("*/stderr.txt"):
            await self.assert_processes_closed(path)
        print(f"E2 LOCAL VALIDATION ONLY (mock model): {self.out}", flush=True)

    async def test_mcp_request_timeout_closes_real_processes(self):
        self.out.mkdir()
        write_pack(self.out / "template", PACK)
        receipt = {"validation_only": True, "not_research_result": True}
        log = self.out / "stderr.txt"

        async def stalled(*args, **kwargs):
            await asyncio.sleep(30)

        try:
            with self.assertRaises(MCPError):
                async with mcp_session(self.out / "template", CODEQL, ENTRY, 120, receipt, log) as client:
                    client.timeout = 0.01
                    with patch.object(client.session, "call_tool", side_effect=stalled):
                        await client.request("codeql_hover", {"file_uri": (self.out / "template/query.ql").as_uri(),
                                                               "line": 3, "character": 5})
        finally:
            save_json(self.out / "session.json", receipt)
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("TimeoutError", receipt["calls"][-1]["error"])
        self.assertEqual(receipt["close_status"], "closed")
        await self.assert_processes_closed(log)

    async def test_cancelled_model_cleans_up_active_real_mcp_session(self):
        self.out.mkdir()
        started = asyncio.Event()

        class WaitingBackend:
            async def generate(self, *args, **kwargs):
                started.set()
                await asyncio.sleep(300)

        def factory(path, receipt, stderr_path):
            return mcp_session(path, CODEQL, ENTRY, 120, receipt, stderr_path)

        directory = self.out / "cancelled"
        task = asyncio.create_task(run_sample({"source_id": "cancelled", "raw": SPEC, "parsed": parse_spec(SPEC)},
            directory, CONFIG, "offline cancellation test", PACK, WaitingBackend(), FakeCompiler(), mcp_factory=factory))
        try:
            await asyncio.wait_for(started.wait(), 60)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            save_json(self.out / "validation.json", {"validation_only": True, "not_research_result": True})
        summary = json.loads((directory / "summary.json").read_text())
        self.assertEqual(summary["reason"], "cancelled")
        self.assertEqual(summary["compile_calls"], 0)
        await self.assert_processes_closed(directory / "mcp/template/stderr.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
