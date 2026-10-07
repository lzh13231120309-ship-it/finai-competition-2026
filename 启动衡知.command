#!/bin/bash
# 衡知 · macOS 启动脚本
# 对应 Windows 版 启动衡知.bat → start_integrated.ps1，功能等价：
#   按配置决定是否拉起本地 Qwen 推理服务，再启动集成版 Web 服务并打开浏览器。
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

"$PY" -X utf8 "$ROOT/金融大赛_集成试用版/start_integrated.py" "$@"
code=$?

if [ $code -ne 0 ]; then
    echo
    echo "启动失败（退出码 ${code}）。请查看："
    echo "  $ROOT/金融大赛_集成试用版/data/logs/integrated.log"
    echo "  $ROOT/金融大赛_集成试用版/data/logs/reasoning.log"
    echo
    read -n 1 -s -r -p "按任意键关闭本窗口…"
fi
exit ${code}
