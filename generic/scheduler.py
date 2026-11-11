"""通用调度器：并发派发 unit、同类限流、超时、熔断、重试。

与领域无关：不认识 stage、不读 state；一切「怎么派发 / 怎么判结果」都通过
注入的回调完成：

    launcher(unit: UnitSpec, attempt: int) -> pane_id
        派发一个 unit，返回 pane 标识（tmux pane_id 或测试用假 id）。
        抛异常表示派发失败（环境问题，不可重派）。

    collector(unit: UnitSpec, pane_id: str) -> UnitResult
        pane 结束后判定结果（读 status 文件 / 抓 pane 输出等由调用方决定）。

    is_dead(pane_id) -> bool
        pane 内进程是否已退出。

    kill(pane_id) -> None
        关闭 pane（失败只记日志，不打断调度）。

重试记录持久化在 <work_dir>/.generic/retry/<safe_id>.json，跨轮次累计。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .models import UnitResult, UnitSpec


def safe_name(name: str) -> str:
    """把 unit id（可能含 / 空格 等）转成安全文件名。

    长度上限：批量 unit（batch_group 把多个 id 拼接）会远超文件系统 255 字节限制，
    直接 stat 会 OSError: File name too long。截断到 120 字符并追加 8 位哈希后缀，
    既保证文件名合法，又保证不同 unit 不撞名（同一 id 每次计算结果确定）。
    """
    out = []
    for ch in name:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out).strip("_") or "unit"
    if len(s) > 120:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        s = s[:110] + "-" + digest
    return s


@dataclass
class SchedulerOptions:
    """调度参数（引擎级，与领域无关）。"""

    max_concurrency: int = 4
    poll_interval: float = 2.0
    unit_timeout: float = 3600.0
    # 保留字段（兼容 CLI/配置），当前不驱动重试：unit 失败即判失败、不重派。
    max_retries: int = 2
    # 熔断：连续 N 个 unit 都在极短时间内失败 → 环境问题，中止本批。
    quick_fail_seconds: float = 5.0
    quick_fail_limit: int = 3


@dataclass
class BatchResult:
    """一批 unit 的调度汇总。"""

    results: List[UnitResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failed(self) -> List[UnitResult]:
        return [r for r in self.results if not r.ok]

    @property
    def completed_ids(self) -> List[str]:
        return [r.id for r in self.results if r.ok]


class Scheduler:
    """并发调度一批 unit（已按 DAG 拓扑排序、已按 batch_group 打包）。"""

    def __init__(
        self,
        work_dir: str | Path,
        launcher: Callable[[UnitSpec, int], str],
        collector: Callable[[UnitSpec, str], UnitResult],
        is_dead: Callable[[str], bool],
        kill: Callable[[str], None],
        capture: Optional[Callable[[str], str]] = None,
        options: Optional[SchedulerOptions] = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.launcher = launcher
        self.collector = collector
        self.is_dead = is_dead
        self.kill = kill
        # 可选：超时前抓 pane 尾部（诊断用）。未注入时退化为空串。
        self.capture = capture
        self.opts = options or SchedulerOptions()
        self.sleep = sleep
        self.log = log or (lambda msg: None)

    # ------------------------------------------------------------------ 重试记录
    def retry_path(self, unit_id: str) -> Path:
        return self.work_dir / ".generic" / "retry" / f"{safe_name(unit_id)}.json"

    def load_retry(self, unit_id: str) -> Dict[str, object]:
        p = self.retry_path(unit_id)
        empty: Dict[str, object] = {"attempts": 0, "history": []}
        if not p.is_file():
            return empty
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return empty
        if not isinstance(data, dict):
            return empty
        attempts = data.get("attempts")
        history = data.get("history")
        return {
            "attempts": attempts if isinstance(attempts, int) and attempts >= 0 else 0,
            "history": history if isinstance(history, list) else [],
        }

    def record_failure(self, unit_id: str, status: str, detail: str) -> Dict[str, object]:
        rec = self.load_retry(unit_id)
        attempts = int(rec.get("attempts") or 0) + 1
        history = list(rec.get("history") or [])
        history.append(
            {
                "attempt": attempts,
                "status": status,
                "detail": (detail or "").strip()[-1000:],
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        merged = {"attempts": attempts, "history": history}
        p = self.retry_path(unit_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return merged

    def clear_retry(self, unit_id: str) -> None:
        p = self.retry_path(unit_id)
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ 限流
    def _limit_of(self, unit: UnitSpec) -> int:
        """该 unit 的并发上限：unit 自带 concurrency_limit > 全局 max_concurrency。"""
        if unit.concurrency_limit is not None:
            return int(unit.concurrency_limit)
        return int(self.opts.max_concurrency)

    def _key_of(self, unit: UnitSpec) -> str:
        return unit.concurrency_key or "default"

    # ------------------------------------------------------------------ 主循环
    def run(self, units: List[UnitSpec]) -> BatchResult:
        """并发执行一批 unit，返回汇总结果。

        失败即停：unit 失败只记失败原因、不重派（交由外层主 Agent 校验后重新
        生成任务），本批其余 unit 继续跑完，最终 BatchResult.ok 为 False。
        """
        result = BatchResult()
        pending: List[UnitSpec] = list(units)
        active: Dict[str, UnitSpec] = {}  # pane_id -> unit
        started: Dict[str, float] = {}
        attempts: Dict[str, int] = {u.id: 0 for u in units}

        quick_fails = 0
        stage_started = time.monotonic()

        while pending or active:
            # 熔断已触发：不再补新槽位，只等已派发的 pane 收尾
            if quick_fails >= self.opts.quick_fail_limit:
                if not active:
                    break
                self.sleep(self.opts.poll_interval)
                continue

            # 补满并发槽位：按 concurrency_key 分别限流
            while pending:
                pick = None
                for idx, cand in enumerate(pending):
                    cand_limit = self._limit_of(cand)
                    cand_key = self._key_of(cand)
                    same_kind = sum(
                        1
                        for u in active.values()
                        if self._key_of(u) == cand_key
                        and self._limit_of(u) == cand_limit
                    )
                    if same_kind < cand_limit:
                        pick = idx
                        break
                if pick is None:
                    break  # 所有候选 unit 的类别槽位都满了，等收尾
                unit = pending.pop(pick)
                try:
                    pane = self.launcher(unit, attempts.get(unit.id, 0))
                except Exception as exc:  # 环境问题，重派同样失败
                    result.results.append(
                        UnitResult(
                            id=unit.id,
                            ok=False,
                            status="launch_failed",
                            detail=str(exc),
                            retryable=False,
                            attempts=attempts.get(unit.id, 0) + 1,
                        )
                    )
                    self.log(f"✗ {unit.id} 启动失败：{exc}")
                    continue
                attempts[unit.id] = attempts.get(unit.id, 0) + 1
                active[pane] = unit
                started[pane] = time.monotonic()
                self.log(f"▶ {unit.id} 派发到 {pane}（第 {attempts[unit.id]} 次）")

            # 检查已结束的 pane
            for pane in list(active):
                unit = active[pane]
                elapsed = time.monotonic() - started.get(pane, 0.0)
                if self.is_dead(pane):
                    res = self.collector(unit, pane)
                    res.attempts = attempts.get(unit.id, 0)
                    self._kill_pane(pane)
                    del active[pane]
                    started.pop(pane, None)
                    first_try = attempts.get(unit.id, 0) <= 1
                    if (
                        not res.ok
                        and first_try
                        and (time.monotonic() - stage_started) < self.opts.quick_fail_seconds
                    ):
                        quick_fails += 1
                    elif res.ok:
                        quick_fails = 0
                    self._settle(unit, res, result)
                    if (
                        quick_fails >= self.opts.quick_fail_limit
                        and len(pending) >= self.opts.quick_fail_limit
                    ):
                        self.log(
                            f"✗ 熔断：连续 {quick_fails} 个 unit 在 "
                            f"{self.opts.quick_fail_seconds:.0f}s 内失败，疑似环境问题，"
                            f"中止本批，不再派发剩余 {len(pending)} 个 unit。"
                        )
                        for u in list(pending):
                            result.results.append(
                                UnitResult(
                                    id=u.id,
                                    ok=False,
                                    status="aborted_by_circuit_breaker",
                                    detail="熔断中止，未执行",
                                    retryable=False,
                                )
                            )
                        pending.clear()
                        active.clear()
                        return result
                elif elapsed > self._timeout_of(unit):
                    self.log(f"✗ {unit.id} 超时（{self._timeout_of(unit)}s）")
                    # 先抓 pane 尾部再 kill，否则「卡在哪」的诊断信息全丢
                    tail = self._capture_pane(pane)
                    self._kill_pane(pane)
                    detail = f"超过 timeout（{self._timeout_of(unit)}s）"
                    if tail:
                        detail += f"\npane 尾部：{tail}"
                    res = UnitResult(
                        id=unit.id,
                        ok=False,
                        status="timeout",
                        detail=detail,
                        attempts=attempts.get(unit.id, 0),
                    )
                    del active[pane]
                    started.pop(pane, None)
                    self._settle(unit, res, result)

            if active:
                self.sleep(self.opts.poll_interval)
        return result

    def _timeout_of(self, unit: UnitSpec) -> float:
        if unit.timeout is not None:
            return float(unit.timeout)
        return float(self.opts.unit_timeout)

    def _settle(
        self,
        unit: UnitSpec,
        res: UnitResult,
        result: BatchResult,
    ) -> None:
        """判定一个已结束 unit 的去向：成功清记录；失败只记原因，**不重派**。

        设计变更（用户口径）：编排器不做任何内部重试。unit 失败即判失败，
        由外层主 Agent 校验后重新生成任务再跑，避免编排器陷入反复重派。
        """
        if res.ok:
            self.clear_retry(unit.id)
            result.results.append(res)
            return
        # 仅记录失败原因（供事后排查 / 外层主 Agent 参考），不重排 pending。
        self.record_failure(unit.id, res.status or "unknown", res.detail)
        self.log(f"✗ {unit.id} 失败（{res.status or '未知'}），不重试（交由主 Agent 重新生成任务）")
        result.results.append(res)

    def _capture_pane(self, pane: str) -> str:
        """抓 pane 尾部文本（尽力而为）；未注入 capture 回调或失败时返回空串。"""
        if self.capture is None:
            return ""
        try:
            return (self.capture(pane) or "").strip()[-500:]
        except Exception:
            return ""

    def _kill_pane(self, pane: str) -> None:
        try:
            self.kill(pane)
        except Exception as exc:
            self.log(f"⚠ 关闭 pane {pane} 失败：{exc}（可能残留，下次启动会清理）")
