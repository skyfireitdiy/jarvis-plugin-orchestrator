"""通用编排器 CLI。

用法：
    orchestrator run --plan plan.json --work-dir <dir>
    orchestrator run --plan plan.json --dry-run

注意：本 CLI 只做「plan → result」的机械执行。它不构造任何提示词、不判定
业务成功与否——这两件事必须由调用方通过 launcher / collector 注入。因此
CLI 的 run 子命令要求 plan 里每个 unit 带 `command` 字段（要执行的 shell 命令）
与 `status_file` 字段（执行方写 status 的路径），CLI 用它们做通用派发与判定：

    - launcher: 在 tmux pane 里执行 unit.command
    - collector: 读 unit.status_file，{"status": "completed"} 视为成功

若需要更复杂的派发/判定（例如带重试提示词注入），请直接使用 Engine API。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .engine import Engine, EngineOptions
from .models import PlanError, UnitResult, UnitSpec, load_plan, load_result


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orchestrator",
        description="通用编排器：按 plan 派发 unit，输出 result",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="执行 plan")
    r.add_argument("--plan", required=True, help="plan JSON 文件路径")
    r.add_argument("--work-dir", required=True, help="工作目录（重试记录/产物落这里）")
    r.add_argument("--result", default=None, help="result JSON 输出路径（缺省 <work-dir>/result.json）")
    r.add_argument("--dry-run", action="store_true", help="只打印 DAG，不派发")
    r.add_argument("--max-concurrency", type=int, default=4)
    r.add_argument("--poll-interval", type=float, default=2.0)
    r.add_argument("--timeout", type=float, default=3600.0, help="单 unit 超时秒数")
    r.add_argument("--max-retries", type=int, default=2)
    r.add_argument("--tmux-session", default="generic-orchestrate")
    r.add_argument("--tmux-bin", default="tmux")

    v = sub.add_parser("validate", help="只校验 plan，不执行")
    v.add_argument("--plan", required=True)

    s = sub.add_parser("show", help="打印 result 摘要")
    s.add_argument("--result", required=True)
    return p


def _print_dag(plan) -> None:
    ordered = plan.topo_order()
    print(f"=== plan（run_id={plan.run_id or '-'}）共 {len(ordered)} 个 unit ===")
    for i, u in enumerate(ordered, 1):
        deps = f" ← {', '.join(u.depends_on)}" if u.depends_on else ""
        grp = f" [group={u.batch_group}]" if u.batch_group else ""
        key = f" key={u.concurrency_key}"
        lim = f" limit={u.concurrency_limit}" if u.concurrency_limit else ""
        to = f" timeout={u.timeout}" if u.timeout else ""
        print(f"  {i:3d}. {u.id}{deps}{grp}{key}{lim}{to}")


def _make_launcher_collector(engine: Engine):
    """CLI 的通用 launcher/collector：靠 unit 的 command / status_file 字段。"""

    def launcher(unit: UnitSpec, attempt: int) -> str:
        cmd = getattr(unit, "command", None)
        if not cmd:
            raise ValueError(
                f"unit「{unit.id}」缺 command 字段（CLI 模式要求 plan 里给出可执行命令）"
            )
        return engine.tmux.create_pane(cmd, cwd=str(engine.work_dir))

    def collector(unit: UnitSpec, pane: str) -> UnitResult:
        sf = unit.status_file
        status = None
        if sf:
            p = Path(sf)
            if p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        status = data.get("status")
                except (json.JSONDecodeError, OSError):
                    status = None
        if status == "completed":
            return UnitResult(id=unit.id, ok=True, status="completed")
        detail = ""
        try:
            detail = (engine.tmux.capture(pane) or "").strip()[-500:]
        except Exception:
            pass
        return UnitResult(
            id=unit.id,
            ok=False,
            status=str(status) if status else "no_status_file",
            detail=detail,
        )

    return launcher, collector


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "validate":
        try:
            plan = load_plan(args.plan)
        except PlanError as exc:
            print(f"✗ plan 非法：{exc}", file=sys.stderr)
            return 1
        print(f"✓ plan 合法：{len(plan.units)} 个 unit")
        return 0

    if args.cmd == "show":
        res = load_result(args.result)
        if res is None:
            print(f"✗ 读不到 result：{args.result}", file=sys.stderr)
            return 1
        print(f"run_id={res.run_id or '-'} ok={res.ok} "
              f"共 {len(res.results)}，失败 {len(res.failed)}")
        for r in res.results:
            mark = "✓" if r.ok else "✗"
            print(f"  {mark} {r.id}（{r.status or '-'}，{r.attempts} 次）")
        return 0 if res.ok else 1

    # run
    try:
        plan = load_plan(args.plan)
    except PlanError as exc:
        print(f"✗ plan 非法：{exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        _print_dag(plan)
        return 0

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    opts = EngineOptions(
        max_concurrency=args.max_concurrency,
        poll_interval=args.poll_interval,
        unit_timeout=args.timeout,
        max_retries=args.max_retries,
        tmux_session=args.tmux_session,
        tmux_bin=args.tmux_bin,
    )
    engine = Engine(work_dir=work_dir, options=opts, log=lambda m: print(m, flush=True))
    launcher, collector = _make_launcher_collector(engine)
    engine._launcher = launcher  # noqa: SLF001（CLI 内部装配）
    engine._collector = collector  # noqa: SLF001
    result_path = args.result or str(work_dir / "result.json")
    run = engine.run(plan, result_path=result_path)
    print(f"\n=== 完成：{len(run.results)} 个 unit，失败 {len(run.failed)}，result={result_path} ===")
    return 0 if run.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
