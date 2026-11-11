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
from .dispatcher import (
    build_jvs_command,
    build_task_json,
    log_path,
    script_path,
    status_path,
    task_json_path,
    write_task_json,
)

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
    "build_jvs_command",
    "build_task_json",
    "load_plan",
    "load_result",
    "log_path",
    "save_result",
    "script_path",
    "status_path",
    "task_json_path",
    "write_task_json",
]