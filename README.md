# QLCoder C/C++ Lite

Research prototype following `syntax.md`. E0 tools and the real model protocol
probe have passed. E1 implements the CLI-only generation/repair loop. Model/MCP
integration and formal A/B experiments remain E2 and E3 respectively.

## Run E1

In the same fish terminal where `QL_API_KEY` and `QL_BASE_URL` were exported:

```fish
cd ~/qlcoder-cpp-lite
source env.sh
.venv/bin/python run.py --spec specs/12051b318b_spec.md --mcp off --out runs/e1-debug-1
```

This command uses the configured real model and can incur API charges. Each
sample allows attempt 0 plus at most three repairs, including malformed JSON.
Only connection failures get one transport retry; timeouts and HTTP errors
(including authentication errors) stop that sample. SDK retries are disabled.
Thus E1 makes at most eight API attempts per sample with the default budget,
and at most four candidate compiles, plus one template compile per batch.

For the three debugging inputs, using a different, new output directory:

```fish
.venv/bin/python run.py --samples samples.txt --mcp off --out runs/e1-debug-batch-1
```

The current samples are unchanged copies of `12051b318b`, `02828845dd`, and
`9c14791748` from the old project's specs. They cover const writes, inline-asm
null-check elimination, and shared reads. They are debugging inputs, not a
frozen evaluation set; E3 must disclose any overlap with the evaluation set.
No import, runtime path, or query from the old project is needed to run E1.

Always pass `--mcp off` for E1. The planned default remains `on`, which currently
fails explicitly before any model request; it never silently becomes group A.
`--smoke` is likewise explicitly deferred. The E0 smoke databases are preserved.
There are no new Python dependencies, model overrides, resume/skip modes, or
automatic dependency installs.

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
are `null` if any attempt lacks usage. MCP is disabled and its call count is 0.
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
then successful compilation at attempt 2. All 33 offline tests also pass.
This validates the harness, not actual model generation quality or CISB accuracy.
No paid E1 generation run has been performed by the implementation agent.

The combined E0/E1 suite passed all 39 tests in 38.660 seconds. Final local
E1 receipts are in `runs/e1-local-20260910T183306.054986Z/`, explicitly marked
as mock-model validation, not a research result. The accompanying E0 tool
recheck is in `runs/e0/20260910T183233.585321Z/`.

To rerun offline tests or opt into local CodeQL integration (no paid model):

```fish
env -u RUN_E0_TESTS -u RUN_E1_TESTS .venv/bin/python -m unittest discover -s tests -v
env RUN_E1_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_e1_local_integration.py -v
```

`runs/e1-local-*` are tool-validation runs using mock HTTP responses and must
not be included in research results. Inspect each real run's sample summary
before drawing conclusions. Do not treat compile success as semantic success.

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

The user-run real model probe also passed; the receipt was inspected at
`runs/e0/model-20260910T175621.772184Z/model-probe.json`. Both responses identify
`deepseek-flash`; all three protocol checks are true. This completes E0's model
connectivity, native tool-call roundtrip, and prompt-based JSON output checks.
Do not put credentials into this repository or the result logs.

## Recheck E0: Model Probe

E0 has passed; rerunning this paid probe is optional, not an E1 prerequisite.
Run it in the same terminal where `QL_API_KEY` and `QL_BASE_URL` were exported.
The command works in fish as well as Bash and reads the model from `config.json`:

```fish
cd ~/qlcoder-cpp-lite
.venv/bin/python probe_model.py --run
```

This explicitly opts into at most two paid Chat Completions requests, each capped
at 1024 output tokens (or a lower configured limit), with no automatic retries.
The configured temperature and per-request timeout are retained. The wire token
limit parameter is `max_tokens`; unsupported parameters fail visibly without
an automatic compatibility fallback or model substitution.

The model first calls a read-only local `get_query_template` function, then
receives the handwritten template with a fresh marker in a QL comment. It must
return exactly the two template strings as JSON, including that marker. This
tests a real native tool-call roundtrip and prompt-based JSON output. It does
not test server-enforced JSON schema, generated-query quality, or the combined
model/MCP harness (E2). Local MCP acceptance was performed separately above.

Success prints `PASS` and exits 0. Failure exits nonzero and is not counted as
E0 completion. Receipts are written to a new `runs/e0/model-<timestamp>/` directory
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
node --test --test-reporter=tap tests/diagnostics.test.mjs
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
