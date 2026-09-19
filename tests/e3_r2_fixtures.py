"""Handwritten regression for query-originated recursion; no archived runs required."""

import json

from codeql_tools import cpp_all_root


RECURSIVE = {
    "qll_code": "import cpp\nclass ShiftOperation extends BinaryOperation {\n"
                "  ShiftOperation() { this.getAQlClass().matches(\"%Shift%\") }\n}\n",
    "ql_code": "import cpp\nimport query\nfrom ShiftOperation s\nselect s, \"shift operation\"\n",
}
REPAIRED = {**RECURSIVE, "qll_code": RECURSIVE["qll_code"].replace(
    'this.getAQlClass().matches("%Shift%")', 'this instanceof LShiftExpr or this instanceof RShiftExpr')}


def recursion_result(query, symbol="ShiftOperation"):
    local = f"{symbol} --> characteristic predicate of {symbol} --> dispatch predicate Expr.getAQlClass -!->(via dispatch) {symbol}"
    external = (f"dispatch predicate Expr.getAQlClass -!->(via dispatch) query::{symbol} "
                f"--> characteristic predicate of query::{symbol} --> dispatch predicate Expr.getAQlClass")
    messages = [{"severity": "ERROR", "message": "Non-monotonic recursion: " + cycle,
                 "position": {"fileName": str(path), "line": 3, "column": 22}}
                for path, cycle in ((query.with_suffix(".qll"), local),
                                    (cpp_all_root() / "semmle/code/cpp/exprs/Expr.qll", external))]
    return {"returncode": 2, "stdout": json.dumps([{"messages": messages}]), "stderr": "",
            "timed_out": False, "seconds": 0.01}
