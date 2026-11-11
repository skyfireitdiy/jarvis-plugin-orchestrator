"""通用编排器的数据契约（plan / result）。

设计原则
--------
1. **零领域依赖**：本模块（以及整个 generic 包）不认识 stage、不知道知识库、
   不 import reverse_state / model。它只认「unit」这一抽象。
2. **plan 是输入**：由调用方（主 Agent / 领域适配层）生成，编排器只读。
   每个 unit 自带完整提示词（task_desc），编排器不拼提示词。
3. **result 是输出**：编排器把每个 unit 的执行结果写回 result 文件，
   调用方据此更新自己的领域状态（如 state.json 的 units_done）。
4. **编排器不持有运行状态**：除了重试计数（.generic/retry/）外不落任何状态，
   领域状态完全由调用方管理。

plan 文件格式（JSON）
--------------------
{
  "run_id": "可选的运行标识（仅用于日志/result 归档）",
  "units": [
    {
      "id": "唯一 unit 标识（必填）",
      "task_desc": "完整提示词（必填）",
      "background": "可选背景",
      "additional_info": "可选附加信息",
      "status_file": "可选：执行方写 status 的文件路径",
      "timeout": 3600,                 // 可选，缺省用引擎全局 timeout
      "concurrency_key": "artifact",   // 可选，同类限流键；缺省用 "default"
      "concurrency_limit": 4,          // 可选，该类上限；缺省用引擎全局并发
      "depends_on": ["other-id"],      // 可选，前置 unit（DAG 排序）
      "batch_group": "lcm"             // 可选，同组打包给一个 Agent
    }
  ]
}

result 文件格式（JSON）
----------------------
{
  "run_id": "...",
  "started_at": "...", "finished_at": "...",
  "ok": true,
  "units": [
    {"id": "...", "ok": true, "status": "completed", "detail": "",
     "attempts": 1, "retryable": true}
  ]
}
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class PlanError(ValueError):
    """plan 文件非法（结构不符 / 缺必填字段 / 重复 id / 依赖环）。"""


# ---------------------------------------------------------------------------
# 输入：plan
# ---------------------------------------------------------------------------
@dataclass
class UnitSpec:
    """一个可派发的任务单元（与领域无关）。"""

    id: str
    task_desc: str
    background: str = ""
    additional_info: str = ""
    status_file: Optional[str] = None
    # 阶段标识：Agent 模式派发 jvs 时写入 status 文件的 stage 字段，
    # 用于多阶段流水线中区分/防串台（status 文件 stage 与 unit.stage 不一致视为无效）。
    stage: Optional[str] = None
    timeout: Optional[float] = None
    # 同类限流：同一 concurrency_key 的活跃 unit 数不超过 concurrency_limit。
    # 缺省 key="default"、limit=None（= 用引擎全局并发上限）。
    concurrency_key: str = "default"
    concurrency_limit: Optional[int] = None
    # 前置 unit：必须全部成功后才派发本 unit（DAG）。
    depends_on: List[str] = field(default_factory=list)
    # 同组打包：batch_group 相同的 unit 会合并成一个「批量 unit」交给同一个 Agent。
    batch_group: Optional[str] = None
    # 可选：要执行的 shell 命令（CLI 直接用；Engine API 调用方可忽略此字段，
    # 改用注入的 launcher 自行构造命令）。领域无关——引擎不理解命令内容。
    command: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_desc": self.task_desc,
            "background": self.background,
            "additional_info": self.additional_info,
            "status_file": self.status_file,
            "stage": self.stage,
            "timeout": self.timeout,
            "concurrency_key": self.concurrency_key,
            "concurrency_limit": self.concurrency_limit,
            "depends_on": list(self.depends_on),
            "batch_group": self.batch_group,
            "command": self.command,
        }


@dataclass
class PlanSpec:
    """一次运行的完整计划。"""

    units: List[UnitSpec] = field(default_factory=list)
    run_id: str = ""

    def by_id(self) -> Dict[str, UnitSpec]:
        return {u.id: u for u in self.units}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "units": [u.to_dict() for u in self.units],
        }

    # ---------------------------------------------------------------- 校验
    def validate(self) -> None:
        """结构校验：id 唯一、必填非空、依赖存在且无环。非法抛 PlanError。"""
        if not isinstance(self.units, list) or not self.units:
            raise PlanError("plan.units 不能为空")
        seen: Dict[str, UnitSpec] = {}
        for i, u in enumerate(self.units):
            if not isinstance(u, UnitSpec):
                raise PlanError(f"plan.units[{i}] 不是 UnitSpec")
            if not (u.id or "").strip():
                raise PlanError(f"plan.units[{i}] 缺 id")
            if not (u.task_desc or "").strip():
                raise PlanError(f"unit「{u.id}」缺 task_desc（每个 unit 必须自带完整提示词）")
            if u.id in seen:
                raise PlanError(f"unit id 重复：{u.id}")
            if u.concurrency_limit is not None and (
                not isinstance(u.concurrency_limit, int) or u.concurrency_limit < 1
            ):
                raise PlanError(f"unit「{u.id}」的 concurrency_limit 必须 >= 1")
            if u.timeout is not None and u.timeout <= 0:
                raise PlanError(f"unit「{u.id}」的 timeout 必须 > 0")
            seen[u.id] = u
        # 依赖必须存在
        for u in self.units:
            for dep in u.depends_on:
                if dep not in seen:
                    raise PlanError(f"unit「{u.id}」依赖不存在的 unit：{dep}")
                if dep == u.id:
                    raise PlanError(f"unit「{u.id}」依赖自己")
        # 无环（DFS 三色）
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {uid: WHITE for uid in seen}

        def dfs(uid: str, stack: List[str]) -> None:
            color[uid] = GRAY
            for dep in seen[uid].depends_on:
                if color[dep] == GRAY:
                    raise PlanError(
                        "unit 依赖存在环：" + " → ".join(stack + [uid, dep])
                    )
                if color[dep] == WHITE:
                    dfs(dep, stack + [uid])
            color[uid] = BLACK

        for uid in seen:
            if color[uid] == WHITE:
                dfs(uid, [])

    def topo_order(self) -> List[UnitSpec]:
        """拓扑排序（依赖在前），同层保持 plan 原始顺序，保证可预期。"""
        self.validate()
        ordered: List[UnitSpec] = []
        done: set = set()
        remaining = list(self.units)
        while remaining:
            progressed = False
            for u in list(remaining):
                if all(dep in done for dep in u.depends_on):
                    ordered.append(u)
                    done.add(u.id)
                    remaining.remove(u)
                    progressed = True
            if not progressed:  # validate() 已排除环，这里只是防御
                raise PlanError("无法拓扑排序（存在环或缺失依赖）")
        return ordered


def load_plan(path: str | Path) -> PlanSpec:
    """从 JSON 文件读 plan 并校验。非法抛 PlanError。"""
    p = Path(path)
    if not p.is_file():
        raise PlanError(f"plan 文件不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlanError(f"plan 文件不是合法 JSON：{p}（{exc}）") from exc
    if not isinstance(data, dict):
        raise PlanError(f"plan 顶层必须是对象：{p}")
    raw_units = data.get("units")
    if not isinstance(raw_units, list):
        raise PlanError(f"plan.units 必须是数组：{p}")
    units: List[UnitSpec] = []
    for i, raw in enumerate(raw_units):
        if not isinstance(raw, dict):
            raise PlanError(f"plan.units[{i}] 必须是对象")
        deps = raw.get("depends_on") or []
        if not isinstance(deps, list):
            raise PlanError(f"plan.units[{i}].depends_on 必须是数组")
        units.append(
            UnitSpec(
                id=str(raw.get("id") or ""),
                task_desc=str(raw.get("task_desc") or ""),
                background=str(raw.get("background") or ""),
                additional_info=str(raw.get("additional_info") or ""),
                status_file=raw.get("status_file"),
                stage=raw.get("stage"),
                timeout=raw.get("timeout"),
                concurrency_key=str(raw.get("concurrency_key") or "default"),
                concurrency_limit=raw.get("concurrency_limit"),
                depends_on=[str(d) for d in deps],
                batch_group=raw.get("batch_group"),
                command=raw.get("command"),
            )
        )
    plan = PlanSpec(units=units, run_id=str(data.get("run_id") or ""))
    plan.validate()
    return plan


# ---------------------------------------------------------------------------
# 输出：result
# ---------------------------------------------------------------------------
@dataclass
class UnitResult:
    """单个 unit 的执行结果。"""

    id: str
    ok: bool
    status: Optional[str] = None
    detail: str = ""
    attempts: int = 0
    # 是否值得重派：环境类失败（tmux 建 pane 失败）重派也没用，其余都可重派。
    retryable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "attempts": self.attempts,
            "retryable": self.retryable,
        }


@dataclass
class RunResult:
    """一次运行的汇总结果。"""

    run_id: str = ""
    results: List[UnitResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failed(self) -> List[UnitResult]:
        return [r for r in self.results if not r.ok]

    @property
    def completed_ids(self) -> List[str]:
        return [r.id for r in self.results if r.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "units": [r.to_dict() for r in self.results],
        }


def save_result(path: str | Path, result: RunResult) -> Path:
    """把运行结果原子写入 JSON（先写临时文件再 rename，避免半截文件）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not result.finished_at:
        result.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(p)
    return p


def load_result(path: str | Path) -> Optional[RunResult]:
    """读 result 文件；不存在或损坏返回 None（不伪造结果）。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    results: List[UnitResult] = []
    for raw in data.get("units") or []:
        if not isinstance(raw, dict):
            continue
        results.append(
            UnitResult(
                id=str(raw.get("id") or ""),
                ok=bool(raw.get("ok")),
                status=raw.get("status"),
                detail=str(raw.get("detail") or ""),
                attempts=int(raw.get("attempts") or 0),
                retryable=bool(raw.get("retryable", True)),
            )
        )
    return RunResult(
        run_id=str(data.get("run_id") or ""),
        results=results,
        started_at=str(data.get("started_at") or ""),
        finished_at=str(data.get("finished_at") or ""),
    )
