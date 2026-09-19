"""Single-agent generation with optional MCP assistance and at most three repairs."""

import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
import json
import math
import time

from agent_backend import API_FORMAT, BackendError, redact, response_text
from codeql_tools import CODEQL_VERSION, CPP_ALL_VERSION, PACK_HASHES, MCPError, model_tools, write_pack
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
    result = json_object(response_text(response))
    if set(result) != {"qll_code", "ql_code"} or not all(isinstance(x, str) and x.strip() for x in result.values()):
        raise ValueError("return exactly two nonempty strings: qll_code and ql_code")
    for key, required in (("qll_code", {"import cpp"}), ("ql_code", {"import cpp", "import query"})):
        if not required <= {line.strip() for line in result[key].splitlines()}:
            raise ValueError(f"{key} requires standalone import lines: {', '.join(sorted(required))}")
    return result


def token_totals(events):
    if not events:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    usages = [event.get("response", {}).get("usage") for event in events]
    fields = ("input_tokens", "output_tokens", "total_tokens")
    if any(not isinstance(usage, dict) or any(type(usage.get(key)) is not int for key in fields)
           for usage in usages):
        return None
    return {key: sum(usage[key] for usage in usages) for key in fields}


def input_error_summary(identifier, reason, mcp="off"):
    return {"source_id": identifier, "api_format": API_FORMAT, "status": "input_error", "reason": reason,
            "successful_attempt": None, "compile_status": "not_run", "attempts": 0,
            "model_calls": 0, "compile_calls": 0, "mcp": mcp, "mcp_calls": 0, "mcp_failures": 0,
            "model_tool_calls": 0, "tokens": token_totals([]),
            "semantic_status": "not_evaluated", "smoke_status": "not_requested"}


async def generate_with_tools(backend, prompt, payload, trace, context, budget, tool_events):
    inputs = [{"role": "user", "content": json.dumps(payload, ensure_ascii=True)}]
    if context is None:
        return await backend.generate(prompt, inputs, trace)
    used = 0
    call_ids = set()
    completions = {}
    for _ in range(budget + 1):
        response = await backend.generate(prompt, inputs, trace, tools=model_tools(),
                                          tool_choice="auto" if used < budget else "none")
        calls = [item for item in response["output"] if item.get("type") == "function_call"]
        if response["status"] != "completed" or not calls:
            return response
        ids = [call.get("call_id") for call in calls]
        if (used + len(calls) > budget or any(not isinstance(value, str) or not value for value in ids)
                or len(set(ids)) != len(ids) or call_ids.intersection(ids)):
            tool_events.append({"status": "rejected", "reason": "tool budget exceeded or invalid/duplicate call_id",
                                "requested_calls": calls, "count": 0})
            return response
        call_ids.update(ids)
        inputs.extend(deepcopy(response["output"]))
        for call in calls:
            used += 1
            event = {"call": call, "context": str(context.directory), "count": 1, "status": "running"}
            tool_events.append(event)
            started = time.monotonic()
            try:
                arguments = json_object(call.get("arguments", ""))
                result = await context.invoke(call.get("name"), arguments)
                if call.get("name") == "codeql_complete":
                    identity = json.dumps({key: result.get(key) for key in
                                           ("items", "pagination", "isIncomplete")}, sort_keys=True)
                    if identity in completions:
                        result = {"duplicate_of": completions[identity],
                                  "query": arguments.get("query", ""), "pagination": result.get("pagination"),
                                  "message": "Same completion page as the earlier call. Change query or offset, "
                                             "or use codeql_search_api for an unknown name."}
                    else:
                        completions[identity] = call["call_id"]
                event["status"] = "passed"
            except (ValueError, TypeError) as error:
                result = {"error": "invalid_tool_arguments", "message": str(error)}
                event["status"] = "invalid_arguments"
            except (MCPError, asyncio.CancelledError) as error:
                event.update(status="failed", error=f"{type(error).__name__}: {error}")
                raise
            finally:
                event["seconds"] = time.monotonic() - started
            event["result"] = result
            inputs.append({"type": "function_call_output", "call_id": call["call_id"],
                           "output": json.dumps({"result": result, "tool_calls_remaining": budget - used})})
    return response


async def run_sample(bundle, directory, config, prompt, pack, backend, compiler, sensitive=(), *,
                     mcp_factory=None, smoke_databases=None):
    directory.mkdir()
    (directory / "spec.md").write_bytes(bundle["raw"].encode("utf-8"))
    summary = {"source_id": bundle["source_id"], "api_format": API_FORMAT,
               "status": "infrastructure_failure", "reason": "interrupted",
               "successful_attempt": None, "successful_candidate": None, "compile_status": "not_run",
               "attempts": 0, "compile_calls": 0, "mcp": "on" if mcp_factory else "off", "mcp_calls": 0,
               "mcp_failures": 0, "model_tool_calls": 0, "semantic_status": "not_evaluated",
               "smoke_status": "not_requested", "compile_initial": False, "compile_within3repairs": False}
    events = []
    previous = None
    started = time.monotonic()
    compile_seconds = 0
    sessions = AsyncExitStack()
    session_logs = []
    context = None

    async def open_context(path):
        await sessions.aclose()
        log_dir = directory / "mcp" / path.name
        log_dir.mkdir(parents=True)
        receipt = {}
        session_logs.append((log_dir, receipt))
        return await sessions.enter_async_context(mcp_factory(path, receipt, log_dir / "stderr.txt"))

    try:
        if mcp_factory:
            write_pack(directory / "template", pack)
            context = await open_context(directory / "template")
        for attempt in range(config["max_repair_attempts"] + 1):
            work = directory / f"attempt_{attempt}"
            work.mkdir()
            summary["attempts"] += 1
            trace = []
            diagnostics = {"format": {"status": "not_run"},
                           "mcp": {"status": "not_run" if mcp_factory else "disabled"},
                           "cli": {"status": "not_run"}, "tools": []}
            response = None
            candidate = None
            try:
                payload = {"source_id": bundle["source_id"], "spec_markdown": bundle["raw"],
                           **bundle["parsed"], "environment": {"codeql": CODEQL_VERSION, "cpp_all": CPP_ALL_VERSION},
                           "template": {"qll_code": pack["query.qll"].decode("utf-8"),
                                        "ql_code": pack["query.ql"].decode("utf-8")},
                           "attempt": attempt, "repairs_remaining": config["max_repair_attempts"] - attempt,
                           "previous_attempt": previous}
                if context is not None:
                    payload["tool_context"] = {**context.context(),
                                               "max_tool_calls": config["max_tool_calls_per_attempt"]}
                response = await generate_with_tools(backend, prompt, payload, trace, context,
                                                     config["max_tool_calls_per_attempt"], diagnostics["tools"])
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
                    if mcp_factory:
                        diagnostics["mcp"] = {"status": "running"}
                        context = await open_context(work)
                        diagnostics["mcp"] = {"status": "checked", "directory": str(context.directory),
                                              "diagnostics": context.diagnostics}
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
            except (BackendError, MCPError) as error:
                if isinstance(error, MCPError):
                    summary["mcp_failures"] += 1
                    diagnostics["mcp"] = {"status": "failed", "error": str(error)}
                else:
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
                summary["model_tool_calls"] += sum(event["count"] for event in diagnostics["tools"])
                save_json(work / "model.json", {"api_format": API_FORMAT, "calls": trace}, sensitive)
                save_json(work / "diagnostics.json", diagnostics, sensitive)
                for channel in ("stdout", "stderr"):
                    (work / f"{channel}.txt").write_text(redact(diagnostics["cli"].get(channel, ""), sensitive),
                                                         encoding="utf-8")
    except asyncio.CancelledError:
        summary.update(status="infrastructure_failure", reason="cancelled")
        raise
    except MCPError as error:
        summary["mcp_failures"] += 1
        summary.update(status="infrastructure_failure", reason=str(error))
    except Exception as error:
        summary.update(status="infrastructure_failure", reason=f"{type(error).__name__}: {error}")
    finally:
        try:
            await sessions.aclose()
        except MCPError as error:
            summary["mcp_failures"] += 1
            summary.update(status="infrastructure_failure", reason=str(error))
        finally:
            mcp_events = [event for _, receipt in session_logs for event in receipt.get("calls", [])]
            summary.update(model_calls=len(events), tokens=token_totals(events), seconds=time.monotonic() - started,
                           model_seconds=sum(event.get("seconds", 0) for event in events), cli_seconds=compile_seconds,
                           mcp_sessions=len(session_logs),
                           mcp_calls=sum(event["operation"].startswith("codeql_") for event in mcp_events),
                           mcp_seconds=sum(event.get("seconds", 0) for event in mcp_events),
                           mcp_startup_seconds=sum(receipt.get("startup_seconds", 0) for _, receipt in session_logs))
            for log_dir, receipt in session_logs:
                save_json(log_dir / "session.json", receipt, sensitive)
                stderr = log_dir / "stderr.txt"
                if stderr.exists():
                    stderr.write_text(redact(stderr.read_text(encoding="utf-8"), sensitive), encoding="utf-8")
            save_json(directory / "summary.json", summary, sensitive)
    # Smoke is outside the repair loop and after MCP cleanup. It never changes the compile verdict.
    summary["generation_seconds"] = summary["seconds"]
    if smoke_databases is not None:
        summary["smoke_status"] = "not_run"
        if summary["compile_status"] == "passed":
            smoke = {}
            smoke_started = time.monotonic()
            try:
                work = directory / f"attempt_{summary['successful_attempt']}"
                await compiler.smoke(work / "query.ql", smoke_databases, work / "smoke", smoke)
            except asyncio.CancelledError:
                smoke.update(status="cancelled", reason="cancelled")
                raise
            except Exception as error:
                smoke.update(status="failed", reason=f"{type(error).__name__}: {error}")
            finally:
                summary.update(smoke_status=smoke.get("status", "failed"), smoke=smoke,
                               smoke_seconds=time.monotonic() - smoke_started,
                               seconds=time.monotonic() - started)
                save_json(directory / "summary.json", summary, sensitive)
    save_json(directory / "summary.json", summary, sensitive)
    return summary
