---
name: orchestrator
description: 当用户需要批量派发任务、按 plan 编排执行、编排多 unit 并发任务时触发。凡用户言及「编排」「批量派发任务」「按 plan 执行」「orchestrator」「并发执行任务」时触发。不触发：仅执行单个命令；仅讨论任务规划不涉及执行；使用其他编排系统。
license: MIT
---
# 通用编排器（orchestrator）规则

## 规则简介

通用编排器（generic engine）是与领域无关的批量任务执行引擎。输入 plan
（unit 清单），按 DAG 依赖排序、并发限流、batch 打包、超时/熔断/失败留痕，
输出 result（每个 unit 的执行结果）。每个 unit 二选一：

- **Agent 模式**（默认）：unit 带 `task_desc`（完整提示词）→ 派发给一个
  Jarvis Agent（`jvs --task-file`），Agent 完成后写 status 文件。
- **shell 模式**（fallback）：unit 带 `command`（shell 命令）→ 在 tmux pane
  里直接执行该命令。

本规则指导何时用 `orchestrator` CLI、如何写 plan、如何判定结果。

## 你必须遵守的原则

- **必须**：批量/并发派发任务时用 `orchestrator` CLI（validate/dry_run/run/show），
  不要自己写循环逐个执行。
- **必须**：先 `validate` 校验 plan、`dry_run` 打印 DAG 确认拓扑，再 `run`。
- **必须**：Agent 模式的 unit 要写完整 `task_desc`（编排器不拼提示词），
  需要给 Agent 背景/约束时用 `background` / `additional_info` 字段。
- **禁止**：在 plan 里让 unit 依赖不存在的 id（DAG 校验会失败）。
- **禁止**：用 `orchestrator` 做需要业务判定/提示词注入的复杂编排——它只做
  plan → result 的机械执行，复杂派发请直接用 generic 包 Engine API。

## 你必须执行的操作

### 1. 确认依赖

```bash
command -v tmux   # 必须存在（并发 pane 派发）
command -v uv     # 必须存在（安装 CLI）
command -v orchestrator   # CLI 已由插件安装 hook 用 uv 装好
command -v jvs    # Agent 模式需要（Jarvis CLI，--task-file 派发）
```

### 2. 写 plan JSON

plan 是 JSON 文件，`units` 数组，每个 unit：

```json
{
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

### 3. 执行

```bash
# 校验 plan
orchestrator validate --plan plan.json
# 打印 DAG 拓扑（不派发）
orchestrator run --plan plan.json --work-dir /tmp/orch --dry-run
# 执行（默认后台化：命令立即返回，编排在后台继续跑，最后写 result.json）
orchestrator run --plan plan.json --work-dir /tmp/orch
# 指定 jvs 路径 / 模型组 / 默认阶段
orchestrator run --plan plan.json --work-dir /tmp/orch \
    --jvs-bin jvs --llm-group default --stage stage1
# 读结果摘要
orchestrator show --result /tmp/orch/result.json
```

> **后台化**：`orchestrator run` 默认在独立会话中后台执行编排，终端立即返回
> （无需任何额外参数，不要加 `--detach` 之类参数）。编排日志写入
> `<work-dir>/.generic/orchestrator.log`，完成后结果写入 `<work-dir>/result.json`，
> 用 `orchestrator show` 查看。仅 `--dry-run` 不后台化（需要立即打印 DAG）。

`run` 的判定约定：unit 执行后读 `status_file`，其中
`{"status": "completed"}` 视为成功；失败 unit 记录原因到
`<work-dir>/.generic/retry/`，不内部重试，交由上层处理。

## 实践指导

- **Agent 模式**：`task_desc` 是 Agent 的完整提示词，写清楚任务目标、范围、
  完成标准；`background`/`additional_info` 给 Agent 背景与约束。编排器把
  task_desc 以 `'<Pin>'` 开头传给 jvs（固定关键约束，长任务不被压缩丢失）。
- **shell 模式**：`command` 里的命令要自己写 status 文件
  （`{"status": "completed"}`）才会被判成功。
- plan 里相邻、`batch_group` 相同的 unit 会合并成一个批量 unit 交给同一执行方。
- 用 `concurrency_key` + `concurrency_limit` 按类别限流，避免打爆资源。
- `timeout` 超时的 unit 会被自动 kill；连续快速失败会触发熔断中止。
- 需要复杂派发/判定（如带重试提示词注入）时，改用 generic 包 Engine API：
  `from generic import Engine, EngineOptions, PlanSpec, UnitSpec`。

## 自检清单

- [ ] tmux / uv / orchestrator 命令均可用；Agent 模式还需 jvs 可用
- [ ] plan 已通过 `validate`，无 DAG 循环、无缺失依赖
- [ ] `dry_run` 打印的拓扑符合预期
- [ ] Agent 模式 unit 的 `task_desc` 完整（含完成标准）；shell 模式 unit 的
      `command` 会写 `status_file`（`{"status": "completed"}`）
- [ ] `run` 后 `show` 结果，失败 unit 有原因记录
