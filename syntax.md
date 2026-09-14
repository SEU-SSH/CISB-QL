# 从 Spec 生成有效 QL：轻量研究实验计划

更新日期：2026-09-11。

定位：单机、串行、固定模型的小型研究原型，不建设通用 Agent 平台。在 `~/qlcoder-cpp-lite/` 独立实施；本文保留实验设计，实际部署与验收以实施状态及回执为准。

实施状态：E0 环境及用户运行的 Responses 探针已通过；E1 CLI 闭环、E2 MCP 接入已实现。`e2-debug-1` / `e2-debug-1-no-mcp` 真实单样本对照仅作 pilot。E3 的 smoke、旧候选独立复验与 A/B 汇总已实现，94 项本地测试全部通过，两份真实 E2 查询在双库复验通过且均为零行。冻结方案下的 A/B 各三次正式重复现已完成，共 18 个样本运行，三次修复内两组均通过 9/9；本批未请求 smoke，语义仍未评估。17 个冻结文件核对通过，完整结果见 `runs/E3-formal-report/` 与 `README.md`。

## 1. 核心目标与精简边界

总目标仍是 **Spec → 有效的 C/C++ QL 查询**，语法正确是第一个可机器验证的子目标：

| 层次 | 本次安排 |
|---|---|
| 编译有效 | 主实验目标：固定 pack 下 `query compile --check-only` 返回 0 |
| 可执行 | 保留 smoke run，作为编译后的辅助检查，不参与语义修复 |
| 语义有效 | 保留原始 spec 意图约束，但本阶段不声称已验证，后续用正负例或人工审查评估 |

`--check-only` 包含语法、名称解析、类型等检查，但不等于完整查询执行，更不等于检测准确。[R3]

**精简工程设施，不精简实验口径。** 与上一版相比：

| 原设置 | 简化后 |
|---|---|
| 多模块 package、安装入口和通用数据模型 | 一个 `run.py` + 四个功能文件，不做发布打包 |
| 分层 TOML、环境变量名映射、toolchain 配置系统 | 一个小型 `config.json`，路径和密钥使用固定环境变量 |
| doctor、bootstrap、导入工具、promote/回滚 | 手工一次性环境检查；不建设这些子命令 |
| CSV/alias/manifest 和旧目录兼容适配 | 固定 spec 快照及 `samples.txt`，暂不映射或写回旧项目 |
| MCP 三种 mode、自动降级/重启、跨轮版本协议 | 只有 on/off；每个候选独立会话，工具失败如实记录 |
| 事件总线、逐层状态及多份 accepted/compiled 副本 | 每轮完整候选和原始日志，summary 指向成功轮次 |
| 旧 Querier 全量基线 + 新增 20～30 个能力 spec | 先少量调试，再直接对固定的现有 CISB spec 做 A/B |
| 90%/95% 等硬门槛、MCP 必须提升 10 个百分点 | 报告实际效果、成本和失败原因，不预设研究结论 |

必须保留：单 Agent、MCP/LSP 辅助、CLI 裁决、双文件输出、初次生成加最多三次修复、原始诊断、固定输入/版本，以及公平的 A/B 对照。

不加入 RAG、CVE 获取、AST diff、多 Agent、多模型切换、全局数据流/path query、SARIF、Refiner、spec 回写或准确率评测。静态 CISB 定义可以作为两组共用的 prompt 上下文。

## 2. 最小目录和重构范围

```text
~/qlcoder-cpp-lite/
├── run.py                    # 单样本/批量入口，同一个主流程
├── report.py                 # 显式配对 A/B 回执，输出 JSON/Markdown 汇总
├── harness.py                # 生成、工具调用、检查、修复、结果保存
├── agent_backend.py          # 唯一模型后端和 tool-call 往返
├── codeql_tools.py            # MCP 会话、CLI compile/run
├── spec_io.py                 # 现有 Markdown spec 解析
├── config.json
├── requirements.txt          # 环境跑通后冻结 Python 包版本
├── prompt.md
├── specs/                    # 筛选后的只读 *_spec.md 快照
├── samples.txt               # 固定实验样本，每行一个 spec 相对路径
├── e3-freeze.sha256          # 17 个控制文件的普通校验清单，不是配置 profile
├── codeql-pack/
│   ├── qlpack.yml
│   ├── codeql-pack.lock.yml
│   ├── query.qll             # 手写环境检查/工具上下文模板
│   └── query.ql
├── fixtures/{c,cpp}/          # 手写 smoke 源码
├── databases/                # 本地数据库，不提交
├── runs/                     # 实验结果，不提交
├── tests/                    # 少量核心单测
└── README.md                 # 部署命令、版本、来源和已知问题

~/codeql-lsp-mcp/              # 固定提交的实验副本，可做必要小补丁
```

彻底替换旧 `agents/querier.py` 的职责，而不是再包装一个 SyntaxQuerier。只参考其 spec 解析、双文件约定和有限修复思路；模型调用、MCP、编译和循环重写。新项目不继承旧 `Agent`，不依赖 Wrapper/Refiner 或旧仓库的 imports。

本阶段不改旧入口，也不自动发布查询。旧 Querier 可保留作历史参考，其重跑和兼容适配不是本实验前置任务。生成结果留在新项目 `runs/`，不覆盖旧 query、spec、testcase 或 pack。

从旧项目只复制选定 spec，保留其原文；文件名作为 `source_id`，重复 ID 直接报错。`source_id/query_key`、`b-6/l-2` 等历史映射问题不影响独立生成，留到确需导出或语义评测时处理。

`.gitignore` 排除 `.venv/`、`__pycache__/`、`runs/`、`databases/`、密钥文件和编译缓存。提交 spec、samples、prompt、配置及依赖锁即可，不建设额外 provenance 数据库。

## 3. 一次性工具部署

沿用上一轮核对的版本，不追求最新工具：CodeQL CLI `2.24.3`、`codeql/cpp-all` `7.0.0`、Python 3.13、Node 24。Python/Node 的实际 patch、模型 ID、MCP 提交和修改记录统一记在 README；每批实验保存该记录与实际配置的副本。

QLCoder 只作方法参考，不需要克隆和启动其完整环境。实际复用 `codeql-lsp-mcp`，它通过 stdio 启动 CodeQL 自带的 LSP，无需 VS Code、独立 LSP 安装包或 HTTP 服务。[R1][R2]

### 3.1 Python 与配置

先安装 Git、curl、tar、Python 3.13、Node 24/npm，以及构建 smoke fixture 所需的 gcc/g++。以下均为后续部署命令，不在本次文档修改中执行：

```bash
mkdir -p "$HOME/qlcoder-cpp-lite"
cd "$HOME/qlcoder-cpp-lite"
python3.13 -m venv .venv
.venv/bin/python -m pip install openai 'mcp>=1.26,<2'
# 完成环境检查后保存实际版本；重建环境使用 pip install -r requirements.txt。
.venv/bin/python -m pip freeze > requirements.txt
```

测试使用 stdlib unittest。直接执行 `run.py`，不引入 pyproject、editable install、构建依赖或发布流程。

只保留会影响实验的配置：

```json
{
  "model": "<fixed-model-id>",
  "temperature": 0.2,
  "max_output_tokens": 8000,
  "max_repair_attempts": 3,
  "max_tool_calls_per_attempt": 6,
  "timeout_seconds": 120
}
```

固定环境变量为 `QL_API_KEY`、`QL_BASE_URL`、`CODEQL_PATH`、`CODEQL_MCP_ENTRY`。不用配置字段再映射变量名，不读取旧项目 `.env`。密钥不得进入日志或仓库。实验中只改变命令行的 MCP on/off；其余设置保持一致，所有运行均保存实际使用的配置。

### 3.2 CodeQL 与 pack

可以直接配置已安装的 CodeQL 2.24.3，例如 `CODEQL_PATH=/usr/local/codeql/codeql`；新机器从固定官方发布下载 bundle。[R5]

```bash
mkdir -p "$HOME/.local/opt/codeql-2.24.3"
curl -fL --retry 3 \
  https://github.com/github/codeql-action/releases/download/codeql-bundle-v2.24.3/codeql-bundle-linux64.tar.gz \
  -o /tmp/codeql-bundle-2.24.3-linux64.tar.gz
# Linux x86_64 示例；按发布页校验下载文件，成功后再解包。
tar -xzf /tmp/codeql-bundle-2.24.3-linux64.tar.gz \
  -C "$HOME/.local/opt/codeql-2.24.3"
export CODEQL_PATH="$HOME/.local/opt/codeql-2.24.3/codeql/codeql"
"$CODEQL_PATH" version --format=json
```

创建 `codeql-pack/qlpack.yml`：

```yaml
name: local/qlcoder-cpp-lite
version: 0.0.1
dependencies:
  codeql/cpp-all: 7.0.0
```

```bash
cd "$HOME/qlcoder-cpp-lite"
"$CODEQL_PATH" pack install codeql-pack
# 保存生成的 lockfile，之后用 pack ci 安装相同依赖。
"$CODEQL_PATH" pack ci codeql-pack
```

只在部署时安装依赖，不在每轮生成中安装。MCP 和 CLI 使用同一个 CodeQL、HOME/pack cache、候选 pack 和 lockfile；不支持多套搜索路径切换。缺依赖或版本不符，先修环境再跑实验。[R3]

### 3.3 MCP

首次部署到不存在的目录；已有副本则检查版本，不覆盖用户修改：

```bash
git clone https://github.com/neuralprogram/codeql-lsp-mcp.git "$HOME/codeql-lsp-mcp"
git -C "$HOME/codeql-lsp-mcp" checkout --detach \
  a33ea82bba156dc8352a0ecd85baff34cbb950ed
cd "$HOME/codeql-lsp-mcp"
npm install
npm run build
export CODEQL_MCP_ENTRY="$HOME/codeql-lsp-mcp/dist/index.js"
cd "$HOME/qlcoder-cpp-lite"
```

上游该提交没有 npm lockfile，首次安装后保留生成的 `package-lock.json`，重装使用 `npm ci`。若修改 MCP，保存补丁及该锁文件到实验材料，不建设 vendor 同步/升级机制，保留上游许可证。[R2]

Python 使用 MCP SDK 的 stdio client 和 ClientSession，Harness 指定 `node`、entrypoint、候选目录 cwd、`CODEQL_PATH` 和必要环境变量。日志走 stderr，stdout 只传协议，不向 MCP 传模型密钥。[R4]

部署检查只做一次：初始化、list_tools、打开手写双文件、读取 diagnostics，以及一次 hover/definition。随后以一次小额模型调用确认所选服务支持 native tool-calling 和完整 JSON 输出；SDK 兼容接口不能替代实测。不要为此建设通用 doctor 系统。

## 4. 简化后的 MCP 使用方式

### 4.1 用独立候选避免跨轮同步系统

**每个候选使用新目录和新的 MCP/LSP 会话，落盘后不再修改该候选。** 编译失败时，Agent 可在这个只读候选上查询 API、生成下一版，然后关闭旧会话，为下一版重新启动。

这样不需要跨轮 `update_file`、自定义 `expected_version`、候选 epoch 或会话重启恢复机制。代价是 MCP 启动次数和耗时增加，实验中直接记录。这是研究原型的实现取舍，不宣称是最优性能方案。

首次生成需要工具上下文时，用同样方式打开固定手写模板；提交首个候选后关闭模板会话。两组 prompt 都包含相同模板，B 组额外具有语言工具访问能力。模板不是生成候选，不计入通过率。

### 4.2 仍需修复的上游问题

上一轮源码核对发现：空 diagnostics 被丢弃、通知缺少缓存、部分错误被包装成普通文本。这会直接影响实验，因此不能随工程设施一起删掉。[R2]

最小补丁只做：

- 缓存 URI 对应的通知，空数组也唤醒等待者，避免通知早于查询时丢失。
- 工具错误明确返回失败，由客户端记录；每个操作有超时。
- 验证两个已落盘、内容不变的文件都能被分析。
- 禁用上游自动重启；会话结束或失败时关闭 MCP 及其 LSP 子进程。

不再要求扩展诊断协议。收到的诊断必须属于本会话内的候选 URI；双文件在启动前写好，之后只读，锁定依赖也不修改，因此不依赖可选 document version 判断跨轮新旧。未收到通知时记录超时，不能当作无错误。若以后复用会话或修改候选，就必须重新引入版本同步检查。

on 模式遇到 MCP 不可用时，结束该样本并记录工具失败；不偷偷降级成 off。需要纯 CLI 时显式选择 off。没有自动重启或跨组恢复，异常后清理子进程即可。

## 5. 输入、输出与核心循环

### 5.1 输入和模型约束

沿用现有 Markdown spec，保留原文并解析：

- Vulnerability Description 的 Source、Description、Evidence、Requirement、Mitigation。
- Code Pattern JSON 的 triggers、vulnerable_pattern、ql_constraints、equivalence_notes、scope_assumptions、control_flow_assumptions、environment_assumptions。

检查必需章节、JSON 和字段类型即可，不增加 spec 归一化、Query Plan 或字段覆盖率系统。无效输入在调用模型前报错。Description、vulnerable_pattern、ql_constraints 为非空字符串，其余列表字段为字符串数组。

模型每轮只提交完整双文件：

```json
{
  "qll_code": "完整 query.qll",
  "ql_code": "完整 query.ql"
}
```

双字段须为非空字符串，不接受外围 Markdown fence、diff 或额外文件。固定 `query.qll` 的 `import cpp`，以及 `query.ql` 的 `import cpp`、`import query`；CLI 负责实际 QL 名称和类型检查，不自行编写 QL 解析器。

Prompt 提供完整 spec、固定版本和模板，只使用普通 AST/局部结构查询。禁止靠删除核心条件、常量输出或 `where false` 规避错误；这是意图约束，不是已经完成语义验证。不再要求 metadata、rationale 或三类 semantic units。

### 5.2 一个后端、少量工具

`agent_backend.py` 只实现一个 OpenAI SDK-compatible Responses 后端，使用 `client.responses.create`。2026-09-11 将原计划中的 Chat Completions 升级为 DeepSeek Responses；不保留自动回退或多后端选择，不改变模型、Prompt、配置值和修复预算。[R6]

请求使用 `instructions`、`input` 和 `max_output_tokens`。DeepSeek Responses 为无状态接口，不使用 `previous_response_id`、`conversation` 或 `store`；每次请求由 Harness 提供所需上下文。E1 每轮继续传完整 spec、模板和上一轮候选/诊断。工具阶段使用扁平 function 定义，将完整 `response.output`（包括 reasoning）与对应 `call_id` 的 `function_call_output` 加入客户端历史，再发下一次请求；不能把函数调用项的 `id` 当作 `call_id`。

最终 JSON 只从 completed 响应中 assistant message 的 `output_text` 汇总提取，不将 reasoning 或工具调用当成代码。`incomplete`（包括输出预算耗尽）按格式失败消耗本轮，不编译不完整候选；服务端 failed、非法协议和 API 错误按基础设施失败停止。工具阶段不强制 JSON 文本模式，最终输出交给 Harness 解析。

保留原始 output 和 usage，新回执标记 `api_format: responses`，汇总使用 `input_tokens`、`output_tokens`、`total_tokens`，推理 token 已包含在 output 中，不重复相加。旧 Chat Completions 回执不改写；正式 A/B 组使用相同 API 格式。E0 本地工具无需重新部署，但切换后应重跑两请求上限的 Responses 模型探针，旧 Chat 探针通过不代表网关支持新接口。E2 的工具次数仍由客户端计数，不依赖服务端 `max_tool_calls`。

Harness 管理 workspace、open 和 diagnostics。模型只访问 `codeql_hover`、`codeql_definition`，必要时使用最多 20 项的 `codeql_complete`。definition 可附锁定库中少量相邻源码；不开放任意文件、shell、安装或写文件工具。位置遵守 LSP 的 0-based/UTF-16 约定。

每轮最多 6 次模型选择的工具调用，正常模型请求最多为工具预算加一次最终提交，不额外配置另一套轮次。提交失败算本轮格式错误，不能无限重问。传输失败最多重试一次，鉴权错误直接结束；只设一层重试，实际 API 调用和 token 均记录。所有模型/MCP/CLI 操作使用配置中的超时，取消时回收相关子进程。

### 5.3 生成、诊断、修复

```text
Spec -> Agent -> 双文件 JSON -> 候选目录
                                  |
                          MCP 诊断（on 时）
                                  |
                           CLI --check-only
                           /              \
                        失败              成功
                         |                 |
             诊断 + API 查询 -> 修复    保存成功轮次
                （最多三次）               |
                                     可选 smoke run
```

执行约定：

1. 批次开始检查必要文件、版本和模型配置，编译手写模板一次，避免明显坏环境消耗模型调用。
2. 初次生成记为 attempt 0，修复为 attempt 1..3。JSON 格式错误也消耗一轮并反馈，但不执行 CLI。
3. 有效 JSON 落盘为独立 pack，复制固定 pack/lockfile；B 组开启新 MCP 会话检查同一份文件。
4. 即使 LSP 报 QL 错误也执行 CLI，以当前候选的 CLI 返回码为准。MCP 服务失败与 QL 错误分开处理。
5. 仅格式/QL 查询错误触发修复。超时、缺依赖、鉴权、工具服务故障等停止该样本，保留原因，不让模型改代码。
6. 修复前允许工具查询失败候选，之后提交完整下一版；不在两个正式候选之间插入未计数的代码修补。

每个样本最多四个候选、四次候选 `--check-only`。固定资源使用单线程；需要调整内存等 CLI 参数时写在代码常量中并固定记录，不增加配置层。以下从候选目录执行，代码中使用参数数组而不是 shell 字符串：[R3]

```bash
"$CODEQL_PATH" query compile --check-only --format=json --threads=1 query.ql
```

反馈只保留必要内容：完整 spec、失败轮次/剩余次数、上一轮原始输出与完整双文件、格式错误、LSP/CLI 诊断、相关工具结果和环境版本。未执行的阶段明确标为未执行。模型看到的普通输出可限长，但不能丢掉当前 error 的位置/消息；原文全部落盘。

不建设复杂异常分类框架。summary 区分输入错误、生成/编译失败、基础设施失败即可，再用文本原因描述；不把所有非零退出码都当 QL 错误。不能确定原因时标记待检查，不虚构分类。

## 6. smoke 检查和最少日志

### 6.1 可执行性检查

先完成编译主实验，再在相同工具链上启用 smoke。仍保留两个很小的非空 C/C++ 数据库，但不为每个 spec 建库，也不做漏洞/修复双版本或多数据库矩阵。

手写模板可用 `query.qll`：

```ql
import cpp
predicate matches(Function f) { f.getName() = "main" }
```

以及 `query.ql`：

```ql
import cpp
import query
from Function f
where matches(f)
select f, f.getName()
```

fixture 至少各有一个 main；C 文件可写 `int main(void) { return 0; }`，C++ 文件可加入一个类方法并在 main 调用。固定源码，从项目根目录真实编译建库：[R3]

```bash
PROJECT_ROOT="$PWD"
mkdir -p databases
"$CODEQL_PATH" database create databases/smoke-c-db \
  --language=c-cpp --source-root=fixtures/c \
  --command="gcc -O0 -g -c \"$PROJECT_ROOT/fixtures/c/sample.c\" -o \"$PROJECT_ROOT/fixtures/c/sample.o\""
"$CODEQL_PATH" database create databases/smoke-cpp-db \
  --language=c-cpp --source-root=fixtures/cpp \
  --command="g++ -std=c++17 -O0 -g -c \"$PROJECT_ROOT/fixtures/cpp/sample.cpp\" -o \"$PROJECT_ROOT/fixtures/cpp/sample.o\""
```

用手写模板运行并解码结果，确认 main 至少命中一行，不能仅凭 DB 目录存在认定有效。数据库只构建一次，重建不默认覆盖旧目录。

```bash
"$CODEQL_PATH" query run "$QUERY" --database="$DATABASE" \
  --output="$RESULT_DIR/result.bqrs" --threads=1
"$CODEQL_PATH" bqrs decode "$RESULT_DIR/result.bqrs" \
  --format=json --output="$RESULT_DIR/rows.json"
```

生成查询零结果不是失败。运行超时/错误单独记录，不触发新一轮语法或语义修复；编译成功仍保留。decode 只是结果查看，不建设 BQRS/SARIF 分析系统。

smoke 可直接使用已保存的成功候选，不必重新调用模型。如果作为正式 A/B 批次的统一步骤，则两组同时启用，耗时分开统计，不把单独的 smoke 开销混入编译收益。

### 6.2 实验产物

```text
runs/<batch>/
├── experiment.json            # 一次记录配置、版本、样本清单、MCP on/off
└── <source_id>/
    ├── spec.md                # 本次实际输入
    ├── attempt_0/             # 后续 attempt_1..3，既是工作 pack 也是存档
    │   ├── qlpack.yml
    │   ├── codeql-pack.lock.yml
    │   ├── query.qll
    │   ├── query.ql
    │   ├── model.json         # 请求、可见响应、工具往返、usage
    │   ├── diagnostics.json   # 格式/LSP/CLI 状态和实际反馈
    │   ├── stdout.txt
    │   └── stderr.txt
    └── summary.json
```

无效 JSON 时保留原始响应，不伪造候选文件。summary 记录成功轮次、编译/可选运行状态、失败原因、调用次数、耗时和 token；缺 usage 写 null，语义状态写 `not_evaluated`。随生成启用的 smoke 输出放在成功轮次的 `smoke/{c,cpp}/`；`generation_seconds` 不含 smoke，`smoke_seconds` 单列，`seconds` 为两阶段总耗时。批次预检另计。

旧候选使用 `--smoke-from <batch>`，不要求模型凭据，不启动模型或 MCP。为保持旧回执不变，在临时 pack 中独立复编译并运行，临时文件随后清理；新目录只保存来源路径、四文件哈希、编译复验和 smoke 产物，不持久保存第二份候选，不写回旧 summary。两个固定数据库的模板预检、源码归档及元数据指纹一并记录；数据库本身的工具缓存/日志可以更新。

不另外复制 work/compiled/accepted，不实现 promote、覆盖备份或回滚。每次实验使用新的 batch 目录，原结果不覆盖。成功候选目录能独立重跑编译，就是本阶段的交付产物。最基本的“不写旧项目、不泄露密钥、模型只能访问候选和固定库”仍需保留。

## 7. 实验设置和运行入口

### 7.1 只保留一组核心 A/B

先用 3～5 个现有 spec 调通流程和 prompt，再固定 samples、spec 快照、prompt 与代码版本。正式实验直接使用选定的现有 CISB spec，不强制补建 20～30 个能力 spec，也不要求先跑完旧 Querier 基线。记录调试集与正式集是否重合，避免隐藏调参过程。

| 组别 | 设置 |
|---|---|
| A | 单 Agent + CLI，`--mcp off` |
| B | 相同 Agent/Harness + MCP/LSP + CLI，`--mcp on` |

固定模型、temperature、token 上限、spec、prompt/模板、CodeQL/pack、候选和工具预算。B 组只是多语言工具及其上下文，额外耗时/token 如实统计，不假称两组实际成本相等。

试跑各一次即可排障；正式结果默认各做三次独立重复，按相同 spec 配对报告。预算受限可减少次数，但必须注明，不把单次随机结果当稳定结论。正式运行后修改 prompt/配置，应作为新实验批次，不能只用改好的结果替换失败样本。

MCP 失败属于 B 组基础设施失败，不能悄悄作为 A 组结果使用。主表分母为固定的全部合法输入，并列报告基础设施失败；如补充环境就绪样本的通过率，注明排除规则。重跑故障样本要保留原记录及预先说明的重跑口径。

### 7.2 指标

只统计：

- `CompileInitial`：首次候选编译通过比例。
- `CompileWithin3Repairs`：四个候选内通过比例，不使用含糊的 `Compile@3`。
- 成功样本平均修复次数，以及全体编译失败/基础设施失败数量。
- 格式失败与 QL 失败的候选次数分别统计，并分别记录后续修复是否启动；平均修复次数仍包含两者，不能把格式修复写成 QL 修复。
- 总耗时、token、MCP 调用/失败次数，能取得时分列工具启动耗时。
- 启用 smoke 时，补充已编译候选的运行成功比例；两个数据库均成功才算通过，零结果合法。

无固定 90%/95% 验收线，也不以提升 10 个百分点作为 MCP 去留的硬条件。观察编译收益、错误变化和额外成本，小样本只报告差异及不确定性，允许“没有观察到收益”的研究结果。

### 7.3 一个入口

单样本和串行批量入口已实现，支持 MCP on/off 和 `--smoke`。`samples.txt` 每行一个相对项目根的 spec 路径，单文件和批量共用同一函数；批量串行运行，失败样本写日志后继续。

```bash
cd "$HOME/qlcoder-cpp-lite"
# 开发时运行单个样本。
.venv/bin/python run.py --spec specs/12051b318b_spec.md --mcp on --out runs/debug
# 正式对照，rep1/rep2/rep3 使用不同结果目录。
.venv/bin/python run.py --samples samples.txt --mcp off --out runs/A-rep1
.venv/bin/python run.py --samples samples.txt --mcp on --out runs/B-rep1
# 可选：成功编译后执行固定的两个 smoke 数据库。
.venv/bin/python run.py --samples samples.txt --mcp on --smoke --out runs/B-smoke
# 无模型调用：复验已保存的成功候选，输出必须在原批次之外。
.venv/bin/python run.py --smoke-from runs/e2-debug-1 --out runs/e3-smoke-B
# 显式输入三次重复，A/B 列表顺序一一对应。
.venv/bin/python report.py \
  --a runs/A-rep1 runs/A-rep2 runs/A-rep3 \
  --b runs/B-rep1 runs/B-rep2 runs/B-rep3 \
  --purpose formal --overlap-note "All three specs also used for debugging; no held-out evaluation" \
  --out runs/formal-report
```

默认读取项目根 `config.json`，默认 MCP on。`--smoke` 固定使用第 6 节两个数据库，无任意数据库列表或配置 profile。全体样本达到请求关卡时退出 0，否则非零，具体原因以 summary 为准。不实现 resume/skip-existing、并发队列或服务模式。

汇总输出 `report.json`（逐样本、逐重复、组汇总、配对结果和来源哈希）与 `report.md`。严格检查两组输入集合/哈希、prompt、配置、代码、API、工具链和共同策略一致；B 组重复还需 MCP 部署一致。拒绝模拟模型回执、重复目录、未完成批次或错分组，不自动扫描并筛选最优结果。预检后未运行的合法样本及基础设施失败仍在分母；缺 usage 保留未知及已知小计。已编译但 smoke 失败不撤销编译成功。

`--purpose pilot/formal` 与调试重合说明必填。正式少于三次重复需用 `--note` 说明预算；声明为 formal 本身不证明统计稳定。当前三份 spec 仍可作为小规模固定集合，但必须披露全部与调试集重合；现有 E2 单样本对照只做 pilot，不能混入 E3 新代码的正式重复。模型、prompt、token 预算保持原值，完整 fish 命令见 README。

### 7.4 已完成的正式编译实验

实际顺序为 A1、B1、B2、A2、A3、B3，对应 `runs/E3-A-rep1..3` 与 `runs/E3-B-rep1..3`。每批三个固定 spec，全部原始批次与失败候选保留，无额外补跑、配置修改或结果替换。完整配对结果为 `runs/E3-formal-report/report.json`，可读表为同目录 `report.md`。

| 指标 | A：MCP off | B：MCP on |
|---|---:|---:|
| 首次候选编译通过 | 2/9 | 3/9 |
| 三次修复内编译通过 | 9/9 | 9/9 |
| 成功样本平均修复次数 | 1.11 | 1.11 |
| 格式失败候选次数 | 0 | 5 |
| QL 失败候选次数 | 10 | 5 |
| 基础设施失败 | 0 | 0 |
| 模型请求次数 | 19 | 42 |
| 样本生成总耗时（分钟，不含批次预检） | 23.89 | 50.82 |
| 总 token | 579,823 | 1,799,355 |

B 记录的 QL 失败轮次较少，但额外五次格式失败抵消了总修复次数上的优势。其中一次格式失败来自单次请求八个工具调用，超过固定的六次上限；不能把它计为 QL 编译错误。B3 的内联汇编 spec 首次候选通过，但已使用四次模型请求、五次工具调用，因此“首次候选”不等于“单次模型请求”。

本次观察到 B 多一次首轮通过，最终通过率和平均修复次数相同；B 耗时为 A 的 2.13 倍、token 为 3.10 倍。这里只比较三个与调试集完全重合的 spec 的重复运行，不能据此推断 MCP 的普遍收益或语义检测准确性。本轮正式实验只测编译，先前 E2 候选的辅助 smoke 回执不混入该表。61 次模型响应均完成且 usage 完整，23 个 MCP 会话均记录关闭，实验后冻结清单全部匹配。

### 7.5 五用例、单轮 A/B 扩展实验

2026-09-12 按用户要求新增两个用例，各组只运行一个批次，共五个不同 spec、十个样本运行，无额外重复或失败补跑。`samples-5.txt` 保留原三例顺序，追加从 `/home/suiren/cisb-llm/specs/` 逐字节复制的 `7185ad2672`（清零操作被优化删除）和 `d50f2ab6f0`（移位未定义行为及无效检查）。保留原文及 provenance，不在观察生成结果后改写 spec，也不把新增两例声明为已认证的 held-out 集。

旧 `samples.txt`、冻结清单、归档及六个历史批次均保留。新增冻结清单为 `e3-5spec-freeze-20260912.sha256`，源码归档为 `runs/e3-5spec-freeze-20260912.tar.gz`。模型、代码、prompt、token/超时/修复预算及 MCP 部署不变，使用当前导出的 API 配置；运行前最新的 E0 Responses 回执 `runs/e0/model-responses-20260912T124335.049170Z/model-probe.json` 已通过。本轮 API 配置更换后独立统计，不混入 7.4 的历史汇总。

实际顺序为 `runs/E3-5spec-A-rep1-20260912`，再运行 `runs/E3-5spec-B-rep1-20260912`；退出码分别为 0 和 1。两组均处理五个合法输入，未启用 smoke。完整配对报告为 `runs/E3-5spec-report-20260912/report.md` 与 `report.json`；`--note` 同时披露单轮预算和运行中发现的分类问题。

| 指标 | A：MCP off | B：MCP on |
|---|---:|---:|
| 首次候选编译通过 | 1/5 | 3/5 |
| 实际运行中三次修复内编译通过 | 5/5 | 4/5 |
| 仅成功样本的平均修复次数 | 1.80 | 0.25 |
| 格式失败候选次数 | 1 | 0 |
| 原始记录中的 QL 失败候选次数 | 8 | 2 |
| 原始记录中的基础设施失败 | 0 | 1 |
| 样本生成总耗时（分钟，不含批次预检） | 18.05 | 24.12 |
| 模型请求次数 | 14 | 14 |
| 模型主动工具调用次数 | 0 | 19 |
| 总 token | 503,902 | 696,462 |

**必须保留的限制：**B 的移位用例在 `attempt_1/query.qll:10-15` 中用 `getAQlClass()` / `getAPrimaryQlClass()` 定义 `ShiftOperation`，编译器报告非单调递归。九条错误中的两条定位到生成文件，七条定位到标准库；当前 `classify_compile` 只接受全部定位在生成双文件中的错误为 QL 失败，因此将该轮归为基础设施失败，处理两个候选后停止，剩余两次修复未使用。这不是 API 超时、MCP 故障或 pack 缺失，不能写成 B 已用尽四候选预算仍失败。

原始分类、候选和回执未回写或重标，自动表保持 QL 失败 2 次、基础设施失败 1 次；诊断复核需额外说明其中 1 次是被误分类的查询递归错误。B 的平均修复次数只使用四个成功样本，不能忽略提前中止样本而宣称无条件优势。当前不修改冻结代码；后续应以混合标准库/本地定位的递归错误作为回归用例修复分类，再另行声明新实验，不补跑替换本轮结果。

逐例成功候选索引（0 为首次）为：A `3, 3, 1, 2, 0`；B `0, 0, 1, 0, 未通过`。B 的内联汇编与清零用例没有主动调用模型工具，移位用例则在七次工具调用后仍因上述问题停止。因此不能把所有首轮差异归因于工具检索。B 的耗时为 A 的 1.34 倍、token 为 1.38 倍；五对单次观察仅支持描述性比较，不证明 MCP 的普遍收益、接口更换的因果效果或语义检测准确率。

本轮二十八次模型响应均完成、usage 完整且加总一致，十二个 MCP 会话均记录关闭，MCP 故障数为零。实验前后新二十项和旧十七项冻结控制均匹配，历史六批元数据及原汇总哈希不变。离线检查通过八十三项，十一项真实工具集成未启用；两组实际批次另有真实 CLI 预检和 B 的 MCP/LSP 调用记录。

## 8. 四步实施与最小验收

| 步骤 | 工作与产物 | 验收 |
|---|---|---|
| E0 环境 | 建目录、安装固定工具、pack、模板、记录版本，完成必要 MCP 补丁 | 手写双文件编译通过；MCP 正确/错误诊断和 API 查询可用 |
| E1 CLI 闭环 | 重写四个功能文件和入口，先 mcp off | JSON/QL 错误能修复，四候选封顶，基础设施错误不修代码 |
| E2 MCP 接入 | 只读候选独立会话、有限工具调用、完整反馈 | 不读到上一候选诊断；MCP 故障有记录，不暗中换组 |
| E3 正式实验 | 固定 spec/prompt，跑 A/B，补 smoke 检查和汇总 | 成功 pack 可独立复验；报告真实结果和成本，输入及旧查询未改 |

仅添加覆盖核心风险的 unittest：spec/JSON 解析；首次成功、修复成功和四轮失败；格式错误占用预算；CLI/模型/MCP 超时不误当 QL 错误；两组开关与日志。用假的模型和工具响应即可，不让单测调用付费 API。

真实工具检查保留三项：正确/错误手写双文件；错误候选切换到新正确候选后无旧诊断；C/C++ smoke DB 非空且模板可运行。暂不建设全面安全测试矩阵、安装 CI 或兼容多平台。

完成标准是核心闭环正确、实验可复核，而非生产级稳健性。语义准确率、正负例、spec 改进和 Refiner 另行规划；通过编译不能写成“已证明查询有效检测 CISB”。

## 9. 参考与验证边界

工具来源和固定版本沿用下列参考。E0 探针、E1/E2/E3 本地验证与真实 E2 pilot 的回执见 `README.md`；E3 正式实验通过率不能从模拟模型测试或单个调试样本推断。

- [R1] QLCoder 固定参考：`https://github.com/neuralprogram/qlcoder/tree/b879ac2c90aac7fcc0b2e73caf117c2a12bf20e9`，只借鉴方法，不复现整个论文环境。
- [R2] MCP 来源：`https://github.com/neuralprogram/codeql-lsp-mcp/tree/a33ea82bba156dc8352a0ecd85baff34cbb950ed`；README/package.json 为部署依据，`src/codeql-lsp-client.ts`、`src/index.ts` 为诊断缺口和工具接口依据。
- [R3] CodeQL CLI：`https://docs.github.com/en/code-security/codeql-cli/codeql-cli-manual/query-compile`；compile、pack install/ci、language-server、database create、query run、bqrs decode 参数已在上一轮用本地固定版本帮助核对。
- [R4] MCP Python SDK 接口参考：`https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0`。
- [R5] 固定 bundle 发布：`https://github.com/github/codeql-action/releases/tag/codeql-bundle-v2.24.3`。
- [R6] DeepSeek Responses 协议：`https://api-docs.deepseek.com/zh-cn/guides/responses_api`；请求/输出字段：`https://api-docs.deepseek.com/zh-cn/api/create-response`。网关兼容性仍以 E0 新协议探针实测为准。
