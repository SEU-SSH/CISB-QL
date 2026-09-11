"""Run an E1 single-sample or serial batch experiment (MCP off only)."""

import argparse
import asyncio
import hashlib
from importlib.metadata import version
import os
from pathlib import Path
import signal
import sys
import time
from urllib.parse import urlsplit

from agent_backend import ChatBackend, redact
from codeql_tools import CodeQL, load_pack, CODEQL_VERSION, CPP_ALL_VERSION
from harness import input_error_summary, load_config, run_sample, save_json
from spec_io import collect_targets, parse_spec, source_id


ROOT = Path(__file__).resolve().parent


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--spec", help="one *_spec.md file")
    inputs.add_argument("--samples", help="list of spec paths relative to the project root")
    parser.add_argument("--mcp", choices=("on", "off"), default="on")
    parser.add_argument("--out", required=True, help="new batch directory under runs/")
    parser.add_argument("--smoke", action="store_true", help="reserved for the later smoke stage")
    return parser.parse_args(argv)


async def run_batch(root, args, backend_factory=ChatBackend, compiler_factory=CodeQL):
    if args.mcp != "off":
        raise ValueError("E1 supports --mcp off only; MCP on will be implemented in E2 (no fallback)")
    if args.smoke:
        raise ValueError("--smoke is not implemented in E1; E0 databases and local checks remain available")
    output = (root / args.out).resolve()
    if not output.is_relative_to(root / "runs") or output == root / "runs":
        raise ValueError("--out must be a new directory under this project's runs/")
    if output.exists():
        raise ValueError("output directory already exists; choose a new batch name")
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
    code_files = ("run.py", "harness.py", "agent_backend.py", "codeql_tools.py", "spec_io.py", "requirements.txt")
    experiment = {"stage": "E1", "status": "running", "mcp": "off", "config": config,
                  "versions": {"python": sys.version, "openai": version("openai"), "mcp": version("mcp"),
                               "codeql": CODEQL_VERSION, "cpp_all": CPP_ALL_VERSION},
                  "prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                  "files_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in code_files},
                  "pack_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in pack.items()},
                  "samples": [{"source_id": b["source_id"], "path": b["path"],
                               "sha256": hashlib.sha256(b["raw"].encode()).hexdigest() if "raw" in b else None,
                               "input_error": b.get("input_error")} for b in bundles],
                  "model_policy": {"sdk_retries": 0, "connection_retries": 1, "timeout_retries": 0,
                                   "tool_calls": 0, "max_requests_per_attempt": 2, "token_parameter": "max_tokens"},
                  "preflight": {}, "summary": []}
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    compiler = compiler_factory(os.environ.get("CODEQL_PATH", "/usr/local/codeql/codeql"), config["timeout_seconds"])
    try:
        save_json(output / "experiment.json", experiment, sensitive)
        await compiler.preflight(output / "preflight", pack, experiment["preflight"])
        async with backend_factory(config, api_key, base_url) as backend:
            for bundle in bundles:
                directory = output / bundle["source_id"]
                if "input_error" in bundle:
                    directory.mkdir()
                    if "raw" in bundle:
                        (directory / "spec.md").write_bytes(bundle["raw"].encode("utf-8"))
                    summary = input_error_summary(bundle["source_id"], bundle["input_error"])
                    save_json(directory / "summary.json", summary, sensitive)
                else:
                    summary = await run_sample(bundle, directory, config, prompt, pack, backend, compiler, sensitive)
                experiment["summary"].append(summary)
                save_json(output / "experiment.json", experiment, sensitive)
                print(f"{bundle['source_id']}: {summary['status']}", flush=True)
        experiment["status"] = "passed" if all(x["status"] == "compiled" for x in experiment["summary"]) else "failed"
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
