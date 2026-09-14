import asyncio
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "codeql-pack"
CODEQL = os.environ.get("CODEQL_PATH", "/usr/local/codeql/codeql")
MCP_ENTRY = Path(os.environ.get("CODEQL_MCP_ENTRY", str(Path.home() / "codeql-lsp-mcp/dist/index.js")))
OUT = ROOT / "runs/e0" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
ENABLED = os.environ.get("RUN_E0_TESTS") == "1"


def record(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def candidate(name, broken=False):
    target = OUT / name
    target.mkdir(parents=True)
    for filename in ("qlpack.yml", "codeql-pack.lock.yml", "query.qll", "query.ql"):
        shutil.copyfile(PACK / filename, target / filename)
    if broken:
        library = target / "query.qll"
        library.write_text(library.read_text().replace("Function", "MissingFunctionForE0"), encoding="utf-8")
    return target


def cli(name, *args):
    argv = [CODEQL, *map(str, args)]
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    result = {"argv": argv, "returncode": process.returncode, "stdout": stdout,
              "stderr": stderr, "timed_out": timed_out, "seconds": time.monotonic() - started}
    record(name, result)
    if timed_out:
        raise TimeoutError(f"{name} timed out; see {OUT}")
    return result


def setUpModule():
    if ENABLED:
        record("environment", {
            "python": __import__("sys").version,
            "openai": version("openai"), "mcp": version("mcp"),
            "codeql": CODEQL, "mcp_entry": str(MCP_ENTRY),
            "model_probe": "not_run: model and credentials are not configured",
        })
        print(f"E0 receipts: {OUT}", flush=True)


@unittest.skipUnless(ENABLED, "set RUN_E0_TESTS=1 for local CodeQL/MCP integration checks")
class E0CliTests(unittest.TestCase):
    def test_sdk_and_codeql_version(self):
        with OpenAI(api_key="e0-local-construction-only", base_url="http://127.0.0.1:1", max_retries=0) as client:
            self.assertTrue(callable(client.responses.create))
        result = cli("codeql-version", "version", "--format=json")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(json.loads(result["stdout"])["version"], "2.24.3")

    def test_good_query_and_error_in_imported_qll(self):
        for name, broken in (("cli-good", False), ("cli-bad-qll", True)):
            path = candidate(name, broken)
            result = cli(name, "query", "compile", "--check-only", "--format=json",
                         "--threads=1", path / "query.ql")
            if broken:
                self.assertNotEqual(result["returncode"], 0)
                self.assertIn("MissingFunctionForE0", result["stdout"] + result["stderr"])
            else:
                self.assertEqual(result["returncode"], 0, result["stderr"])

    def test_c_and_cpp_databases_are_nonempty(self):
        for language in ("c", "cpp"):
            database = ROOT / "databases" / f"smoke-{language}-db"
            self.assertTrue(database.is_dir(), f"build {database} first")
            output = OUT / f"smoke-{language}.bqrs"
            result = cli(f"run-{language}", "query", "run", PACK / "query.ql",
                         f"--database={database}", f"--output={output}", "--threads=1")
            self.assertEqual(result["returncode"], 0, result["stderr"])
            decoded = cli(f"decode-{language}", "bqrs", "decode", output, "--format=json")
            self.assertEqual(decoded["returncode"], 0, decoded["stderr"])
            payload = json.loads(decoded["stdout"])
            tuples = [row for result_set in payload.values() if isinstance(result_set, dict)
                      for row in result_set.get("tuples", [])]
            self.assertTrue(tuples, f"empty {language} database")
            self.assertTrue(any("main" in row for row in tuples), tuples)


@unittest.skipUnless(ENABLED, "set RUN_E0_TESTS=1 for local CodeQL/MCP integration checks")
class E0McpTests(unittest.IsolatedAsyncioTestCase):
    async def inspect_candidate(self, name, broken=False, api_checks=False):
        path = candidate(name, broken)
        trace = []
        stderr_path = OUT / f"{name}.stderr.txt"
        params = StdioServerParameters(command="node", args=[str(MCP_ENTRY)], cwd=path,
                                       env={"CODEQL_PATH": CODEQL})

        async def call(session, tool, arguments, expect_error=False, parse=False):
            started = time.monotonic()
            result = await session.call_tool(tool, arguments, read_timeout_seconds=timedelta(seconds=120))
            trace.append({"tool": tool, "arguments": arguments,
                          "seconds": time.monotonic() - started, "result": result.model_dump(mode="json")})
            self.assertEqual(result.isError, expect_error, str(result))
            if parse:
                text = "\n".join(item.text for item in result.content if item.type == "text")
                return json.JSONDecoder().raw_decode(text)[0]
            return result

        try:
            with stderr_path.open("w", encoding="utf-8") as log:
                async with stdio_client(params, errlog=log) as (reader, writer):
                    async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=120)) as session:
                        initialized = await session.initialize()
                        trace.append({"initialize": initialized.model_dump(mode="json")})
                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        self.assertTrue({"codeql_open_file", "codeql_diagnostics", "codeql_hover",
                                         "codeql_definition", "codeql_complete"} <= names)
                        await call(session, "codeql_set_workspace", {"folders": [str(path)]})
                        for filename in ("query.qll", "query.ql"):
                            file = path / filename
                            await call(session, "codeql_open_file", {"file_uri": file.as_uri(),
                                                                     "content": file.read_text(encoding="utf-8")})
                        diagnostics = {}
                        for filename in ("query.qll", "query.ql"):
                            diagnostics[filename] = await call(session, "codeql_diagnostics",
                                                              {"file_uri": (path / filename).as_uri()}, parse=True)
                        errors = [item for items in diagnostics.values() for item in items if item.get("severity") == 1]
                        if broken:
                            self.assertTrue(errors, diagnostics)
                            self.assertIn("MissingFunctionForE0", json.dumps(errors))
                        else:
                            self.assertEqual(errors, [], diagnostics)
                            for filename in ("query.qll", "query.ql"):
                                cached = await call(session, "codeql_diagnostics",
                                                    {"file_uri": (path / filename).as_uri()}, parse=True)
                                self.assertEqual(cached, diagnostics[filename])
                        if api_checks:
                            position = {"file_uri": (path / "query.ql").as_uri(), "line": 3, "character": 5}
                            hover = await call(session, "codeql_hover", position, parse=True)
                            self.assertTrue(hover)
                            self.assertIn("Function", json.dumps(hover))
                            definition = await call(session, "codeql_definition", position, parse=True)
                            self.assertTrue(definition)
                            self.assertIn("cpp-all/7.0.0/", json.dumps(definition))
                            local = await call(session, "codeql_definition",
                                               {**position, "line": 4, "character": 6}, parse=True)
                            self.assertIn((path / "query.qll").as_uri(), json.dumps(local))
                            completion = await call(session, "codeql_complete", {
                                **position, "line": 5, "character": 12,
                                "trigger_character": ".", "limit": 20,
                            }, parse=True)
                            self.assertTrue(completion.get("items"))
                            self.assertLessEqual(len(completion["items"]), 20)
                            await call(session, "unknown_e0_tool", {}, expect_error=True)
        finally:
            record(name, trace)

        pids = re.findall(r"CodeQL (?:MCP|LSP) pid=(\d+)", stderr_path.read_text())
        self.assertGreaterEqual(len(pids), 2, stderr_path.read_text())
        for pid in map(int, pids):
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.1)
            else:
                self.fail(f"MCP/LSP process {pid} survived session shutdown")

    async def test_good_candidate_diagnostics_and_api_tools(self):
        await self.inspect_candidate("mcp-good", api_checks=True)

    async def test_bad_then_clean_candidate_use_fresh_sessions(self):
        await self.inspect_candidate("mcp-bad-qll", broken=True)
        await self.inspect_candidate("mcp-clean-after-bad")


if __name__ == "__main__":
    unittest.main(verbosity=2)
