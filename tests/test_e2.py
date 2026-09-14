from contextlib import asynccontextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mcp.types import CallToolResult, TextContent

from codeql_tools import CandidateMCP, MCPError, mcp_session, model_tools, write_pack
from harness import run_sample
import run
import test_e1 as e1
from test_e1 import CONFIG, GOOD, PACK, SPEC, FakeBackend, FakeCompiler, generated, parse_spec


def tool_response(index=0, name="codeql_hover", arguments=None, count=1):
    arguments = arguments if arguments is not None else {"file": "query.ql", "line": 3, "character": 5}
    output = [{"type": "reasoning", "id": f"rs_{index}", "summary": [],
               "content": [{"type": "reasoning_text", "text": "inspect current candidate"}]}]
    output.extend({"type": "function_call", "id": f"fc_{index}_{i}", "call_id": f"call_{index}_{i}",
                   "name": name, "arguments": json.dumps(arguments), "status": "completed"} for i in range(count))
    return {**generated(), "output": output}


class FakeMCP:
    def __init__(self, fail_on=None, fail_tool=False, close_failure=False):
        self.opened, self.closed, self.invocations = [], [], []
        self.fail_on, self.fail_tool, self.close_failure = fail_on, fail_tool, close_failure
        self.active = None

    @asynccontextmanager
    async def __call__(self, path, receipt, stderr_path):
        if self.active is not None:
            raise AssertionError("previous session was not closed")
        self.opened.append(path.name)
        receipt.update(status="starting", calls=[])
        stderr_path.write_text("mock MCP stderr\n")
        if self.fail_on == path.name:
            receipt.update(status="failed", error="offline MCP startup failure")
            raise MCPError("offline MCP startup failure")
        client = CandidateMCP(None, path, 1, receipt)
        client.diagnostics = {"query.ql": [], "query.qll": []}
        if "MissingType" in client.files["query.qll"].decode():
            client.diagnostics["query.qll"] = [{"severity": 1, "message": "LSP MissingType",
                "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 11}}}]
        receipt.update(status="ready", diagnostics=client.diagnostics)
        self.active = path.name

        async def invoke(name, arguments):
            params = client.arguments(name, arguments)
            self.invocations.append((path.name, name, params))
            receipt["calls"].append({"operation": name, "actor": "model", "status": "passed"})
            if self.fail_tool:
                raise MCPError("offline MCP tool failure")
            return {"contents": "Function documentation"}

        client.invoke = invoke
        try:
            yield client
        finally:
            client.unchanged()
            self.active = None
            self.closed.append(path.name)
            receipt.update(status="closed", close_status="closed")
            if self.close_failure:
                raise MCPError("offline close failure")


class E2HarnessTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, outputs, statuses, service=None, config=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "sample"
        self.backend, self.compiler = FakeBackend(outputs), FakeCompiler(statuses)
        self.service = service or FakeMCP()
        return await run_sample({"source_id": "sample", "raw": SPEC, "parsed": parse_spec(SPEC)}, self.directory,
                                config or CONFIG, "same prompt", PACK, self.backend, self.compiler,
                                mcp_factory=self.service)

    async def test_tools_on_template_then_failed_candidate_with_fresh_diagnostics(self):
        bad = {**GOOD, "qll_code": "import cpp\npredicate p(MissingType f) { any() }\n"}
        summary = await self.exercise([tool_response(), generated(json.dumps(bad)), tool_response(1), generated()],
                                      ["query_error", "passed"])
        self.assertEqual(summary["status"], "compiled")
        self.assertEqual(summary["successful_attempt"], 1)
        self.assertEqual(summary["model_tool_calls"], 2)
        self.assertEqual(summary["model_calls"], 4)
        self.assertEqual(summary["compile_calls"], 2)
        self.assertEqual(self.service.opened, ["template", "attempt_0", "attempt_1"])
        self.assertEqual(self.service.closed, self.service.opened)
        self.assertEqual([item[0] for item in self.service.invocations], ["template", "attempt_0"])
        payload = json.loads(self.backend.inputs[2][0]["content"])
        self.assertIn("LSP MissingType", json.dumps(payload["previous_attempt"]["diagnostics"]["mcp"]))
        self.assertIn("MissingType", payload["previous_attempt"]["diagnostics"]["cli"]["stdout"])
        self.assertEqual(payload["tool_context"]["files"]["query.qll"], bad["qll_code"])
        clean = json.loads((self.directory / "attempt_1/diagnostics.json").read_text())
        self.assertEqual(clean["mcp"]["diagnostics"], {"query.ql": [], "query.qll": []})
        self.assertEqual(self.backend.inputs[1][1:-1], tool_response()["output"])
        self.assertEqual(self.backend.inputs[1][-1]["call_id"], "call_0_0")

    async def test_six_calls_then_forced_final_submission(self):
        summary = await self.exercise([*[tool_response(i) for i in range(6)], generated()], ["passed"])
        self.assertEqual(summary["model_calls"], 7)
        self.assertEqual(summary["model_tool_calls"], 6)
        self.assertEqual(len(self.service.invocations), 6)
        self.assertEqual(self.backend.requests[-1]["tool_choice"], "none")
        self.assertTrue(all(request["instructions"] == "same prompt" for request in self.backend.requests))

    async def test_parallel_calls_are_counted_individually(self):
        summary = await self.exercise([tool_response(count=2), generated()], ["passed"])
        self.assertEqual(summary["model_tool_calls"], 2)
        self.assertEqual(summary["model_calls"], 2)
        self.assertEqual(len(self.backend.inputs[1]), 6)

    async def test_budget_violations_never_execute_extra_calls_or_compile(self):
        summary = await self.exercise([tool_response(i, count=7) for i in range(4)], [])
        self.assertEqual(summary["status"], "generation_compile_failure")
        self.assertEqual(summary["model_calls"], 4)
        self.assertEqual(summary["compile_calls"], 0)
        self.assertEqual(self.service.invocations, [])
        self.assertEqual(self.service.opened, ["template"])

    async def test_zero_tool_budget_still_allows_final_candidate(self):
        summary = await self.exercise([generated()], ["passed"], config={**CONFIG, "max_tool_calls_per_attempt": 0})
        self.assertEqual(summary["status"], "compiled")
        self.assertEqual(self.backend.requests[0]["tool_choice"], "none")
        self.assertEqual(self.service.invocations, [])

    async def test_invalid_arguments_consume_tool_budget_without_remote_access(self):
        summary = await self.exercise([tool_response(arguments={"file": "../secret", "line": 0, "character": 0}),
                                       tool_response(1, name="codeql_update_file"), generated()], ["passed"])
        self.assertEqual(summary["model_tool_calls"], 2)
        self.assertEqual(self.service.invocations, [])
        self.assertIn("invalid_tool_arguments", self.backend.inputs[-1][-1]["output"])

    async def test_duplicate_call_id_is_a_format_failure_without_reexecution(self):
        summary = await self.exercise([tool_response(), tool_response(), generated()], ["passed"])
        self.assertEqual(summary["successful_attempt"], 1)
        self.assertEqual(len(self.service.invocations), 1)

    async def test_startup_failure_stops_before_model(self):
        summary = await self.exercise([], [], FakeMCP(fail_on="template"))
        self.assertEqual(summary["status"], "infrastructure_failure")
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(summary["mcp_failures"], 1)
        self.assertTrue((self.directory / "mcp/template/session.json").exists())

    async def test_candidate_mcp_failure_does_not_fall_back_to_cli(self):
        summary = await self.exercise([generated()], [], FakeMCP(fail_on="attempt_0"))
        self.assertEqual(summary["status"], "infrastructure_failure")
        self.assertEqual(summary["compile_calls"], 0)
        self.assertEqual(summary["model_calls"], 1)
        self.assertEqual(self.service.closed, ["template"])

    async def test_tool_failure_is_not_format_repair(self):
        summary = await self.exercise([tool_response(), generated()], [], FakeMCP(fail_tool=True))
        self.assertEqual(summary["status"], "infrastructure_failure")
        self.assertEqual(summary["mcp_failures"], 1)
        self.assertEqual(summary["model_calls"], 1)
        self.assertEqual(self.service.closed, ["template"])

    async def test_format_error_reuses_unchanged_context_and_consumes_attempt(self):
        summary = await self.exercise([generated("invalid JSON"), generated()], ["passed"])
        self.assertEqual(summary["successful_attempt"], 1)
        self.assertEqual(self.service.opened, ["template", "attempt_1"])
        self.assertFalse((self.directory / "attempt_0/query.ql").exists())

    async def test_lsp_error_does_not_override_successful_cli(self):
        bad = {**GOOD, "qll_code": "import cpp\npredicate p(MissingType f) { any() }\n"}
        summary = await self.exercise([generated(json.dumps(bad))], ["passed"])
        self.assertEqual(summary["status"], "compiled")
        self.assertEqual(summary["model_calls"], 1)

    async def test_cleanup_failure_is_recorded(self):
        summary = await self.exercise([generated()], [], FakeMCP(close_failure=True))
        self.assertEqual(summary["status"], "infrastructure_failure")
        self.assertGreater(summary["mcp_failures"], 0)


class ToolBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        write_pack(self.directory, PACK)
        (self.directory / "query.qll").write_text("// a\U0001f600b\nimport cpp\n")
        self.client = CandidateMCP(None, self.directory, 1, {"calls": []})

    def test_only_three_read_only_tools_are_advertised(self):
        self.assertEqual({tool["name"] for tool in model_tools()},
                         {"codeql_hover", "codeql_definition", "codeql_complete"})

    def test_utf16_boundary_and_completion_limit(self):
        args = {"file": "query.qll", "line": 0, "character": 6}
        self.assertEqual(self.client.arguments("codeql_complete", args)["limit"], 20)
        for change in ({"character": 5}, {"line": -1}, {"character": True}, {"line": 50},
                       {"file": "file:///tmp/secret"}, {"offset": 20}, {"limit": 100}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.client.arguments("codeql_hover", {**args, **change})

    def test_modified_candidate_is_infrastructure_error(self):
        (self.directory / "query.qll").write_text("changed")
        with self.assertRaises(MCPError):
            self.client.unchanged()

    def test_definition_cannot_read_arbitrary_or_other_version_files(self):
        result = self.client.definition_sources([
            {"uri": "file:///etc/passwd", "range": {"start": {"line": 0}}},
            {"uri": (self.directory / "query.qll").as_uri(), "range": {"start": {"line": 1}}}])
        self.assertEqual(len(result), 1)
        self.assertIn("import cpp", result[0]["text"])


class MCPAdapterTests(unittest.IsolatedAsyncioTestCase):
    setUp = ToolBoundaryTests.setUp

    async def test_server_error_is_infrastructure_failure_and_raw_result_is_kept(self):
        result = CallToolResult(isError=True, content=[TextContent(type="text", text="language server exited")])
        self.client.session = Mock(call_tool=AsyncMock(return_value=result))
        with self.assertRaises(MCPError):
            await self.client.invoke("codeql_hover", {"file": "query.ql", "line": 3, "character": 5})
        event = self.client.receipt["calls"][0]
        self.assertEqual(event["status"], "failed")
        self.assertTrue(event["result"]["isError"])

    async def test_null_diagnostics_is_not_a_clean_candidate(self):
        names = [tool["name"] for tool in model_tools()] + ["codeql_set_workspace", "codeql_open_file", "codeql_diagnostics"]
        replies = [{}, {"tools": [{"name": name} for name in names]}, {}, {}, {}, None]
        with patch.object(self.client, "request", side_effect=replies), self.assertRaises(MCPError):
            await self.client.start()
        self.assertEqual(self.client.diagnostics, {})

    async def test_model_credentials_are_not_forwarded_to_mcp(self):
        receipt = {}
        with patch.dict(os.environ, {"QL_API_KEY": "secret-test-key", "QL_BASE_URL": "https://private.invalid"}):
            with patch("codeql_tools.stdio_client", side_effect=OSError("offline launch failure")) as launch:
                with self.assertRaises(MCPError):
                    async with mcp_session(self.directory, "/test/codeql", "/test/index.js", 1, receipt,
                                           self.directory / "stderr.txt"):
                        self.fail("unexpected successful launch")
        parameters = launch.call_args.args[0]
        self.assertNotIn("QL_API_KEY", parameters.env)
        self.assertNotIn("QL_BASE_URL", parameters.env)
        self.assertNotIn("secret-test-key", json.dumps(receipt))


class E2BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        e1.BatchTests.setUp(self)
        entry = self.root / "mcp/dist/index.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("offline MCP fixture")
        entry.with_name("codeql-lsp-client.js").write_text("offline MCP fixture")
        (entry.parent.parent / "package-lock.json").write_text("{}")
        env = patch.dict(os.environ, {"CODEQL_MCP_ENTRY": str(entry)})
        env.start()
        self.addCleanup(env.stop)

    async def test_off_never_initializes_mcp(self):
        service = Mock(side_effect=AssertionError("off must not open MCP"))
        args = run.arguments(["--spec", "specs/a_spec.md", "--mcp", "off", "--out", "runs/off"])
        with redirect_stdout(io.StringIO()):
            code = await run.run_batch(self.root, args, lambda *args: FakeBackend([generated()]),
                                       lambda *args: FakeCompiler(["passed"]), service)
        self.assertEqual(code, 0)
        service.assert_not_called()

    async def test_on_preserves_mode_and_actual_budget(self):
        args = run.arguments(["--spec", "specs/a_spec.md", "--mcp", "on", "--out", "runs/on"])
        service = FakeMCP()
        with redirect_stdout(io.StringIO()):
            code = await run.run_batch(self.root, args, lambda *args: FakeBackend([tool_response(), generated()]),
                                       lambda *args: FakeCompiler(["passed"]), service)
        self.assertEqual(code, 0)
        experiment = json.loads((self.root / "runs/on/experiment.json").read_text())
        self.assertEqual(experiment["mcp"], "on")
        self.assertEqual(experiment["model_policy"]["max_requests_per_attempt"], 14)
        self.assertEqual(experiment["summary"][0]["model_tool_calls"], 1)

    async def test_missing_mcp_deployment_fails_before_model(self):
        args = run.arguments(["--spec", "specs/a_spec.md", "--mcp", "on", "--out", "runs/missing"])
        backend = Mock()
        with patch.dict(os.environ, {"CODEQL_MCP_ENTRY": "/nonexistent/e2-mcp/index.js"}), self.assertRaises(OSError):
            await run.run_batch(self.root, args, backend)
        backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
