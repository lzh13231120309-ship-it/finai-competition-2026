#!/bin/bash
# 衡知 · macOS 停止脚本
# 对应 Windows 版 停止衡知.bat → stop_integrated.ps1，安全约束完全一致：
#   · 进程身份不匹配则拒绝关闭（避免误杀同名程序）
#   · Agent 对话仍在执行时拒绝停止
#   · 有提取任务未结束时拒绝停止
# 用法：在 Finder 里双击本文件。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/金融大赛_集成试用版"
PY="$ROOT/runtime/python/bin/python"

fail() {
    echo
    echo "停止失败：$1"
    echo
    read -n 1 -s -r -p "按任意键关闭本窗口…"
    exit 1
}

[ -x "$PY" ] || fail "未找到运行环境：${PY}
请先双击「安装环境.command」完成安装。"

# 读取 pid 文件；文件不存在或内容非法时输出空
read_pid() {
    local f="$1"
    [ -f "$f" ] || return 0
    local v
    v="$(tr -d '[:space:]' < "$f" 2>/dev/null)"
    case "$v" in
        ''|*[!0-9]*) return 0 ;;
    esac
    printf '%s' "$v"
}

# 进程是否存活
alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

# 进程完整命令行
cmdline() { ps -p "$1" -o command= 2>/dev/null; }

# 进程可执行文件名
procname() { basename "$(ps -p "$1" -o comm= 2>/dev/null)" 2>/dev/null; }

# ------------------------------------------------------------ 1. 定位 Web 服务
SERVICE_PID="$(read_pid "$APP/data/logs/integrated.pid")"
if [ -n "$SERVICE_PID" ] && alive "$SERVICE_PID"; then
    CMD="$(cmdline "$SERVICE_PID")"
    case "$CMD" in
        *server.app:app*) ;;
        *) fail "服务进程身份不匹配，拒绝关闭其他程序（PID ${SERVICE_PID}）。" ;;
    esac
    case "$CMD" in
        *17904*) ;;
        *) fail "服务进程身份不匹配，拒绝关闭其他程序（PID ${SERVICE_PID}）。" ;;
    esac

    if [ ! -x "$PY" ]; then
        fail "未找到运行环境 ${PY}，无法查询服务状态。"
    fi
    RUNNING_TASKS="$("$PY" -c "
import json, urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:17904/api/health', timeout=4) as r:
        print(json.load(r).get('running_tasks', 0))
except Exception:
    print('ERR')
" 2>/dev/null)"

    [ "$RUNNING_TASKS" = "ERR" ] && fail "无法连接服务健康检查接口（http://127.0.0.1:17904/api/health）。"
    case "$RUNNING_TASKS" in
        ''|*[!0-9]*) fail "健康检查返回异常：$RUNNING_TASKS" ;;
    esac
    if [ "$RUNNING_TASKS" -gt 0 ]; then
        fail "Agent 对话仍在执行，请结束后再停止。"
    fi
else
    SERVICE_PID=""
fi

# ------------------------------------------------------------ 2. 提取任务是否结束
JOBS="$ROOT/第一阶段_金融提取/local_data/jobs"
if [ -d "$JOBS" ]; then
    while IFS= read -r -d '' f; do
        status="$("$PY" -c "
import json,sys
try:
    print(json.load(open(sys.argv[1], encoding='utf-8')).get('status',''))
except Exception:
    print('')
" "$f" 2>/dev/null)"
        case "$status" in
            running|queued) fail "有提取任务尚未结束，请完成后再停止。" ;;
        esac
    done < <(find "$JOBS" -name state.json -print0 2>/dev/null)
fi

# ------------------------------------------------------------ 3. 停止 Web 服务
if [ -n "$SERVICE_PID" ]; then
    kill -TERM "$SERVICE_PID" 2>/dev/null
    for _ in $(seq 1 50); do
        alive "$SERVICE_PID" || break
        sleep 0.1
    done
    if alive "$SERVICE_PID"; then
        kill -KILL "$SERVICE_PID" 2>/dev/null
    fi
    rm -f "$APP/data/logs/integrated.pid"
fi

# ------------------------------------------------------------ 4. 停止本地推理服务
MODEL_PID="$(read_pid "$APP/data/logs/reasoning.pid")"
if [ -n "$MODEL_PID" ] && alive "$MODEL_PID"; then
    NAME="$(procname "$MODEL_PID")"
    MCMD="$(cmdline "$MODEL_PID")"

    # 二进制名按平台判定：macOS/Linux 为 llama-server，Windows 为 llama-server.exe
    case "$NAME" in
        llama-server|llama-server.exe) ;;
        *) fail "推理进程身份不匹配，拒绝关闭其他模型（PID ${MODEL_PID}，实际为 ${NAME}）。" ;;
    esac
    case "$MCMD" in
        *17907*) ;;
        *) fail "推理进程身份不匹配，拒绝关闭其他模型（PID ${MODEL_PID}）。" ;;
    esac
    case "$MCMD" in
        *finai-qwen3-4b*) ;;
        *) fail "推理进程身份不匹配，拒绝关闭其他模型（PID ${MODEL_PID}）。" ;;
    esac

    kill -TERM "$MODEL_PID" 2>/dev/null
    for _ in $(seq 1 50); do
        alive "$MODEL_PID" || break
        sleep 0.1
    done
    if alive "$MODEL_PID"; then
        kill -KILL "$MODEL_PID" 2>/dev/null
    fi
    rm -f "$APP/data/logs/reasoning.pid"
fi

echo "集成版与本地推理服务已停止。"
echo
read -n 1 -s -r -p "按任意键关闭本窗口…"
