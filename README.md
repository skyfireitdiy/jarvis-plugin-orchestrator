# orchestrator —— 通用编排器插件

把一批 unit 按 plan 编排执行的 Jarvis 插件。每个 unit 二选一：

- **Agent 模式**（默认）：unit 带 `task_desc`（完整提示词）→ 派发给一个
  Jarvis Agent（`jvs --task-file`），Agent 完成后写 status 文件。
- **shell 模式**（fallback）：unit 带 `command`（shell 命令）→ 在 tmux pane
  里直接执行该命令。

移植自 [knowledge-toolkit](https://github.com/skyfire/knowledge-toolkit) 的
`generic` 包（与领域无关的执行引擎）+ Agent 派发层（`dispatcher`），零第三方依赖。
插件安装后通过 **`orchestrator` CLI 命令**使用（由安装 hook 用 uv 安装），
并提供 **规则**（`rules/orchestrator.md`）指导 Agent 何时使用、怎么写 plan。

## 功能

- **plan → result**：输入 plan（unit 清单），输出每个 unit 的执行结果
- **Agent 派发**：unit 带 `task_desc` 时派发 Jarvis Agent（`jvs --task-file`），
  `task_desc` 以 `'<Pin>'` 开头固定关键约束，`background`/`additional_info`
  给 Agent 背景与约束
- **shell fallback**：unit 带 `command` 时直接在 tmux pane 执行该命令
- **DAG 依赖**：`depends_on` 前置 unit 全部成功后才会派发
- **并发限流**：`concurrency_key` / `concurrency_limit` 按类别限流
- **batch 打包**：`batch_group` 相同的相邻 unit 合并成一个批量 unit 交给同一 Agent
- **超时 / 熔断**：单 unit 超时自动 kill；连续快速失败触发熔断中止
- **失败留痕**：失败 unit 记录原因（`.generic/retry/`），不内部重试，交由上层处理
- **stage 防串台**：status 文件带 stage 且与 unit.stage 不一致时视为无效

## 目录结构

```text
orchestrator/
├── config.yaml            # 插件配置（rules_load_dirs + hooks 声明）
├── pyproject.toml         # Python 包定义（orchestrator CLI 入口）
├── generic/               # 通用编排引擎包（零第三方依赖）
│   ├── models.py          # plan / result 数据契约 + 校验 + 拓扑排序
│   ├── dispatcher.py      # Agent 派发层（task JSON + jvs 命令 + bash 脚本）
│   ├── engine.py          # Engine 主流程（打包 / 分层调度）
│   ├── scheduler.py       # 并发调度（限流 / 超时 / 熔断 / 失败留痕）
│   ├── tmux_driver.py     # tmux 会话 / pane 操作封装
│   ├── cli.py             # 通用 CLI（orchestrator 命令）
│   └── __init__.py
├── rules/
│   └── orchestrator.md    # 规则（何时用 CLI、plan 格式、run 约定）
├── scripts/
│   └── install.sh         # 安装 hook（检测 tmux + uv 安装 CLI）
└── tests/
    └── test_generic_engine.py   # 引擎 + Agent 派发测试（27 个用例）
```

## 安装 / 加载

插件目录放入 `~/.jarvis/plugins/` 后，Jarvis 读取 `config.yaml` 自动加载规则，
并在 **post-install 钩子**执行 `scripts/install.sh`：

1. 检测 **tmux**（并发 pane 派发的硬依赖），缺失则报错退出
2. 检测 **uv**（安装 CLI 的工具），缺失则报错退出
3. 用 `uv tool install` 安装 `orchestrator` CLI（`~/.local/bin/orchestrator`）

也可手动执行安装 hook：

```bash
bash ~/.jarvis/plugins/orchestrator/scripts/install.sh
```

硬依赖：

- **tmux**（并发 pane 派发）
- **uv**（安装 CLI）
- **jvs**（Agent 模式派发 Jarvis Agent，`--task-file`）
- **Python >= 3.10**（仅标准库，无需额外安装）

## CLI 用法：orchestrator

`orchestrator` 命令四个子命令：

| 子命令 | 说明 |
|---|---|
| `validate` | 校验 plan JSON 合法性（不执行） |
| `run --dry-run` | 打印 DAG 拓扑，不派发 |
| `run` | 按 plan 派发执行，输出 result |
| `show` | 读 result JSON 打印摘要 |

`run` 可选参数：

| 参数 | 说明 |
|---|---|
| `--jvs-bin` | jvs 可执行文件路径（Agent 模式，默认 `jvs`） |
| `--llm-group` | Agent 模式使用的模型组（`-g`） |
| `--stage` | 所有 unit 的默认阶段标识（Agent 模式） |

### plan 格式

plan 是 JSON 文件，`units` 数组，每个 unit：

```json
{
  "run_id": "可选运行标识",
  "units": [
    {
      "id": "唯一标识（必填）",
      "task_desc": "完整提示词（Agent 模式必填，作为 Agent 的任务）",
      "background": "可选背景信息",
      "additional_info": "可选附加信息",
      "stage": "可选阶段标识（写入 status 文件，防串台）",
      "command": "可选 shell 命令（有则走 shell 模式，否则走 Agent 模式）",
      "status_file": "可选：执行方写 status 的路径（缺省 Agent 模式自动生成）",
      "depends_on": ["other-id"],
      "concurrency_key": "default",
      "concurrency_limit": 4,
      "batch_group": "lcm",
      "timeout": 3600
    }
  ]
}
```

`run` 的判定约定：unit 执行后读 `status_file`，其中
`{"status": "completed"}` 视为成功。

### 示例：Agent 模式

```json
{
  "units": [
    {
      "id": "a",
      "task_desc": "写一个 hello world C 程序，编译并运行，输出到 out.txt",
      "background": "工作目录是 /tmp/orch",
      "stage": "stage1"
    },
    {
      "id": "b",
      "task_desc": "写一个 Python 脚本统计 out.txt 的行数",
      "depends_on": ["a"]
    }
  ]
}
```

```bash
# 校验
orchestrator validate --plan plan.json
# 打印 DAG
orchestrator run --plan plan.json --work-dir /tmp/orch --dry-run
# 执行（派发 jvs Agent）
orchestrator run --plan plan.json --work-dir /tmp/orch
# 读结果
orchestrator show --result /tmp/orch/result.json
```

### 示例：shell 模式

```json
{
  "units": [
    {
      "id": "a",
      "command": "echo hello > a.txt && echo '{\"status\": \"completed\"}' > a.status",
      "status_file": "a.status"
    },
    {
      "id": "b",
      "command": "echo world > b.txt && echo '{\"status\": \"completed\"}' > b.status",
      "status_file": "b.status",
      "depends_on": ["a"]
    }
  ]
}
```

## 规则

`rules/orchestrator.md` 由 Jarvis 自动发现并加载，指导 Agent：

- 何时用 `orchestrator` CLI（批量派发 / 按 plan 编排 / 并发执行）
- plan JSON 格式与字段说明
- Agent 模式（`task_desc` → jvs Agent）与 shell 模式（`command`）的用法
- validate → dry_run → run → show 的标准流程

## 作为 Python API 复用

`generic` 包可被其他插件 / Agent 直接 import：

```python
from generic import Engine, EngineOptions, PlanSpec, UnitSpec

engine = Engine(work_dir="/tmp/orch", options=EngineOptions(max_concurrency=4))
run = engine.run(PlanSpec(units=[UnitSpec(id="a", task_desc="...")]))
```

引擎不构造提示词、不判定业务结果——launcher / collector 由调用方注入。
Agent 派发层也可单独使用：

```python
from generic import UnitSpec, build_jvs_command

u = UnitSpec(id="a", task_desc="写 hello world")
cmd = build_jvs_command(u, "/tmp/orch", jvs_bin="jvs", llm_group="default")
# cmd == "bash /tmp/orch/.generic/scripts/a.sh"（脚本内含 jvs 命令）
```
