"""通用编排器（generic engine）。

与领域无关的执行引擎：输入 plan（unit 清单 + 每个 unit 的完整提示词），
输出 result（每个 unit 的执行结果）。不认识 stage、不读写领域状态文件。

对外主要入口：
    from generic import Engine, load_plan, PlanSpec, UnitSpec
"""
from .models import (
    PlanError,
    PlanSpec,
    RunResult,
    UnitResult,
    UnitSpec,
    load_plan,
    load_result,
    save_result,
)
from .engine import Engine, EngineOptions
from .tmux_driver import TmuxDriver, TmuxError

__all__ = [
    "Engine",
    "EngineOptions",
    "PlanError",
    "PlanSpec",
    "RunResult",
    "TmuxDriver",
    "TmuxError",
    "UnitResult",
    "UnitSpec",
    "load_plan",
    "load_result",
    "save_result",
]
