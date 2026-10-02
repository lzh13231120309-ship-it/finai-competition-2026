"""应用启动器。

由 macOS 应用外壳（人工智能金融大赛.app）作为子进程拉起，承担两件事：

1. 承载 uvicorn 服务；
2. 监视父进程存活状态 —— 一旦应用外壳退出（含被强制退出），
   本进程自动终止，避免留下占着端口的孤儿进程。

单独手动运行时也完全可用：

    python -m server.launcher --port 17899
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time


def _watch_parent(parent_pid: int, interval: float = 1.0) -> None:
    """父进程退出后 os.getppid() 会变成 1（被 launchd 收养），据此自杀。"""
    while True:
        time.sleep(interval)
        try:
            if os.getppid() != parent_pid:
                os._exit(0)
        except Exception:
            os._exit(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="server.launcher", description="本地 Agent 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log-level", default="warning")
    parser.add_argument("--no-parent-watch", action="store_true",
                        help="禁用父进程监视（手动前台运行时可加）")
    args = parser.parse_args(argv)

    if not args.no_parent_watch:
        parent_pid = os.getppid()
        threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True).start()

    import uvicorn

    from server.app import app

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
