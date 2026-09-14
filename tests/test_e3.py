import asyncio
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codeql_tools import CodeQL, decoded_rows
from harness import run_sample, save_json
import report
import run
import test_e1 as e1


DATABASES = {language: Path(f"/fixed/smoke-{language}-db") for language in ("c", "cpp")}


class SmokeCompiler(e1.FakeCompiler):
    def __init__(self, smoke_status="passed", *, preflight_error=False):
        super().__init__(["passed"] * 5)
        self.smoke_status = smoke_status
        self.smokes = []
        self.preflight_error = preflight_error

    async def smoke_preflight(self, root, directory, receipt):
        if self.preflight_error:
            raise ValueError("fixture main not found")
        receipt["status"] = "passed"
        return DATABASES

    async def smoke(self, query, databases, directory, receipt):
        self.smokes.append(query)
        receipt.update(status=self.smoke_status, seconds=0.02, databases={
            language: {"status": self.smoke_status, "row_count": 0} for language in databases})
        directory.mkdir(parents=True)
        save_json(directory / "smoke.json", receipt)
        return receipt


class CommandCompiler(CodeQL):
    def __init__(self, *, rows=None, fail=None, malformed=False):
        super().__init__("fake-codeql", 1)
        self.rows = [] if rows is None else rows
        self.fail = fail
        self.malformed = malformed
        self.calls = []

    async def command(self, *args, cwd):
        self.calls.append(args)
        output = Path(next(str(arg).removeprefix("--output=") for arg in args if str(arg).startswith("--output=")))
        operation = "run" if args[0] == "query" else "decode"
        if self.fail == "cancel":
            error = asyncio.CancelledError()
            error.command_result = {"status": "cancelled", "stdout": "partial", "seconds": 0.01}
            raise error
        if operation == "decode":
            output.write_text("not json" if self.malformed else json.dumps({"#select": {
                "columns": [{"kind": "String"}], "tuples": self.rows}}))
        else:
            output.write_bytes(b"fake-bqrs")
        return {"returncode": 1 if self.fail == operation else 0, "timed_out": self.fail == "timeout",
                "stdout": "", "stderr": "", "seconds": 0.01}


class SmokeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, compiler, *, require_main=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            receipt = {}
            await compiler.smoke(root / "query.ql", DATABASES, root / "smoke", receipt, require_main=require_main)
            self.assertEqual(json.loads((root / "smoke/smoke.json").read_text()), receipt)
            return receipt

    async def test_zero_rows_are_valid_for_both_databases(self):
        compiler = CommandCompiler()
        receipt = await self.check(compiler)
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual([item["row_count"] for item in receipt["databases"].values()], [0, 0])
        self.assertEqual(len(compiler.calls), 4)

    async def test_template_requires_main_not_merely_nonempty_database(self):
        for rows, expected in (([], "failed"), ([["other"]], "failed"), ([["main"]], "passed")):
            with self.subTest(rows=rows):
                receipt = await self.check(CommandCompiler(rows=rows), require_main=True)
                self.assertEqual(receipt["status"], expected)

    async def test_run_decode_timeout_and_malformed_json_fail_separately(self):
        for failure in ("run", "decode", "timeout", "malformed"):
            compiler = CommandCompiler(fail=failure, malformed=failure == "malformed")
            with self.subTest(failure=failure):
                receipt = await self.check(compiler)
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(len(receipt["databases"]), 2)
                self.assertTrue(all(item["reason"] for item in receipt["databases"].values()))

    async def test_cancellation_preserves_partial_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(asyncio.CancelledError):
                await CommandCompiler(fail="cancel").smoke(root / "query.ql", DATABASES, root / "smoke", {})
            receipt = json.loads((root / "smoke/smoke.json").read_text())
            self.assertEqual(receipt["status"], "cancelled")
            self.assertEqual(receipt["databases"]["c"]["run"]["stdout"], "partial")

    async def test_invalid_result_structure_is_not_zero_results(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rows.json"
            for payload in ({}, [], {"x": {}}, {"x": {"columns": [], "tuples": [[1]]}}):
                path.write_text(json.dumps(payload))
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    decoded_rows(path)


class SmokeHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_smoke_failure_preserves_compile_and_never_repairs(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "sample"
            compiler = SmokeCompiler("failed")
            backend = e1.FakeBackend([e1.generated()])
            summary = await run_sample({"source_id": "sample", "raw": e1.SPEC, "parsed": e1.parse_spec(e1.SPEC)},
                directory, e1.CONFIG, "offline", e1.PACK, backend, compiler, smoke_databases=DATABASES)
            self.assertEqual(summary["compile_status"], "passed")
            self.assertEqual(summary["status"], "compiled")
            self.assertTrue(summary["compile_initial"])
            self.assertEqual(summary["smoke_status"], "failed")
            self.assertEqual(summary["model_calls"], 1)
            self.assertEqual(summary["compile_calls"], 1)
            self.assertEqual(summary["attempts"], 1)
            self.assertLess(summary["generation_seconds"], summary["seconds"])

    async def test_compile_failure_skips_smoke(self):
        with tempfile.TemporaryDirectory() as temp:
            compiler = SmokeCompiler()
            compiler.statuses = iter(["query_error"] * 4)
            summary = await run_sample({"source_id": "sample", "raw": e1.SPEC, "parsed": e1.parse_spec(e1.SPEC)},
                Path(temp) / "sample", e1.CONFIG, "offline", e1.PACK,
                e1.FakeBackend([e1.generated()] * 4), compiler, smoke_databases=DATABASES)
            self.assertEqual(compiler.smokes, [])
            self.assertEqual(summary["smoke_status"], "not_run")

    async def test_cancelled_smoke_preserves_compile_receipt(self):
        compiler = SmokeCompiler()
        async def cancelled(*args):
            raise asyncio.CancelledError()
        compiler.smoke = cancelled
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "sample"
            with self.assertRaises(asyncio.CancelledError):
                await run_sample({"source_id": "sample", "raw": e1.SPEC, "parsed": e1.parse_spec(e1.SPEC)},
                    directory, e1.CONFIG, "offline", e1.PACK,
                    e1.FakeBackend([e1.generated()]), compiler, smoke_databases=DATABASES)
            summary = json.loads((directory / "summary.json").read_text())
            self.assertEqual(summary["smoke_status"], "cancelled")
            self.assertTrue(summary["compile_initial"])


class E3BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        e1.BatchTests.setUp(self)

    async def test_bad_database_stops_before_any_model_call(self):
        factory = Mock()
        args = run.arguments(["--spec", "specs/a_spec.md", "--mcp", "off", "--smoke", "--out", "runs/bad-db"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = await run.run_batch(self.root, args, factory, lambda *args: SmokeCompiler(preflight_error=True))
        self.assertEqual(code, 1)
        factory.assert_not_called()
        receipt = json.loads((self.root / "runs/bad-db/experiment.json").read_text())
        self.assertEqual(receipt["status"], "infrastructure_failure")
        self.assertEqual(len(receipt["samples"]), 1)
        self.assertEqual(receipt["summary"], [])

    async def test_smoke_failure_sets_batch_exit_but_keeps_compiled_summary(self):
        args = run.arguments(["--spec", "specs/a_spec.md", "--mcp", "off", "--smoke", "--out", "runs/smoke-fail"])
        with redirect_stdout(io.StringIO()):
            code = await run.run_batch(self.root, args, lambda *args: e1.FakeBackend([e1.generated()]),
                                       lambda *args: SmokeCompiler("failed"))
        receipt = json.loads((self.root / "runs/smoke-fail/experiment.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["summary"][0]["compile_status"], "passed")

    async def test_saved_smoke_needs_no_credentials_and_never_modifies_source(self):
        args = run.arguments(["--spec", "specs/a_spec.md", "--mcp", "off", "--out", "runs/source"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(await run.run_batch(self.root, args, lambda *args: e1.FakeBackend([e1.generated()]),
                                                lambda *args: SmokeCompiler()), 0)
        source = self.root / "runs/source"
        before = {path: path.read_bytes() for path in source.rglob("*") if path.is_file()}
        args = run.arguments(["--smoke-from", "runs/source", "--out", "runs/saved-smoke"])
        backend = Mock(side_effect=AssertionError("model must not be opened"))
        compiler = SmokeCompiler()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(await run.run_batch(self.root, args, backend, lambda *args: compiler), 0)
        backend.assert_not_called()
        self.assertFalse(compiler.queries[0].is_relative_to(source))
        self.assertEqual(before, {path: path.read_bytes() for path in source.rglob("*") if path.is_file()})
        receipt = json.loads((self.root / "runs/saved-smoke/experiment.json").read_text())
        self.assertEqual(receipt["model_calls"], 0)
        self.assertEqual(receipt["summary"][0]["smoke_status"], "passed")
        self.assertEqual(len(receipt["summary"][0]["source_sha256"]), 4)

    async def test_saved_smoke_cannot_write_inside_archived_run(self):
        args = run.arguments(["--smoke-from", "runs/source", "--out", "runs/source/smoke"])
        with self.assertRaises(ValueError):
            await run.run_batch(self.root, args)


class ReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        prompt = "offline report test"
        self.base = {"stage": "E3", "api_format": "responses", "status": "passed", "config": e1.CONFIG,
                     "versions": {"codeql": "2.24.3"}, "prompt": prompt,
                     "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "files_sha256": {"run.py": "same"},
                     "pack_sha256": {"qlpack.yml": "same"}, "sampling_note": {}, "summary": [], "seconds": 1,
                     "samples": [{"source_id": "sample", "sha256": hashlib.sha256(e1.SPEC.encode()).hexdigest(), "input_error": None}],
                     "model_policy": {"max_tool_calls_per_attempt": 0, "max_requests_per_attempt": 2},
                     "preflight": {}}
        self.a = self.make_batch("A", "off", first_error="query_error")
        self.b = self.make_batch("B", "on", first_error="format")

    def make_batch(self, name, mode, first_error="format"):
        directory = self.root / name
        directory.mkdir()
        experiment = deepcopy(self.base)
        experiment.update(mcp=mode, model_tools=[] if mode == "off" else [{"name": "codeql_hover"}],
                          mcp_environment=None if mode == "off" else {"entry": "fixed"})
        if mode == "on":
            experiment["model_policy"].update(max_tool_calls_per_attempt=6, max_requests_per_attempt=14)
        sample = directory / "sample"
        sample.mkdir()
        (sample / "spec.md").write_text(e1.SPEC)
        for attempt in (0, 1):
            work = sample / f"attempt_{attempt}"
            work.mkdir()
            save_json(work / "diagnostics.json", {
                "format": {"status": "error" if attempt == 0 and first_error == "format" else "passed"},
                "cli": {"status": "passed" if attempt == 1 else "query_error" if first_error == "query_error" else "not_run"}})
        summary = {"source_id": "sample", "status": "compiled", "compile_status": "passed", "successful_attempt": 1,
                   "attempts": 2, "seconds": 3, "model_calls": 2, "compile_calls": 1,
                   "tokens": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}, "smoke_status": "not_requested"}
        experiment["summary"].append(summary)
        save_json(sample / "summary.json", summary)
        save_json(directory / "experiment.json", experiment)
        return directory

    def change(self, batch, update):
        experiment = report.read_json(batch / "experiment.json")
        update(experiment)
        save_json(batch / "experiment.json", experiment)
        for summary in experiment["summary"]:
            save_json(batch / summary["source_id"] / "summary.json", summary)

    def build(self, **kwargs):
        return report.build_report([self.a], [self.b], purpose="pilot", overlap_note="debug sample reused", **kwargs)

    def test_pairing_counts_format_and_ql_errors_separately(self):
        result = self.build()
        for group in result["groups"].values():
            self.assertEqual(group["CompileInitial"], report.rate(0, 1))
            self.assertEqual(group["CompileWithin3Repairs"], report.rate(1, 1))
            self.assertEqual(group["mean_repairs_on_success"], 1)
        self.assertEqual(result["groups"]["A"]["ql_errors"], 1)
        self.assertEqual(result["groups"]["B"]["format_errors"], 1)
        self.assertEqual(result["groups"]["B"]["ql_errors"], 0)
        self.assertEqual(result["paired_within3"]["both"], 1)
        self.assertIn("pilot", report.markdown(result))

    def test_preflight_failure_and_unprocessed_legal_input_stay_in_denominator(self):
        for batch in (self.a, self.b):
            self.change(batch, lambda e: e["samples"].extend([
                {"source_id": "not_started", "sha256": "fixed", "input_error": None},
                {"source_id": "bad", "sha256": "bad", "input_error": "invalid spec"}]))
        result = self.build()
        for group in result["groups"].values():
            self.assertEqual(group["CompileWithin3Repairs"], report.rate(1, 2))
            self.assertEqual(group["infrastructure_failures"], 1)
            self.assertEqual(group["invalid_inputs"], 1)
            self.assertIsNone(group["costs"]["total_tokens"]["total"])
            self.assertEqual(group["costs"]["total_tokens"]["known_total"], 30)

    def test_absent_usage_is_unknown_not_zero(self):
        self.change(self.a, lambda e: e["summary"][0].update(tokens=None))
        self.assertIsNone(self.build()["groups"]["A"]["costs"]["total_tokens"]["total"])

    def test_cancelled_batch_uses_partial_sample_summary(self):
        def cancel(experiment):
            experiment.update(status="cancelled", summary=[])
        self.change(self.a, cancel)
        self.assertEqual(self.build()["groups"]["A"]["CompileWithin3Repairs"], report.rate(1, 1))

    def test_mock_wrong_mode_running_batch_and_changed_controls_are_rejected(self):
        original = report.read_json(self.b / "experiment.json")
        for update in ({"validation_only": True}, {"mcp": "off"}, {"status": "running"},
                       {"api_format": "chat_completions"}, {"files_sha256": {"run.py": "changed"}},
                       {"smoke_requested": True}, {"config": {**e1.CONFIG, "max_output_tokens": 999}}):
            save_json(self.b / "experiment.json", {**original, **update})
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.build()

    def test_modified_spec_snapshot_is_rejected(self):
        (self.a / "sample/spec.md").write_text("changed")
        with self.assertRaises(ValueError):
            self.build()

    def test_success_without_matching_cli_evidence_is_rejected(self):
        save_json(self.a / "sample/attempt_1/diagnostics.json", {"cli": {"status": "query_error"}})
        with self.assertRaises(ValueError):
            self.build()

    def test_three_repetitions_and_reduced_budget_disclosure(self):
        with self.assertRaises(ValueError):
            report.build_report([self.a], [self.b], purpose="formal", overlap_note="all debug")
        result = report.build_report([self.a], [self.b], purpose="formal", overlap_note="all debug", note="limited budget")
        self.assertIn("fewer than three", result["warning"])
        a = [self.a] + [self.make_batch(f"A{rep}", "off") for rep in (2, 3)]
        b = [self.b] + [self.make_batch(f"B{rep}", "on") for rep in (2, 3)]
        result = report.build_report(a, b, purpose="formal", overlap_note="all debug")
        self.assertEqual(result["repetitions"], 3)
        self.assertEqual(len(result["pairs"]), 3)
        self.assertEqual(result["groups"]["A"]["legal_inputs"], 3)
        with self.assertRaises(ValueError):
            report.build_report([self.a, self.a], [self.b, b[1]], purpose="pilot", overlap_note="all debug")

    def test_smoke_is_auxiliary_and_does_not_erase_compile_success(self):
        for batch, status in ((self.a, "failed"), (self.b, "passed")):
            self.change(batch, lambda e, status=status: (e.update(smoke_requested=True),
                e["summary"][0].update(smoke_status=status, smoke_seconds=2, generation_seconds=1)))
        result = self.build()
        self.assertEqual(result["groups"]["A"]["CompileWithin3Repairs"], report.rate(1, 1))
        self.assertEqual(result["groups"]["A"]["smoke"], report.rate(0, 1))
        self.assertEqual(result["groups"]["B"]["smoke"], report.rate(1, 1))
        self.assertEqual(result["groups"]["A"]["costs"]["generation_seconds"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
