import asyncio
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from agent_backend import BackendError, ChatBackend
from codeql_tools import CodeQL, classify_compile, load_pack
from harness import load_config, parse_candidate, run_sample, token_totals
import run
from spec_io import collect_targets, parse_spec


ROOT = Path(__file__).resolve().parents[1]
CONFIG = {"model": "offline-test", "temperature": 0.2, "max_output_tokens": 8000,
          "max_repair_attempts": 3, "max_tool_calls_per_attempt": 6, "timeout_seconds": 1}
PACK = load_pack(ROOT / "codeql-pack")
GOOD = {"qll_code": PACK["query.qll"].decode(), "ql_code": PACK["query.ql"].decode()}
SPEC = (ROOT / "specs/12051b318b_spec.md").read_bytes().decode()


def generated(content=None, finish="stop"):
    return {"finish_reason": finish, "message": {"role": "assistant", "content": content or json.dumps(GOOD)}}


def sdk_response(content=None):
    return {"id": "offline", "object": "chat.completion", "created": 0, "model": CONFIG["model"],
            "choices": [{"index": 0, **generated(content)}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}


def compile_result(status="passed", query=None):
    query = query or Path("/tmp/attempt/query.ql")
    errors = [{"severity": "ERROR", "message": "could not resolve type MissingType",
               "position": {"fileName": str(query.with_suffix(".qll")), "line": 3, "column": 19}}]
    return {"status": status, "returncode": 0 if status == "passed" else 2, "timed_out": False,
            "seconds": 0.01, "stdout": json.dumps([{"messages": errors}]) if status != "passed" else "[]",
            "stderr": "", "argv": ["fake-codeql", str(query)]}


class FakeBackend:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.messages = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def generate(self, messages, trace):
        self.messages.append(deepcopy(messages))
        value = next(self.outputs)
        event = {"request": {"messages": deepcopy(messages)}, "seconds": 0.01}
        trace.append(event)
        if isinstance(value, Exception):
            event["error"] = str(value)
            raise value
        event["response"] = {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                             "choices": [value]}
        return value


class FakeCompiler:
    def __init__(self, statuses=()):
        self.statuses = iter(statuses)
        self.queries = []
        self.preflights = 0

    async def preflight(self, directory, pack, receipt):
        self.preflights += 1
        receipt["fake"] = True

    async def compile(self, query):
        self.queries.append(query)
        return compile_result(next(self.statuses), query)


class ParsingTests(unittest.TestCase):
    def test_debug_specs_parse_and_keep_original_strings(self):
        for path in (ROOT / "specs").glob("*_spec.md"):
            with self.subTest(path=path.name):
                parsed = parse_spec(path.read_bytes().decode())
                self.assertTrue(parsed["description_fields"]["description"])
                self.assertIsInstance(parsed["pattern_json"]["triggers"], list)

    def test_invalid_spec_sections_types_and_json(self):
        invalid = [SPEC.replace("## Code Pattern", "## Missing"),
                   SPEC.replace("**Evidence**", "**Other**"),
                   SPEC.replace('"triggers": [', '"triggers": null, "unused": ['),
                   SPEC.replace('"triggers": [', '"triggers": [0,'),
                   SPEC.replace('"triggers": [', '"triggers": [invalid,')]
        for raw in invalid:
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                parse_spec(raw)

    def test_json_contract_rejects_fences_extras_duplicates_and_missing_imports(self):
        invalid = ["```json\n{}\n```", "{}", "[]", json.dumps({**GOOD, "extra": 1}),
                   json.dumps({**GOOD, "qll_code": ""}),
                   json.dumps({**GOOD, "ql_code": "import cpp\nselect 1"}),
                   '{"qll_code":"a","qll_code":"b","ql_code":"c"}',
                   '{"qll_code":NaN,"ql_code":"c"}']
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_candidate(generated(text))
        with self.assertRaises(ValueError):
            parse_candidate(generated(finish="length"))
        self.assertEqual(parse_candidate(generated()), GOOD)

    def test_duplicate_sample_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "samples.txt").write_text("specs/a_spec.md\nother/a_spec.md\n")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                collect_targets(root, samples="samples.txt")

    def test_config_cannot_exceed_four_attempts_or_six_tools(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            for key, value in (("max_repair_attempts", 4), ("max_tool_calls_per_attempt", 7),
                               ("timeout_seconds", 0), ("max_output_tokens", True)):
                path.write_text(json.dumps({**CONFIG, key: value}))
                with self.subTest(key=key), self.assertRaises(ValueError):
                    load_config(path)

    def test_unknown_usage_is_not_reported_as_zero(self):
        self.assertIsNone(token_totals([{"response": {}}]))
        self.assertIsNone(token_totals([{"error": "lost response"}]))
        self.assertEqual(token_totals([])["total_tokens"], 0)


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, outputs, statuses):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "sample"
        self.backend, self.compiler = FakeBackend(outputs), FakeCompiler(statuses)
        return await run_sample({"source_id": "sample", "raw": SPEC, "parsed": parse_spec(SPEC)},
                                self.directory, CONFIG, "fixed prompt", PACK, self.backend, self.compiler,
                                sensitive=("secret-test-key",))

    async def test_initial_success_is_preserved_as_one_standalone_pack(self):
        summary = await self.exercise([generated()], ["passed"])
        self.assertEqual(summary["successful_attempt"], 0)
        self.assertTrue(summary["compile_initial"])
        self.assertEqual(summary["semantic_status"], "not_evaluated")
        self.assertEqual(summary["tokens"]["total_tokens"], 3)
        self.assertEqual((self.directory / "spec.md").read_bytes(), SPEC.encode())
        self.assertEqual((self.directory / "attempt_0/query.qll").read_text(), GOOD["qll_code"])
        self.assertFalse((self.directory / "attempt_1").exists())

    async def test_format_then_ql_error_then_success(self):
        summary = await self.exercise([generated("not json"), generated(), generated()], ["query_error", "passed"])
        self.assertEqual(summary["successful_attempt"], 2)
        self.assertEqual(summary["compile_calls"], 2)
        self.assertEqual(summary["model_calls"], 3)
        self.assertFalse((self.directory / "attempt_0/query.ql").exists())
        feedback = json.loads(self.backend.messages[2][1]["content"])
        self.assertEqual(feedback["repairs_remaining"], 1)
        self.assertIn("MissingType", feedback["previous_attempt"]["diagnostics"]["cli"]["stdout"])
        self.assertEqual(feedback["spec_markdown"], SPEC)
        self.assertEqual(feedback["previous_attempt"]["candidate"], GOOD)

    async def test_four_format_failures_use_budget_without_compilation(self):
        summary = await self.exercise([generated("invalid")] * 5, [])
        self.assertEqual(summary["status"], "generation_compile_failure")
        self.assertEqual(summary["model_calls"], 4)
        self.assertEqual(summary["compile_calls"], 0)
        self.assertFalse((self.directory / "attempt_4").exists())

    async def test_four_query_failures_are_capped(self):
        summary = await self.exercise([generated()] * 5, ["query_error"] * 5)
        self.assertEqual(summary["compile_calls"], 4)
        self.assertEqual(summary["model_calls"], 4)
        self.assertIsNone(summary["successful_attempt"])

    async def test_cli_infrastructure_error_does_not_trigger_repair(self):
        summary = await self.exercise([generated(), generated()], ["infrastructure_error", "passed"])
        self.assertEqual(summary["status"], "infrastructure_failure")
        self.assertEqual(summary["model_calls"], 1)
        self.assertEqual(summary["compile_calls"], 1)

    async def test_model_failure_is_not_format_repair_and_secrets_are_redacted(self):
        summary = await self.exercise([BackendError("bad secret-test-key"), generated()], [])
        self.assertEqual(summary["status"], "infrastructure_failure")
        self.assertEqual(summary["model_calls"], 1)
        self.assertIsNone(summary["tokens"])
        for path in self.directory.rglob("*.json"):
            self.assertNotIn("secret-test-key", path.read_text())

    async def test_cancelled_compile_is_logged_as_started_and_cancelled(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "sample"
            compiler = FakeCompiler()
            async def cancelled(query):
                error = asyncio.CancelledError()
                error.command_result = {"status": "cancelled", "stdout": "partial output", "seconds": 0.1}
                raise error
            compiler.compile = cancelled
            with self.assertRaises(asyncio.CancelledError):
                await run_sample({"source_id": "sample", "raw": SPEC, "parsed": parse_spec(SPEC)},
                                 directory, CONFIG, "prompt", PACK, FakeBackend([generated()]), compiler)
            diagnostics = json.loads((directory / "attempt_0/diagnostics.json").read_text())
            self.assertEqual(diagnostics["cli"]["status"], "cancelled")
            self.assertEqual((directory / "attempt_0/stdout.txt").read_text(), "partial output")
            self.assertEqual(json.loads((directory / "summary.json").read_text())["reason"], "cancelled")


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, mode):
        self.requests = []
        self.trace = []

        async def handler(request):
            self.requests.append(json.loads(request.content))
            if mode == "auth":
                return httpx.Response(401, json={"error": {"message": "unauthorized"}})
            if mode == "connect" and len(self.requests) == 1 or mode == "disconnected":
                raise httpx.ConnectError("offline test", request=request)
            if mode == "timeout":
                await asyncio.sleep(1)
            return httpx.Response(200, json=sdk_response())

        async with ChatBackend({**CONFIG, "timeout_seconds": 0.05}, "offline-key", "https://offline.invalid/v1",
                               httpx.AsyncClient(transport=httpx.MockTransport(handler))) as backend:
            return await backend.generate([{"role": "user", "content": "offline"}], self.trace)

    async def test_one_connection_retry_with_full_trace(self):
        result = await self.exercise("connect")
        self.assertEqual(result["finish_reason"], "stop")
        self.assertEqual(len(self.requests), 2)
        self.assertIn("error", self.trace[0])
        self.assertIn("response", self.trace[1])
        self.assertNotIn("tools", self.requests[0])
        self.assertEqual(self.requests[0]["temperature"], CONFIG["temperature"])
        self.assertEqual(self.requests[0]["max_tokens"], 8000)

    async def test_connection_retry_is_bounded(self):
        with self.assertRaises(BackendError):
            await self.exercise("disconnected")
        self.assertEqual(len(self.requests), 2)

    async def test_authentication_failure_is_not_retried(self):
        with self.assertRaises(BackendError):
            await self.exercise("auth")
        self.assertEqual(len(self.requests), 1)

    async def test_model_timeout_is_not_retried(self):
        with self.assertRaisesRegex(BackendError, "model_timeout"):
            await self.exercise("timeout")
        self.assertEqual(len(self.requests), 1)


class CompilerTests(unittest.IsolatedAsyncioTestCase):
    def test_only_clear_local_query_errors_are_repairable(self):
        query = Path("/tmp/attempt/query.ql")
        result = compile_result("query_error", query)
        self.assertEqual(classify_compile(result, query), "query_error")
        cases = [{**result, "timed_out": True}, {**result, "returncode": 3},
                 {**result, "stdout": "not JSON"}, {**result, "stderr": "Referenced pack not installed"},
                 {**result, "stdout": result["stdout"].replace(str(query.with_suffix('.qll')), '/fixed/library.qll')}]
        for item in cases:
            with self.subTest(item=item):
                self.assertEqual(classify_compile(item, query), "infrastructure_error")

    async def test_missing_executable_is_infrastructure_error(self):
        result = await CodeQL("/nonexistent/e1-codeql", 1).compile(Path("/tmp/query.ql"))
        self.assertEqual(result["status"], "infrastructure_error")

    async def test_timeout_returns_partial_output_and_reaps_process(self):
        result = await CodeQL(sys.executable, 0.1).command(
            "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(10)", cwd=Path("/tmp"))
        self.assertTrue(result["timed_out"])
        pid = int(result["stdout"].strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_timeout_also_stops_descendants(self):
        script = ("import os,subprocess,sys,time; "
                  "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)']); "
                  "print(child.pid, flush=True); time.sleep(10)")
        result = await CodeQL(sys.executable, 0.2).command("-c", script, cwd=Path("/tmp"))
        self.assertTrue(result["timed_out"])
        state = Path(f"/proc/{int(result['stdout'].strip())}/stat")
        if state.exists():
            self.assertEqual(state.read_text().split()[2], "Z", "descendant is still running")

    async def test_cancellation_reaps_process(self):
        with tempfile.TemporaryDirectory() as temp:
            pidfile = Path(temp) / "pid"
            script = "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(10)"
            task = asyncio.create_task(CodeQL(sys.executable, 10).command("-c", script, pidfile, cwd=Path(temp)))
            for _ in range(100):
                if pidfile.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(pidfile.exists())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("run.py", "harness.py", "agent_backend.py", "codeql_tools.py", "spec_io.py", "requirements.txt", "prompt.md"):
            shutil.copyfile(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "codeql-pack", self.root / "codeql-pack", ignore=shutil.ignore_patterns("*.qlx", "*.qlo", ".cache"))
        (self.root / "config.json").write_text(json.dumps(CONFIG))
        (self.root / "specs").mkdir()
        (self.root / "specs/a_spec.md").write_text(SPEC)
        (self.root / "specs/b_spec.md").write_text(SPEC)
        (self.root / "specs/bad_spec.md").write_text("invalid input")
        (self.root / "samples.txt").write_text("specs/a_spec.md\nspecs/bad_spec.md\nspecs/b_spec.md\n")
        self.env = patch.dict(os.environ, {"QL_API_KEY": "offline-key", "QL_BASE_URL": "https://offline.invalid/v1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_batch_keeps_failures_and_continues_with_one_preflight(self):
        args = run.arguments(["--samples", "samples.txt", "--mcp", "off", "--out", "runs/batch"])
        backend = FakeBackend([BackendError("transport error"), generated()])
        compiler = FakeCompiler(["passed"])
        with redirect_stdout(io.StringIO()):
            code = await run.run_batch(self.root, args, lambda *args: backend, lambda *args: compiler)
        self.assertEqual(code, 1)
        experiment = json.loads((self.root / "runs/batch/experiment.json").read_text())
        self.assertEqual([x["status"] for x in experiment["summary"]],
                         ["infrastructure_failure", "input_error", "compiled"])
        self.assertEqual(compiler.preflights, 1)
        self.assertEqual(len(backend.messages), 2)
        self.assertEqual(experiment["mcp"], "off")

    async def test_on_smoke_existing_output_and_outside_path_fail_before_model(self):
        (self.root / "runs/existing").mkdir(parents=True)
        for flags in (["--mcp", "on", "--out", "runs/new"],
                      ["--mcp", "off", "--smoke", "--out", "runs/new"],
                      ["--mcp", "off", "--out", "runs/existing"],
                      ["--mcp", "off", "--out", "/tmp/outside-e1"]):
            factory = unittest.mock.Mock()
            args = run.arguments(["--spec", "specs/a_spec.md", *flags])
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                await run.run_batch(self.root, args, factory, factory)
            factory.assert_not_called()

    async def test_broken_template_preflight_never_opens_model(self):
        compiler = FakeCompiler()
        async def failed(*args):
            raise ValueError("bad template")
        compiler.preflight = failed
        factory = unittest.mock.Mock()
        args = run.arguments(["--spec", "specs/a_spec.md", "--mcp", "off", "--out", "runs/bad-env"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = await run.run_batch(self.root, args, factory, lambda *args: compiler)
        self.assertEqual(code, 1)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
