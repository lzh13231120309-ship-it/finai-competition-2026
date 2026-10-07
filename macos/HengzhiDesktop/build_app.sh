#!/bin/bash
# ============================================================================
#  构建「衡知」macOS 桌面应用
#
#  用法：
#      ./build_app.sh                  # 输出到 ~/Applications/衡知.app
#      ./build_app.sh /Applications/衡知.app
#
#  做四件事：
#      1. 用 swiftc 编译 Sources/main.swift（不需要 Xcode，命令行工具就够）
#      2. 从仓库的 app.ico 生成 AppIcon.icns
#      3. 组装标准 .app 目录结构，写入 Info.plist（含仓库绝对路径）
#      4. ad-hoc 签名，让系统能正常启动它
#
#  产物里不含任何金融逻辑，只是把仓库里的 Python 服务包了个原生窗口。
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
APP="${1:-$HOME/Applications/衡知.app}"
BUILD="${HERE}/build"
PYTHON="${REPO}/runtime/python/bin/python3"

say()  { printf '\033[1;35m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- 前置检查
command -v swiftc   >/dev/null 2>&1 || die "找不到 swiftc，请先安装 Xcode 命令行工具：xcode-select --install"
command -v iconutil >/dev/null 2>&1 || die "找不到 iconutil（应随 macOS 自带）"

[ -f "${HERE}/Sources/main.swift" ] || die "缺少源码：${HERE}/Sources/main.swift"
[ -f "${HERE}/Info.plist" ]         || die "缺少模板：${HERE}/Info.plist"
[ -f "${REPO}/app.ico" ]            || die "缺少图标源文件：${REPO}/app.ico"

# 图标生成依赖 Pillow，用仓库自己的虚拟环境，避免污染系统 Python
if [ ! -x "${PYTHON}" ]; then
    warn "还没装好运行环境（缺 ${PYTHON}）。"
    warn "应用仍可编译，但打开后会提示你先运行「安装环境.command」。"
fi

ARCH="$(uname -m)"
mkdir -p "${BUILD}"

# ---------------------------------------------------------------- 1. 编译
say "编译 Swift 源码（架构 ${ARCH}）"
swiftc -O -swift-version 5 \
    -target "${ARCH}-apple-macos13.0" \
    -framework Cocoa -framework WebKit \
    -o "${BUILD}/HengzhiDesktop" \
    "${HERE}/Sources/main.swift"

if [ ! -x "${BUILD}/HengzhiDesktop" ]; then
    die "编译未产出可执行文件"
fi
say "编译完成（$(du -h "${BUILD}/HengzhiDesktop" | cut -f1)）"

# ---------------------------------------------------------------- 2. 图标
ICNS="${BUILD}/AppIcon.icns"
if [ -x "${PYTHON}" ]; then
    say "生成应用图标"
    "${PYTHON}" "${HERE}/make_icon.py" "${REPO}/app.ico" "${ICNS}" \
        || die "图标生成失败"
elif [ -f "${ICNS}" ]; then
    warn "跳过图标生成，沿用上次的 ${ICNS}"
else
    warn "跳过图标生成（没有可用的 Python 环境，也没有历史图标），将使用系统默认图标"
fi

# ---------------------------------------------------------------- 3. 组装
say "组装 .app 应用包"
# 只清理我们自己的构建产物，且必须是以 .app 结尾的路径
case "${APP}" in
    *.app) ;;
    *) die "输出路径必须以 .app 结尾：${APP}" ;;
esac
rm -rf "${APP}"

mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"
cp "${BUILD}/HengzhiDesktop" "${APP}/Contents/MacOS/HengzhiDesktop"
chmod 755 "${APP}/Contents/MacOS/HengzhiDesktop"

if [ -f "${ICNS}" ]; then
    cp "${ICNS}" "${APP}/Contents/Resources/AppIcon.icns"
fi

printf 'APPL????' > "${APP}/Contents/PkgInfo"

# 把仓库绝对路径写进 Info.plist
sed "s|__REPO_ROOT__|${REPO}|g" "${HERE}/Info.plist" > "${APP}/Contents/Info.plist"
plutil -lint "${APP}/Contents/Info.plist" >/dev/null || die "Info.plist 格式不合法"

# ---------------------------------------------------------------- 4. 签名
say "ad-hoc 签名"
codesign --force --sign - "${APP}" 2>/dev/null || warn "签名失败（不影响本机运行）"

# ---------------------------------------------------------------- 完成
echo
say "构建完成"
echo "   应用：${APP}"
echo "   仓库：${REPO}"
echo
echo "   双击「衡知」即可打开。首次启动要加载本地模型，耐心等十几秒到一分钟。"
echo "   如果提示没装环境，先在仓库里双击「安装环境.command」。"
echo
echo "   想放到「启动台」：把 ${APP} 拖到「应用程序」文件夹即可。"
echo
