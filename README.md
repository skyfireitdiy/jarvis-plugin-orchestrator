# orchestrator —— 通用编排器插件

把一批 unit（每个自带完整提示词或 shell 命令）按 plan 编排执行的 Jarvis 插件。
移植自 [knowledge-toolkit](https://github.com/skyfire/knowledge-toolkit) 的
`generic` 包（与领域无关的执行引擎），零第三方依赖。

插件安装后通过 **`orchestrator` CLI 命令**使用（由安装 hook 用 uv 安装），
并提供 **规则**（`rules/orchestrator.md`）指导 Agent 何时使用、怎么写 plan。

## 功能

- **plan → result**：输入 plan（unit 清单），输出每个 unit 的执行结果
- **DAG 依赖**：`depends_on` 前置 unit 全部成功后才会派发
- **并发限流**：`concurrency_key` / `concurrency_limit` 按类别限流
- **batch 打包**：`batch_group` 相同的相邻 unit 合并成一个批量 unit 交给同一 Agent
- **超时 / 熔断**：单 unit 超时自动 kill；连续快速失败触发熔断中止
- **失败留痕**：失败 unit 记录原因（`.generic/retry/`），不内部重试，交由上层处理

## 目录结构

```text
orchestrator/
├── config.yaml            # 插件配置（rules_load_dirs + hooks 声明）
├── pyproject.toml         # Python 包定义（orchestrator CLI 入口）
├── generic/               # 通用编排引擎包（零第三方依赖）
│   ├── models.py          # plan / result 数据契约 + 校验 + 拓扑排序
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
    └── test_generic_engine.py   # 引擎测试（20 个用例）
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
- **Python >= 3.10**（仅标准库，无需额外安装）

## CLI 用法：orchestrator

`orchestrator` 命令四个子命令：

| 子命令 | 说明 |
|---|---|
| `validate` | 校验 plan JSON 合法性（不执行） |
| `run --dry-run` | 打印 DAG 拓扑，不派发 |
| `run` | 按 plan 派发执行，输出 result |
| `show` | 读 result JSON 打印摘要 |

### plan 格式

plan 是 JSON 文件，`units` 数组，每个 unit：

```json
{
  "run_id": "可选运行标识",
  "units": [
    {
      "id": "唯一标识（必填）",
      "task_desc": "完整提示词（必填）",
      "command": "要执行的 shell 命令（run 动作需要）",
      "status_file": "执行方写 status 的路径（run 动作需要）",
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

### 示例

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

```bash
# 校验
orchestrator validate --plan plan.json
# 打印 DAG
orchestrator run --plan plan.json --work-dir /tmp/orch --dry-run
# 执行（work_dir 缺省取 plan 所在目录）
orchestrator run --plan plan.json --work-dir /tmp/orch
# 读结果
orchestrator show --result /tmp/orch/result.json
```

## 规则

`rules/orchestrator.md` 由 Jarvis 自动发现并加载，指导 Agent：

- 何时用 `orchestrator` CLI（批量派发 / 按 plan 编排 / 并发执行）
- plan JSON 格式与字段说明
- `run` 的 `command` + `status_file` 约定
- validate → dry_run → run → show 的标准流程

## 作为 Python API 复用

`generic` 包可被其他插件 / Agent 直接 import：

```python
from generic import Engine, EngineOptions, PlanSpec, UnitSpec

engine = Engine(work_dir="/tmp/orch", options=EngineOptions(max_concurrency=4))
run = engine.run(PlanSpec(units=[UnitSpec(id="a", task_desc="...")]))
```

引擎不构造提示词、不判定业务结果——launcher / collector 由调用方注入。
