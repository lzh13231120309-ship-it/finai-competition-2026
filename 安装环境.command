#!/bin/bash
# 衡知 · macOS 环境安装脚本
# 对应 Windows 版 安装环境.ps1 / 安装环境.bat，功能等价：
#   1. 定位 Python 3.13   2. 创建 runtime/python 虚拟环境   3. 安装依赖
#   4. 下载 MinerU 资源与 Qwen3-4B 模型并校验官方 SHA256   5. 运行完整检查
# 用法：在 Finder 里双击本文件；或在终端执行 ./安装环境.command [--skip-models]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="$ROOT/第一阶段_金融提取"
LOG_DIR="$ROOT/runtime"
LOG="$LOG_DIR/install.log"
SKIP_MODELS=0
[ "${1:-}" = "--skip-models" ] && SKIP_MODELS=1

mkdir -p "$LOG_DIR"
# 与 Windows 版 Start-Transcript 等价：全部输出同时进终端和 runtime/install.log
exec > >(tee -a "$LOG") 2>&1

echo "=== 衡知 macOS 环境安装 ==="
echo "仓库位置：$ROOT"
echo "安装日志：$LOG"
echo

fail() {
    echo
    echo "安装失败：${1}"
    echo "完整日志见：$LOG"
    echo
    read -n 1 -s -r -p "按任意键关闭本窗口…"
    exit 1
}

# 本地模型对非 ASCII 路径兼容性差，与 Windows 版保持同一约束
if printf '%s' "$ROOT" | LC_ALL=C grep -q '[^ -~]'; then
    fail "请把仓库移到纯英文路径，例如 /Users/$(whoami)/FinAI/finai-competition-2026；本地模型不支持所有中文路径。"
fi

# ---------------------------------------------------------------- 1. Python 3.13
# 优先使用用户自行安装的 Python（python.org 安装包 / Homebrew），
# 避免把项目环境绑到某个工具的私有运行时上。
find_python() {
    local cand path
    for cand in \
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
        /opt/homebrew/bin/python3.13 \
        /usr/local/bin/python3.13 \
        /usr/local/bin/python3 \
        python3.13 \
        python3 ; do
        path="$(command -v "$cand" 2>/dev/null)" || continue
        # 跳过工具自带的隔离运行时，保证环境独立可迁移
        case "$path" in */.workbuddy-ai/*|*/.codebuddy-ai/*) continue ;; esac
        if "$path" -c 'import sys; raise SystemExit(0 if sys.version_info[:2]==(3,13) else 1)' 2>/dev/null; then
            printf '%s' "$path"
            return 0
        fi
    done
    return 1
}

PYBIN="$(find_python)"
if [ -z "$PYBIN" ]; then
    fail "未找到 Python 3.13。请先安装：
  方式一（推荐）：https://www.python.org/downloads/macos/ 下载 Python 3.13 安装包
  方式二：brew install python@3.13"
fi
echo "使用 Python：${PYBIN}（$("$PYBIN" -V 2>&1)）"

# ---------------------------------------------------------------- 2. 虚拟环境
PY="$ROOT/runtime/python/bin/python"
if [ ! -x "$PY" ]; then
    echo "创建虚拟环境 runtime/python …"
    "$PYBIN" -m venv "$ROOT/runtime/python" || fail "创建虚拟环境失败。"
fi
[ -x "$PY" ] || fail "虚拟环境创建后仍找不到 ${PY}。"

"$PY" -c 'import sys; assert sys.version_info[:2]==(3,13), "需要 Python 3.13"' \
    || fail "虚拟环境 Python 版本不是 3.13。"

# ---------------------------------------------------------------- 3. 安装依赖
echo
echo "安装依赖（首次较慢，请耐心等待）…"
"$PY" -m pip install --upgrade pip || fail "升级 pip 失败。"
"$PY" -m pip install -r "$STAGE/requirements-lock.txt" || fail "依赖安装失败，请检查网络或代理设置。"
"$PY" -m pip check || fail "依赖自检未通过（pip check）。"
echo "依赖安装完成。"

# ---------------------------------------------------------------- 4. 下载模型
if [ "$SKIP_MODELS" -eq 0 ]; then
    echo
    echo "下载 MinerU 资源与 Qwen3-4B 模型（约数 GB，请保持网络畅通）…"
    export MINERU_HOME="$ROOT/runtime/mineru"
    export MINERU_CONFIG="$STAGE/mineru-local.yaml"
    export MINERU_MODEL_BASE_DIR="$ROOT/runtime/models"
    export MINERU_MODEL_SMALL_BACKEND='onnx'
    export MINERU_MODEL_VLM_ENGINE='llama-cpp'

    DOWNLOADER="$ROOT/runtime/python/bin/mineru-models-download"
    [ -x "$DOWNLOADER" ] || fail "未找到 ${DOWNLOADER}，依赖可能未安装完整。"
    "$DOWNLOADER" --tier standard --small-backend onnx --vlm-engine llama-cpp --source modelscope \
        || fail "MinerU 资源下载失败。"

    "$PY" -X utf8 "$ROOT/金融大赛_集成试用版/download_reasoning.py" \
        || fail "Qwen3-4B 模型下载或指纹校验失败。"

    # ------------------------------------------------------------ 5. 完整检查
    echo
    echo "运行安装检查 …"
    "$PY" -X utf8 "$ROOT/scripts/check_install.py" || fail "安装检查未通过。"
else
    echo
    echo "已跳过模型下载（--skip-models）。"
fi

echo
echo "软件环境安装成功。完整模型检查通过后，可双击「启动衡知.command」。"
echo
read -n 1 -s -r -p "按任意键关闭本窗口…"
