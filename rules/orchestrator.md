---
name: orchestrator
description: 当用户需要批量派发任务、按 plan 编排执行、编排多 unit 并发任务时触发。凡用户言及「编排」「批量派发任务」「按 plan 执行」「orchestrator」「并发执行任务」时触发。不触发：仅执行单个命令；仅讨论任务规划不涉及执行；使用其他编排系统。
license: MIT
---

# 通用编排器（orchestrator）规则

## 规则简介

通用编排器（generic engine）是与领域无关的批量任务执行引擎。输入 plan
（unit 清单，每个 unit 自带完整提示词或 shell 命令），按 DAG 依赖排序、
并发限流、batch 打包、超时/熔断/失败留痕，输出 result（每个 unit 的执行结果）。
本规则指导何时用 `orchestrator` CLI、如何写 plan、如何判定结果。

## 你必须遵守的原则

- **必须**：批量/并发派发任务时用 `orchestrator` CLI（validate/dry_run/run/show），
  不要自己写循环逐个执行。
- **必须**：`run` 动作要求每个 unit 带 `command`（要执行的 shell 命令）与
  `status_file`（执行方写 status 的路径，`{"status": "completed"}` 视为成功）。
- **必须**：先 `validate` 校验 plan、`dry_run` 打印 DAG 确认拓扑，再 `run`。
- **禁止**：在 plan 里让 unit 依赖不存在的 id（DAG 校验会失败）。
- **禁止**：用 `orchestrator` 做需要业务判定/提示词注入的复杂编排——它只做
  plan → result 的机械执行，复杂派发请直接用 generic 包 Engine API。

## 你必须执行的操作

### 1. 确认依赖

```bash
command -v tmux   # 必须存在（并发 pane 派发）
command -v uv     # 必须存在（安装 CLI）
command -v orchestrator   # CLI 已由插件安装 hook 用 uv 装好
```

### 2. 写 plan JSON

plan 是 JSON 文件，`units` 数组，每个 unit：

```json
{
  "units": [
    {
      "id": "唯一标识（必填）",
      "task_desc": "完整提示词（可选）",
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

### 3. 执行

```bash
# 校验 plan
orchestrator validate --plan plan.json
# 打印 DAG 拓扑（不派发）
orchestrator run --plan plan.json --work-dir /tmp/orch --dry-run
# 执行
orchestrator run --plan plan.json --work-dir /tmp/orch
# 读结果摘要
orchestrator show --result /tmp/orch/result.json
```

`run` 的判定约定：unit 执行后读 `status_file`，其中
`{"status": "completed"}` 视为成功；失败 unit 记录原因到
`<work-dir>/.generic/retry/`，不内部重试，交由上层处理。

## 实践指导

- plan 里相邻、`batch_group` 相同的 unit 会合并成一个批量 unit 交给同一执行方。
- 用 `concurrency_key` + `concurrency_limit` 按类别限流，避免打爆资源。
- `timeout` 超时的 unit 会被自动 kill；连续快速失败会触发熔断中止。
- 需要复杂派发/判定（如带重试提示词注入）时，改用 generic 包 Engine API：
  `from generic import Engine, EngineOptions, PlanSpec, UnitSpec`。

## 自检清单

- [ ] tmux / uv / orchestrator 命令均可用
- [ ] plan 已通过 `validate`，无 DAG 循环、无缺失依赖
- [ ] `dry_run` 打印的拓扑符合预期
- [ ] 每个 unit 的 `command` 会写 `status_file`（`{"status": "completed"}`）
- [ ] `run` 后 `show` 结果，失败 unit 有原因记录
