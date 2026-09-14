"""Bounded CodeQL commands and read-only, per-candidate MCP sessions."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from urllib.parse import unquote, urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


CODEQL_VERSION = "2.24.3"
CPP_ALL_VERSION = "7.0.0"
PACK_HASHES = {
    "qlpack.yml": "4841af63fc8ab8af1c2e5399d2c8e5cffd216c7183d7f19feba91c184a3793d2",
    "codeql-pack.lock.yml": "712146adbcc9bfd2cf94b60e6abe76b65220c5a1404351304ed55ebcca830d9d",
}
PACK_FILES = (*PACK_HASHES, "query.qll", "query.ql")
MODEL_TOOLS = ("codeql_hover", "codeql_definition", "codeql_complete")


class MCPError(Exception):
    pass


def model_tools():
    properties = {"file": {"type": "string", "enum": ["query.ql", "query.qll"]},
                  "line": {"type": "integer", "minimum": 0, "description": "0-based source line"},
                  "character": {"type": "integer", "minimum": 0, "description": "0-based UTF-16 offset"}}
    return [{"type": "function", "name": name,
             "description": f"{name} on a read-only file in tool_context; completion is capped at 20 items. "
                            "Inspect existing code only; submit the next complete pair as final JSON.",
             "parameters": {"type": "object", "properties": properties,
                            "required": list(properties), "additionalProperties": False}}
            for name in MODEL_TOOLS]


class CandidateMCP:
    def __init__(self, session, directory, timeout, receipt):
        self.session, self.directory, self.timeout, self.receipt = session, directory.resolve(), timeout, receipt
        self.files = {name: (self.directory / name).read_bytes() for name in PACK_FILES}
        self.diagnostics = {}

    def unchanged(self):
        try:
            if any((self.directory / name).read_bytes() != data for name, data in self.files.items()):
                raise MCPError("read-only candidate changed during its MCP session")
        except OSError as error:
            raise MCPError(f"candidate read failed: {error}") from error

    def context(self):
        return {"directory": str(self.directory), "read_only": True,
                "position_encoding": "0-based lines and UTF-16 characters",
                "files": {name: self.files[name].decode("utf-8") for name in ("query.ql", "query.qll")},
                "diagnostics": self.diagnostics}

    async def request(self, operation, arguments=None, *, actor="harness", parse=False):
        event = {"operation": operation, "arguments": arguments, "actor": actor, "status": "running"}
        self.receipt["calls"].append(event)
        started = time.monotonic()
        try:
            self.unchanged()
            if operation == "initialize":
                pending = self.session.initialize()
            elif operation == "list_tools":
                pending = self.session.list_tools()
            else:
                pending = self.session.call_tool(operation, arguments)
            result = await asyncio.wait_for(pending, self.timeout)
            event["result"] = result.model_dump(mode="json", exclude_none=True)
            if getattr(result, "isError", False):
                raise MCPError(f"{operation} returned isError: {event['result']}")
            value = event["result"]
            if parse:
                text = "\n".join(item.text for item in result.content if item.type == "text")
                # The pinned completion adapter appends a pagination note after its JSON.
                if operation == "codeql_complete":
                    value, end = json.JSONDecoder().raw_decode(text.lstrip())
                    tail = text.lstrip()[end:].strip()
                    if tail and not tail.startswith("[PAGINATION INFO:"):
                        raise ValueError("unexpected completion suffix")
                else:
                    value = json.loads(text)
            event["status"] = "passed"
            return value
        except asyncio.CancelledError:
            event.update(status="cancelled", error="cancelled")
            raise
        except Exception as error:
            event.update(status="failed", error=f"{type(error).__name__}: {error}")
            raise MCPError(f"MCP {operation} failed: {error}") from error
        finally:
            event["seconds"] = time.monotonic() - started

    async def start(self):
        await self.request("initialize")
        tools = await self.request("list_tools")
        required = {*MODEL_TOOLS, "codeql_set_workspace", "codeql_open_file", "codeql_diagnostics"}
        if not required <= {tool["name"] for tool in tools["tools"]}:
            raise MCPError("MCP server is missing required tools")
        await self.request("codeql_set_workspace", {"folders": [str(self.directory)]})
        for name in ("query.qll", "query.ql"):
            await self.request("codeql_open_file", {"file_uri": (self.directory / name).as_uri(),
                                                    "content": self.files[name].decode("utf-8")})
        for name in ("query.qll", "query.ql"):
            value = await self.request("codeql_diagnostics", {"file_uri": (self.directory / name).as_uri()}, parse=True)
            if not isinstance(value, list) or any(not isinstance(item, dict) or "message" not in item for item in value):
                raise MCPError("invalid diagnostics payload; absence of a notification is not a clean result")
            self.diagnostics[name] = value
        self.receipt["diagnostics"] = self.diagnostics

    def arguments(self, name, arguments):
        if name not in MODEL_TOOLS or not isinstance(arguments, dict):
            raise ValueError("only hover, definition and completion are allowed")
        if set(arguments) != {"file", "line", "character"} or arguments["file"] not in ("query.ql", "query.qll"):
            raise ValueError("use file query.ql/query.qll and integer line/character only")
        line, character = arguments["line"], arguments["character"]
        lines = self.files[arguments["file"]].decode("utf-8").split("\n")
        if type(line) is not int or type(character) is not int or not 0 <= line < len(lines):
            raise ValueError("invalid 0-based source position")
        boundaries = {0}
        offset = 0
        for char in lines[line].rstrip("\r"):
            offset += len(char.encode("utf-16-le")) // 2
            boundaries.add(offset)
        if character not in boundaries:
            raise ValueError("character must be a UTF-16 character boundary within the line")
        result = {"file_uri": (self.directory / arguments["file"]).as_uri(), "line": line, "character": character}
        if name == "codeql_complete":
            result.update(limit=20, offset=0)
        return result

    def definition_sources(self, locations):
        sources = []
        library = (Path.home() / ".codeql/packages/codeql/cpp-all" / CPP_ALL_VERSION).resolve()
        items = locations if isinstance(locations, list) else [locations]
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            uri = urlsplit(item.get("uri", item.get("targetUri", "")))
            if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                continue
            path = Path(unquote(uri.path)).resolve()
            if path not in {self.directory / "query.ql", self.directory / "query.qll"} and not path.is_relative_to(library):
                continue
            position = item.get("range", item.get("targetSelectionRange", {})).get("start", {})
            line = position.get("line")
            if type(line) is not int or line < 0 or path.suffix not in (".ql", ".qll"):
                continue
            try:
                with path.open(encoding="utf-8") as source:
                    excerpt = []
                    for index, text in enumerate(source):
                        if index >= line + 16:
                            break
                        if index >= max(0, line - 4):
                            excerpt.append(text)
                sources.append({"uri": path.as_uri(), "start_line": max(0, line - 4),
                                "text": "".join(excerpt)[:8000]})
            except OSError:
                continue
        return sources

    async def invoke(self, name, arguments):
        parameters = self.arguments(name, arguments)
        result = await self.request(name, parameters, actor="model", parse=True)
        if name == "codeql_complete":
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise MCPError("invalid completion result")
            result["items"] = result["items"][:20]
        if name == "codeql_definition":
            return {"locations": result, "sources": self.definition_sources(result)}
        return result


@asynccontextmanager
async def mcp_session(directory, executable, entry, timeout, receipt, stderr_path):
    receipt.update(directory=str(directory.resolve()), status="starting", calls=[], close_status="pending")
    started = time.monotonic()
    env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "JAVA_HOME", "LANG", "LC_ALL", "LD_LIBRARY_PATH")
           if key in os.environ}
    env["CODEQL_PATH"] = executable
    params = StdioServerParameters(command="node", args=[str(entry)], cwd=str(directory), env=env)
    receipt["launch"] = {"command": params.command, "args": params.args, "cwd": params.cwd, "codeql": executable}
    pending_error = None
    try:
        with stderr_path.open("w", encoding="utf-8") as log:
            async with stdio_client(params, errlog=log) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                    try:
                        client = CandidateMCP(session, directory, timeout, receipt)
                        try:
                            await client.start()
                        finally:
                            receipt["startup_seconds"] = time.monotonic() - started
                        receipt["status"] = "ready"
                        yield client
                        client.unchanged()
                    except BaseException as error:
                        # Exit SDK task groups normally, then propagate the original failure.
                        pending_error = error
        receipt["close_status"] = "closed"
        if pending_error is not None:
            raise pending_error
        receipt["status"] = "closed"
    except asyncio.CancelledError:
        receipt.update(status="cancelled", error="cancelled")
        raise
    except Exception as error:
        receipt.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise MCPError(f"MCP session failed: {error}") from error
    finally:
        receipt["seconds"] = time.monotonic() - started


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

    async def smoke(self, query, databases, directory, receipt, *, require_main=False):
        directory.mkdir(parents=True, exist_ok=False)
        started = time.monotonic()
        receipt.update(status="running", databases={}, require_main=require_main)
        try:
            for language, database in databases.items():
                work = directory / language
                work.mkdir()
                item = {"database": str(database), "status": "running", "row_count": None}
                receipt["databases"][language] = item
                operation = "run"
                try:
                    item[operation] = await self.command(
                        "query", "run", query, f"--database={database}",
                        f"--output={work / 'result.bqrs'}", "--threads=1", cwd=query.parent)
                    result = item[operation]
                    if result["returncode"] != 0 or result["timed_out"]:
                        item.update(status="failed", reason="run_failed")
                        continue
                    operation = "decode"
                    item[operation] = await self.command(
                        "bqrs", "decode", work / "result.bqrs", "--format=json",
                        f"--output={work / 'rows.json'}", cwd=query.parent)
                    result = item[operation]
                    if result["returncode"] != 0 or result["timed_out"]:
                        item.update(status="failed", reason="decode_failed")
                        continue
                    rows = decoded_rows(work / "rows.json")
                    item.update(status="passed", row_count=len(rows))
                    if require_main and not any("main" in row for row in rows):
                        item.update(status="failed", reason="template_did_not_find_main")
                except asyncio.CancelledError as error:
                    item[operation] = getattr(error, "command_result", {"status": "cancelled"})
                    item.update(status="cancelled", reason="cancelled")
                    raise
                except (OSError, ValueError) as error:
                    item.update(status="failed", reason=f"{operation}: {type(error).__name__}: {error}")
            receipt["status"] = "passed" if all(
                item["status"] == "passed" for item in receipt["databases"].values()) else "failed"
        except asyncio.CancelledError:
            receipt["status"] = "cancelled"
            raise
        finally:
            receipt["seconds"] = time.monotonic() - started
            (directory / "smoke.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        return receipt

    async def smoke_preflight(self, root, directory, receipt):
        databases = smoke_databases(root)
        receipt["inputs_sha256"] = {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for language, database in databases.items()
            for path in (database / "codeql-database.yml", database / "src.zip",
                         root / "fixtures" / language / ("sample.c" if language == "c" else "sample.cpp"))}
        await self.smoke(directory / "query.ql", databases, directory / "smoke", receipt, require_main=True)
        if receipt["status"] != "passed":
            raise ValueError("smoke database preflight failed: handwritten template must find main in both databases")
        return databases


def smoke_databases(root):
    return {language: root / "databases" / f"smoke-{language}-db" for language in ("c", "cpp")}


def decoded_rows(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError("invalid decoded BQRS result sets")
    rows = []
    for result in payload.values():
        if (not isinstance(result, dict) or not isinstance(result.get("columns"), list)
                or not isinstance(result.get("tuples"), list)
                or any(not isinstance(row, list) or len(row) != len(result["columns"])
                       for row in result["tuples"])):
            raise ValueError("invalid decoded BQRS columns/tuples")
        rows.extend(result["tuples"])
    return rows
