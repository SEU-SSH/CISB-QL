"""Opt-in E0 model protocol probe; not a generation harness."""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import urlsplit

from openai import AsyncOpenAI

from agent_backend import API_FORMAT, response_text, validate_response


ROOT = Path(__file__).resolve().parent
TOOL = {
    "type": "function",
    "name": "get_query_template",
    "description": "Read the two QL template files for this protocol probe.",
    "parameters": {
        "type": "object", "properties": {}, "required": [],
        "additionalProperties": False,
    },
}


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config.json must contain an object")
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("set model in config.json")
    for key in ("temperature", "timeout_seconds"):
        value = config.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"config.json: {key} must be a finite number")
    if not 0 <= config["temperature"] <= 2 or config["timeout_seconds"] <= 0:
        raise ValueError("temperature must be in [0, 2]; timeout_seconds must be positive")
    if type(config.get("max_output_tokens")) is not int or config["max_output_tokens"] <= 0:
        raise ValueError("config.json: max_output_tokens must be a positive integer")
    return {key: config[key] for key in
            ("model", "temperature", "timeout_seconds", "max_output_tokens")}


def redact(value, sensitive):
    if isinstance(value, str):
        for secret in sensitive:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, sensitive) for item in value]
    if isinstance(value, dict):
        return {redact(key, sensitive): redact(item, sensitive) for key, item in value.items()}
    return value


async def run_probe(client, config, template, receipt):
    instructions = (
        "This is a short protocol test, not a coding task. First call "
        "get_query_template exactly once with empty JSON arguments {}. "
        "After receiving its result, return only that JSON object, with exactly "
        "qll_code and ql_code. Copy both strings verbatim, including comments, "
        "whitespace and the final newline. No Markdown fences or explanation."
    )
    input_items = [{"role": "user", "content": "Read the template using the tool, then return its JSON."}]
    receipt["api_format"] = API_FORMAT
    receipt["checks"] = {"native_tool_call": False, "dual_file_json": False,
                         "tool_result_roundtrip": False}
    receipt["requests"] = []

    async def ask(tool_choice):
        request = {
            "model": config["model"], "temperature": config["temperature"],
            "max_output_tokens": min(config["max_output_tokens"], 1024),
            "instructions": instructions, "input": input_items, "tools": [TOOL], "tool_choice": tool_choice,
        }
        entry = {"request": deepcopy(request)}
        receipt["requests"].append(entry)
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                client.responses.create(**request), config["timeout_seconds"])
            entry["response"] = response.model_dump(mode="json", exclude_none=True)
        finally:
            entry["seconds"] = time.monotonic() - started
        raw = entry["response"]
        validate_response(raw)
        if raw["status"] != "completed":
            raise ValueError(f"incomplete response: {raw.get('incomplete_details')}")
        return raw

    first = await ask("auto")
    calls = [item for item in first["output"] if item["type"] == "function_call"]
    if len(calls) != 1:
        raise ValueError("expected one native tool call; plain-text tool descriptions do not count")
    call = calls[0]
    if call.get("name") != "get_query_template" or not call.get("call_id"):
        raise ValueError("unexpected tool call or missing call_id")
    if json.loads(call["arguments"]) != {}:
        raise ValueError("get_query_template requires empty JSON arguments")
    receipt["checks"]["native_tool_call"] = True

    # Created after the first response, so it can reach the model only through the tool result.
    expected = dict(template)
    expected["qll_code"] = f"// e0-probe-{secrets.token_hex(8)}\n" + template["qll_code"]
    receipt["tool_result"] = expected
    # DeepSeek is stateless: replay all output items, including reasoning, before the tool result.
    input_items.extend(deepcopy(first["output"]))
    input_items.append({"type": "function_call_output", "call_id": call["call_id"],
                        "output": json.dumps(expected)})
    final = await ask("none")
    result = json.loads(response_text(final))
    if (not isinstance(result, dict) or set(result) != {"qll_code", "ql_code"}
            or not all(isinstance(value, str) and value.strip() for value in result.values())):
        raise ValueError("final JSON must have exactly two nonempty strings: qll_code and ql_code")
    receipt["checks"]["dual_file_json"] = True
    if result != expected:
        raise ValueError("final JSON did not preserve the tool result, including the probe marker")
    receipt["checks"]["tool_result_roundtrip"] = True


async def execute(config, api_key, base_url, template, receipt):
    async with AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                           timeout=config["timeout_seconds"]) as client:
        await run_probe(client, config, template, receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="send up to two paid API requests; no automatic retries")
    args = parser.parse_args(argv)
    if not args.run:
        parser.print_help()
        return 0

    api_key = os.environ.get("QL_API_KEY", "").strip()
    base_url = os.environ.get("QL_BASE_URL", "").strip()
    sensitive = (api_key, base_url)
    try:
        config = load_config(ROOT / "config.json")
        if not api_key or not base_url:
            raise ValueError("export QL_API_KEY and QL_BASE_URL in the terminal running this script")
        url = urlsplit(base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("QL_BASE_URL must be an HTTP(S) API base URL")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("QL_BASE_URL must not contain credentials, a query, or a fragment")
        template = {key: (ROOT / "codeql-pack" / filename).read_text(encoding="utf-8")
                    for key, filename in (("qll_code", "query.qll"), ("ql_code", "query.ql"))}
    except (ValueError, OSError) as error:
        print(f"FAIL preflight: {redact(str(error), sensitive)}", file=sys.stderr)
        return 2

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = ROOT / "runs/e0" / f"model-responses-{timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    receipt = {"status": "running", "api_format": API_FORMAT, "config": config, "max_requests": 2,
               "sdk_max_retries": 0, "json_output_mode": "prompt_only"}
    started = time.monotonic()
    print("Running E0 Responses probe: at most 2 requests, <=1024 output tokens each, no retries.",
          flush=True)
    try:
        asyncio.run(execute(config, api_key, base_url, template, receipt))
        receipt["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        receipt["status"] = "failed"
        receipt["error"] = {"type": type(error).__name__, "message": str(error),
                            "status_code": getattr(error, "status_code", None)}
    finally:
        receipt["seconds"] = time.monotonic() - started
        receipt = redact(receipt, sensitive)
        (output / "model-probe.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    print(f"Receipts: {output}")
    if receipt["status"] == "passed":
        print("PASS: native tool call, tool-result roundtrip, and dual-file JSON.")
        return 0
    error = receipt["error"]
    print(f"FAIL: {error['type']}: {error['message']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
