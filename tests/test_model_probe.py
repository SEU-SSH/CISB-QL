import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI, AuthenticationError

import probe_model


CONFIG = {"model": "offline-test-model", "temperature": 0.2,
          "max_output_tokens": 8000, "timeout_seconds": 1}
TEMPLATE = {"qll_code": "import cpp\n", "ql_code": "import cpp\nimport query\n"}


def response(output, status="completed"):
    return {"id": "resp_offline", "object": "response", "created_at": 0,
            "model": CONFIG["model"], "status": status, "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 5}}}


def message(text):
    return {"id": "msg_offline", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


class ModelProbeTests(unittest.IsolatedAsyncioTestCase):
    async def inspect_probe(self, mode="ok"):
        self.requests = []
        self.receipt = {}

        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/responses")
            body = json.loads(request.content)
            self.requests.append(body)
            if mode == "unauthorized":
                return httpx.Response(401, json={"error": {"message": "bad test credentials"}})
            if len(self.requests) == 1:
                if mode == "no_tool":
                    return httpx.Response(200, json=response([message("{}")]))
                arguments = '{"unexpected": 1}' if mode == "bad_arguments" else "{}"
                output = [{"id": "rs_e0", "type": "reasoning", "summary": [],
                           "content": [{"type": "reasoning_text", "text": "provider reasoning"}]},
                          {"id": "fc_e0", "type": "function_call", "call_id": "call_e0", "status": "completed",
                           "name": "get_query_template", "arguments": arguments}]
                if mode == "missing_call_id":
                    del output[1]["call_id"]
                elif mode == "multiple_tools":
                    output.append({**output[1], "id": "fc_other", "call_id": "call_other"})
                raw = response(output)
                if mode == "truncated_tool":
                    raw.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
                return httpx.Response(200, json=raw)
            tool_result = body["input"][-1]
            self.assertEqual(tool_result["type"], "function_call_output")
            self.assertEqual(tool_result["call_id"], "call_e0")
            self.assertEqual(body["input"][1:-1], self.receipt["requests"][0]["response"]["output"])
            self.assertEqual(body["input"][1]["content"][0]["text"], "provider reasoning")
            self.assertEqual(body["input"][-2]["id"], "fc_e0")
            self.assertEqual(body["instructions"], self.requests[0]["instructions"])
            content = tool_result["output"]
            if mode == "fence":
                content = f"```json\n{content}\n```"
            elif mode == "ignored_result":
                content = json.dumps(TEMPLATE)
            elif mode == "wrong_shape":
                content = '{"qll_code": "import cpp"}'
            raw = response([message(content)])
            if mode == "truncated":
                raw.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
            elif mode == "refusal":
                raw["output"][0]["content"] = [{"type": "refusal", "refusal": "refused"}]
            return httpx.Response(200, json=raw)

        async with AsyncOpenAI(api_key="offline-key", base_url="https://offline.invalid/v1",
                               max_retries=0, http_client=httpx.AsyncClient(
                                   transport=httpx.MockTransport(handler))) as client:
            await probe_model.run_probe(client, CONFIG, TEMPLATE, self.receipt)

    async def test_tool_roundtrip_and_json(self):
        await self.inspect_probe()
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(self.receipt["checks"].values()))
        self.assertEqual(self.requests[0]["max_output_tokens"], 1024)
        self.assertEqual(len(self.receipt["requests"][0]["request"]["input"]), 1)
        self.assertEqual(len(self.requests[1]["input"]), 4)
        self.assertEqual(self.requests[1]["tool_choice"], "none")
        self.assertEqual(self.receipt["api_format"], "responses")
        self.assertEqual(self.requests[0]["tools"][0]["name"], "get_query_template")
        self.assertNotIn("function", self.requests[0]["tools"][0])
        for body in self.requests:
            self.assertEqual(set(body), {"model", "temperature", "max_output_tokens", "instructions",
                                         "input", "tools", "tool_choice"})

    async def test_plain_text_and_bad_arguments_stop_after_first_request(self):
        for mode in ("no_tool", "bad_arguments", "missing_call_id", "multiple_tools", "truncated_tool"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                await self.inspect_probe(mode)
            self.assertEqual(len(self.requests), 1)

    async def test_invalid_final_outputs_are_not_success(self):
        for mode in ("fence", "ignored_result", "wrong_shape", "truncated", "refusal"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                await self.inspect_probe(mode)
            self.assertFalse(self.receipt["checks"]["tool_result_roundtrip"])
            self.assertEqual(len(self.requests), 2)

    async def test_authentication_error_is_not_retried(self):
        with self.assertRaises(AuthenticationError):
            await self.inspect_probe("unauthorized")
        self.assertEqual(len(self.requests), 1)
        self.assertIn("seconds", self.receipt["requests"][0])

    async def test_wall_clock_timeout_is_not_success(self):
        async def slow_handler(request):
            await asyncio.sleep(5)
            return httpx.Response(500)

        receipt = {}
        async with AsyncOpenAI(api_key="offline-key", max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow_handler))) as client:
            with self.assertRaises(TimeoutError):
                await probe_model.run_probe(client, {**CONFIG, "timeout_seconds": 0.01}, TEMPLATE, receipt)
        self.assertFalse(receipt["checks"]["native_tool_call"])
        self.assertEqual(len(receipt["requests"]), 1)


class ModelProbeLocalTests(unittest.TestCase):
    def test_no_network_without_explicit_run(self):
        with patch.object(probe_model, "execute") as execute, redirect_stdout(io.StringIO()):
            self.assertEqual(probe_model.main([]), 0)
        execute.assert_not_called()

    def test_missing_key_fails_before_network(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(probe_model, "execute") as execute:
            with redirect_stderr(io.StringIO()), patch.object(probe_model, "load_config", return_value=CONFIG):
                self.assertEqual(probe_model.main(["--run"]), 2)
        execute.assert_not_called()

    def test_known_credentials_are_redacted_before_serialization(self):
        key = 'test-key-"quoted"'
        data = {"error": f"invalid {key}", "response": [key, "https://private.invalid/v1"]}
        clean = probe_model.redact(data, (key, "https://private.invalid/v1"))
        self.assertEqual(clean, {"error": "invalid [REDACTED]", "response": ["[REDACTED]", "[REDACTED]"]})


if __name__ == "__main__":
    unittest.main()
