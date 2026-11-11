"""通用编排引擎：把 plan 跑成 result。

职责边界（严格）：
- **输入**：PlanSpec（unit 清单，每个 unit 自带完整提示词）
- **输出**：RunResult（每个 unit 的执行结果），可落盘 result.json
- **不做**：不拼提示词、不读领域状态、不认识 stage、不做领域校验

流程：
    1. plan.validate() + topo_order()（依赖在前）
    2. 按 batch_group 把相邻同组 unit 打包成一个「批量 unit」
    3. 逐层调度：某 unit 的 depends_on 全部成功后才进入可派发集合
    4. 交 Scheduler 并发执行（限流 / 超时 / 熔断 / 重试）
    5. 汇总成 RunResult，可选落盘

批量 unit 的语义：batch_group 相同的 unit 合并为一个 UnitSpec，其 id 形如
`__batch__:<id1>||<id2>`，task_desc 由各成员的 task_desc 拼接（由调用方在
plan 生成阶段就写好合并提示词更佳；引擎只做机械合并兜底）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from .models import PlanSpec, RunResult, UnitResult, UnitSpec, save_result
from .scheduler import BatchResult, Scheduler, SchedulerOptions
from .tmux_driver import TmuxDriver

BATCH_PREFIX = "__batch__:"
BATCH_SEP = "||"


def is_batch_unit(unit_id: str) -> bool:
    return unit_id.startswith(BATCH_PREFIX)


def batch_members(unit_id: str) -> List[str]:
    if not is_batch_unit(unit_id):
        return [unit_id]
    return [m for m in unit_id[len(BATCH_PREFIX):].split(BATCH_SEP) if m]


def make_batch_id(ids: List[str]) -> str:
    return BATCH_PREFIX + BATCH_SEP.join(ids)


@dataclass
class EngineOptions:
    """引擎级参数（与领域无关）。"""

    max_concurrency: int = 4
    poll_interval: float = 2.0
    unit_timeout: float = 3600.0
    max_retries: int = 2
    tmux_session: str = "generic-orchestrate"
    tmux_bin: str = "tmux"
    quick_fail_seconds: float = 5.0
    quick_fail_limit: int = 3


class Engine:
    """通用执行引擎。

    launcher / collector 可注入（测试用 FakeTmux），缺省用 TmuxDriver + 通用
    约定（status 文件里写 {"status": "completed"} 视为成功）。
    """

    def __init__(
        self,
        work_dir: str | Path,
        options: Optional[EngineOptions] = None,
        tmux: Optional[TmuxDriver] = None,
        launcher: Optional[Callable[[UnitSpec, int], str]] = None,
        collector: Optional[Callable[[UnitSpec, str], UnitResult]] = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.opts = options or EngineOptions()
        self.tmux = tmux if tmux is not None else TmuxDriver(
            self.opts.tmux_session,
            tmux_bin=TmuxDriver.resolve_bin(self.opts.tmux_bin) or self.opts.tmux_bin,
        )
        self._launcher = launcher
        self._collector = collector
        self.sleep = sleep
        self.log = log or (lambda msg: None)

    # ------------------------------------------------------------------ 派发/收集
    def _launch(self, unit: UnitSpec, attempt: int) -> str:
        if self._launcher is not None:
            return self._launcher(unit, attempt)
        raise NotImplementedError(
            "未注入 launcher：通用引擎不构造命令，必须由调用方提供 "
            "launcher(unit, attempt) -> pane_id（或注入 tmux + 自定义 launcher）"
        )

    def _collect(self, unit: UnitSpec, pane: str) -> UnitResult:
        if self._collector is not None:
            return self._collector(unit, pane)
        raise NotImplementedError(
            "未注入 collector：通用引擎不判定结果，必须由调用方提供 "
            "collector(unit, pane) -> UnitResult"
        )

    def _is_dead(self, pane: str) -> bool:
        return self.tmux.is_dead(pane)

    def _kill(self, pane: str) -> None:
        self.tmux.kill(pane)

    def _capture(self, pane: str) -> str:
        """抓 pane 尾部（超时诊断用）；未注入 tmux 时返回空串。"""
        if self.tmux is None:
            return ""
        try:
            return self.tmux.capture(pane) or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------ 打包
    def _pack(self, units: List[UnitSpec]) -> List[UnitSpec]:
        """按 batch_group 把相邻同组 unit 打包。

        只有 batch_group 非空且相同的连续 unit 才合并；无 batch_group 的 unit
        原样保留（逐 unit 派发）。合并后 id 为 __batch__:id1||id2，task_desc 由
        各成员 task_desc 拼接（调用方最好在 plan 阶段就写好合并提示词）。
        """
        out: List[UnitSpec] = []
        i = 0
        n = len(units)
        while i < n:
            u = units[i]
            grp = u.batch_group
            if not grp:
                out.append(u)
                i += 1
                continue
            members = [u]
            j = i + 1
            while j < n and units[j].batch_group == grp:
                members.append(units[j])
                j += 1
            if len(members) == 1:
                out.append(u)
                i = j
                continue
            ids = [m.id for m in members]
            merged = UnitSpec(
                id=make_batch_id(ids),
                task_desc="\n\n".join(
                    f"===== 子任务 {k}/{len(members)}：{m.id} =====\n{m.task_desc}"
                    for k, m in enumerate(members, 1)
                ),
                background="\n".join(m.background for m in members if m.background),
                additional_info=members[0].additional_info,
                timeout=max(
                    (m.timeout for m in members if m.timeout is not None), default=None
                ),
                concurrency_key=members[0].concurrency_key,
                concurrency_limit=members[0].concurrency_limit,
                depends_on=sorted({d for m in members for d in m.depends_on} - set(ids)),
                batch_group=grp,
                command=None,
            )
            out.append(merged)
            i = j
        return out

    # ------------------------------------------------------------------ 主流程
    def run(self, plan: PlanSpec, result_path: Optional[str | Path] = None) -> RunResult:
        """执行 plan，返回 RunResult；给了 result_path 就落盘。"""
        plan.validate()
        ordered = plan.topo_order()
        packed = self._pack(ordered)

        run = RunResult(run_id=plan.run_id, started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        # 已完成 id 集合（用于 depends_on 判定）；批量 unit 的成员 id 也算完成
        done_ids: set = set()
        remaining = list(packed)
        # 按依赖分层：反复扫，把「依赖全完成」的 unit 挑出来跑一批
        while remaining:
            ready = [
                u for u in remaining
                if all(d in done_ids for d in u.depends_on)
            ]
            if not ready:
                # 理论上 validate 已排除环；防御：把剩余全标失败，避免死循环
                for u in remaining:
                    run.results.append(
                        UnitResult(
                            id=u.id,
                            ok=False,
                            status="unsatisfiable_dependency",
                            detail=f"依赖无法满足：{u.depends_on}",
                            retryable=False,
                        )
                    )
                break
            for u in ready:
                remaining.remove(u)
            batch = self._run_batch(ready)
            run.results.extend(batch.results)
            for r in batch.results:
                if r.ok:
                    for mid in batch_members(r.id):
                        done_ids.add(mid)

        run.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        if result_path:
            save_result(result_path, run)
        return run

    def _run_batch(self, units: List[UnitSpec]) -> BatchResult:
        opts = SchedulerOptions(
            max_concurrency=self.opts.max_concurrency,
            poll_interval=self.opts.poll_interval,
            unit_timeout=self.opts.unit_timeout,
            max_retries=self.opts.max_retries,
            quick_fail_seconds=self.opts.quick_fail_seconds,
            quick_fail_limit=self.opts.quick_fail_limit,
        )
        sched = Scheduler(
            work_dir=self.work_dir,
            launcher=self._launch,
            collector=self._collect,
            is_dead=self._is_dead,
            kill=self._kill,
            capture=self._capture,
            options=opts,
            sleep=self.sleep,
            log=self.log,
        )
        return sched.run(units)
