#!/usr/bin/env bash
# orchestrator 插件安装 hook：检测依赖并用 uv 安装编排 CLI。
# 由 Jarvis 在插件 post-install 阶段调用（cwd=插件目录），也可手动执行。
set -euo pipefail

# 插件根目录 = 本脚本所在目录的上一级（脚本位于 <plugin>/scripts/install.sh）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"

echo "==> orchestrator 插件安装 hook"
echo "    插件目录: $PLUGIN_DIR"

# 1) 检测 tmux（编排引擎并发 pane 派发的硬依赖）
if ! command -v tmux >/dev/null 2>&1; then
    echo "❌ 未检测到 tmux。通用编排器依赖 tmux 做并发 pane 派发，请先安装："
    echo "   apt install tmux   (Debian/Ubuntu)"
    echo "   brew install tmux  (macOS)"
    exit 1
fi
echo "✓ tmux: $(command -v tmux) ($(tmux -V 2>/dev/null || echo '?'))"

# 2) 检测 uv（安装编排 CLI 的工具）
if ! command -v uv >/dev/null 2>&1; then
    echo "❌ 未检测到 uv。请先安装 uv："
    echo "   curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
echo "✓ uv: $(command -v uv) ($(uv --version))"

# 3) 用 uv 安装编排 CLI（从插件目录的 pyproject.toml）
#    -e editable 安装：CLI 直接链接插件源码目录，插件更新后立即生效，无需重装
#    --refresh 强制重建，避免 uv 缓存导致装到旧版源码
echo "==> uv tool install orchestrator（--force -e --refresh 覆盖已装版本）"
uv tool install --force -e --refresh "$PLUGIN_DIR"

if ! command -v orchestrator >/dev/null 2>&1; then
    echo "❌ 安装后未找到 orchestrator 命令，请检查 ~/.local/bin 是否在 PATH"
    exit 1
fi
echo "✓ orchestrator: $(command -v orchestrator)"
echo "==> orchestrator 插件安装完成"
