# 从 Spec 生成有效 QL：轻量研究实验计划

更新日期：2026-09-10。

定位：单机、串行、固定模型的小型研究原型，不建设通用 Agent 平台。推荐在 `~/qlcoder-cpp-lite/` 独立实施；本文只规定后续建设方案，不表示工具或代码已部署。

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
├── harness.py                # 生成、工具调用、检查、修复、结果保存
├── agent_backend.py          # 唯一模型后端和 tool-call 往返
├── codeql_tools.py            # MCP 会话、CLI compile/run
├── spec_io.py                 # 现有 Markdown spec 解析
├── config.json
├── requirements.txt          # 环境跑通后冻结 Python 包版本
├── prompt.md
├── specs/                    # 筛选后的只读 *_spec.md 快照
├── samples.txt               # 固定实验样本，每行一个 spec 相对路径
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

`agent_backend.py` 只实现一个 OpenAI-compatible Chat Completions 后端，保留 tool-call/result 往返和 usage。工具阶段不强制与工具响应冲突的 JSON 文本模式，最终输出交给 Harness 解析；不切换 API 类型、Agent CLI 或模型。[R6]

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

无效 JSON 时保留原始响应，不伪造候选文件。summary 记录成功轮次、编译/可选运行状态、失败原因、调用次数、耗时和 token；缺 usage 写 null，语义状态写 `not_evaluated`。smoke 输出放在该成功轮次下，按 C/C++ 区分。

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
- 总耗时、token、MCP 调用/失败次数，能取得时分列工具启动耗时。
- 启用 smoke 时，补充已编译候选的运行成功比例；两个数据库均成功才算通过，零结果合法。

无固定 90%/95% 验收线，也不以提升 10 个百分点作为 MCP 去留的硬条件。观察编译收益、错误变化和额外成本，小样本只报告差异及不确定性，允许“没有观察到收益”的研究结果。

### 7.3 一个入口

以下是实现目标，不是已有脚本。`samples.txt` 每行一个相对项目根的 spec 路径，单文件和批量共用同一函数；批量串行运行，失败样本写日志后继续。

```bash
cd "$HOME/qlcoder-cpp-lite"
# 开发时运行单个样本。
.venv/bin/python run.py --spec specs/12051b318b_spec.md --mcp on --out runs/debug
# 正式对照，rep1/rep2/rep3 使用不同结果目录。
.venv/bin/python run.py --samples samples.txt --mcp off --out runs/A-rep1
.venv/bin/python run.py --samples samples.txt --mcp on --out runs/B-rep1
# 可选：成功编译后执行固定的两个 smoke 数据库。
.venv/bin/python run.py --samples samples.txt --mcp on --smoke --out runs/B-smoke
```

默认读取项目根 `config.json`，默认 MCP on。`--smoke` 固定使用第 6 节两个数据库，无任意数据库列表或配置 profile。全体样本达到请求关卡时退出 0，否则非零，具体原因以 summary 为准。不实现 resume/skip-existing、并发队列或服务模式。

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

沿用上一轮已核对的本地 CodeQL 2.24.3 和上游源码信息，本轮只精简方案，不重新部署或宣称测试已运行。具体 Python/npm 锁、模型工具调用、MCP 补丁和实验通过率仍需 E0～E3 实测。

- [R1] QLCoder 固定参考：`https://github.com/neuralprogram/qlcoder/tree/b879ac2c90aac7fcc0b2e73caf117c2a12bf20e9`，只借鉴方法，不复现整个论文环境。
- [R2] MCP 来源：`https://github.com/neuralprogram/codeql-lsp-mcp/tree/a33ea82bba156dc8352a0ecd85baff34cbb950ed`；README/package.json 为部署依据，`src/codeql-lsp-client.ts`、`src/index.ts` 为诊断缺口和工具接口依据。
- [R3] CodeQL CLI：`https://docs.github.com/en/code-security/codeql-cli/codeql-cli-manual/query-compile`；compile、pack install/ci、language-server、database create、query run、bqrs decode 参数已在上一轮用本地固定版本帮助核对。
- [R4] MCP Python SDK 接口参考：`https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0`。
- [R5] 固定 bundle 发布：`https://github.com/github/codeql-action/releases/tag/codeql-bundle-v2.24.3`。
- [R6] 模型工具协议参考：`https://developers.openai.com/api/reference/cli/resources/chat`；不据此推断第三方服务一定兼容。
