from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mcp.types import CallToolResult, TextContent

from codeql_tools import classify_compile, cpp_all_root, model_tools, write_pack, CandidateMCP
from harness import run_sample
from e3_r2_fixtures import RECURSIVE, REPAIRED, recursion_result
from test_e1 import CONFIG, PACK, SPEC, FakeBackend, FakeCompiler, generated, parse_spec
from test_e2 import FakeMCP, tool_response


class RecursionClassificationTests(unittest.TestCase):
    def setUp(self):
        self.query = Path("/tmp/e3-r2-candidate/query.ql")
        self.result = recursion_result(self.query)
        self.messages = json.loads(self.result["stdout"])[0]["messages"]

    def classify(self, messages):
        return classify_compile({**self.result, "stdout": json.dumps([{"messages": messages}])}, self.query)

    def test_query_cycle_with_library_locations_is_repairable(self):
        self.assertEqual(classify_compile(self.result, self.query), "query_error")

    def test_every_external_error_must_be_linked_by_a_complete_query_symbol(self):
        unrelated = deepcopy(self.messages[1])
        unrelated["message"] = unrelated["message"].replace("ShiftOperation", "ShiftOperationExtra")
        self.assertEqual(self.classify([*self.messages, unrelated]), "infrastructure_error")
        unrelated["message"] = self.messages[1]["message"].replace("query::", "other::")
        self.assertEqual(self.classify([self.messages[0], unrelated]), "infrastructure_error")

    def test_library_only_and_local_nonrecursive_errors_do_not_authorize_repair(self):
        self.assertEqual(self.classify(self.messages[1:]), "infrastructure_error")
        local = {**self.messages[0], "message": "could not resolve type MissingType"}
        self.assertEqual(self.classify([local, self.messages[1]]), "infrastructure_error")

    def test_unrecognized_message_or_noncycle_is_conservative(self):
        for text in ("unknown compiler error", "Non-monotonic recursion: characteristic predicate of query::ShiftOperation",
                     self.messages[1]["message"] + " --> unrelated"):
            self.assertEqual(self.classify([self.messages[0], {**self.messages[1], "message": text}]),
                             "infrastructure_error")

    def test_untrusted_locations_are_not_query_errors(self):
        for filename in (None, "/tmp/library.qll", str(cpp_all_root().parent / "8.0.0/library.qll")):
            item = {**self.messages[1], "position": {"fileName": filename}}
            self.assertEqual(self.classify([self.messages[0], item]), "infrastructure_error")

    def test_infrastructure_guards_take_precedence(self):
        for changes in ({"timed_out": True}, {"launch_error": "missing binary"}, {"returncode": 3},
                        {"stderr": "out of memory"}, {"stderr": "Referenced pack not installed"},
                        {"stderr": "Permission denied"}, {"stdout": "invalid json"}):
            self.assertEqual(classify_compile({**self.result, **changes}, self.query), "infrastructure_error")


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        write_pack(self.directory, PACK)
        self.client = CandidateMCP(None, self.directory, 1, {"calls": []})

    def test_search_is_position_independent_and_strictly_bounded(self):
        self.assertEqual(self.client.arguments("codeql_search_api", {"query": "shift"}),
                         {"query": "shift", "offset": 0})
        for args in ({}, {"query": " "}, {"query": "x" * 129}, {"query": "x", "offset": True},
                     {"query": "x", "offset": -1}, {"query": "x", "offset": 2 ** 53},
                     {"query": "x", "file": "query.ql"}, {"query": "x", "root": "/etc"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.client.arguments("codeql_search_api", args)
        self.assertEqual(self.client.arguments("codeql_search_api", {"query": ".*"})["query"], ".*")

    def test_completion_preserves_old_call_and_accepts_filter_and_page(self):
        args = {"file": "query.ql", "line": 0, "character": 0}
        self.assertEqual(self.client.arguments("codeql_complete", args)["offset"], 0)
        params = self.client.arguments("codeql_complete", {**args, "query": "Expr", "offset": 20})
        self.assertEqual((params["query"], params["offset"], params["limit"]), ("Expr", 20, 20))
        for changes in ({"query": 1}, {"offset": 1.5}, {"limit": 50}):
            with self.assertRaises(ValueError):
                self.client.arguments("codeql_complete", {**args, **changes})

    async def test_projection_keeps_full_warning_and_native_receipt(self):
        warning = "x" * 3000 + " Note: getAQlClass is a debugging tool. It is not suitable for production QL code."
        native = {"query": "getAQlClass", "isIncomplete": True, "unfiltered_total": 1200,
                  "pagination": {"offset": 0, "limit": 20, "total": 1, "hasMore": False},
                  "items": [{"label": "getAQlClass", "kind": 2, "detail": "string getAQlClass()",
                             "documentation": {"kind": "markdown", "value": warning},
                             "textEdit": {"newText": "getAQlClass()"}, "sections": ["editor snippet"]}]}
        self.client.session = Mock(call_tool=AsyncMock(return_value=CallToolResult(
            content=[TextContent(type="text", text=json.dumps(native))])))
        result = await self.client.invoke("codeql_complete", {"file": "query.ql", "line": 0, "character": 0})
        self.assertEqual(result["items"][0]["documentation"]["value"], warning)
        self.assertNotIn("textEdit", result["items"][0])
        self.assertNotIn("sections", result["items"][0])
        self.assertTrue(result["isIncomplete"])
        self.assertIn("textEdit", self.client.receipt["calls"][0]["result"]["content"][0]["text"])

    def test_search_schema_does_not_expose_file_or_pack(self):
        tool = next(tool for tool in model_tools() if tool["name"] == "codeql_search_api")
        self.assertEqual(set(tool["parameters"]["properties"]), {"query", "offset"})
        self.assertFalse(tool["parameters"]["additionalProperties"])


class HarnessRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, outputs, compiler=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "sample"
        self.backend, self.service = FakeBackend(outputs), FakeMCP()
        return await run_sample({"source_id": "sample", "raw": SPEC, "parsed": parse_spec(SPEC)}, self.directory,
                                CONFIG, "same prompt", PACK, self.backend, compiler or FakeCompiler(["passed"]),
                                mcp_factory=self.service)

    async def test_mixed_recursion_continues_with_complete_raw_feedback(self):
        class Compiler(FakeCompiler):
            async def compile(self, query):
                if not self.queries:
                    self.queries.append(query)
                    result = recursion_result(query)
                    return {**result, "status": classify_compile(result, query)}
                return await super().compile(query)

        compiler = Compiler(["passed"])
        summary = await self.exercise([generated(json.dumps(RECURSIVE)), generated(json.dumps(REPAIRED))], compiler)
        self.assertEqual(summary["successful_attempt"], 1)
        self.assertEqual(summary["compile_calls"], 2)
        previous = json.loads(self.backend.inputs[1][0]["content"])["previous_attempt"]
        self.assertIn("raw_response", previous)
        self.assertIn("cpp-all/7.0.0", previous["diagnostics"]["cli"]["stdout"])
        self.assertEqual(previous["candidate"], RECURSIVE)

    async def test_search_pagination_invalid_args_and_duplicates_share_six_call_budget(self):
        completion = {"file": "query.ql", "line": 0, "character": 0, "query": "getAQlClass"}
        outputs = [tool_response(0, "codeql_search_api", {"query": "shift"}),
                   tool_response(1, "codeql_search_api", {"query": "shift", "offset": 10}),
                   tool_response(2, "codeql_search_api", {"query": " "}),
                   tool_response(3, "codeql_complete", completion),
                   tool_response(4, "codeql_complete", {**completion, "character": 1}),
                   tool_response(5, "codeql_complete", {**completion, "offset": 20}), generated()]
        summary = await self.exercise(outputs)
        self.assertEqual(summary["model_tool_calls"], 6)
        self.assertEqual(summary["compile_calls"], 1)
        self.assertEqual(len(self.service.invocations), 5)
        self.assertEqual(self.backend.requests[-1]["tool_choice"], "none")
        tools = json.loads((self.directory / "attempt_0/diagnostics.json").read_text())["tools"]
        self.assertEqual(tools[2]["status"], "invalid_arguments")
        self.assertEqual(tools[4]["result"]["duplicate_of"], "call_3_0")
        self.assertNotIn("duplicate_of", tools[5]["result"])
        self.assertIn("Not suitable", tools[3]["result"]["items"][0]["documentation"])

    async def test_completion_deduplication_resets_between_attempts(self):
        outputs = [tool_response(0, "codeql_complete"), generated(json.dumps(RECURSIVE)),
                   tool_response(1, "codeql_complete"), generated(json.dumps(REPAIRED))]
        summary = await self.exercise(outputs, FakeCompiler(["query_error", "passed"]))
        self.assertEqual(summary["successful_attempt"], 1)
        tools = json.loads((self.directory / "attempt_1/diagnostics.json").read_text())["tools"]
        self.assertNotIn("duplicate_of", tools[0]["result"])


if __name__ == "__main__":
    unittest.main()
