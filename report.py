"""Aggregate explicitly paired A/B batches; never invoke a model or modify input runs."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from harness import save_json
from run import ROOT, new_output
from spec_io import json_object


CONTROL_FIELDS = ("api_format", "config", "versions", "prompt_sha256", "files_sha256", "pack_sha256", "sampling_note")
COST_FIELDS = ("generation_seconds", "model_seconds", "cli_seconds", "smoke_seconds", "model_calls",
               "compile_calls", "model_tool_calls", "mcp_calls", "mcp_failures", "mcp_seconds", "mcp_startup_seconds",
               "input_tokens", "output_tokens", "total_tokens")


def read_json(path):
    return json_object(path.read_text(encoding="utf-8"))


def identities(experiment):
    result = {}
    for sample in experiment["samples"]:
        identifier = sample["source_id"]
        if (not isinstance(identifier, str) or Path(identifier).name != identifier or identifier in (".", "..")
                or identifier in result):
            raise ValueError("invalid or duplicate sample ID")
        result[identifier] = {"sha256": sample["sha256"], "input_error": sample.get("input_error")}
        if sample.get("input_error") is None and not sample["sha256"]:
            raise ValueError("legal input requires a recorded spec hash")
    if not result:
        raise ValueError("empty sample manifest")
    return result


def shared_controls(experiment):
    controls = {key: experiment[key] for key in CONTROL_FIELDS}
    if hashlib.sha256(experiment["prompt"].encode()).hexdigest() != experiment["prompt_sha256"]:
        raise ValueError("prompt hash does not match recorded prompt")
    policy = dict(experiment["model_policy"])
    budget = experiment["config"]["max_tool_calls_per_attempt"] if experiment["mcp"] == "on" else 0
    if policy.pop("max_tool_calls_per_attempt") != budget or policy.pop("max_requests_per_attempt") != 2 * (budget + 1):
        raise ValueError("unexpected on/off tool budget")
    controls.update(model_policy=policy, samples=identities(experiment),
                    smoke_requested=experiment.get("smoke_requested", False))
    return controls


def sample_rows(batch, experiment, group, repetition):
    summaries = {item["source_id"]: item for item in experiment["summary"]}
    samples = identities(experiment)
    if len(summaries) != len(experiment["summary"]) or not summaries.keys() <= samples.keys():
        raise ValueError("duplicate or unexpected sample summary")
    rows = []
    for identifier, sample in samples.items():
        directory = batch / identifier
        if not directory.resolve().is_relative_to(batch):
            raise ValueError("sample directory escapes its batch")
        snapshot = directory / "spec.md"
        if snapshot.exists() and hashlib.sha256(snapshot.read_bytes()).hexdigest() != sample["sha256"]:
            raise ValueError(f"spec snapshot was changed: {snapshot}")
        summary = summaries.get(identifier)
        path = directory / "summary.json"
        if path.exists():
            stored = read_json(path)
            if summary is not None and stored != summary:
                raise ValueError(f"batch/sample summaries disagree: {path}")
            summary = stored
        missing = summary is None
        if missing:
            summary = {"status": "input_error" if sample["input_error"] is not None else "infrastructure_failure",
                       "reason": "missing sample summary: " + str(experiment.get("error", experiment["status"]))}
        legal = sample["input_error"] is None
        if (summary["status"] == "input_error") == legal:
            raise ValueError("input validity disagrees with the frozen manifest")
        attempts = []
        for index in range(experiment["config"]["max_repair_attempts"] + 1):
            path = directory / f"attempt_{index}" / "diagnostics.json"
            if path.exists():
                attempts.append((index, read_json(path)))
        passed = [index for index, diagnostics in attempts if diagnostics.get("cli", {}).get("status") == "passed"]
        success = min(passed) if passed else None
        if summary.get("compile_status") == "passed" and summary.get("successful_attempt") != success:
            raise ValueError(f"compiled summary has no matching CLI evidence: {directory}")
        if summary["status"] == "compiled" and success is None:
            raise ValueError(f"compiled summary has no CLI evidence: {directory}")
        row = {"group": group, "repetition": repetition, "batch": str(batch), "source_id": identifier,
               "legal_input": legal, "status": summary["status"], "reason": summary.get("reason"),
               "missing_summary": missing, "compile_initial": success == 0, "compile_within3repairs": success is not None,
               "successful_attempt": success, "smoke_status": summary.get("smoke_status", "not_requested"),
               "format_errors": sum(d.get("format", {}).get("status") == "error" for _, d in attempts),
               "ql_errors": sum(d.get("cli", {}).get("status") == "query_error" for _, d in attempts),
               "attempt_logs_missing": max(0, summary.get("attempts", 0) - len(attempts))}
        for kind, section, status in (("format", "format", "error"), ("ql", "cli", "query_error")):
            row[f"{kind}_repairs_started"] = sum(d.get(section, {}).get("status") == status
                and any(later > index for later, _ in attempts) for index, d in attempts)
        for key in COST_FIELDS:
            if key.endswith("_tokens"):
                row[key] = (summary.get("tokens") or {}).get(key)
            elif key == "generation_seconds":
                row[key] = summary.get(key, summary.get("seconds"))
            elif key == "smoke_seconds":
                row[key] = summary.get(key, 0 if row["smoke_status"] == "not_requested" else None)
            else:
                row[key] = summary.get(key)
        rows.append(row)
    return rows


def rate(numerator, denominator):
    return {"passed": numerator, "total": denominator, "rate": numerator / denominator if denominator else None}


def aggregate(rows, smoke_requested):
    legal = [row for row in rows if row["legal_input"]]
    successes = [row for row in legal if row["compile_within3repairs"]]
    metrics = {"legal_inputs": len(legal), "invalid_inputs": len(rows) - len(legal),
               "CompileInitial": rate(sum(row["compile_initial"] for row in legal), len(legal)),
               "CompileWithin3Repairs": rate(len(successes), len(legal)),
               "mean_repairs_on_success": sum(row["successful_attempt"] for row in successes) / len(successes) if successes else None,
               "generation_compile_failures": sum(row["status"] == "generation_compile_failure" for row in legal),
               "infrastructure_failures": sum(row["status"] == "infrastructure_failure" for row in legal),
               "not_compiled": len(legal) - len(successes),
               "smoke": rate(sum(row["smoke_status"] == "passed" for row in successes), len(successes)) if smoke_requested else None}
    for key in ("missing_summary", "attempt_logs_missing", "format_errors", "ql_errors", "format_repairs_started", "ql_repairs_started"):
        metrics[key] = sum(row[key] for row in legal)
    metrics["costs"] = {}
    for key in COST_FIELDS:
        values = [row[key] for row in legal if row[key] is not None]
        metrics["costs"][key] = {"total": sum(values) if len(values) == len(legal) else None,
                                "known_total": sum(values), "missing_samples": len(legal) - len(values)}
    return metrics


def build_report(a_paths, b_paths, *, purpose, overlap_note, note=""):
    if purpose not in ("pilot", "formal") or not overlap_note.strip():
        raise ValueError("declare pilot/formal purpose and debug/evaluation overlap")
    if not a_paths or len(a_paths) != len(b_paths):
        raise ValueError("supply equally many A/B batches in repetition order")
    repetitions = len(a_paths)
    if purpose == "formal" and repetitions < 3 and not note.strip():
        raise ValueError("formal experiments default to three repetitions; explain a reduced budget with --note")
    all_paths = [Path(path).resolve() for path in (*a_paths, *b_paths)]
    if len(set(all_paths)) != len(all_paths):
        raise ValueError("a batch cannot be counted twice")
    controls = None
    b_environment = None
    b_tools = None
    db_hashes = None
    rows, inputs = [], []
    for group, paths, mode in (("A", a_paths, "off"), ("B", b_paths, "on")):
        for repetition, path in enumerate(paths, 1):
            batch = Path(path).resolve()
            experiment = read_json(batch / "experiment.json")
            if (experiment.get("validation_only") or experiment.get("not_research_result")
                    or experiment.get("model_transport") == "httpx.MockTransport"):
                raise ValueError(f"validation/mock receipt is not a research result: {batch}")
            if experiment.get("stage") not in ("E1", "E2", "E3") or experiment.get("api_format") != "responses":
                raise ValueError("only Responses generation batches can be compared")
            if experiment.get("status") not in ("passed", "failed", "infrastructure_failure", "cancelled"):
                raise ValueError(f"batch is unfinished or has an unknown status: {batch}")
            if experiment.get("mcp") != mode:
                raise ValueError(f"group {group} requires --mcp {mode}: {batch}")
            current = shared_controls(experiment)
            if controls is None:
                controls = current
            elif current != controls:
                changed = [key for key in controls if controls[key] != current[key]]
                raise ValueError(f"experimental controls differ ({', '.join(changed)}): {batch}")
            if mode == "off" and experiment.get("model_tools"):
                raise ValueError("off group must not expose model tools")
            if mode == "on":
                environment, tools = experiment["mcp_environment"], experiment["model_tools"]
                if b_environment is not None and (environment != b_environment or tools != b_tools):
                    raise ValueError("B repetitions use different MCP deployments or tool schemas")
                b_environment, b_tools = environment, tools
            hashes = experiment.get("preflight", {}).get("smoke", {}).get("inputs_sha256")
            if hashes is not None:
                if db_hashes is not None and hashes != db_hashes:
                    raise ValueError("smoke database/fixture fingerprints differ")
                db_hashes = hashes
            rows.extend(sample_rows(batch, experiment, group, repetition))
            inputs.append({"group": group, "repetition": repetition, "batch": str(batch),
                           "status": experiment["status"], "batch_seconds": experiment.get("seconds"),
                           "experiment_sha256": hashlib.sha256((batch / "experiment.json").read_bytes()).hexdigest()})
    if not any(row["legal_input"] for row in rows):
        raise ValueError("no legal inputs to compare")
    groups = {group: aggregate([row for row in rows if row["group"] == group], controls["smoke_requested"])
              for group in ("A", "B")}
    paired = []
    lookup = {(row["group"], row["repetition"], row["source_id"]): row for row in rows}
    for row in rows:
        if row["group"] == "A" and row["legal_input"]:
            other = lookup[("B", row["repetition"], row["source_id"])]
            paired.append({"repetition": row["repetition"], "source_id": row["source_id"],
                           "A_initial": row["compile_initial"], "B_initial": other["compile_initial"],
                           "A_within3": row["compile_within3repairs"], "B_within3": other["compile_within3repairs"]})
    outcomes = {"both": 0, "A_only": 0, "B_only": 0, "neither": 0}
    for pair in paired:
        a, b = pair["A_within3"], pair["B_within3"]
        outcomes["both" if a and b else "A_only" if a else "B_only" if b else "neither"] += 1
    return {"stage": "E3-report", "created_at": datetime.now(timezone.utc).isoformat(), "purpose": purpose,
            "repetitions": repetitions, "overlap_note": overlap_note, "note": note,
            "warning": "pilot/debug observations, not formal evaluation" if purpose == "pilot" else
                       "fewer than three repetitions; exploratory evidence only" if repetitions < 3 else
                       "paired descriptive results; repetitions are not distinct specs or proof of generalization",
            "semantic_status": "not_evaluated", "controls": controls, "inputs": inputs,
            "mcp_environment": b_environment, "model_tools_B": b_tools, "smoke_inputs_sha256": db_hashes,
            "groups": groups, "pairs": paired, "paired_within3": outcomes,
            "per_repetition": {str(rep): {group: aggregate([row for row in rows if row["group"] == group
                and row["repetition"] == rep], controls["smoke_requested"]) for group in ("A", "B")}
                for rep in range(1, repetitions + 1)}, "rows": rows}


def markdown(report):
    lines = [f"# A/B {report['purpose']} report", "", report["warning"], "",
             f"Repetitions per group: {report['repetitions']}", f"Overlap: {report['overlap_note']}",
             f"Note: {report['note'] or 'none'}", "", "| Metric | A (off) | B (on) |", "|---|---:|---:|"]
    for key in ("legal_inputs", "invalid_inputs", "CompileInitial", "CompileWithin3Repairs", "mean_repairs_on_success",
                "generation_compile_failures", "infrastructure_failures", "format_errors", "ql_errors", "smoke"):
        values = []
        for group in ("A", "B"):
            value = report["groups"][group][key]
            if isinstance(value, dict):
                value = f"{value['passed']}/{value['total']}"
            values.append(str(value) if value is not None else "n/a")
        lines.append(f"| {key} | {' | '.join(values)} |")
    for key in COST_FIELDS:
        values = []
        for group in ("A", "B"):
            value = report["groups"][group]["costs"][key]
            total = value["total"]
            values.append(f"{total:.3f}" if isinstance(total, float) else str(total) if total is not None
                          else f"unknown ({value['missing_samples']} missing; known {value['known_total']})")
        lines.append(f"| {key} | {' | '.join(values)} |")
    lines.extend(["", "All legal inputs remain in the denominator, including infrastructure failures and unprocessed samples.",
                  "Repair counts include format repairs; format/QL failed attempts and started repairs are also recorded separately.",
                  "Generation wall time excludes smoke; batch wall time includes preflight. Overlapping MCP timings are not additive.",
                  "Unknown usage stays unknown. Zero smoke rows are valid. Semantics: not_evaluated.", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", nargs="+", required=True, help="off batches in repetition order")
    parser.add_argument("--b", nargs="+", required=True, help="on batches in matching repetition order")
    parser.add_argument("--purpose", choices=("pilot", "formal"), required=True)
    parser.add_argument("--overlap-note", required=True)
    parser.add_argument("--note", default="")
    parser.add_argument("--out", required=True, help="new report directory under runs/")
    args = parser.parse_args(argv)
    try:
        output = new_output(ROOT, args.out)
        paths = [ROOT / path for path in (*args.a, *args.b)]
        if any(output.is_relative_to(path.resolve()) for path in paths):
            raise ValueError("report output must be outside input batches")
        report = build_report(paths[:len(args.a)], paths[len(args.a):], purpose=args.purpose,
                              overlap_note=args.overlap_note, note=args.note)
        report["report_script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        output.mkdir(parents=True, exist_ok=False)
        save_json(output / "report.json", report)
        (output / "report.md").write_text(markdown(report), encoding="utf-8")
        print(markdown(report))
        print(f"Report: {output}")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Report failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
