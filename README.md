# QLCoder C/C++ Lite

## Current Recommendation: MCP-Off Baseline

Decision recorded on September 20, 2026 (Asia/Shanghai), based on the completed
September 17 E3-r2 experiment: **use the current harness with explicit `--mcp off`
as the research baseline and routine generation path**. Keep `--mcp on` as an
experimental variant to improve, not a required step or an established enhancement.
Here "variant" is a research/workflow distinction; no Git branch was created.

The CLI parser still defaults to `on`. This documentation-only update does not
change that behavior: **always pass `--mcp off` for baseline use**. Off mode starts
no MCP/LSP session and exposes no model tools, but retains Responses generation,
format/QL repair, CLI adjudication, error classification, receipts and optional
smoke checks. The existing Python environment still includes the `mcp` package;
off mode does not require starting or rebuilding the MCP server.

With API environment variables already configured, choose a new output name:

```fish
cd ~/qlcoder-cpp-lite
source env.sh
# Paid generation, one serial batch of the five existing specs.
.venv/bin/python run.py --samples samples-5.txt --mcp off --out runs/baseline-off-20260920
```

No new probe, A/B repetition or model request was run for this documentation
decision. See [Recommended CLI-Only Baseline](#recommended-cli-only-baseline)
for single-spec use and the existing optional smoke/independent-recheck workflow.

### Latest Evidence: September 17, 2026

Exactly one pair, five unchanged specs per group, A off then B on:

| Metric | A (off) | B (on) |
|---|---:|---:|
| Initial compilation success | 4/5 | 2/5 |
| Success within three repairs | 5/5 | 5/5 |
| Mean repairs on success | 0.6 | 1.4 |
| QL-failed candidates | 3 | 7 |
| Generation/compilation time | 750.035 s | 1600.290 s |
| Model-requested MCP calls | 0 | 56 |
| Tokens, four pairs with complete usage | 189690 | 1097367 |

Across all five specs, B took 2.13 times A's generation/compilation time. For the
**four complete-usage pairs**, B used 5.79 times A's tokens. A/7185ad2672 had a
connection failure with missing usage, so the exact
whole-batch token ratio is unknown. There was no final compilation gain in this
run. This small, reused set and one serial pair do not establish statistical
significance, universal MCP harm, or an improvement over the old Querier.

MCP did supply adopted API evidence (`PointerDereferenceExpr`, `isRValue`), but
49 searches included 11 empty results, eight from declaration-phrase queries
that do not match the identifier-only search. Other observed limitations were
mixed AST/IR APIs, repair lookups away from compiler errors, and expensive
retained context. P0's mixed-location recursion rule was not exercised in this
live run. Semantic accuracy remains untested; the final shift query approximates
required CFG dominance with source-line ordering. Compilation is not spec fidelity.

The decision, baseline capabilities, evidence boundaries and deferred MCP work
are recorded in [the status note](revisions/20260920-baseline-and-mcp-status.md).
Raw results and detailed analysis remain in
[the unchanged experiment report](runs/E3-r2-5spec-20260917T085049.069920Z/report/analysis.md).
The historical implementation and experiment sections below are retained, not
instructions to rerun or promote MCP-on for routine use.

Research prototype following `syntax.md`. E0 tools and the user-run Responses
protocol probe have passed. E1 provides the CLI-only generation/repair loop;
E2 adds read-only MCP/LSP assistance to that same loop. The user's real-model
`e2-debug-1` / `e2-debug-1-no-mcp` pair is preserved as pilot evidence. E3 adds
compiled-query smoke checks and paired A/B aggregation. The frozen three-repeat
A/B compilation experiment is complete: both groups compiled all nine sample
runs within three repairs. Semantic accuracy is outside this phase. The separate
September 12 five-spec experiment is also complete; its B failure exposed the
error-classification limitation documented below.

## E3-r2: Local Implementation Acceptance

E3-r2 fixes query-originated mixed-location recursion classification and adds
targeted API lookup. It does not change the five specs, config, Responses backend,
CodeQL 2.24.3, or cpp-all 7.0.0. The implementation acceptance itself used no paid
probe or A/B. The separately authorized September 17 experiment is summarized
above; its results do not rewrite the local acceptance record in `revisions/E3-r2.md`.

- `codeql_search_api(query, offset=0)` is a real MCP server tool. It reads only
  `.qll` files in the pinned library root supplied by the harness, without following
  symlinks. No model-supplied directory or version is accepted. Literal queries
  have at most 128 characters; empty search is invalid. No match is an empty
  success, while an unavailable pack is a tool failure.
- Search returns ten source locations per page, with 1-based line numbers,
  version, up to twenty lines / 2,000 characters per excerpt, and truncation flags.
  Declaration hints precede identifier occurrences; within each group, exact,
  case-insensitive and substring matches are ordered by path and line. This is
  lexical evidence, not a QL parser, type resolver, or curated public API index.
  Broad terms can include experimental/internal APIs and require another page
  or a narrower query. Nothing is hardcoded to the five sample identifiers.
- `codeql_complete(file, line, character, query="", offset=0)` filters the full
  LSP-returned collection before paging twenty items. It reports filtered total,
  unfiltered total and `isIncomplete`. Model results omit editor fields but keep
  full documentation, including debugging-only and deprecated-use warnings.
- Identical completion pages within a generation attempt refer to the earlier
  call. Search, pagination, duplicates and invalid arguments share the existing
  six-call budget. Native MCP responses and model-facing results are both saved.
- Mixed-location non-monotonic recursion is repairable only when every external
  error belongs to the pinned pack and explicitly names a query-defined symbol
  also present in a local recursion cycle. Other infrastructure guards remain.
  The original diagnostic text is not removed from repair feedback.

Current revision metadata hashes all deployed MCP JavaScript, including
`api-tools.js`, and records tool schemas, bounds and the fixed search root.
`e3-r2-freeze.sha256` is a separate implementation manifest, not a new experimental
baseline. Old freeze files, archives and results remain unchanged; old manifests
are expected not to match revised source. Future paid A/B needs a new disclosed
freeze and cannot mix old/new implementation hashes. The September 20 update
changes only this README and `syntax.md` from that 52-file manifest and adds a
decision note. The old manifest is intentionally not regenerated: verify its
documentation against the archived `source.tar.gz`, not the edited live files.

```bash
cd "$HOME/codeql-lsp-mcp"
npm run build
node --test --test-isolation=none --test-reporter=tap tests/*.test.mjs
cd "$HOME/qlcoder-cpp-lite"
env -u RUN_E0_TESTS -u RUN_E1_TESTS -u RUN_E2_TESTS -u RUN_E3_TESTS -u RUN_E3_R2_TESTS .venv/bin/python -m unittest discover -s tests -v
env RUN_E0_TESTS=1 RUN_E1_TESTS=1 RUN_E2_TESTS=1 RUN_E3_TESTS=1 RUN_E3_R2_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

All model responses in these tests are mocked. The new local cases use
handwritten recursion/repair fixtures, not model-generated research observations.
No full-history pruning, LSP reuse, draft edits or new dependencies are introduced.

## Historical Five-Spec Experiment: September 12, 2026

On September 12, 2026, the user requested five specs and exactly one A/B pair:
five samples with MCP off, then the same five with MCP on. This is a separate
experiment following the API key/base URL change, not a replacement repetition
or an extension of the earlier pooled report. The latest user-run E0 receipt,
`runs/e0/model-responses-20260912T124335.049170Z/model-probe.json`, passed all
three Responses checks. No extra paid probe or debugging generation is planned.

`samples-5.txt` preserves the old three-spec order and appends two byte-identical
copies from `/home/suiren/cisb-llm/specs/`, selected before any new generation:

| Spec | Pattern in the source spec | Selection |
|---|---|---|
| `12051b318b` | Modification of const-qualified storage | Existing |
| `02828845dd` | Inline assembly and deleted null checks | Existing |
| `9c14791748` | Stale shared-variable reads | Existing |
| `7185ad2672` | Dead-store elimination of memory clearing | New pattern coverage |
| `d50f2ab6f0` | Undefined shifts and ineffective result checks | New pattern coverage |

Original spec wording and provenance are retained, including any limitations in
their semantic assumptions. The first three overlap prior debugging/formal runs;
the two additions come from the same upstream project, not a certified held-out
dataset. There is no selection based on newly generated query outcomes.

The model (`deepseek-flash`), Responses backend, prompt, CodeQL/MCP deployment,
384000-token cap, 300-second timeout, and initial-plus-three-repair budget remain
unchanged. Only B exposes the existing read-only MCP tools, at most six admitted
calls per candidate. Both batches inherit the current exported API credentials;
no API keys are written into these instructions. This run measures compilation
only: no inline smoke or semantic evaluation. Keep all failures; do not rerun
failed batches or add repetitions. Existing connection retry policy is unchanged.

The old `samples.txt`, `e3-freeze.sha256`, archive and six historical batches are
retained. The new control manifest is `e3-5spec-freeze-20260912.sha256`; its source
archive is `runs/e3-5spec-freeze-20260912.tar.gz`. Check the manifest before and
after running this exact sequence, using new output directories:

```fish
sha256sum -c e3-5spec-freeze-20260912.sha256
.venv/bin/python run.py --samples samples-5.txt --mcp off --out runs/E3-5spec-A-rep1-20260912
.venv/bin/python run.py --samples samples-5.txt --mcp on --out runs/E3-5spec-B-rep1-20260912
.venv/bin/python report.py \
  --a runs/E3-5spec-A-rep1-20260912 --b runs/E3-5spec-B-rep1-20260912 \
  --purpose formal \
  --overlap-note "Three specs overlap earlier debugging/formal runs; two additions are unchanged upstream specs, not a certified held-out set" \
  --note "User requested exactly one A/B pair with five specs after an API key/base URL change; no repetitions or replacement runs; compilation only; exploratory evidence" \
  --out runs/E3-5spec-report-20260912
sha256sum -c e3-5spec-freeze-20260912.sha256
```

### Single-Round Results

The two batches ran once, serially in the declared A-then-B order. A exited 0;
B exited 1. All five samples were processed in each group. No additional paid
probe, debugging generation, replacement batch, or repetition was run. The
paired report is `runs/E3-5spec-report-20260912/report.md`, with the complete
records in `report.json`. Its note additionally discloses the classification
issue discovered during this run; neither original batch was rewritten.

| Metric | A (MCP off) | B (MCP on) |
|---|---:|---:|
| Distinct legal specs / repetitions | 5 / 1 | 5 / 1 |
| CompileInitial | 1/5 | 3/5 |
| CompileWithin3Repairs | 5/5 | 4/5 |
| Mean repairs on successful samples only | 1.80 | 0.25 |
| Format-failed candidates | 1 | 0 |
| Recorded QL-failed candidates | 8 | 2 |
| Recorded infrastructure failures | 0 | 1 |
| Generation minutes, excluding batch preflight | 18.05 | 24.12 |
| Model requests | 14 | 14 |
| Candidate CLI compiles | 13 | 7 |
| Admitted model tool calls | 0 | 19 |
| MCP CodeQL RPCs / MCP failures | 0 / 0 | 79 / 0 |
| Total tokens | 503,902 | 696,462 |

Successful attempt indexes below use 0 for the initial candidate and 1-3 for
repairs. An early stop is not a completed four-candidate search.

| Spec | A successful attempt | B successful attempt | B model calls / tool calls |
|---|---:|---|---:|
| `12051b318b` | 3 | 0 | 4 / 6 |
| `02828845dd` | 3 | 0 | 1 / 0 |
| `9c14791748` | 1 | 1 | 4 / 6 |
| `7185ad2672` | 2 | 0 | 1 / 0 |
| `d50f2ab6f0` | 0 | Stopped after attempt 1 | 4 / 7 |

**Historical classification limitation.** B's shift spec did not encounter an API timeout,
MCP failure, or missing pack. Its second candidate declares `ShiftOperation` and
uses `getAQlClass()` / `getAPrimaryQlClass()` in the class characteristic predicate
(`runs/E3-5spec-B-rep1-20260912/d50f2ab6f0/attempt_1/query.qll`, lines 10-15).
CodeQL rejected this with nine non-monotonic-recursion diagnostics: two located
in the generated file and seven in the standard library, in the same dependency
cycle. The frozen `classify_compile` conservatively treated any error location outside the
generated pair as infrastructure failure, so it stopped after two candidates
with two repairs unused. An offline, in-memory check reproduced that decision;
retaining only the two local diagnostics changed the verdict to `query_error`.
No saved diagnostics were edited and no additional CLI/model run was needed.

The table preserves the frozen classifier's operational counts. B has two
recorded QL-failed candidates **plus this query-recursion failure recorded as
infrastructure**; 8 versus 2 is not a complete count of underlying query errors.
The 4/5 result describes the executed harness, not exhaustion of B's full repair
budget, and cannot establish what would have happened after fixing classification.
The 0.25 repair mean uses only B's four successes, excluding this stopped sample;
it should not be treated as an unconditional advantage over A's five successes.
The classifier was deliberately not changed after the experiment began. The E3-r2
fix has focused mixed-location recursion regression tests; future A/B needs a separately
declared experiment, not retroactive replacement of this observation.

B produced more first-candidate successes, but not all involved model-selected
tools: the inline-assembly and clearing specs passed without any such calls.
Conversely, the shift spec failed despite seven admitted calls across its two
candidates, while A passed initially. B used 1.34 times the generation time and
1.38 times the tokens. These are descriptive observations from five pairs, not
proof of a general MCP benefit or of an effect caused by the new API service.
No semantic accuracy or smoke result is claimed.

Audit: all 28 native model responses completed, reported `deepseek-flash`, and
had consistent input/output/total usage matching the group totals. All 12 MCP
sessions recorded closure. All 20 new frozen hashes matched before A, before B,
and after B; the archive contents also matched. The API base URL fingerprint was
unchanged at those three checks (SHA-256:
`a34e2a4708ed1c61008a151688838dcf1c44d4e7f08054633e72ba7c0b16cfc1`), without
recording the URL or key here. The six old batch metadata files and their pooled
report retained their pre-run hashes. Offline validation passed 83 tests, with
11 opt-in integrations skipped; the existing parser test covered all five specs.
The two real batch preflights checked the local CLI/template, and B exercised
the actual MCP/LSP deployment.

## E3: Smoke and A/B Experiments

No dependency deployment, model configuration, prompt or candidate-budget change
is needed. `run.py --smoke` checks both fixed databases with the handwritten
template **before opening the model**. Each database must yield `main`; the
generated query may legitimately yield zero rows.

```fish
cd ~/qlcoder-cpp-lite
source env.sh
# Paid generation, then local smoke only for compiled candidates.
.venv/bin/python run.py --samples samples.txt --mcp off --smoke --out runs/A-smoke-rep1
```

Smoke runs after the repair loop and MCP cleanup. A timeout, runtime error or
decode failure does not consume another candidate or invoke a model. The sample
keeps `compile_status: passed` and its compile metrics; `smoke_status` separately
records the failure. Batch exit status is nonzero if either requested gate fails.
Each successful candidate has `attempt_N/smoke/{c,cpp}/result.bqrs`, `rows.json`
and `smoke/smoke.json`, retaining commands, stdout/stderr, row counts and timing.
`generation_seconds` excludes smoke; `smoke_seconds` is separate; `seconds`
includes both. Preflight time belongs to the batch, not sample generation cost.

### Check Saved Candidates Without a Model

`--smoke-from` takes a finished generation **batch**, selects its compiled
candidates, independently recompiles each, and executes both fixed databases.
It requires neither `QL_API_KEY` nor `QL_BASE_URL`, and never opens MCP or a model.

```fish
.venv/bin/python run.py --smoke-from runs/e2-debug-1-no-mcp --out runs/e3-smoke-A
.venv/bin/python run.py --smoke-from runs/e2-debug-1 --out runs/e3-smoke-B
```

Both output names must be new and outside the source batch. Results go to
`<new-out>/<source_id>/smoke/`, not the historical candidate. The receipt links
the original batch/candidate and hashes all four pack files. A temporary pack
is removed after execution, keeping CLI caches out of the archive; no additional
persistent accepted/compiled copy is introduced. Original logs and queries are
not rewritten. CodeQL can still update evaluation caches/logs in the fixed DBs.
Saved smoke is an auxiliary receipt, not a new model repetition and not an input
to `report.py`. Inline smoke is the option for a unified A/B smoke metric.

### Paired Reporting

Pass explicit A and B directories in matching repetition order; there is no
automatic scan of `runs/` and no selection of the best repetition:

```fish
.venv/bin/python report.py --a runs/e2-debug-1-no-mcp --b runs/e2-debug-1 \
  --purpose pilot --overlap-note "Same debugging spec in both groups; not held out" \
  --out runs/e3-pilot-report
```

The new directory contains a readable `report.md` and a complete `report.json`:

- Pairs by repetition and `source_id`, reporting per-repetition and pooled results.
  Repeated observations do not become additional distinct specs.
- Checks identical spec hashes/sets, config, prompt, generation code, API format,
  toolchain, pack and common model policy. Only expected MCP/tool differences are
  allowed. B repetitions must use the same MCP deployment and schemas.
- Rejects unfinished batches, duplicate directories, wrong group labels, changed
  spec snapshots, missing CLI evidence for claimed success, and marked mock runs.
  Historical E2 pairs cannot be mixed with E3 generation code in one comparison.
- Counts all declared legal inputs, including infrastructure failures and samples
  not reached after preflight/cancellation. Invalid inputs are separate. Partial
  sample summaries absent from a cancelled batch index are read without rewriting.
- Separates format-failed and QL-failed attempts, and repairs started after each
  error type. Successful mean repair count still includes both kinds.
- Reports model/CLI/MCP calls, tokens and timing, including MCP startup time.
  Missing usage/timing remains unknown; known subtotals and missing counts are
  retained. Overlapping MCP timing fields must not be added together.
- Optional inline smoke uses all compiled candidates as its denominator; both
  DBs must pass. A smoke failure never removes the compile success.

`--purpose` and `--overlap-note` are mandatory. A formal report with fewer than
three repetitions additionally requires `--note` explaining the reduced budget.
These declarations do not certify a held-out dataset or a stable improvement.
Preserve every attempted repetition, including failures; the reporter cannot
discover batches the operator omits.

### Historical Formal Run Procedure

The six `E3-A-rep1..3` / `E3-B-rep1..3` batches below have now been completed.
Their results are in `runs/E3-formal-report/`; the commands document the procedure
and will not overwrite existing outputs. Any new experiment needs new batch names
and a disclosed freeze, not replacement of these observations.

The current `samples.txt` contains three existing debug specs: `12051b318b`,
`02828845dd`, and `9c14791748`. Reusing this small set is permitted for a limited
research experiment, but **all three are debug/evaluation overlap, not a held-out
benchmark**. Do not silently replace specs based on the E2 outcomes.

Freeze the samples, spec snapshots, `prompt.md`, `config.json`, Python code,
requirements and fixed pack before the first formal call. Each batch records
hashes and actual inputs; the reporter rejects cross-run drift. Keep these files
and MCP unchanged through all repetitions. E2 debug runs remain pilot records,
not A-rep1/B-rep1 of the new E3 revision.

The September 11, 2026 implementation is frozen in `e3-freeze.sha256` (17 files)
and `runs/e3-freeze-20260911.tar.gz`. Check before the first run and after the last:

```fish
sha256sum -c e3-freeze.sha256
```

This is a plain checksum list and source archive, not another configuration
profile. The three specs are unchanged copies; choosing a different evaluation
set requires a new disclosed freeze. CLI version/pack checks and MCP deployment
hashes remain recorded by the existing batch preflight/metadata.

The following fish commands are **paid real-model experiments**, not local tests.
Default: three repetitions per group, three specs per repetition (18 sample runs,
not 18 distinct specs). Each sample allows initial generation plus three repairs.
Order alternates to avoid always running the same group first. There is no seed
control, cache flushing, automatic batch retry or best-of filter.

```fish
for rep in 1 2 3
    if test $rep -eq 2
        .venv/bin/python run.py --samples samples.txt --mcp on --out runs/E3-B-rep$rep
        .venv/bin/python run.py --samples samples.txt --mcp off --out runs/E3-A-rep$rep
    else
        .venv/bin/python run.py --samples samples.txt --mcp off --out runs/E3-A-rep$rep
        .venv/bin/python run.py --samples samples.txt --mcp on --out runs/E3-B-rep$rep
    end
end
.venv/bin/python report.py \
  --a runs/E3-A-rep1 runs/E3-A-rep2 runs/E3-A-rep3 \
  --b runs/E3-B-rep1 runs/E3-B-rep2 runs/E3-B-rep3 \
  --purpose formal --overlap-note "All three specs were also debugging inputs; no held-out evaluation" \
  --out runs/E3-formal-report
```

Default formal runs measure compilation only. Add `--smoke` to **every** A and B
command for a unified smoke experiment, or use `--smoke-from` later without
regenerating. Changed config/prompt/code means a new experiment, not replacement
of failed observations in an existing comparison.

### E3 Verification

Offline unit checks and opt-in real local tool checks never call a paid model:

```fish
env -u RUN_E0_TESTS -u RUN_E1_TESTS -u RUN_E2_TESTS -u RUN_E3_TESTS .venv/bin/python -m unittest discover -s tests -v
env RUN_E0_TESTS=1 RUN_E1_TESTS=1 RUN_E2_TESTS=1 RUN_E3_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

E3 integrations use real CodeQL/MCP and both fixed databases with an offline
model: off mode generates a zero-match query; on mode generates the main-finding
template. These validate execution, not model quality. Their timestamped
`runs/e3-local-*` receipts are marked `validation_only`, not research results.

On September 11, 2026, the complete E0/E1/E2/E3 suite passed **94 tests in
141.741 seconds**, with all integration flags enabled. Offline-only validation
passed 83 tests with 11 integrations skipped. E3 end-to-end receipts:

- `runs/e3-local-off-20260911T140110.680877Z`: zero generated-query rows on both DBs.
- `runs/e3-local-on-20260911T140139.057736Z`: one generated-query row on each DB.
- Both preflights found exactly one `main` row in each database.

The user's real E2 candidates were separately recompiled and smoke-tested with
both API credential variables explicitly unset:

- `runs/e3-smoke-A-20260911`: independent compilation and both executions passed.
- `runs/e3-smoke-B-20260911`: independent compilation and both executions passed.
- Both queries produced zero rows on both fixtures, which is valid smoke, not
  evidence of successful CISB detection. No model calls were made.
- Full file-tree hashes of `runs/e2-debug-1`, `runs/e2-debug-1-no-mcp`, `specs/`
  and `codeql-pack/` matched before and after these auxiliary checks.

`runs/e3-pilot-report-20260911/report.md` reports the existing real-model pair:

| Metric | A (off) | B (on) |
|---|---:|---:|
| CompileInitial | 0/1 | 0/1 |
| CompileWithin3Repairs | 1/1 | 1/1 |
| Format-failed / QL-failed attempts | 0 / 1 | 1 / 0 |
| Generation seconds | 107.328 | 197.529 |
| Total tokens | 48,974 | 84,295 |

This is one debugging pair under the historical E2 code, not an E3 formal
repetition. It was not included in the formal results below.

### Formal Results

The six real-model batches ran serially in the planned order
`A1, B1, B2, A2, A3, B3`, with three specs per batch. All six batches exited
successfully. No failed candidates were replaced, no extra batches were run,
and no model/config/prompt/code/spec changes were made. Smoke was not requested
for these batches; the earlier auxiliary checks remain separate.

The complete paired report is `runs/E3-formal-report/report.md`, with per-sample,
per-repetition, control and provenance details in `report.json`.

| Metric | A (MCP off) | B (MCP on) |
|---|---:|---:|
| CompileInitial | 2/9 | 3/9 |
| CompileWithin3Repairs | 9/9 | 9/9 |
| Mean repairs on success | 1.11 | 1.11 |
| Format-failed candidate attempts | 0 | 5 |
| QL-failed candidate attempts | 10 | 5 |
| Infrastructure failures | 0 | 0 |
| Model API requests | 19 | 42 |
| Candidate CLI compiles | 19 | 14 |
| Admitted model tool calls | 0 | 57 |
| Generation wall time, total minutes | 23.89 | 50.82 |
| Input tokens | 265,514 | 1,185,883 |
| Output tokens | 314,309 | 613,472 |
| Total tokens | 579,823 | 1,799,355 |

Generation wall time excludes batch preflight; it includes model, CLI and MCP
work. B used **2.13 times** the generation wall time and **3.10 times** the tokens.
These are usage measurements, not a monetary bill. B's model request time was
2,775.846 seconds versus A's 1,314.897 seconds; its MCP startup time was 154.079
seconds. Startup overlaps RPC timing and must not be added to it.

| Repetition | A initial | B initial | A within 3 repairs | B within 3 repairs |
|---|---:|---:|---:|---:|
| 1 | 1/3 | 1/3 | 3/3 | 3/3 |
| 2 | 0/3 | 1/3 | 3/3 | 3/3 |
| 3 | 1/3 | 1/3 | 3/3 | 3/3 |

The observed B runs had fewer QL-failed attempts, but five additional format
failures offset that reduction in the total repair count. One format failure
was an eight-call tool batch rejected by the fixed six-call budget. Tool service
failures were zero. A first-candidate success can still include several model
requests in B: B3's inline-assembly query passed initially after four model
requests and five tool calls. `CompileInitial` is not a one-model-call metric.

Thus the observed advantage is one additional initial success, with no gain in
final compilation success or mean repair count and with higher measured cost.
This describes this harness/model/sample set, not a general result about MCP.
There are only **three distinct specs**, all reused from debugging; nine runs per
group are repeated observations, not nine held-out specs. No semantic detection
accuracy or runtime behavior was evaluated by this formal compilation experiment.

Final audit: all 61 recorded model responses reported `deepseek-flash` and
`completed`, with complete, internally consistent input/output/total usage.
All 23 recorded MCP sessions closed. The paired reporter accepted the frozen
controls and saved CLI evidence for all 18 compiled candidates, and all 17
entries in `e3-freeze.sha256` matched after the experiment. Only documentation
was updated after reporting; the freeze archive and original runs are unchanged.

## Experimental MCP-On Use

Use this retained path only for a deliberately scoped MCP experiment, not routine
generation. In the fish terminal with `QL_API_KEY` and `QL_BASE_URL` exported:

```fish
cd ~/qlcoder-cpp-lite
source env.sh
.venv/bin/python run.py --spec specs/12051b318b_spec.md --mcp on --out runs/e2-debug-1
```

This is a paid model run, not the small E0 probe. The output directory must be
new. `--mcp on` remains the parser default, but is not the recommended usage mode.
For the current CLI-only baseline, explicitly use `--mcp off` and a new directory.
Both modes share the same `prompt.md`, model, config and four-candidate
budget. `--samples samples.txt` uses the same serial workflow. No reinstall or
new config fields are needed for the E0 deployment on this host.

### Tool-Assisted Loop

- Before initial generation, open an independent copy of the fixed template in
  MCP/LSP. After a candidate is submitted, close the old session and open a new
  session for that candidate's complete `.ql` and `.qll` files. Each candidate
  remains unchanged. Format failures reuse the current read-only context.
- The model may call `codeql_hover`, `codeql_definition`, `codeql_complete`, and
  (since E3-r2) position-independent `codeql_search_api` as described above.
  Wrapper arguments are `file` (`query.ql` or `query.qll`), `line`, and `character`.
  The harness resolves the file URI and checks 0-based/UTF-16 source positions.
  Completion uses a fixed 20-item page; E3-r2 adds optional `query` and `offset`.
  Definition may include up to five bounded excerpts from the current pair or
  the pinned `codeql/cpp-all/7.0.0` cache, never arbitrary files or other versions.
- Workspace/open/diagnostics operations belong to the harness, not the model.
  The model has no file writes, formatting, update-file, shell, install or
  unrestricted file-reading tool. MCP subprocesses do not receive model keys.
- LSP diagnostics and related tool results accompany the existing spec, previous
  pair and CLI feedback. An LSP query error still goes to CLI `--check-only`;
  CLI remains the compilation judge. MCP service failure or timeout stops the
  sample as infrastructure failure, without retrying MCP or falling back to off.
- Responses tool rounds replay complete output items and append results with
  the matching `call_id`. Each generation attempt admits at most six model tool
  calls. Invalid arguments consume that budget and return an error without
  touching MCP. Oversized batches or invalid/duplicate call IDs end the attempt
  as a format failure. After exhaustion, `tool_choice: none` requests final JSON;
  another tool call is rejected, not executed.

With the current budget, on mode allows at most seven normal model requests per
attempt, or fourteen API attempts including one connection retry per request.
Across four candidates that is at most 28 normal requests / 56 API attempts,
versus 4 / 8 in off mode. These are ceilings, not mandatory calls. Timeout and
HTTP errors are not retried. Both modes still compile at most four candidates,
plus one batch preflight template. Model and output-token limits are unchanged.

### E2 Logs and Verification

Each sample adds `mcp/template/` and `mcp/attempt_N/`, containing `session.json`
and `stderr.txt`. Session records include initialization/tool discovery, exact
workspace/file URIs, raw results, diagnostics, timing, errors and close status.
`attempt_N/diagnostics.json` includes the model-selected tool calls/results and
the candidate's LSP diagnostics; `model.json` retains every API request/response.
Batch metadata records the on/off mode, exposed schemas, budgets and hashes of
the MCP entrypoint, LSP adapter and npm lockfile.

`model_tool_calls` counts admitted model calls, including invalid arguments;
rejected over-budget batches are retained in diagnostics but not executed.
`mcp_calls` counts actual CodeQL tool RPCs, including harness setup/diagnostics,
but excludes initialize/list_tools. `mcp_failures` counts caught MCP failures,
including cleanup failures. `mcp_seconds` sums RPC time; `mcp_startup_seconds`
measures startup through initial diagnostics. These overlap and must not be
added together. Per-session lifetime also includes time spent waiting for the
model and CLI. Use sample `seconds` for end-to-end wall time.

The combined E0/E1/E2 suite passed all 71 tests in 83.155 seconds. The E2
end-to-end receipt is `runs/e2-local-20260911T120257.590269Z/`: a format error,
template hover, a real QLL type error, definition/completion on the failed
candidate, then a clean new candidate that compiled at attempt 2. It used five
mock model requests, three model-selected tools, eighteen CodeQL MCP tool RPCs,
three sessions and two candidate compiles. All tested MCP/LSP PIDs exited,
including in timeout/cancellation checks. The off-mode regression receipt is
`runs/e1-responses-local-20260911T120235.524742Z/`; E0 receipts are in
`runs/e0/20260911T120159.184912Z/`.

Local checks, without paid model calls:

```fish
env -u RUN_E0_TESTS -u RUN_E1_TESTS -u RUN_E2_TESTS -u RUN_E3_TESTS .venv/bin/python -m unittest discover -s tests -v
env RUN_E2_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_e2_local_integration.py -v
env RUN_E0_TESTS=1 RUN_E1_TESTS=1 RUN_E2_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

`runs/e2-local-*` are validation-only runs using mock model responses and must
not enter research result tables. The user's later real E2 pair is documented in
the E3 pilot section; it is not part of these mock tests. Neither local mock
integration nor one debugging pair establishes a stable E2 model success rate.

## Responses API

As of September 11, 2026, the generation harness and the opt-in E0 model probe use
`client.responses.create`. There is no Chat Completions fallback or API selector.
The existing SDK, model, prompt, config values, repair budget and timeout/retry
policy are unchanged. `QL_BASE_URL` remains an API base URL, not the full
`/responses` endpoint; a gateway must support Responses at that base path.

Requests use `instructions`, `input`, and `max_output_tokens`. DeepSeek's Responses
API is stateless: the client supplies the required context on every request, with
no `previous_response_id`, `conversation`, or `store`. E1 still sends the full
spec/template and previous candidate/diagnostics on each repair. The E0 tool probe
uses flat function-tool definitions, replays all response output items (including
reasoning), and supplies `function_call_output` using the function's `call_id`.

Final JSON comes only from completed assistant `output_text` blocks, never from
reasoning or tool-call items. `incomplete` output, including `max_output_tokens`
truncation, consumes the existing format-repair budget without compiling partial
code. Failed responses and API/protocol errors stop as infrastructure failures.
Prompt-only JSON validation remains unchanged; no new JSON mode is enabled.

New experiment, sample and model logs record `api_format: responses`. Aggregate
usage now uses `input_tokens`, `output_tokens`, and `total_tokens`; raw usage
retains reasoning/cache details. Reasoning tokens are already part of output
tokens and are not added again. Old receipts are not rewritten. Keep the API
format fixed within an A/B comparison rather than mixing historical Chat results
with new Responses results.

Protocol reference: <https://api-docs.deepseek.com/zh-cn/guides/responses_api>.

## Recommended CLI-Only Baseline

Use the current enhanced harness, not the old `agents/querier.py`. If the API
configuration is unchanged and a compatible probe has already passed, no repeat
probe is needed. For a new setup or API change, use the separately scoped
Responses probe below. In the fish terminal with the API variables exported:

```fish
cd ~/qlcoder-cpp-lite
source env.sh
.venv/bin/python run.py --spec specs/12051b318b_spec.md --mcp off --out runs/baseline-off-single-20260920
```

The output directory must not exist yet; existing results are never overwritten.

This command uses the configured real model and can incur API charges. Each
sample allows attempt 0 plus at most three repairs, including malformed JSON.
Only connection failures get one transport retry; timeouts and HTTP errors
(including authentication errors) stop that sample. SDK retries are disabled.
Thus off mode makes at most eight API attempts per sample with the default budget,
and at most four candidate compiles, plus one template compile per batch.

For the five retained inputs, using a different, new output directory:

```fish
.venv/bin/python run.py --samples samples-5.txt --mcp off --out runs/baseline-off-batch-20260920
```

`samples-5.txt` adds `7185ad2672` (memory clearing) and `d50f2ab6f0` (shifts) to
the original const-write, inline-assembly and shared-read specs. `samples.txt`
retains the original three-spec historical set. All five have now appeared in
earlier runs and are not held-out. No import, runtime path or query from the old
project is needed for this baseline.

Pass `--mcp off` explicitly; omitting it still enables MCP and is not a baseline
run. The baseline includes later shared fixes, including E3-r2 CLI error
classification, rather than reverting to the old E1 code. E3 supports `--smoke`,
using the preserved E0 databases; see the E3 section above.
There are no new Python dependencies, model overrides, resume/skip modes, or
automatic dependency installs.

### Thinking Mode and Sampling Parameters

`deepseek-flash` is DeepSeek-V4.1-Flash. It runs in thinking mode by default at
reasoning effort `high`, and thinking mode is deliberately left enabled for
every E1-E3 run. Thinking tokens count against `max_output_tokens` (previously
sent as Chat Completions `max_tokens`): one measured fresh Chat generation
consumed 19,361 completion tokens, of which 18,812 were reasoning,
leaving about 549 for the answer.

**While thinking mode is on, the provider silently ignores `temperature`,
`presence_penalty`, and `frequency_penalty`, and clamps `top_p` to a minimum of
0.95.** The `temperature: 0.2` in `config.json` therefore has no effect on the
samples actually drawn, and neither does the value copied into each
`experiment.json`. The effective sampling parameters are the provider's
defaults. This is a property of the model, not of this harness, so it applies to
every recorded run; the temperature field is retained in the config only because
`syntax.md` fixes it there. Do not read it as a controlled variable, and do not
claim a fixed temperature in any A/B comparison until thinking mode is
accounted for.

`max_output_tokens` is set to the documented maximum output length of 384K.
That is a ceiling, not a demand. Because reasoning dominates the budget and the
observed throughput is roughly 230-250 tokens/second, `timeout_seconds` is the wall
that actually binds a runaway generation; it is set to 300 so that a repair
attempt carrying a full previous candidate plus diagnostics is not killed as an
unrepairable infrastructure failure. Sources:
<https://api-docs.deepseek.com/zh-cn/quick_start/pricing> and
<https://api-docs.deepseek.com/zh-cn/guides/thinking_mode>.

### E1 Results and Boundaries

`runs/<batch>/experiment.json` records the configuration, input/code/pack hashes,
prompt, versions, preflight output, and sample summaries. Each sample keeps its
original `spec.md`, immutable `attempt_0` through `attempt_3`, and `summary.json`.
Format errors retain the raw model response but create no QL candidate files.
Successful candidates contain the complete fixed pack and dependency lock.

Sample status is `compiled`, `input_error`, `generation_compile_failure`, or
`infrastructure_failure`. Only clear local QL diagnostics trigger code repair;
unknown compiler failures stop for inspection. Raw requests/responses, usage,
compiler stdout/stderr, error positions, and timings are retained per attempt.
`model_calls` counts API attempts including transport retries; aggregate tokens
are `null` if any attempt lacks usage. In off mode, MCP is disabled and its call count is 0.
Every result marks semantic validity as `not_evaluated`.

A batch exits 0 only when every requested sample compiles, otherwise nonzero.
Invalid samples and per-sample failures are logged and the serial batch continues.
Once a batch is created, tool preflight failures are recorded in
`experiment.json` before any model request. Earlier configuration/input-list
validation errors are reported in the terminal without creating a batch.
Output directories must be new and under this project's `runs/`;
existing results are never overwritten. The exact E0 manifest and lock are
pinned by hash in `codeql_tools.py`, in addition to checking CLI version 2.24.3
and compiling the fixed template once before generation.

E1 verification has passed with offline model responses and real local CodeQL:
format failure at attempt 0, a real imported `.qll` type error at attempt 1,
then successful compilation at attempt 2. All 39 offline tests also pass.
This validates the harness, not actual model generation quality or CISB accuracy.

After the Responses migration, the combined E0/E1 suite passed all 45 tests in
42.528 seconds. E1 receipts are in
`runs/e1-responses-local-20260911T082345.136361Z/`, explicitly marked as mock-model
validation, not a research result. The accompanying E0 tool recheck is in
`runs/e0/20260911T082309.488472Z/`. This includes native Responses request/usage
handling, preserved reasoning and `call_id` in tool roundtrips, rejection of
truncated/refused/tool-only final output, and bounded retries/repairs.

The pre-migration local receipts remain in
`runs/e1-local-20260910T183306.054986Z/` and
`runs/e0/20260910T183233.585321Z/` (39 tests in 38.660 seconds on that revision).

#### Historical Chat Completions Runs

Both are paid Chat Completions runs with `--mcp off`, one spec (`12051b318b`),
and no smoke step. They do not verify the new Responses transport. No paid
Responses probe or generation was performed by the implementation agent during the migration.

`runs/e1-debug-1/` **does not exercise the loop.** `max_output_tokens` was 8000
and thinking-mode reasoning consumed all of it, so all four attempts returned
`finish_reason: length` with no `content` field at all. Thirty-two thousand
completion tokens produced no answer, and `compile_calls` is 0: no CLI compile
ever ran. The recorded status `generation_compile_failure` with reason
`format_error: ...` describes the symptom, not the cause. Keep it as the
evidence for why `max_output_tokens` is now sized as described above.

`runs/e1-debug-2/` **compiled at attempt 0.** One model call, 1848 prompt and
19314 completion tokens, 85.4 seconds total (77.7 model, 7.7 compile). The
saved candidate pack was then recompiled independently from its own directory
and returned 0, so it is self-contained and reproducible as `syntax.md` §6.2
requires. The generated query matches the spec's const-write intent rather than
degenerating to `where false` or a constant select.

Two limits remain. `e1-debug-2` needed no repair, so the repair path against a
real model is still covered only by offline tests and the mock-model local
receipt. And the repair path is the one that carries a full previous candidate
plus diagnostics back into the prompt, making it the most likely to hit the
`timeout_seconds` wall. Compile success is not semantic success: every result,
including `e1-debug-2`, remains `not_evaluated`.

To rerun offline tests or opt into local CodeQL integration (no paid model):

```fish
env -u RUN_E0_TESTS -u RUN_E1_TESTS .venv/bin/python -m unittest discover -s tests -v
env RUN_E1_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_e1_local_integration.py -v
```

`runs/e1-local-*` and `runs/e1-responses-local-*` are tool-validation runs using
mock HTTP responses and must not be included in research results. Inspect each
real run's sample summary before drawing conclusions. Do not treat compile
success as semantic success.

## Local Tools

- Python: 3.13.12, in `.venv` (existing uv-managed interpreter).
- Python SDKs: `openai==3.11.0`, `mcp==1.30.0`; full freeze in `requirements.txt`.
- Node.js: 24.14.1; npm: 11.11.0.
- CodeQL CLI: 2.24.3, `/usr/local/codeql/codeql`.
- CodeQL library: `codeql/cpp-all` 7.0.0, with a fixed dependency lock.
- GCC/G++: 13.3.0.
- MCP source: `~/codeql-lsp-mcp`, upstream commit
  `a33ea82bba156dc8352a0ecd85baff34cbb950ed`.
- Node dependencies: MCP SDK 1.30.0, LSP protocol 3.17.5, JSON-RPC 8.2.0,
  TypeScript 5.9.3; full lock in `mcp-package-lock.json`.

Source `env.sh` to set the two local tool paths. The file contains no secrets.
The model is selected in `config.json`; the user has configured it since local
tool acceptance. No paid model probe has been run by the implementation agent.

## Acceptance Results

Authoritative local receipts:
`runs/e0/20260910T125318.599663Z/`.

- All 5 Python integration tests passed in 37.754 seconds.
- All 7 Node adapter regression tests passed; see `mcp-unit-tests.tap`.
- The handwritten query compiles. An invalid type in its imported `.qll`
  produces a real compiler error at the library source location.
- C and C++ smoke databases both contain a queryable `main` function.
- Real MCP initialize/list_tools, workspace/open, diagnostics, hover,
  standard-library and local definition, and bounded completion all passed.
- Incorrect QL produces diagnostics; a new correct candidate in a fresh
  session has no old errors. Repeated empty diagnostics return from cache.
- Unknown tools return `isError: true`. Recorded MCP/LSP PIDs disappear after
  each tested session closes.

The receipts contain command arguments, exit codes, full stdout/stderr,
MCP tool arguments/results, timings, and copied candidate packs. Earlier E0
directories are retained debugging runs, not final acceptance results.

The user-run historical Chat Completions model probe also passed; the receipt was inspected at
`runs/e0/model-20260910T175621.772184Z/model-probe.json`. Both responses identify
`deepseek-flash`; all three protocol checks are true. This completed E0's model
connectivity, native tool-call roundtrip, and prompt-based JSON output checks
for Chat Completions, not the newly selected Responses API.
Do not put credentials into this repository or the result logs.

## Recheck E0: Model Probe

Local E0 tools and the user-run Responses probe have passed. The Responses receipt
is `runs/e0/model-responses-20260911T110224.338640Z/model-probe.json`; native tool
calling, dual-file JSON and the marker roundtrip all passed. This used the local
template function, not the real CodeQL MCP tools. Rerunning is optional unless
the model or gateway changes; `run.py` never runs this paid probe automatically.
Run it in the same terminal where `QL_API_KEY` and `QL_BASE_URL` were exported.
The command works in fish as well as Bash and reads the model from `config.json`:

```fish
cd ~/qlcoder-cpp-lite
.venv/bin/python probe_model.py --run
```

This explicitly opts into at most two paid Responses requests, each capped
at 1024 output tokens (or a lower configured limit), with no automatic retries.
The configured temperature and per-request timeout are retained. The wire token
limit parameter is `max_output_tokens`; unsupported parameters fail visibly without
an automatic compatibility fallback or model substitution.

The model first calls a read-only local `get_query_template` function, then
receives the handwritten template with a fresh marker in a QL comment. It must
return exactly the two template strings as JSON, including that marker. This
tests a real native tool-call roundtrip and prompt-based JSON output. It does
not test server-enforced JSON schema, generated-query quality, or the combined
model/MCP harness (E2). Local MCP acceptance was performed separately above.

Success prints `PASS` and exits 0. Failure exits nonzero and is not counted as
Responses acceptance. Receipts are written to a new `runs/e0/model-responses-<timestamp>/` directory
as `model-probe.json`: requests, responses, usage when supplied by the service,
timings, and individual check results. Authentication headers are not recorded;
the API key and base URL are redacted from saved content and error messages.
If it fails, inspect the receipt before issuing another paid run.

Without `--run`, the script only prints help. It does not read old credentials
or automatically load `.env` files. Configuring a variable in a fish terminal
does not update an already-running agent's environment; launch the probe in
that terminal. No credential needs to be sent through chat.

Offline probe tests (mock HTTP transport, no paid API calls):

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_model_probe.py' -v
```

## Reproduce the Local Checks

From this project directory:

```bash
source env.sh
.venv/bin/python -m pip check
"$CODEQL_PATH" pack ci codeql-pack
RUN_E0_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

These checks use real local tools and write a new timestamped `runs/e0/`
directory. Without `RUN_E0_TESTS=1`, integration tests are skipped. They never
invoke a paid model. Both smoke databases must already exist.

For the MCP regression tests:

```bash
cd "$HOME/codeql-lsp-mcp"
npm run build
node --test --test-isolation=none --test-reporter=tap tests/*.test.mjs
```

## Recreate Dependencies

This host already had Node, CodeQL, GCC/G++, and the managed Python interpreter.
No system tool installation or global configuration was changed. To recreate
the venv on this host:

```bash
cd "$HOME/qlcoder-cpp-lite"
"$HOME/.local/share/uv/python/cpython-3.13.12-linux-x86_64-gnu/bin/python3.13" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On another host, install the recorded tool versions first; `syntax.md` contains
the fixed CodeQL bundle location. The saved pack/lockfile is self-contained
and does not depend on the old `cisb-llm` checkout.

Rebuild MCP in a fresh checkout, not over an already patched working tree:

```bash
git clone https://github.com/neuralprogram/codeql-lsp-mcp.git "$HOME/codeql-lsp-mcp"
git -C "$HOME/codeql-lsp-mcp" checkout --detach a33ea82bba156dc8352a0ecd85baff34cbb950ed
git -C "$HOME/codeql-lsp-mcp" apply "$HOME/qlcoder-cpp-lite/mcp.patch"
cp "$HOME/qlcoder-cpp-lite/mcp-package-lock.json" "$HOME/codeql-lsp-mcp/package-lock.json"
cd "$HOME/codeql-lsp-mcp"
env -u NODE_TLS_REJECT_UNAUTHORIZED npm ci
npm run build
node --test --test-reporter=tap tests/diagnostics.test.mjs
```

TLS verification was enabled for the final dependency installs, without
changing the shell's global TLS settings. The upstream MIT license remains
in the MCP checkout. `mcp.patch` includes both source changes and Node tests.

### Necessary MCP Changes

- Cache diagnostics, including empty arrays and early notifications.
- Keep both opened query files visible to the LSP.
- Report tool/hover failures instead of disguising them as empty results.
- Bound requests and shutdown; clean up on stdin EOF and signals.
- Serialize lazy LSP startup and remove automatic restarts.
- Respect the configured CodeQL executable and log child PIDs for verification.
- Pin LSP protocol 3.17.5 and JSON-RPC 8.2.0 to one compatible protocol stack.

The original caret dependency ranges initially installed LSP protocol 3.18.3
alongside JSON-RPC 8.2.1/9.0.2. The resulting build needed different module
resolution, and real initialization timed out. Pinning the protocol versions
made the original TypeScript configuration usable and real MCP acceptance
pass. No TypeScript configuration change is retained.

## Smoke Databases

The source fixtures are handwritten, not generated by a model. To create new
databases, run from the project root with `env.sh` sourced:

```bash
ROOT="$PWD"
mkdir -p databases
"$CODEQL_PATH" database create databases/smoke-c-db \
  --language=c-cpp --source-root=fixtures/c --threads=1 \
  --command="gcc -O0 -g -c \"$ROOT/fixtures/c/sample.c\" -o \"$ROOT/fixtures/c/sample.o\""
"$CODEQL_PATH" database create databases/smoke-cpp-db \
  --language=c-cpp --source-root=fixtures/cpp --threads=1 \
  --command="g++ -std=c++17 -O0 -g -c \"$ROOT/fixtures/cpp/sample.cpp\" -o \"$ROOT/fixtures/cpp/sample.o\""
```

Do not recreate over existing databases without explicitly choosing how to
retain the old results. The tests run the same template against both databases
and decode BQRS to assert a nonempty result. For future generated queries,
zero rows are valid execution, not evidence of semantic correctness or failure.
