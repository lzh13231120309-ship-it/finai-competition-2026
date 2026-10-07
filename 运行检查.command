#!/bin/bash
# 衡知 · macOS 安装检查脚本
# 对应 Windows 版 运行检查.bat，功能等价：校验依赖、MinerU 资源与 Qwen 模型指纹。
# 用法：在 Finder 里双击本文件。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/runtime/python/bin/python"

if [ ! -x "$PY" ]; then
    echo "未找到运行环境：${PY}"
    echo "请先双击「安装环境.command」完成安装。"
    echo
    read -n 1 -s -r -p "按任意键关闭本窗口…"
    exit 1
fi

"$PY" -X utf8 "$ROOT/scripts/check_install.py"
code=$?

echo
if [ $code -eq 0 ]; then
    echo "检查通过。"
else
    echo "检查未通过（退出码 ${code}）。"
fi
read -n 1 -s -r -p "按任意键关闭本窗口…"
exit ${code}
