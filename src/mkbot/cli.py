"""mkbot 本地实例的命令行入口。"""

import argparse
import os
import secrets
from pathlib import Path

import uvicorn

from mkbot.api.app import create_app
from mkbot.runtime.kernel import Kernel


def admin_token(data_dir: Path) -> str:
    if value := os.environ.get("MKBOT_ADMIN_TOKEN"):
        if len(value) < 16:
            raise ValueError("MKBOT_ADMIN_TOKEN 至少需要 16 个字符")
        return value
    path = data_dir / "admin-token"
    data_dir.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(32))
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 16:
        raise ValueError("管理令牌文件无效，请检查实例目录")
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mkbot", description="自主角色运行框架")
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化本地实例及管理令牌")
    serve = commands.add_parser("serve", help="启动管理服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8096)
    args = parser.parse_args(argv)
    data_dir = args.data_dir.resolve()
    token = admin_token(data_dir)
    if args.command == "init":
        instance = Kernel(data_dir)
        instance.close()
        print(f"mkbot 实例已初始化：{data_dir}")
        if os.environ.get("MKBOT_ADMIN_TOKEN"):
            print("管理令牌来源：MKBOT_ADMIN_TOKEN 环境变量")
        else:
            print(f"管理令牌文件：{data_dir / 'admin-token'}")
    else:
        uvicorn.run(create_app(data_dir, token), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
