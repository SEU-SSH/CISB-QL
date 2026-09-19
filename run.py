"""Run a single-sample or serial batch experiment with MCP on/off."""

import argparse
import asyncio
import hashlib
from importlib.metadata import version
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from urllib.parse import urlsplit

from agent_backend import API_FORMAT, ResponsesBackend, redact
from codeql_tools import CodeQL, load_pack, mcp_session, model_tools, write_pack, CODEQL_VERSION, CPP_ALL_VERSION
from codeql_tools import TOOL_POLICY, cpp_all_root
from harness import input_error_summary, load_config, run_sample, save_json
from spec_io import collect_targets, parse_spec, source_id


ROOT = Path(__file__).resolve().parent


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--spec", help="one *_spec.md file")
    inputs.add_argument("--samples", help="list of spec paths relative to the project root")
    inputs.add_argument("--smoke-from", help="saved generation batch to check without any model calls")
    parser.add_argument("--mcp", choices=("on", "off"), default="on")
    parser.add_argument("--out", required=True, help="new batch directory under runs/")
    parser.add_argument("--smoke", action="store_true", help="run compiled candidates on the fixed C/C++ smoke databases")
    return parser.parse_args(argv)


def new_output(root, value):
    output = (root / value).resolve()
    if not output.is_relative_to(root / "runs") or output == root / "runs":
        raise ValueError("--out must be a new directory under this project's runs/")
    if output.exists():
        raise ValueError("output directory already exists; choose a new batch name")
    return output


async def run_batch(root, args, backend_factory=ResponsesBackend, compiler_factory=CodeQL, mcp_factory=None):
    output = new_output(root, args.out)
    if args.smoke_from:
        return await run_saved_smoke(root, args.smoke_from, output, compiler_factory)
    config = load_config(root / "config.json")
    api_key = os.environ.get("QL_API_KEY", "").strip()
    base_url = os.environ.get("QL_BASE_URL", "").strip()
    if not api_key or not base_url:
        raise ValueError("export QL_API_KEY and QL_BASE_URL in the terminal running this command")
    url = urlsplit(base_url)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise ValueError("QL_BASE_URL must be an HTTP(S) API base URL without credentials, query, or fragment")
    sensitive = (api_key, base_url)
    paths = collect_targets(root, args.spec, args.samples)
    bundles = []
    for path in paths:
        bundle = {"source_id": source_id(path), "path": str(path)}
        try:
            bundle["raw"] = path.read_bytes().decode("utf-8")
            bundle["parsed"] = parse_spec(bundle["raw"])
        except (ValueError, OSError) as error:
            bundle["input_error"] = str(error)
        bundles.append(bundle)
    pack = load_pack(root / "codeql-pack")
    prompt = (root / "prompt.md").read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("prompt.md is empty")
    executable = os.environ.get("CODEQL_PATH", "/usr/local/codeql/codeql")
    mcp_environment = None
    tool_budget = config["max_tool_calls_per_attempt"] if args.mcp == "on" else 0
    if args.mcp == "on":
        entry = Path(os.environ.get("CODEQL_MCP_ENTRY", str(Path.home() / "codeql-lsp-mcp/dist/index.js"))).resolve()
        deployment = {entry, entry.with_name("codeql-lsp-client.js"), entry.with_name("api-tools.js"),
                      entry.parent.parent / "package-lock.json", *entry.parent.rglob("*.js")}
        mcp_environment = {"entry": str(entry), "cpp_all_root": str(cpp_all_root()),
                           "cpp_all_version": CPP_ALL_VERSION, "tool_policy": TOOL_POLICY, "files_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(deployment)}}
        if mcp_factory is None:
            def mcp_factory(path, receipt, stderr_path):
                return mcp_session(path, executable, entry, config["timeout_seconds"], receipt, stderr_path)
    code_files = ("run.py", "harness.py", "agent_backend.py", "codeql_tools.py", "spec_io.py", "requirements.txt")
    experiment = {"stage": "E3", "revision": "E3-r2", "api_format": API_FORMAT, "status": "running", "mcp": args.mcp, "config": config,
                  "smoke_requested": args.smoke,
                  "mcp_environment": mcp_environment, "model_tools": model_tools() if args.mcp == "on" else [],
                  "versions": {"python": sys.version, "openai": version("openai"), "mcp": version("mcp"),
                               "codeql": CODEQL_VERSION, "cpp_all": CPP_ALL_VERSION},
                  "prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                  "files_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in code_files},
                  "pack_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in pack.items()},
                  "samples": [{"source_id": b["source_id"], "path": b["path"],
                               "sha256": hashlib.sha256(b["raw"].encode()).hexdigest() if "raw" in b else None,
                               "input_error": b.get("input_error")} for b in bundles],
                  "model_policy": {"sdk_retries": 0, "connection_retries": 1, "timeout_retries": 0,
                                   "max_tool_calls_per_attempt": tool_budget,
                                   "max_requests_per_attempt": 2 * (tool_budget + 1),
                                   "token_parameter": "max_output_tokens", "context": "client_managed_stateless"},
                  "sampling_note": {
                      "thinking_mode": "provider default; deliberately kept enabled for all runs",
                      "ignored_parameters": ["temperature", "presence_penalty", "frequency_penalty"],
                      "clamped_parameters": {"top_p": ">= 0.95"},
                      "reasoning_tokens_count_against": "max_output_tokens",
                      "effect": "the configured temperature is recorded but not applied to the samples drawn"},
                  "preflight": {}, "summary": []}
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    compiler = compiler_factory(executable, config["timeout_seconds"])
    try:
        save_json(output / "experiment.json", experiment, sensitive)
        await compiler.preflight(output / "preflight", pack, experiment["preflight"])
        databases = None
        if args.smoke:
            experiment["preflight"]["smoke"] = {}
            databases = await compiler.smoke_preflight(root, output / "preflight", experiment["preflight"]["smoke"])
        async with backend_factory(config, api_key, base_url) as backend:
            for bundle in bundles:
                directory = output / bundle["source_id"]
                if "input_error" in bundle:
                    directory.mkdir()
                    if "raw" in bundle:
                        (directory / "spec.md").write_bytes(bundle["raw"].encode("utf-8"))
                    summary = input_error_summary(bundle["source_id"], bundle["input_error"], args.mcp)
                    save_json(directory / "summary.json", summary, sensitive)
                else:
                    summary = await run_sample(bundle, directory, config, prompt, pack, backend, compiler, sensitive,
                                               mcp_factory=mcp_factory if args.mcp == "on" else None,
                                               smoke_databases=databases)
                experiment["summary"].append(summary)
                save_json(output / "experiment.json", experiment, sensitive)
                print(f"{bundle['source_id']}: {summary['status']}", flush=True)
        experiment["status"] = "passed" if all(
            x["status"] == "compiled" and (not args.smoke or x["smoke_status"] == "passed")
            for x in experiment["summary"]) else "failed"
    except asyncio.CancelledError:
        experiment.update(status="cancelled", error="cancelled; inspect any partial sample summary")
        raise
    except Exception as error:
        experiment.update(status="infrastructure_failure", error=f"{type(error).__name__}: {error}")
        print(redact(experiment["error"], sensitive), file=sys.stderr)
    finally:
        experiment["seconds"] = time.monotonic() - started
        save_json(output / "experiment.json", experiment, sensitive)
    print(f"Results: {output}")
    return 0 if experiment["status"] == "passed" else 1


async def run_saved_smoke(root, source, output, compiler_factory=CodeQL):
    from spec_io import json_object

    source = (root / source).resolve()
    if not source.is_relative_to(root / "runs") or output.is_relative_to(source):
        raise ValueError("--smoke-from must be a saved batch under runs/; --out must be outside that batch")
    original = json_object((source / "experiment.json").read_text(encoding="utf-8"))
    if original.get("stage") not in ("E1", "E2", "E3") or original.get("status") in ("running", "cancelled"):
        raise ValueError("--smoke-from requires a finished generation batch")
    candidates = []
    for summary in original["summary"]:
        if summary.get("compile_status") != "passed":
            continue
        identifier, attempt = summary["source_id"], summary["successful_attempt"]
        if (not isinstance(identifier, str) or Path(identifier).name != identifier or identifier in (".", "..")
                or type(attempt) is not int or not 0 <= attempt <= 3):
            raise ValueError("invalid saved candidate identity")
        candidate = (source / identifier / f"attempt_{attempt}").resolve()
        if not candidate.is_relative_to(source):
            raise ValueError("saved candidate escapes its batch")
        diagnostics = json_object((candidate / "diagnostics.json").read_text(encoding="utf-8"))
        if diagnostics.get("cli", {}).get("status") != "passed":
            raise ValueError("saved compile verdict disagrees with candidate diagnostics")
        candidates.append((identifier, candidate, load_pack(candidate)))
    if not candidates:
        raise ValueError("saved batch contains no compiled candidates")
    config = load_config(root / "config.json")
    compiler = compiler_factory(os.environ.get("CODEQL_PATH", "/usr/local/codeql/codeql"), config["timeout_seconds"])
    receipt = {"stage": "E3-smoke", "status": "running", "source_batch": str(source),
               "source_experiment_sha256": hashlib.sha256((source / "experiment.json").read_bytes()).hexdigest(),
               "model_calls": 0, "semantic_status": "not_evaluated", "timeout_seconds": config["timeout_seconds"],
               "preflight": {}, "summary": []}
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    try:
        await compiler.preflight(output / "preflight", load_pack(root / "codeql-pack"), receipt["preflight"])
        receipt["preflight"]["smoke"] = {}
        databases = await compiler.smoke_preflight(root, output / "preflight", receipt["preflight"]["smoke"])
        for identifier, candidate, files in candidates:
            directory = output / identifier
            directory.mkdir()
            item = {"source_id": identifier, "source_candidate": str(candidate), "compile_status": "passed",
                    "source_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                    "smoke_status": "not_run", "smoke": {}, "semantic_status": "not_evaluated"}
            receipt["summary"].append(item)
            try:
                # CodeQL may create compilation caches. Never run it inside the archived candidate.
                with tempfile.TemporaryDirectory(prefix="qlcoder-smoke-") as temporary:
                    work = Path(temporary)
                    write_pack(work, files)
                    item["compile_check"] = await compiler.compile(work / "query.ql")
                    if item["compile_check"]["status"] == "passed":
                        await compiler.smoke(work / "query.ql", databases, directory / "smoke", item["smoke"])
                        item["smoke_status"] = item["smoke"]["status"]
                    else:
                        item.update(smoke_status="failed", reason="independent_recompile_failed")
            finally:
                item["smoke_status"] = item["smoke"].get("status", item["smoke_status"])
                save_json(directory / "summary.json", item)
            print(f"{identifier}: smoke {item['smoke_status']}", flush=True)
        receipt["status"] = "passed" if all(item["smoke_status"] == "passed" for item in receipt["summary"]) else "failed"
    except asyncio.CancelledError:
        receipt.update(status="cancelled", error="cancelled")
        raise
    except Exception as error:
        receipt.update(status="infrastructure_failure", error=f"{type(error).__name__}: {error}")
    finally:
        receipt["seconds"] = time.monotonic() - started
        save_json(output / "experiment.json", receipt)
    print(f"Results: {output}")
    return 0 if receipt["status"] == "passed" else 1


async def cancellable_run(args):
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    try:
        return await run_batch(ROOT, args)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main(argv=None):
    args = arguments(argv)
    try:
        return asyncio.run(cancellable_run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    except (ValueError, OSError) as error:
        sensitive = (os.environ.get("QL_API_KEY", ""), os.environ.get("QL_BASE_URL", ""))
        print(f"Preflight failed: {redact(str(error), sensitive)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
