"""通用编排引擎（knowledge_toolkit.generic）测试。

用 FakeTmux 替代真实 tmux，验证：plan 校验、DAG 拓扑排序、depends_on 阻塞、
concurrency_key 限流、batch_group 打包、重试上限、熔断、result 落盘、dry-run。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from generic import (
    Engine,
    EngineOptions,
    PlanError,
    PlanSpec,
    UnitResult,
    UnitSpec,
    load_plan,
    load_result,
)
from generic.cli import main as generic_main


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
class FakeTmux:
    """假 tmux：pane 立刻 dead，collector 由测试注入。"""

    def __init__(self, dead_after: int = 0):
        self.dead_after = dead_after
        self.created = []
        self.killed = []
        self._n = 0

    def create_pane(self, cmd, cwd=None):
        self._n += 1
        pid = f"%{self._n}"
        self.created.append((pid, cmd))
        return pid

    def is_dead(self, pane):
        return True

    def kill(self, pane):
        self.killed.append(pane)

    def capture(self, pane, lines=2000):
        return ""


def make_engine(tmp_path, tmux=None, launcher=None, collector=None, sleep=None, **opts):
    tmux = tmux or FakeTmux()
    o = EngineOptions(poll_interval=0.0, **opts)
    return Engine(
        work_dir=tmp_path, options=o, tmux=tmux,
        launcher=launcher, collector=collector,
        sleep=sleep or (lambda s: None),
    )


def unit(uid, **kw):
    return UnitSpec(id=uid, task_desc=f"task {uid}", **kw)


# ---------------------------------------------------------------------------
# plan 校验
# ---------------------------------------------------------------------------
def test_plan_validate_rejects_empty_units():
    with pytest.raises(PlanError, match="不能为空"):
        PlanSpec(units=[]).validate()


def test_plan_validate_rejects_missing_task_desc():
    with pytest.raises(PlanError, match="缺 task_desc"):
        PlanSpec(units=[UnitSpec(id="a", task_desc="")]).validate()


def test_plan_validate_rejects_duplicate_id():
    with pytest.raises(PlanError, match="重复"):
        PlanSpec(units=[unit("a"), unit("a")]).validate()


def test_plan_validate_rejects_missing_dependency():
    with pytest.raises(PlanError, match="依赖不存在的 unit"):
        PlanSpec(units=[unit("a", depends_on=["zzz"])]).validate()


def test_plan_validate_rejects_cycle():
    with pytest.raises(PlanError, match="存在环"):
        PlanSpec(units=[unit("a", depends_on=["b"]), unit("b", depends_on=["a"])]).validate()


def test_plan_topo_order_puts_dependencies_first():
    plan = PlanSpec(units=[unit("c", depends_on=["b"]), unit("b", depends_on=["a"]), unit("a")])
    assert [u.id for u in plan.topo_order()] == ["a", "b", "c"]


def test_load_plan_roundtrip_and_invalid(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text(
        json.dumps({"run_id": "r", "units": [{"id": "a", "task_desc": "x"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    plan = load_plan(p)
    assert plan.run_id == "r" and plan.units[0].id == "a"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(PlanError, match="不是合法 JSON"):
        load_plan(bad)


# ---------------------------------------------------------------------------
# DAG 阻塞
# ---------------------------------------------------------------------------
def test_engine_respects_depends_on_order(tmp_path):
    """b 依赖 a：a 未完成前 b 不能被派发。"""
    order = []

    def launcher(u, attempt):
        order.append(u.id)
        return u.id

    def collector(u, pane):
        return UnitResult(id=u.id, ok=True, status="completed")

    eng = make_engine(tmp_path, launcher=launcher, collector=collector)
    plan = PlanSpec(units=[unit("b", depends_on=["a"]), unit("a")])
    run = eng.run(plan)
    assert run.ok
    assert order == ["a", "b"]  # a 必须先于 b


def test_engine_dependency_failure_blocks_dependents(tmp_path):
    """a 失败（不可重派）→ b 依赖 a，不应被派发。"""
    launched = []

    def launcher(u, attempt):
        launched.append(u.id)
        return u.id

    def collector(u, pane):
        if u.id == "a":
            return UnitResult(id="a", ok=False, status="failed", retryable=False)
        return UnitResult(id=u.id, ok=True, status="completed")

    eng = make_engine(tmp_path, launcher=launcher, collector=collector, max_retries=0)
    plan = PlanSpec(units=[unit("a"), unit("b", depends_on=["a"])])
    run = eng.run(plan)
    assert not run.ok
    assert "b" not in launched  # b 从未派发
    assert "b" in [r.id for r in run.failed]


# ---------------------------------------------------------------------------
# concurrency_key 限流
# ---------------------------------------------------------------------------
def test_engine_concurrency_key_limits_same_kind(tmp_path):
    """key=vp limit=1 的 unit 必须串行；key=artifact 可并行。"""
    peak = {"vp": 0, "artifact": 0}
    active = {"vp": 0, "artifact": 0}
    panes = {}
    state = {"n": 0}

    def launcher(u, attempt):
        state["n"] += 1
        pane = f"%{state['n']}"
        panes[pane] = u
        k = u.concurrency_key
        active[k] += 1
        peak[k] = max(peak[k], active[k])
        return pane

    # 用可控 is_dead：同一轮内所有 pane 都「活着」，让补槽逻辑把槽位填满；
    # 槽位填满后（launcher 不再被调用）才放行收尾，从而暴露真实并发峰值。
    def is_dead(pane):
        return False

    def collector(u, pane):
        active[u.concurrency_key] -= 1
        return UnitResult(id=u.id, ok=True, status="completed")

    tmux = FakeTmux()
    tmux.is_dead = is_dead

    # 首次调度轮次填满槽位后，把 is_dead 换成恒 True，让所有 pane 收尾
    orig_launcher = launcher
    filled = {"done": False}

    def launcher_wrap(u, attempt):
        pane = orig_launcher(u, attempt)
        # 一轮补槽结束的标志：pending 里已无可派发者，由 scheduler 自然退出内层 while；
        # 这里用「launcher 连续被调用后第一次 sleep」近似——改用 collector 前切换。
        return pane

    def collector_wrap(u, pane):
        filled["done"] = True
        return collector(u, pane)

    # 用 sleep 钩子：第一次 sleep（说明本轮补槽已满）后放行收尾
    def sleep_hook(sec):
        if not filled["done"]:
            filled["done"] = True
            tmux.is_dead = lambda p: True

    eng = make_engine(tmp_path, tmux=tmux, launcher=launcher_wrap,
                      collector=collector_wrap, sleep=sleep_hook,
                      max_concurrency=4)
    plan = PlanSpec(units=[
        unit("vp1", concurrency_key="vp", concurrency_limit=1),
        unit("vp2", concurrency_key="vp", concurrency_limit=1),
        unit("a1", concurrency_key="artifact", concurrency_limit=4),
        unit("a2", concurrency_key="artifact", concurrency_limit=4),
    ])
    run = eng.run(plan)
    assert run.ok
    assert peak["vp"] == 1, f"版本包应串行，实际峰值 {peak['vp']}"
    assert peak["artifact"] >= 2, f"制品应并行，实际峰值 {peak['artifact']}"


# ---------------------------------------------------------------------------
# batch_group 打包
# ---------------------------------------------------------------------------
def test_engine_packs_batch_group(tmp_path):
    """同 batch_group 的相邻 unit 合并成一个批量 unit 派发。"""
    launched = []

    def launcher(u, attempt):
        launched.append(u.id)
        return u.id

    def collector(u, pane):
        return UnitResult(id=u.id, ok=True, status="completed")

    eng = make_engine(tmp_path, launcher=launcher, collector=collector)
    plan = PlanSpec(units=[
        unit("m1", batch_group="lcm"),
        unit("m2", batch_group="lcm"),
        unit("m3", batch_group="lcm"),
    ])
    run = eng.run(plan)
    assert run.ok
    assert len(launched) == 1
    assert launched[0].startswith("__batch__:")
    assert "m1" in launched[0] and "m3" in launched[0]


def test_engine_pack_keeps_ungrouped_separate(tmp_path):
    launched = []

    def launcher(u, attempt):
        launched.append(u.id)
        return u.id

    def collector(u, pane):
        return UnitResult(id=u.id, ok=True, status="completed")

    eng = make_engine(tmp_path, launcher=launcher, collector=collector)
    plan = PlanSpec(units=[unit("a"), unit("b", batch_group="g"), unit("c", batch_group="g"), unit("d")])
    run = eng.run(plan)
    assert run.ok
    # a 单独、b+c 打包、d 单独 → 3 次派发
    assert len(launched) == 3


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------
def test_engine_does_not_retry_failed_unit(tmp_path):
    """编排器不做内部重试：unit 失败只跑一次，交由外层主 Agent 重新生成任务。"""
    calls = {"n": 0}

    def launcher(u, attempt):
        calls["n"] += 1
        return u.id

    def collector(u, pane):
        return UnitResult(id=u.id, ok=False, status="failed", retryable=True)

    eng = make_engine(tmp_path, launcher=launcher, collector=collector, max_retries=3)
    run = eng.run(PlanSpec(units=[unit("a")]))
    assert not run.ok
    # 失败即止，只派发 1 次（不再重试）
    assert calls["n"] == 1


def test_engine_failure_recorded_without_retry(tmp_path):
    """失败 unit 记录失败原因，但不重派（max_retries 不再驱动重试）。"""
    calls = {"n": 0}

    def launcher(u, attempt):
        calls["n"] += 1
        return u.id

    def collector(u, pane):
        return UnitResult(id=u.id, ok=False, status="failed", retryable=True)

    eng = make_engine(tmp_path, launcher=launcher, collector=collector, max_retries=2)
    run = eng.run(PlanSpec(units=[unit("a")]))
    assert not run.ok
    # 只跑一次，不因 max_retries 重派
    assert calls["n"] == 1


def test_engine_non_retryable_fails_immediately(tmp_path):
    calls = {"n": 0}

    def launcher(u, attempt):
        calls["n"] += 1
        return u.id

    def collector(u, pane):
        return UnitResult(id=u.id, ok=False, status="launch_failed", retryable=False)

    eng = make_engine(tmp_path, launcher=launcher, collector=collector, max_retries=5)
    run = eng.run(PlanSpec(units=[unit("a")]))
    assert not run.ok
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 熔断
# ---------------------------------------------------------------------------
def test_engine_circuit_breaker_aborts_batch(tmp_path):
    """连续多个 unit 快速失败 → 熔断，剩余 unit 不派发。"""
    launched = []

    def launcher(u, attempt):
        launched.append(u.id)
        return u.id

    def collector(u, pane):
        return UnitResult(id=u.id, ok=False, status="failed", retryable=False)

    eng = make_engine(
        tmp_path, launcher=launcher, collector=collector,
        max_retries=0, quick_fail_limit=3, quick_fail_seconds=1000.0,
    )
    plan = PlanSpec(units=[unit(f"u{i}") for i in range(10)])
    run = eng.run(plan)
    assert not run.ok
    # 熔断后剩余 unit 标 aborted_by_circuit_breaker
    statuses = {r.status for r in run.results}
    assert "aborted_by_circuit_breaker" in statuses
    assert len(launched) < 10


# ---------------------------------------------------------------------------
# result 落盘
# ---------------------------------------------------------------------------
def test_engine_writes_result_json(tmp_path):
    def launcher(u, attempt):
        return u.id

    def collector(u, pane):
        return UnitResult(id=u.id, ok=True, status="completed")

    eng = make_engine(tmp_path, launcher=launcher, collector=collector)
    rp = tmp_path / "result.json"
    eng.run(PlanSpec(units=[unit("a"), unit("b")]), result_path=rp)
    assert rp.is_file()
    loaded = load_result(rp)
    assert loaded is not None
    assert loaded.ok
    assert {r.id for r in loaded.results} == {"a", "b"}


# ---------------------------------------------------------------------------
# dry-run 无副作用
# ---------------------------------------------------------------------------
def test_cli_dry_run_no_side_effects(tmp_path):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps({"units": [{"id": "a", "task_desc": "x"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    work = tmp_path / "w"
    rc = generic_main(["run", "--plan", str(plan_file), "--work-dir", str(work), "--dry-run"])
    assert rc == 0
    assert not (work / "result.json").is_file()
    assert not (work / ".generic").exists()


def test_cli_validate_rejects_bad_plan(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"units": [{"id": "a"}]}), encoding="utf-8")  # 缺 task_desc
    assert generic_main(["validate", "--plan", str(p)]) == 1


# ---------------------------------------------------------------------------
# 零领域依赖
# ---------------------------------------------------------------------------
def test_generic_package_has_no_domain_dependency():
    """generic 包不得 import reverse_state / knowledge_toolkit.model。"""
    import ast
    root = Path(__file__).resolve().parents[1] / "generic"
    for py in root.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
                # 相对 import：level>0 表示从包外（.. 及以上）引入
                if node.level and node.level >= 2:
                    raise AssertionError(
                        f"{py} 从包外 import（level={node.level} module={node.module}）"
                    )
            for m in mods:
                assert "reverse_state" not in m, f"{py} import 了 {m}"
                assert m != "model" and not m.startswith("knowledge_toolkit.model"), (
                    f"{py} import 了 {m}"
                )


# ---------------------------------------------------------------------------
# Agent 派发模式（dispatcher）
# ---------------------------------------------------------------------------
def test_build_task_json_has_pin_and_fields(tmp_path):
    """task JSON：task_desc 以 '<Pin>' 开头，含 background/additional_info/status_file。"""
    from generic import UnitSpec, build_task_json, status_path

    u = UnitSpec(
        id="a",
        task_desc="写一个 hello world 程序",
        background="背景",
        additional_info="附加",
        stage="s1",
    )
    payload = build_task_json(u, tmp_path)
    assert payload["task_desc"].startswith("'<Pin>'写一个 hello world 程序")
    assert payload["background"] == "背景"
    assert payload["additional_info"] == "附加"
    assert payload["status_file"] == str(status_path(u, tmp_path))


def test_status_path_prefers_unit_status_file(tmp_path):
    """status_path：unit 自带 status_file 优先，否则自动生成 .generic/status/。"""
    from generic import UnitSpec, status_path

    u1 = UnitSpec(id="a", task_desc="x", status_file="/tmp/custom.status")
    assert str(status_path(u1, tmp_path)) == "/tmp/custom.status"

    u2 = UnitSpec(id="a", task_desc="x")
    p = status_path(u2, tmp_path)
    assert str(p).startswith(str(tmp_path / ".generic" / "status"))
    assert p.name.endswith(".json")


def test_build_jvs_command_writes_bash_script(tmp_path):
    """Agent 模式：pane 命令是 `bash <script>`，脚本含 jvs 完整参数。"""
    from generic import UnitSpec, build_jvs_command, script_path

    u = UnitSpec(id="hello", task_desc="写 hello world", stage="s1")
    pane_cmd = build_jvs_command(
        u, tmp_path, jvs_bin="/fake/jvs", llm_group="default"
    )
    sp = script_path(u, tmp_path)
    assert pane_cmd == f"bash {sp}"
    assert sp.is_file()
    body = sp.read_text(encoding="utf-8")
    assert "export PATH=\"$HOME/.local/bin:$PATH\"" in body
    assert "set -o pipefail" in body
    assert "/fake/jvs -D -g default --task-file" in body
    assert "--status-file" in body and "--stage s1" in body
    assert "--non-interactive" in body
    assert "2>&1 | tee" in body


def test_build_jvs_command_no_stage_no_group(tmp_path):
    """无 stage / 无 llm_group 时不拼对应参数。"""
    from generic import UnitSpec, build_jvs_command, script_path

    u = UnitSpec(id="a", task_desc="x")
    build_jvs_command(u, tmp_path, jvs_bin="jvs")
    body = script_path(u, tmp_path).read_text(encoding="utf-8")
    assert "--stage" not in body
    assert "-g " not in body
    assert "jvs -D --task-file" in body


def test_cli_launcher_dual_mode(tmp_path):
    """launcher 双模式：unit 带 command→shell 模式；无 command→Agent 模式。"""
    from generic import Engine, EngineOptions, UnitSpec
    from generic.cli import _make_launcher_collector

    tmux = FakeTmux()
    eng = Engine(work_dir=tmp_path, options=EngineOptions(poll_interval=0.0), tmux=tmux)
    launcher, _ = _make_launcher_collector(eng, jvs_bin="/fake/jvs")

    # shell 模式：直接执行 command
    u_shell = UnitSpec(id="s", task_desc="x", command="echo hi > out.txt")
    launcher(u_shell, 0)
    assert tmux.created[-1][1] == "echo hi > out.txt"

    # Agent 模式：生成 jvs 命令（bash 脚本）
    u_agent = UnitSpec(id="a", task_desc="写 hello world")
    launcher(u_agent, 0)
    pane_cmd = tmux.created[-1][1]
    assert pane_cmd.startswith("bash ")
    # jvs 命令在脚本内容里（pane 命令只是 bash <script>）
    from generic import script_path

    body = script_path(u_agent, tmp_path).read_text(encoding="utf-8")
    assert "/fake/jvs -D" in body


def test_collector_reads_status_and_stage_check(tmp_path):
    """collector：读 status 文件判定 completed；stage 串台校验。"""
    from generic import Engine, EngineOptions, UnitSpec
    from generic.cli import _make_launcher_collector

    tmux = FakeTmux()
    eng = Engine(work_dir=tmp_path, options=EngineOptions(poll_interval=0.0), tmux=tmux)
    _, collector = _make_launcher_collector(eng)

    # 成功：status 文件 {"status": "completed"}
    u_ok = UnitSpec(id="ok", task_desc="x", status_file=str(tmp_path / "ok.status"))
    (tmp_path / "ok.status").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    res = collector(u_ok, "%1")
    assert res.ok and res.status == "completed"

    # stage 串台：status 文件 stage 与 unit.stage 不一致 → 视为未产出
    u_stage = UnitSpec(id="st", task_desc="x", stage="s1",
                       status_file=str(tmp_path / "st.status"))
    (tmp_path / "st.status").write_text(
        json.dumps({"status": "completed", "stage": "s2"}), encoding="utf-8"
    )
    res = collector(u_stage, "%2")
    assert not res.ok and res.status == "no_status_file"

    # 无 status 文件 → 失败
    u_none = UnitSpec(id="none", task_desc="x", status_file=str(tmp_path / "nope.status"))
    res = collector(u_none, "%3")
    assert not res.ok and res.status == "no_status_file"


def test_engine_agent_mode_end_to_end(tmp_path):
    """端到端：Agent 模式 unit 经 Engine 跑通（FakeTmux + 预写 status 文件）。"""
    from generic import Engine, EngineOptions, PlanSpec, UnitSpec

    tmux = FakeTmux()

    def launcher(u, attempt):
        # 模拟 jvs Agent 完成：写 status 文件
        from generic import status_path

        sp = status_path(u, tmp_path)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        return tmux.create_pane("bash x", cwd=str(tmp_path))

    def collector(u, pane):
        from generic import status_path

        sp = status_path(u, tmp_path)
        data = json.loads(sp.read_text(encoding="utf-8"))
        return UnitResult(id=u.id, ok=data.get("status") == "completed", status=data.get("status"))

    eng = Engine(
        work_dir=tmp_path,
        options=EngineOptions(poll_interval=0.0),
        tmux=tmux,
        launcher=launcher,
        collector=collector,
        sleep=lambda s: None,
    )
    plan = PlanSpec(units=[UnitSpec(id="a", task_desc="写 hello world")])
    run = eng.run(plan)
    assert run.ok
    assert run.results[0].id == "a"
