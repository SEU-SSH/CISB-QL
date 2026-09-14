"""Real CLI/MCP and fixed smoke databases with offline model responses only."""

from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import patch

from harness import save_json
import run
import test_e1 as e1


@unittest.skipUnless(os.environ.get("RUN_E3_TESTS") == "1", "set RUN_E3_TESTS=1 for real local smoke checks")
class E3LocalTests(unittest.IsolatedAsyncioTestCase):
    async def check_mode(self, mode, zero_rows):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        output = e1.ROOT / "runs" / f"e3-local-{mode}-{timestamp}"
        candidate = dict(e1.GOOD)
        if zero_rows:
            candidate["qll_code"] = candidate["qll_code"].replace('"main"', '"NoFunctionWithThisNameForE3"')
        backend = e1.FakeBackend([e1.generated(json.dumps(candidate))])
        args = run.arguments(["--spec", "specs/12051b318b_spec.md", "--mcp", mode, "--smoke", "--out", str(output)])
        try:
            with patch.dict(os.environ, {"QL_API_KEY": "offline-test-key", "QL_BASE_URL": "https://offline.invalid/v1"}):
                code = await run.run_batch(e1.ROOT, args, lambda *args: backend)
        finally:
            path = output / "experiment.json"
            if path.exists():
                experiment = json.loads(path.read_text())
                experiment.update(validation_only=True, not_research_result=True, model_transport="FakeBackend")
                save_json(path, experiment)
        self.assertEqual(code, 0, experiment)
        sample = experiment["summary"][0]
        self.assertEqual(sample["model_calls"], 1)
        self.assertEqual(sample["compile_calls"], 1)
        self.assertEqual(sample["successful_attempt"], 0)
        self.assertEqual(sample["smoke_status"], "passed")
        self.assertEqual(sample["semantic_status"], "not_evaluated")
        for language in ("c", "cpp"):
            self.assertGreater(experiment["preflight"]["smoke"]["databases"][language]["row_count"], 0)
            count = sample["smoke"]["databases"][language]["row_count"]
            if zero_rows:
                self.assertEqual(count, 0)
            else:
                self.assertGreater(count, 0)
        self.assertEqual(sample["mcp_calls"] > 0, mode == "on")
        print(f"E3 LOCAL VALIDATION ONLY (offline model): {output}", flush=True)

    async def test_off_zero_result_query(self):
        await self.check_mode("off", True)

    async def test_on_main_query(self):
        await self.check_mode("on", False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
