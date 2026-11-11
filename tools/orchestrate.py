"""
orchestrate 工具：通用编排器（generic engine）的 Agent 侧入口。

把一批 unit（每个自带完整提示词或 shell 命令）按 plan 编排执行：
- validate：校验 plan JSON 合法性（不执行）
- dry_run：只打印 DAG 拓扑，不派发
- run：按 plan 派发 unit 到 tmux pane 并发执行（DAG 依赖 / 限流 / batch 打包 /
  超时 / 熔断 / 失败留痕），输出 result
- show：读 result JSON 打印摘要

run 动作复用 generic.cli 的通用约定：每个 unit 需带 `command`（要执行的 shell
命令）与 `status_file`（执行方写 status 的路径，{"status": "completed"} 视为成功）。

返回:
- success (bool)
- stdout (str)
- stderr (str)
"""
import os
import sys
from typing import Any
from typing import Dict


def _load_generic():
    """把插件根目录加入 sys.path 后导入 generic 包。

    tools/ 由 ToolRegistry 临时加入 sys.path，但 generic 包位于插件根目录，
    需手动把根目录加入 sys.path 才能 import。模块名固定以便复用缓存。
    """
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)
    import generic  # noqa: F401  (确保包已加载)
    import generic.cli as cli
    return cli


class OrchestrateTool:
    # 文件名必须与工具名一致，便于注册表自动加载
    name = "orchestrate"
    description = (
        "通用编排器：把一批 unit（每个自带完整提示词或 shell 命令）按 plan 编排执行。"
        "支持 validate（校验 plan）/ dry_run（打印 DAG 不派发）/ run（并发派发执行）/ "
        "show（读 result 摘要）四个动作。plan 是 JSON 文件，含 units 数组，每个 unit 有 "
        "id、task_desc（或 command+status_file）、可选 depends_on（DAG 依赖）、"
        "concurrency_key/limit（限流）、batch_group（打包）、timeout。"
        "run 动作要求 unit 带 command（shell 命令）与 status_file（执行方写 "
        "{\"status\": \"completed\"} 的路径）。适合「批量派发任务」「按 plan 执行」等场景。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["validate", "dry_run", "run", "show"],
                "description": "要执行的动作：validate 校验 plan；dry_run 打印 DAG 不派发；"
                               "run 执行 plan；show 读 result 摘要",
            },
            "plan": {
                "type": "string",
                "description": "plan JSON 文件路径（validate/dry_run/run 需要）",
            },
            "work_dir": {
                "type": "string",
                "description": "工作目录（run 需要，重试记录/产物落这里；缺省取 plan 所在目录）",
            },
            "result": {
                "type": "string",
                "description": "result JSON 输出路径（run 可选，缺省 <work_dir>/result.json）",
            },
            "max_concurrency": {
                "type": "integer",
                "description": "全局并发上限（run 可选，默认 4）",
            },
            "poll_interval": {
                "type": "number",
                "description": "轮询间隔秒数（run 可选，默认 2.0）",
            },
            "timeout": {
                "type": "number",
                "description": "单 unit 超时秒数（run 可选，默认 3600）",
            },
            "tmux_session": {
                "type": "string",
                "description": "tmux 会话名（run 可选，默认 generic-orchestrate）",
            },
            "tmux_bin": {
                "type": "string",
                "description": "tmux 可执行文件（run 可选，默认 tmux）",
            },
        },
        "required": ["action"],
    }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            action = str(args.get("action") or "").strip()
            if action not in ("validate", "dry_run", "run", "show"):
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": f"未知 action：{action!r}（可选 validate/dry_run/run/show）",
                }
            cli = _load_generic()
            if action == "show":
                return self._show(cli, args)
            plan = args.get("plan")
            if not plan:
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": f"action={action} 需要 plan 参数",
                }
            if action == "validate":
                return self._validate(cli, plan)
            if action == "dry_run":
                return self._dry_run(cli, plan)
            return self._run(cli, plan, args)
        except Exception as exc:  # noqa: BLE001 - 工具边界，任何异常都转成可读输出
            return {"success": False, "stdout": "", "stderr": f"orchestrate 执行异常：{exc}"}

    # ------------------------------------------------------------------ 动作
    def _validate(self, cli, plan: str) -> Dict[str, Any]:
        try:
            p = cli.load_plan(plan)
        except cli.PlanError as exc:
            return {"success": False, "stdout": "", "stderr": f"✗ plan 非法：{exc}"}
        return {
            "success": True,
            "stdout": f"✓ plan 合法：{len(p.units)} 个 unit（run_id={p.run_id or '-'}）",
            "stderr": "",
        }

    def _dry_run(self, cli, plan: str) -> Dict[str, Any]:
        try:
            p = cli.load_plan(plan)
        except cli.PlanError as exc:
            return {"success": False, "stdout": "", "stderr": f"✗ plan 非法：{exc}"}
        lines = []
        ordered = p.topo_order()
        lines.append(f"=== plan（run_id={p.run_id or '-'}）共 {len(ordered)} 个 unit ===")
        for i, u in enumerate(ordered, 1):
            deps = f" ← {', '.join(u.depends_on)}" if u.depends_on else ""
            grp = f" [group={u.batch_group}]" if u.batch_group else ""
            key = f" key={u.concurrency_key}"
            lim = f" limit={u.concurrency_limit}" if u.concurrency_limit else ""
            to = f" timeout={u.timeout}" if u.timeout else ""
            lines.append(f"  {i:3d}. {u.id}{deps}{grp}{key}{lim}{to}")
        return {"success": True, "stdout": "\n".join(lines), "stderr": ""}

    def _run(self, cli, plan: str, args: Dict[str, Any]) -> Dict[str, Any]:
        from pathlib import Path

        try:
            p = cli.load_plan(plan)
        except cli.PlanError as exc:
            return {"success": False, "stdout": "", "stderr": f"✗ plan 非法：{exc}"}

        work_dir = Path(args.get("work_dir") or Path(plan).resolve().parent)
        work_dir.mkdir(parents=True, exist_ok=True)
        opts = cli.EngineOptions(
            max_concurrency=int(args.get("max_concurrency") or 4),
            poll_interval=float(args.get("poll_interval") or 2.0),
            unit_timeout=float(args.get("timeout") or 3600.0),
            tmux_session=str(args.get("tmux_session") or "generic-orchestrate"),
            tmux_bin=str(args.get("tmux_bin") or "tmux"),
        )
        engine = cli.Engine(work_dir=work_dir, options=opts, log=lambda m: None)
        launcher, collector = cli._make_launcher_collector(engine)
        engine._launcher = launcher  # noqa: SLF001（复用 CLI 内部装配）
        engine._collector = collector  # noqa: SLF001
        result_path = args.get("result") or str(work_dir / "result.json")
        run = engine.run(p, result_path=result_path)

        lines = [
            f"=== 完成：{len(run.results)} 个 unit，失败 {len(run.failed)}，"
            f"result={result_path} ===",
        ]
        for r in run.results:
            mark = "✓" if r.ok else "✗"
            lines.append(f"  {mark} {r.id}（{r.status or '-'}，{r.attempts} 次）")
        if run.failed:
            lines.append("失败明细：")
            for r in run.failed:
                lines.append(f"  ✗ {r.id}：{r.detail[:300]}")
        return {
            "success": run.ok,
            "stdout": "\n".join(lines),
            "stderr": "" if run.ok else f"{len(run.failed)} 个 unit 失败",
        }

    def _show(self, cli, args: Dict[str, Any]) -> Dict[str, Any]:
        result = args.get("result")
        if not result:
            return {
                "success": False,
                "stdout": "",
                "stderr": "action=show 需要 result 参数",
            }
        res = cli.load_result(result)
        if res is None:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"✗ 读不到 result：{result}",
            }
        lines = [
            f"run_id={res.run_id or '-'} ok={res.ok} "
            f"共 {len(res.results)}，失败 {len(res.failed)}",
        ]
        for r in res.results:
            mark = "✓" if r.ok else "✗"
            lines.append(f"  {mark} {r.id}（{r.status or '-'}，{r.attempts} 次）")
        return {
            "success": res.ok,
            "stdout": "\n".join(lines),
            "stderr": "" if res.ok else f"{len(res.failed)} 个 unit 失败",
        }
