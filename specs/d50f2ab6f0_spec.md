# CISB Specification

## Vulnerability Description

**Source**
CVE-2009-4307, Linux kernel commit ext4: fix undefined behavior in ext4_fill_flex_info()

**Description**
Compiler optimization removes a sanity check due to undefined behavior in a shift operation. The code checks the result of a shift rather than the shift amount, allowing the compiler to assume the shift is valid and optimize away the check.

**Evidence**
Source: groups_per_flex = 1 << s_log_groups_per_flex; if (groups_per_flex == 0). Clang 3.0 binary: Check removed. Fix: Validate s_log_groups_per_flex range before shift.

**Requirement**
Optimizing compiler (e.g., Clang 3.0, GCC), C/C++ language semantics where oversized shift is undefined behavior, optimization level enabling UB-based simplifications.

**Mitigation**
Validate shift amount against type bit-width limits before performing the shift operation. Use safe arithmetic libraries or intrinsics.

---

## Code Pattern

```json
{
  "triggers": [
    "Compiler optimization removes check",
    "Undefined behavior in shift operand",
    "Check depends on UB result"
  ],
  "vulnerable_pattern": "Assignment of a shift expression result to a variable, followed by a conditional check on that variable. The shift amount is derived from an unchecked input. The check condition is logically redundant under defined behavior assumptions, leading to optimization removal.",
  "ql_constraints": "ShiftExpr operand flows from external or unchecked source. Conditional statement uses ShiftExpr result downstream in CFG. No dominating check validates ShiftExpr operand range against type bit-width.",
  "equivalence_notes": [
    "Left shift and right shift both invoke UB on oversized operands",
    "Check == 0, != 0, or range check on result are equivalent patterns",
    "Implicit cast of operand before shift"
  ],
  "scope_assumptions": [
    "Within the same function body"
  ],
  "control_flow_assumptions": [
    "Shift expression dominates the conditional check",
    "No intervening validation of shift operand on the path"
  ],
  "environment_assumptions": [
    "Compiler adheres to C/C++ undefined behavior semantics",
    "Optimization level enables UB-based simplifications"
  ]
}
```

---

## Provenance

- Source analysis: /home/suiren/cisb-llm/results/kernel/P/d50f2ab6f0_analysis.md
- Source id: d50f2ab6f0
- Digest: available
- Generated at: 2026-05-01T13:56:27.999301+00:00
- Model: qwen3.5-397b-a17b
