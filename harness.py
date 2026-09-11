"""Single-agent CLI generation and at most three complete-file repairs."""

import asyncio
import json
import math
import time

from agent_backend import BackendError, redact
from codeql_tools import CODEQL_VERSION, CPP_ALL_VERSION, PACK_HASHES, write_pack
from spec_io import json_object


def load_config(path):
    config = json_object(path.read_text(encoding="utf-8"))
    required = {"model", "temperature", "max_output_tokens", "max_repair_attempts",
                "max_tool_calls_per_attempt", "timeout_seconds"}
    if set(config) != required:
        raise ValueError("config.json must contain exactly the six planned fields")
    if not isinstance(config["model"], str) or not config["model"].strip():
        raise ValueError("set model in config.json")
    for key in ("temperature", "timeout_seconds"):
        if type(config[key]) not in (int, float) or not math.isfinite(config[key]):
            raise ValueError(f"{key} must be a finite number")
    if not 0 <= config["temperature"] <= 2 or config["timeout_seconds"] <= 0:
        raise ValueError("invalid temperature or timeout_seconds")
    for key, low, high in (("max_output_tokens", 1, math.inf),
                           ("max_repair_attempts", 0, 3), ("max_tool_calls_per_attempt", 0, 6)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"{key} must be an integer in [{low}, {high}]")
    return config


def save_json(path, data, sensitive=()):
    path.write_text(json.dumps(redact(data, sensitive), indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def parse_candidate(response):
    message = response["message"]
    if response["finish_reason"] != "stop" or message.get("tool_calls") or message.get("refusal"):
        raise ValueError("expected a complete final JSON response, without tools or refusal")
    text = message.get("content")
    if not isinstance(text, str):
        raise ValueError("missing response text")
    result = json_object(text)
    if set(result) != {"qll_code", "ql_code"} or not all(isinstance(x, str) and x.strip() for x in result.values()):
        raise ValueError("return exactly two nonempty strings: qll_code and ql_code")
    for key, required in (("qll_code", {"import cpp"}), ("ql_code", {"import cpp", "import query"})):
        if not required <= {line.strip() for line in result[key].splitlines()}:
            raise ValueError(f"{key} requires standalone import lines: {', '.join(sorted(required))}")
    return result


def token_totals(events):
    if not events:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usages = [event.get("response", {}).get("usage") for event in events]
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    if any(not isinstance(usage, dict) or any(type(usage.get(key)) is not int for key in fields)
           for usage in usages):
        return None
    return {key: sum(usage[key] for usage in usages) for key in fields}


def input_error_summary(identifier, reason):
    return {"source_id": identifier, "status": "input_error", "reason": reason,
            "successful_attempt": None, "compile_status": "not_run", "attempts": 0,
            "model_calls": 0, "compile_calls": 0, "mcp_calls": 0, "tokens": token_totals([]),
            "semantic_status": "not_evaluated", "smoke_status": "not_requested"}


async def run_sample(bundle, directory, config, prompt, pack, backend, compiler, sensitive=()):
    directory.mkdir()
    (directory / "spec.md").write_bytes(bundle["raw"].encode("utf-8"))
    summary = {"source_id": bundle["source_id"], "status": "infrastructure_failure", "reason": "interrupted",
               "successful_attempt": None, "successful_candidate": None, "compile_status": "not_run",
               "attempts": 0, "compile_calls": 0, "mcp_calls": 0, "semantic_status": "not_evaluated",
               "smoke_status": "not_requested", "compile_initial": False, "compile_within3repairs": False}
    events = []
    previous = None
    started = time.monotonic()
    compile_seconds = 0
    try:
        for attempt in range(config["max_repair_attempts"] + 1):
            work = directory / f"attempt_{attempt}"
            work.mkdir()
            summary["attempts"] += 1
            trace = []
            diagnostics = {"format": {"status": "not_run"}, "mcp": {"status": "disabled"},
                           "cli": {"status": "not_run"}}
            response = None
            candidate = None
            try:
                payload = {"source_id": bundle["source_id"], "spec_markdown": bundle["raw"],
                           **bundle["parsed"], "environment": {"codeql": CODEQL_VERSION, "cpp_all": CPP_ALL_VERSION},
                           "template": {"qll_code": pack["query.qll"].decode("utf-8"),
                                        "ql_code": pack["query.ql"].decode("utf-8")},
                           "attempt": attempt, "repairs_remaining": config["max_repair_attempts"] - attempt,
                           "previous_attempt": previous}
                response = await backend.generate([
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=True)}], trace)
                try:
                    candidate = parse_candidate(response)
                except ValueError as error:
                    diagnostics["format"] = {"status": "error", "message": str(error)}
                    summary.update(status="generation_compile_failure", reason=f"format_error: {error}")
                else:
                    diagnostics["format"] = {"status": "passed"}
                    files = {name: pack[name] for name in PACK_HASHES}
                    files.update({"query.qll": candidate["qll_code"].encode("utf-8"),
                                  "query.ql": candidate["ql_code"].encode("utf-8")})
                    write_pack(work, files)
                    summary["compile_calls"] += 1
                    diagnostics["cli"] = {"status": "running"}
                    result = await compiler.compile(work / "query.ql")
                    compile_seconds += result["seconds"]
                    diagnostics["cli"] = result
                    if result["status"] == "passed":
                        summary.update(status="compiled", reason=None, compile_status="passed",
                                       successful_attempt=attempt, successful_candidate=f"{directory.name}/attempt_{attempt}",
                                       compile_initial=attempt == 0, compile_within3repairs=True)
                        break
                    if result["status"] != "query_error":
                        summary.update(status="infrastructure_failure", compile_status="unknown",
                                       reason="cli_infrastructure_error: inspect raw output; no code repair attempted")
                        break
                    summary.update(status="generation_compile_failure", compile_status="failed", reason="query_error")
                previous = {"attempt": attempt, "raw_response": response, "candidate": candidate,
                            "diagnostics": diagnostics}
            except BackendError as error:
                diagnostics["model_error"] = str(error)
                summary.update(status="infrastructure_failure", reason=str(error))
                break
            except asyncio.CancelledError as error:
                if diagnostics["cli"]["status"] == "running":
                    diagnostics["cli"] = getattr(error, "command_result", {"status": "cancelled"})
                    compile_seconds += diagnostics["cli"].get("seconds", 0)
                    summary["compile_status"] = "unknown"
                raise
            finally:
                events.extend(trace)
                save_json(work / "model.json", {"calls": trace}, sensitive)
                save_json(work / "diagnostics.json", diagnostics, sensitive)
                for channel in ("stdout", "stderr"):
                    (work / f"{channel}.txt").write_text(redact(diagnostics["cli"].get(channel, ""), sensitive),
                                                         encoding="utf-8")
    except asyncio.CancelledError:
        summary.update(status="infrastructure_failure", reason="cancelled")
        raise
    except Exception as error:
        summary.update(status="infrastructure_failure", reason=f"{type(error).__name__}: {error}")
    finally:
        summary.update(model_calls=len(events), tokens=token_totals(events), seconds=time.monotonic() - started,
                       model_seconds=sum(event.get("seconds", 0) for event in events), cli_seconds=compile_seconds)
        save_json(directory / "summary.json", summary, sensitive)
    return summary
