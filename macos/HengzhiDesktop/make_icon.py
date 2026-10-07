#!/usr/bin/env python3
"""把仓库里的 app.ico 转成 macOS 的 .icns 图标。

用法：
    make_icon.py <app.ico> <输出.icns>

Windows 用的 .ico 里通常只做到 256px，而 macOS 需要 512/1024 的 @2x 图。
所以策略是：小尺寸直接用 ico 里的原始帧（不重采样，最清晰），
大尺寸用 LANCZOS 放大后再做一次 UnsharpMask 补锐度。
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter

# macOS iconset 需要的全部文件名 -> 像素边长
ICONSET = {
    'icon_16x16.png': 16,
    'icon_16x16@2x.png': 32,
    'icon_32x32.png': 32,
    'icon_32x32@2x.png': 64,
    'icon_128x128.png': 128,
    'icon_128x128@2x.png': 256,
    'icon_256x256.png': 256,
    'icon_256x256@2x.png': 512,
    'icon_512x512.png': 512,
    'icon_512x512@2x.png': 1024,
}


def load_source(path):
    """打开 .ico 并返回 (可复用句柄, 原生可用尺寸集合, 最大边长)。"""
    img = Image.open(path)
    sizes = set()
    if hasattr(img, 'ico'):
        try:
            sizes = {int(s[0]) for s in img.ico.sizes()}
        except Exception:
            sizes = set()
    if not sizes:
        sizes = {img.size[0]}
    largest = max(sizes)
    img.size = (largest, largest)
    return img, sizes, largest


def frame(img, size, native_sizes, largest):
    """取出指定边长的图像：原生帧直接用，否则从最大帧缩放。"""
    if size in native_sizes:
        img.size = (size, size)
        return img.convert('RGBA')
    img.size = (largest, largest)
    base = img.convert('RGBA')
    out = base.resize((size, size), Image.LANCZOS)
    if size > largest:
        # 放大必然发虚，轻微锐化一下，缩略图看起来更利落
        out = out.filter(ImageFilter.UnsharpMask(radius=1.6, percent=115, threshold=3))
    return out


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    src = Path(sys.argv[1]).expanduser().resolve()
    dst = Path(sys.argv[2]).expanduser().resolve()
    if not src.is_file():
        print(f'找不到图标源文件：{src}')
        return 1

    img, native_sizes, largest = load_source(src)
    print(f'源图标：{src.name}  原生尺寸 {sorted(native_sizes)}  最大 {largest}px')

    work = Path(tempfile.mkdtemp(prefix='hengzhi-icon-'))
    iconset = work / 'AppIcon.iconset'
    iconset.mkdir()

    try:
        for name, size in ICONSET.items():
            frame(img, size, native_sizes, largest).save(iconset / name, 'PNG')
        dst.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(dst)],
                       check=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f'已生成图标：{dst}  ({dst.stat().st_size / 1024:.0f} KB)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
