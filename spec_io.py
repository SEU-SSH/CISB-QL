"""Read existing Markdown specs without rewriting their contents."""

import json
from pathlib import Path
import re


DESCRIPTION_FIELDS = ("Source", "Description", "Evidence", "Requirement", "Mitigation")
LIST_FIELDS = ("triggers", "equivalence_notes", "scope_assumptions",
               "control_flow_assumptions", "environment_assumptions")


def json_object(text):
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    result = json.loads(text, object_pairs_hook=unique_fields, parse_constant=reject_constant)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result


def source_id(path):
    name = Path(path).name
    if not name.endswith("_spec.md"):
        raise ValueError("spec filenames must end in _spec.md")
    identifier = name[:-8]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", identifier):
        raise ValueError("invalid source_id in spec filename")
    return identifier


def collect_targets(root, spec=None, samples=None):
    if spec:
        paths = [(root / spec).resolve()]
    else:
        entries = (root / samples).read_text(encoding="utf-8").splitlines()
        paths = []
        for entry in entries:
            entry = entry.strip()
            if not entry or entry.startswith("#"):
                continue
            relative = Path(entry)
            if relative.is_absolute() or not (root / relative).resolve().is_relative_to(root):
                raise ValueError("sample paths must be relative to the project root")
            paths.append((root / relative).resolve())
    if not paths:
        raise ValueError("no input samples")
    ids = [source_id(path) for path in paths]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source_id in sample list")
    return paths


def parse_spec(raw):
    headings = list(re.finditer(r"^##[ \t]+([^\r\n]+)[ \t]*\r?$", raw, re.MULTILINE))
    sections = {}
    for index, match in enumerate(headings):
        name = match.group(1).strip()
        if name in sections:
            raise ValueError(f"duplicate spec section: {name}")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(raw)
        sections[name] = raw[match.end():end]
    for name in ("Vulnerability Description", "Code Pattern"):
        if name not in sections:
            raise ValueError(f"missing spec section: {name}")

    description = sections["Vulnerability Description"]
    fields = list(re.finditer(r"^\*\*([A-Za-z ]+)\*\*[ \t]*\r?$", description, re.MULTILINE))
    values = {}
    for index, match in enumerate(fields):
        name = match.group(1)
        if name in values:
            raise ValueError(f"duplicate description field: {name}")
        end = fields[index + 1].start() if index + 1 < len(fields) else len(description)
        values[name] = re.split(r"^---[ \t]*\r?$", description[match.end():end],
                                maxsplit=1, flags=re.MULTILINE)[0].strip()
    if any(name not in values for name in DESCRIPTION_FIELDS) or not values.get("Description"):
        raise ValueError("all description fields are required; Description must be nonempty")

    fences = re.findall(r"^```json[ \t]*\r?\n(.*?)^```[ \t]*\r?$",
                        sections["Code Pattern"], re.MULTILINE | re.DOTALL)
    if len(fences) != 1:
        raise ValueError("Code Pattern must contain exactly one fenced JSON object")
    pattern = json_object(fences[0])
    for name in ("vulnerable_pattern", "ql_constraints"):
        if not isinstance(pattern.get(name), str) or not pattern[name].strip():
            raise ValueError(f"{name} must be a nonempty string")
    for name in LIST_FIELDS:
        if not isinstance(pattern.get(name), list) or not all(isinstance(x, str) for x in pattern[name]):
            raise ValueError(f"{name} must be an array of strings")
    return {"description_fields": {key.lower(): values[key] for key in DESCRIPTION_FIELDS},
            "pattern_json": pattern}
