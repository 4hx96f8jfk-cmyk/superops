"""CLI 入口：superops-console 命令。token 缺省走 load_or_create_token()。"""

from __future__ import annotations

import argparse
import sys

from .app import create_app
from .auth import load_or_create_token


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog="superops-console", description="HongYun SuperOps 运维控制台")
    ap.add_argument("--db", required=True, help="effect-ledger SQLite 路径")
    ap.add_argument("--mode", default="read-only", choices=["read-only", "full"],
                    help="read-only=写端点 403；full=全功能+全审计（默认 read-only）")
    ap.add_argument("--modelhub-db", default=None, help="modelhub SQLite 路径（可选）")
    ap.add_argument("--host", default=None,
                    help="显式绑定地址（缺省 127.0.0.1，--lan 时 0.0.0.0）")
    ap.add_argument("--lan", action="store_true",
                    help="绑定 0.0.0.0 暴露到局域网（谨慎：token 即唯一凭据）")
    ap.add_argument("--port", type=int, default=8795)
    args = ap.parse_args()

    # 默认只绑回环（M3 安全项）：--lan 显式放开到 0.0.0.0，并在 stderr 醒目警告
    if args.host is not None:
        host = args.host
    else:
        host = "0.0.0.0" if args.lan else "127.0.0.1"
    if host not in ("127.0.0.1", "localhost"):
        print(
            "\n" + "!" * 66 + "\n"
            "!!  警告：控制台绑定在非回环地址 " + host + "，已暴露到局域网！\n"
            "!!  会话 token 是唯一凭据，明文 HTTP 下存在被嗅探/爆破风险。\n"
            "!!  请确认网络环境可信，或仅在使用完毕后立即关闭服务。\n"
            + "!" * 66 + "\n",
            file=sys.stderr,
        )

    token = load_or_create_token()
    app = create_app(args.db, token, mode=args.mode, modelhub_db=args.modelhub_db)
    uvicorn.run(app, host=host, port=args.port)


if __name__ == "__main__":
    main()
