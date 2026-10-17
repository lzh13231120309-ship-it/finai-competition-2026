#!/bin/zsh
# 一键构建 macOS 应用：人工智能金融大赛.app
#
# 用法：在项目根目录执行  zsh macos/build_app.sh
# 产物：项目根目录下 人工智能金融大赛.app

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="衡知"
APP="$ROOT/$APP_NAME.app"
BUILD="$ROOT/build"
SRC="$ROOT/macos/FinAgentApp/main.swift"
SDK="$(xcrun --sdk macosx --show-sdk-path)"

# Moving a live bundle invalidates imported Python resource paths. Match the
# exact executable path, including spaces, before touching any build products.
while read -r running_pid running_command; do
    if [[ "$running_command" == "$APP/Contents/MacOS/FinAgent" ]]; then
        echo "构建已停止：当前应用仍在运行（PID $running_pid）。请先退出 $APP，再重新构建。" >&2
        exit 1
    fi
done < <(ps -axo pid=,comm=)

echo "==> 1/5 清理旧产物"
# 旧产物移入废纸篓，不用 rm：
#   · 本机约定是"删除一律移入备份目录或废纸篓"，可恢复；
#   · 同时避开沙箱的批量删除保护（.app 与 runtime 各有 5000+ 文件）。
STAMP="$(date +%Y%m%d-%H%M%S)"
TRASH="$HOME/.Trash"
mkdir -p "$TRASH"
if [ -e "$APP" ]; then mv "$APP" "$TRASH/${APP_NAME}.app-${STAMP}"; fi
if [ -e "$BUILD/staging" ]; then mv "$BUILD/staging" "$TRASH/finagent-staging-${STAMP}"; fi
if [ -e "$BUILD/runtime" ]; then mv "$BUILD/runtime" "$TRASH/finagent-runtime-${STAMP}"; fi
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> 2/5 准备 runtime（server + web + venv + 种子数据）"
if [ -f "$ROOT/server/native/ocr.swift" ]; then
    swiftc -O -swift-version 5 -sdk "$SDK" -target arm64-apple-macosx13.0 \
        -framework Vision -framework AppKit "$ROOT/server/native/ocr.swift" \
        -o "$ROOT/server/native/finocr"
    chmod +x "$ROOT/server/native/finocr"
fi
# 依赖有变动时执行：zsh macos/build_app.sh --refresh-venv
if [ "${1:-}" = "--refresh-venv" ]; then
    echo "    刷新 venv 缓存"
    rm -rf "$BUILD/venv-cache"
fi
mkdir -p "$BUILD/runtime/seed"
# 用 rsync 的排除规则代替"先整目录复制、再回头删 pycache"：
# 既少一遍遍历，也不会因为批量删除 __pycache__/*.pyc 触发沙箱删除保护。
rsync -a --exclude='__pycache__' --exclude='*.pyc' "$ROOT/server/" "$BUILD/runtime/server/"
rsync -a --exclude='__pycache__' --exclude='*.pyc' "$ROOT/web/" "$BUILD/runtime/web/"
# venv 很大且基本不变：不存在时才从项目里复制
if [ ! -d "$BUILD/venv-cache" ]; then
    echo "    首次构建：复制 venv（约 65M，只需一次）"
    cp -R "$ROOT/.venv" "$BUILD/venv-cache"
fi
rsync -a --exclude='__pycache__' --exclude='*.pyc' "$BUILD/venv-cache/" "$BUILD/runtime/venv/"
cp -R "$ROOT/seed/skills" "$BUILD/runtime/seed/skills"
# 本地知识库必须一起打包：否则 .app 里知识库是空的，
# 而开发模式下一切正常 —— 属于打包后才暴露的问题。
mkdir -p "$BUILD/runtime/seed/knowledge"
cp -R "$ROOT/seed/knowledge/." "$BUILD/runtime/seed/knowledge/"
cp -R "$BUILD/runtime" "$APP/Contents/Resources/runtime"

echo "==> 3/5 写入 Info.plist 与图标"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
    <key>CFBundleDisplayName</key><string>衡知</string>
    <key>CFBundleName</key><string>衡知</string>
    <key>CFBundleExecutable</key><string>FinAgent</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundleIdentifier</key><string>local.finagent.desktop</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.6.0</string>
    <key>CFBundleVersion</key><string>13</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSPrincipalClass</key><string>NSApplication</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key><true/>
    </dict>
</dict>
</plist>
PLIST

cp "$ROOT/macos/Hengzhi.icns" "$APP/Contents/Resources/AppIcon.icns"

echo "==> 4/5 编译 Swift 外壳"
swiftc -O -swift-version 5 \
    -sdk "$SDK" \
    -target arm64-apple-macosx13.0 \
    -framework Cocoa -framework WebKit \
    "$SRC" -o "$APP/Contents/MacOS/FinAgent"

echo "==> 5/5 Ad-hoc 签名"
codesign --force --sign - "$APP" >/dev/null 2>&1 || true

echo
echo "构建完成：$APP"
du -sh "$APP"
