"""Agent 派发层：把 unit 转成 jvs 任务并生成可执行命令。

与领域无关：不读 stage 状态、不拼业务提示词。unit 自带完整 task_desc，
本模块只做三件事：
1. 把 unit 序列化成 jvs 的 task JSON（task_desc/background/additional_info/
   status_file/stage）；
2. 生成 jvs 命令（-D --task-file --status-file --stage --non-interactive）；
3. 把命令包进 bash 脚本再交给 pane 执行——tmux pane 的默认 shell 可能是
   fish（非 bash），直接发 bash 语法（export / source / 管道）会报错；
   用 `bash <script>` 调用则与 pane 默认 shell 完全解耦。

命令在 pane 里跑的是「非登录 shell」：PATH 不含 ~/.local/bin（jvs 所在），
代理脚本（/etc/profile.d/proxy.sh）也不加载（连公网模型会失败）。脚本里
显式补齐这两项，保证 pane 与登录 shell 环境一致。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from .models import UnitSpec
from .scheduler import safe_name

# jvs / tmux 装在 ~/.local/bin，tmux pane 的非登录 shell 的 PATH 不含该目录
_EXTRA_PATH = "$HOME/.local/bin"
# 代理脚本：/etc/profile.d/*.sh 只在「登录 shell」加载，pane 里拿不到代理
_PROXY_SCRIPT = "/etc/profile.d/proxy.sh"


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def task_json_path(unit: UnitSpec, work_dir: str | Path) -> Path:
    """unit 的 task JSON 路径（<work_dir>/.generic/tasks/<safe_id>.json）。"""
    return Path(work_dir) / ".generic" / "tasks" / f"{safe_name(unit.id)}.json"


def status_path(unit: UnitSpec, work_dir: str | Path) -> Path:
    """Agent 模式的 status 文件路径：unit 自带 status_file 则用之，否则自动生成。

    自动生成放在 <work_dir>/.generic/status/ 下，保证每个 unit 有独立的
    status 文件供 collector 判定（jvs 会把 status 写到这里）。
    """
    if unit.status_file:
        return Path(unit.status_file)
    return Path(work_dir) / ".generic" / "status" / f"{safe_name(unit.id)}.json"


def log_path(unit: UnitSpec, work_dir: str | Path) -> Path:
    """unit 的 jvs 输出日志路径（<work_dir>/.generic/logs/<safe_id>.log）。"""
    return Path(work_dir) / ".generic" / "logs" / f"{safe_name(unit.id)}.log"


def script_path(unit: UnitSpec, work_dir: str | Path) -> Path:
    """unit 的 bash 脚本路径（<work_dir>/.generic/scripts/<safe_id>.sh）。"""
    return Path(work_dir) / ".generic" / "scripts" / f"{safe_name(unit.id)}.sh"


# ---------------------------------------------------------------------------
# task JSON
# ---------------------------------------------------------------------------
def build_task_json(unit: UnitSpec, work_dir: str | Path) -> Dict[str, object]:
    """把 unit 序列化成 jvs 的 task JSON。

    task_desc 以 ``'<Pin>'`` 开头：jvs 的 builtin_input_handler 只固定标记
    「之后」的内容，把任务目标/范围/完成标准全部 pin 住（每轮重注入），
    避免长任务中途被压缩丢失关键约束。
    """
    return {
        "task_desc": "'<Pin>'" + (unit.task_desc or ""),
        "background": unit.background,
        "additional_info": unit.additional_info,
        "status_file": str(status_path(unit, work_dir)),
    }


def write_task_json(unit: UnitSpec, work_dir: str | Path) -> Path:
    """把 task JSON 写到 .generic/tasks/ 下，返回路径。"""
    path = task_json_path(unit, work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_task_json(unit, work_dir)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# jvs 命令
# ---------------------------------------------------------------------------
def _proxy_prefix() -> str:
    """bash 脚本前缀：补齐 PATH（找 jvs）+ source 代理脚本（连公网模型）。

    代理脚本不存在时跳过，不影响没有该脚本的机器。
    """
    return (
        f'export PATH="{_EXTRA_PATH}:$PATH"\n'
        f"if [ -f {_PROXY_SCRIPT} ]; then . {_PROXY_SCRIPT}; fi\n"
    )


def build_jvs_command(
    unit: UnitSpec,
    work_dir: str | Path,
    jvs_bin: str = "jvs",
    llm_group: Optional[str] = None,
    default_stage: Optional[str] = None,
) -> str:
    """生成一条可执行的 pane 命令（Agent 模式）：`bash <script>`。

    流程：
    1. 写 task JSON 到 .generic/tasks/；
    2. 生成 jvs 命令（-D --task-file --status-file --stage --non-interactive）；
    3. 把「proxy prefix + set -o pipefail + jvs 2>&1 | tee 日志」写进 bash 脚本；
    4. 返回 `bash <script>`（与 pane 默认 shell 解耦）。

    必须带 `--non-interactive`（-n）：子 Agent 跑在 tmux pane 里（伪终端，
    isatty 为真），不带 -n 时 jarvis 每轮结束若未检测到工具调用会阻塞等待
    键盘输入，pane 永久挂起。带 -n 后无人值守语义：无工具调用 → 输出
    `!!!COMPLETE!!!` 结束；user_confirm 取默认值。

    stage 优先级：unit.stage > default_stage（CLI --stage）> 不拼 --stage。
    """
    import shlex

    json_path = write_task_json(unit, work_dir)
    sp = status_path(unit, work_dir)
    lp = log_path(unit, work_dir)
    lp.parent.mkdir(parents=True, exist_ok=True)
    # status 文件父目录也要建：jvs 写 status 时不会自动 mkdir，
    # 目录不存在会报 No such file or directory，导致 collector 判 no_status_file。
    sp.parent.mkdir(parents=True, exist_ok=True)

    stage = unit.stage or default_stage
    group_opt = ""
    if llm_group:
        group_opt = f"-g {shlex.quote(llm_group)} "
    stage_opt = f"--stage {shlex.quote(stage)} " if stage else ""
    jvs_cmd = (
        f"{shlex.quote(jvs_bin)} -D {group_opt}--task-file {shlex.quote(str(json_path))} "
        f"--status-file {shlex.quote(str(sp))} {stage_opt}--non-interactive"
    )
    # tee 双写：pane 里仍能看到实时输出，同时完整落盘供事后回溯。
    # `2>&1` 把 stderr 也并进来（jarvis 的报错多在 stderr）；
    # `set -o pipefail` 保证 jvs 非 0 退出时整条管道也非 0（否则 tee 成功会吞掉退出码）。
    body = (
        f"{_proxy_prefix()}"
        f"set -o pipefail\n"
        f"{jvs_cmd} 2>&1 | tee {shlex.quote(str(lp))}\n"
    )
    sp_path = script_path(unit, work_dir)
    sp_path.parent.mkdir(parents=True, exist_ok=True)
    sp_path.write_text(body, encoding="utf-8")
    return f"bash {shlex.quote(str(sp_path))}"
