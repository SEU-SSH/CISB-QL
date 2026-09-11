"""Bounded local CodeQL commands and conservative compile-error classification."""

import asyncio
import hashlib
import json
import os
import signal
import time


CODEQL_VERSION = "2.24.3"
CPP_ALL_VERSION = "7.0.0"
PACK_HASHES = {
    "qlpack.yml": "4841af63fc8ab8af1c2e5399d2c8e5cffd216c7183d7f19feba91c184a3793d2",
    "codeql-pack.lock.yml": "712146adbcc9bfd2cf94b60e6abe76b65220c5a1404351304ed55ebcca830d9d",
}
PACK_FILES = (*PACK_HASHES, "query.qll", "query.ql")


def load_pack(path):
    files = {name: (path / name).read_bytes() for name in PACK_FILES}
    # Pin the exact E0 manifest and complete dependency lock, without a YAML dependency.
    for name, expected in PACK_HASHES.items():
        if hashlib.sha256(files[name]).hexdigest() != expected:
            raise ValueError(f"fixed E0 pack file changed: {name}")
    return files


def write_pack(path, files):
    path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (path / name).write_bytes(content)


def classify_compile(result, query):
    if result.get("timed_out") or result.get("launch_error"):
        return "infrastructure_error"
    if result["returncode"] == 0:
        return "passed"
    if result["returncode"] != 2:
        return "infrastructure_error"
    text = (result["stdout"] + result["stderr"]).lower()
    infrastructure = ("outofmemory", "out of memory", "no space left", "permission denied",
                      "could not resolve module cpp", "pack install", "pack download",
                      "referenced pack", "not installed", "could not locate", "fatal error")
    if any(marker in text for marker in infrastructure):
        return "infrastructure_error"
    try:
        reports = json.loads(result["stdout"])
        if not isinstance(reports, list):
            return "infrastructure_error"
        errors = [message for report in reports for message in report.get("messages", [])
                  if message.get("severity") == "ERROR"]
        local_files = {query.resolve(), query.with_suffix(".qll").resolve()}
        for error in errors:
            filename = error.get("position", {}).get("fileName")
            if not filename or (query.parent / filename).resolve() not in local_files:
                return "infrastructure_error"
        return "query_error" if errors else "infrastructure_error"
    except (ValueError, TypeError, AttributeError):
        return "infrastructure_error"


class CodeQL:
    def __init__(self, executable, timeout):
        self.executable = executable
        self.timeout = timeout

    async def command(self, *args, cwd):
        argv = [self.executable, *map(str, args)]
        result = {"argv": argv, "cwd": str(cwd), "returncode": None,
                  "stdout": "", "stderr": "", "timed_out": False}
        env = {key: os.environ[key] for key in
               ("PATH", "HOME", "TMPDIR", "JAVA_HOME", "LANG", "LC_ALL", "LD_LIBRARY_PATH")
               if key in os.environ}
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, start_new_session=True,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as error:
            result["launch_error"] = str(error)
            result["seconds"] = time.monotonic() - started
            return result
        communication = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), self.timeout)
        except (TimeoutError, asyncio.CancelledError) as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = await asyncio.shield(communication)
            if isinstance(error, asyncio.CancelledError):
                error.command_result = {**result, "status": "cancelled", "returncode": process.returncode,
                                        "stdout": stdout.decode("utf-8", errors="replace"),
                                        "stderr": stderr.decode("utf-8", errors="replace"),
                                        "seconds": time.monotonic() - started}
                raise
            result["timed_out"] = True
        result.update(returncode=process.returncode, stdout=stdout.decode("utf-8", errors="replace"),
                      stderr=stderr.decode("utf-8", errors="replace"), seconds=time.monotonic() - started)
        return result

    async def compile(self, query):
        result = await self.command("query", "compile", "--check-only", "--format=json",
                                    "--threads=1", query, cwd=query.parent)
        result["status"] = classify_compile(result, query)
        return result

    async def preflight(self, directory, pack, receipt):
        write_pack(directory, pack)
        version = await self.command("version", "--format=json", cwd=directory)
        receipt["version"] = version
        if version["returncode"] != 0 or version["timed_out"]:
            raise ValueError("CodeQL version check failed")
        if json.loads(version["stdout"]).get("version") != CODEQL_VERSION:
            raise ValueError(f"CodeQL {CODEQL_VERSION} is required")
        compiled = await self.compile(directory / "query.ql")
        receipt["template_compile"] = compiled
        if compiled["status"] != "passed":
            raise ValueError("handwritten template did not compile; fix the environment before generation")
