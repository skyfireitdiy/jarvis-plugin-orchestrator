"""TmuxDriver：纯 tmux 操作封装（不关心业务）。

设计要点（通用编排器，与领域无关）：
- `remain-on-exit on`：命令结束后 pane 不消失，仍可 capture 输出；
- 所有调用走 subprocess.run(capture_output=True)，失败抛 TmuxError；
- 只做 create/capture/is_dead/wait/kill，不构造命令、不读 state。
"""
from __future__ import annotations

import shutil
import subprocess
import time
from typing import Optional


class TmuxError(RuntimeError):
    """tmux 调用失败。"""


class TmuxDriver:
    """tmux 会话与 pane 的操作封装。"""

    def __init__(self, session: str, tmux_bin: str = "tmux", timeout: float = 15.0,
                 panes_per_window: int = 6, window_width: int = 268,
                 window_height: int = 69) -> None:
        self.session = session
        self.tmux_bin = tmux_bin
        self.timeout = timeout
        # 多 window 池化参数：单个 window 最多容纳 panes_per_window 个 pane，
        # 满了就开新 window，从而支持任意并发（不受单 window 宽度限制）。
        # window 尺寸默认 268x69（匹配常见终端），配合 tiled 网格布局，
        # 每个 window 的 6 个 pane 能在终端一屏内全部看到（2 列 x 3 行）。
        self.panes_per_window = max(1, int(panes_per_window))
        self.window_width = max(80, int(window_width))
        self.window_height = max(24, int(window_height))

    # ------------------------------------------------------------------ 内部
    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.tmux_bin, *args]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError as exc:
            raise TmuxError(f"找不到 tmux 可执行文件：{self.tmux_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TmuxError(f"tmux 命令超时：{' '.join(cmd)}") from exc
        if check and proc.returncode != 0:
            raise TmuxError(
                f"tmux 命令失败（{proc.returncode}）：{' '.join(cmd)}\n{proc.stderr.strip()}"
            )
        return proc

    # ------------------------------------------------------------------ 会话
    def has_session(self) -> bool:
        """会话是否存在。"""
        proc = self._run("has-session", "-t", self.session, check=False)
        return proc.returncode == 0

    def ensure_session(self) -> None:
        """会话不存在则创建（detached）；已存在则先清掉残留的 dead pane。

        编排器异常退出（Ctrl-C / 崩溃）时，已 dead 但未 kill 的 pane 会留在会话里，
        下次复用会话就会越积越多。这里在复用前统一清理，保证「会话里的 pane
        都是活的」这一不变量。
        """
        if self.has_session():
            self._prune_dead_panes()
            return
        # 会话 detached 创建，尺寸由后续 resize-window 控制。这里同时把
        # window-size 设为 manual：tmux 默认策略（smallest/latest）会让窗口
        # 尺寸跟随 attached client（例如 268 宽的终端），把窗口压窄导致 pane
        # 切不出来。manual 让窗口尺寸完全由我们 resize 决定，不受客户端干扰。
        self._run("new-session", "-d", "-s", self.session,
                  "-x", str(self.window_width), "-y", str(self.window_height))
        self._run("set-option", "-t", self.session, "window-size", "manual",
                  check=False)

    def _new_window(self) -> str:
        """新建一个 window 并设定尺寸，返回其 target（如 ``kb-orchestrate:3``）。

        tmux 3.0a 的 new-window 不支持 -x/-y，需先 new-window 再 resize-window
        设定尺寸（否则新 window 默认 80x24，切几个 pane 就 no space）。
        """
        new_proc = self._run(
            "new-window", "-d", "-P", "-F", "#{window_index}", "-t", self.session,
            check=False,
        )
        if new_proc.returncode == 0 and new_proc.stdout.strip():
            win = new_proc.stdout.strip()
            target = f"{self.session}:{win}"
            self._run("resize-window", "-t", target,
                      "-x", str(self.window_width), "-y", str(self.window_height),
                      check=False)
            return target
        # 兜底：new-window 失败时退回当前 window（至少不中断派发）
        return self.session

    def _pick_or_create_window(self) -> str:
        """选一个未满的 window（pane 数 < panes_per_window），没有就新建。

        返回 window 的 target 字符串（如 ``kb-orchestrate:0``），供 split-window
        定位。多 window 池化：单 window 宽度有限，pane 数受限于「宽度 ÷ 每 pane
        最小宽」；把 pane 分散到多个 window 后，每个 window 都能容纳固定数量，
        从而支持任意并发。
        """
        proc = self._run(
            "list-windows", "-t", self.session,
            "-F", "#{window_index} #{window_panes}", check=False,
        )
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            idx, panes = parts[0], int(parts[1])
            if panes < self.panes_per_window:
                return f"{self.session}:{idx}"
        # 都满了：新建 window
        return self._new_window()

    def _prune_dead_panes(self) -> int:
        """清掉会话内所有已 dead 的 pane，返回清理数量。

        `list-panes -s` 列出会话内全部 pane（含非当前窗口），
        `#{pane_dead}` 为 1 表示进程已退出但 pane 仍保留（remain-on-exit on）。
        多 window 池化下，kill 掉某 window 最后一个 pane 时 tmux 会自动销毁
        该 window，因此无需额外清理空 window。
        """
        proc = self._run(
            "list-panes", "-s", "-t", self.session, "-F", "#{pane_id} #{pane_dead}",
            check=False,
        )
        removed = 0
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "1":
                self._run("kill-pane", "-t", parts[0], check=False)
                removed += 1
        return removed

    def kill_session(self) -> None:
        """销毁会话（不存在时静默）。"""
        if self.has_session():
            self._run("kill-session", "-t", self.session, check=False)

    # ------------------------------------------------------------------ pane
    def create_pane(self, cmd: str, cwd: Optional[str] = None) -> str:
        """新开一个 pane 执行 cmd，返回 pane_id（如 %3）。

        pane 结束后保留（remain-on-exit on），便于后续 capture 输出。

        多 window 池化：先选一个未满的 window（pane 数 < panes_per_window），
        没有就新建 window。这样并发不受单 window 宽度限制，可任意扩展。
        """
        self.ensure_session()
        target = self._pick_or_create_window()
        # 水平分割（左右排列）：宽度能容纳多个并列 pane，避免并发高时
        # 垂直分割把高度分光导致「no space for new pane」。
        args = ["split-window", "-h", "-P", "-F", "#{pane_id}", "-t", target]
        if cwd:
            args += ["-c", cwd]
        args.append(cmd)
        proc = self._run(*args, check=False)
        if proc.returncode != 0:
            if "no space" in (proc.stderr or "").lower():
                # 该 window 已放不下更多 pane（panes_per_window 偏大或尺寸偏小）。
                # 不能靠加宽 window 兜底：加宽会让 window 超出终端尺寸，用户就
                # 看不全 pane 了。改为新建一个 window 来放这个 pane。
                target = self._new_window()
                args = ["split-window", "-h", "-P", "-F", "#{pane_id}", "-t", target]
                if cwd:
                    args += ["-c", cwd]
                args.append(cmd)
                proc = self._run(*args)
            else:
                raise TmuxError(
                    f"tmux 命令失败（{proc.returncode}）：{' '.join(args)}\n"
                    f"{proc.stderr.strip()}"
                )
        pane_id = proc.stdout.strip()
        if not pane_id:
            raise TmuxError(f"创建 pane 未返回 pane_id：{proc.stdout!r}")
        # 结束后不关闭 pane，保留输出供 capture
        self._run("set-option", "-t", pane_id, "remain-on-exit", "on", check=False)
        # split-window 是「对半切」：每切一次把当前 pane 对半分，越晚切的越窄
        # （6 个 pane 会变成 800/400/200/100/50/50），窄 pane 日志显示不全。
        # 切完统一重排成 tiled 网格（2 列 x 3 行），让 window 内所有 pane 在
        # 终端一屏内全部可见（横排 even-horizontal 会超出一屏宽度看不全）。
        self._run("select-layout", "-t", target, "tiled", check=False)
        return pane_id

    def capture(self, pane_id: str, lines: int = 2000) -> str:
        """抓取 pane 当前屏幕内容（含回滚）。"""
        proc = self._run(
            "capture-pane", "-p", "-t", pane_id, "-S", f"-{lines}", check=False
        )
        return proc.stdout

    def is_dead(self, pane_id: str) -> bool:
        """pane 内进程是否已退出（pane_dead == 1）。"""
        proc = self._run(
            "display-message", "-p", "-t", pane_id, "#{pane_dead}", check=False
        )
        return proc.stdout.strip() == "1"

    def wait(self, pane_id: str, timeout: float, poll: float = 1.0) -> bool:
        """轮询等待 pane 结束；超时返回 False。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_dead(pane_id):
                return True
            time.sleep(poll)
        return self.is_dead(pane_id)

    def kill(self, pane_id: str) -> bool:
        """关闭 pane（不存在时静默）。返回是否成功，失败由调用方决定是否告警。

        不抛异常：kill 失败不应打断调度主循环，但静默吞掉会导致 pane 悄悄累积，
        所以把结果返回给上层，由上层记日志。

        注意 dead pane：create_pane 设了 remain-on-exit on（为了 capture 输出），
        进程退出后 pane 变成「死的但还在」。这种 pane 用 kill-pane 删不掉
        （tmux 认为没有进程可杀），必须先 respawn 成 /bin/true 再 kill，
        否则每派发一个 unit 就永久多一个 pane，几百个 unit 下来把会话撑爆。
        """
        proc = self._run("kill-pane", "-t", pane_id, check=False)
        if proc.returncode == 0:
            return True
        # dead pane 兜底：respawn 成立刻退出的命令，再 kill
        self._run("respawn-pane", "-k", "-t", pane_id, "true", check=False)
        return self._run("kill-pane", "-t", pane_id, check=False).returncode == 0

    # ------------------------------------------------------------------ 可用性
    @staticmethod
    def resolve_bin(tmux_bin: str = "tmux") -> str | None:
        """定位 tmux 可执行文件，返回绝对路径；找不到返回 None。

        查找顺序：显式传入的绝对路径 > PATH > 常见用户态安装位置。
        用户态安装位置是为了兼容「解包到 ~/.local 但没进 PATH」的机器
        （如 ci 上 tmux 装在 ~/.local/bin/tmux_pkg/tmux，非交互 ssh 的
        PATH 里没有 ~/.local/bin，会导致编排器整体起不来）。
        """
        from pathlib import Path

        if not tmux_bin:
            return None
        found = shutil.which(tmux_bin)
        if found:
            return found
        cand = Path(tmux_bin)
        if cand.is_absolute() and cand.is_file():
            return str(cand)
        home = Path.home()
        for rel in ("bin/tmux", "bin/tmux_pkg/tmux", ".local/bin/tmux",
                    ".local/bin/tmux_pkg/tmux"):
            p = home / rel
            if p.is_file():
                return str(p)
        return None

    @staticmethod
    def available(tmux_bin: str = "tmux") -> bool:
        """tmux 是否可用（可执行文件存在）。"""
        return TmuxDriver.resolve_bin(tmux_bin) is not None
