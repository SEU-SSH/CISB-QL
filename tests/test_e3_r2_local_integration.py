"""Real pinned MCP/CLI with handwritten candidates and mock LLM only; never calls a model API."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest

from codeql_tools import CodeQL, cpp_all_root, mcp_session, write_pack
from harness import run_sample, save_json
from e3_r2_fixtures import RECURSIVE, REPAIRED
from test_e1 import CONFIG, PACK, SPEC, FakeBackend, generated, parse_spec
from test_e2 import tool_response
import test_e2_local_integration as e2local


ROOT = Path(__file__).resolve().parents[1]
CODEQL = os.environ.get("CODEQL_PATH", "/usr/local/codeql/codeql")
ENTRY = Path(os.environ.get("CODEQL_MCP_ENTRY", str(Path.home() / "codeql-lsp-mcp/dist/index.js")))


@unittest.skipUnless(os.environ.get("RUN_E3_R2_TESTS") == "1", "set RUN_E3_R2_TESTS=1 for real local revision checks")
class E3R2LocalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.out = ROOT / "runs" / f"e3-r2-local-{timestamp}"
        self.out.mkdir()
        save_json(self.out / "validation.json", {"revision": "E3-r2", "validation_only": True,
                  "not_research_result": True, "paid_model_requests": 0, "model_transport": "offline FakeBackend",
                  "test": self.id(), "status": "started"})

    async def check_closed(self, log):
        await e2local.E2LocalTests.assert_processes_closed(self, log)

    async def test_real_name_lookup_and_filtered_completion_on_initial_template(self):
        write_pack(self.out / "template", PACK)
        receipt, results = {}, {}
        log = self.out / "stderr.txt"
        try:
            async with mcp_session(self.out / "template", CODEQL, ENTRY, 120, receipt, log) as client:
                for query in ("shift", "EqExpr", "UnknownApiForE3R2", ".*"):
                    results[query] = await client.invoke("codeql_search_api", {"query": query})
                self.assertEqual(results["EqExpr"]["items"][0]["name"], "EQExpr")
                self.assertEqual(results["UnknownApiForE3R2"]["items"], [])
                self.assertEqual(results[".*"]["items"], [])
                page = await client.invoke("codeql_search_api", {"query": "shift", "offset": 10})
                results["shift_page_2"] = page
                self.assertTrue({"LShiftExpr", "RShiftExpr"} <=
                                {x["name"] for x in results["shift"]["items"] + page["items"]})
                self.assertEqual(page["pagination"]["total"], results["shift"]["pagination"]["total"])
                first = {(x["path"], x["line"]) for x in results["shift"]["items"]}
                self.assertTrue(first.isdisjoint((x["path"], x["line"]) for x in page["items"]))
                for result in results.values():
                    self.assertEqual(result["version"], "7.0.0")
                    self.assertLessEqual(len(result["items"]), 10)
                    for item in result["items"]:
                        source = (cpp_all_root() / item["path"]).read_text().splitlines()
                        self.assertIn(item["name"], source[item["line"] - 1])
                        self.assertLessEqual(len(item["source"]), 2000)
                        self.assertLessEqual(len(item["source"].splitlines()), 20)
                position = {"file": "query.ql", "line": 5, "character": 12}
                complete = await client.invoke("codeql_complete", {**position, "query": "getAQlClass"})
                results["completion"] = complete
                self.assertTrue(complete["items"])
                self.assertIn("getAQlClass", [x["label"] for x in complete["items"]])
                self.assertIn("debugging tool", json.dumps(complete["items"]))
                self.assertGreater(complete["unfiltered_total"], complete["pagination"]["total"])
                self.assertNotIn("textEdit", json.dumps(complete))
                self.assertEqual(client.context()["files"]["query.qll"], PACK["query.qll"].decode())
        finally:
            save_json(self.out / "session.json", receipt)
            save_json(self.out / "tool-results.json", results)
        self.assertEqual(receipt["close_status"], "closed")
        await self.check_closed(log)
        self.finish()

    async def repair(self, enabled):
        def factory(path, receipt, stderr_path):
            return mcp_session(path, CODEQL, ENTRY, 120, receipt, stderr_path)

        outputs = []
        if enabled:
            outputs.append(tool_response(0, "codeql_search_api", {"query": "shift"}))
        outputs.append(generated(json.dumps(RECURSIVE)))
        if enabled:
            outputs.append(tool_response(1, "codeql_search_api", {"query": "LShiftExpr"}))
        outputs.append(generated(json.dumps(REPAIRED)))
        backend = FakeBackend(outputs)
        directory = self.out / "sample"
        summary = await run_sample({"source_id": "sample", "raw": SPEC, "parsed": parse_spec(SPEC)}, directory,
            {**CONFIG, "timeout_seconds": 120}, "offline regression fixture, not a spec accuracy evaluation", PACK,
            backend, CodeQL(CODEQL, 120), mcp_factory=factory if enabled else None)
        self.assertEqual(summary["status"], "compiled", json.dumps(summary))
        self.assertEqual(summary["successful_attempt"], 1)
        self.assertEqual(summary["compile_calls"], 2)
        self.assertEqual(summary["model_tool_calls"], 2 if enabled else 0)
        self.assertEqual(summary["mcp_sessions"], 3 if enabled else 0)
        diagnostics = json.loads((directory / "attempt_0/diagnostics.json").read_text())
        self.assertEqual(diagnostics["cli"]["status"], "query_error")
        errors = [e for report in json.loads(diagnostics["cli"]["stdout"]) for e in report["messages"]
                  if e["severity"] == "ERROR"]
        self.assertTrue(any("cpp-all/7.0.0" in e["position"]["fileName"] for e in errors))
        self.assertTrue(any("attempt_0/query.qll" in e["position"]["fileName"] for e in errors))
        repair_request = backend.inputs[2 if enabled else 1]
        previous = json.loads(repair_request[0]["content"])["previous_attempt"]
        self.assertEqual(previous["diagnostics"]["cli"]["stdout"], diagnostics["cli"]["stdout"])
        self.assertIn("raw_response", previous)
        self.assertEqual((directory / "attempt_0/query.qll").read_text(), RECURSIVE["qll_code"])
        self.assertEqual((directory / "attempt_1/query.qll").read_text(), REPAIRED["qll_code"])
        if enabled:
            for log in (directory / "mcp").glob("*/stderr.txt"):
                await self.check_closed(log)
        else:
            self.assertFalse((directory / "mcp").exists())
        self.finish()

    async def test_real_mixed_recursion_continues_repair_with_mcp(self):
        await self.repair(True)

    async def test_real_mixed_recursion_continues_repair_without_mcp(self):
        await self.repair(False)

    def finish(self):
        path = self.out / "validation.json"
        save_json(path, {**json.loads(path.read_text()), "status": "passed"})
        print(f"E3-r2 LOCAL VALIDATION ONLY (no paid model): {self.out}", flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
