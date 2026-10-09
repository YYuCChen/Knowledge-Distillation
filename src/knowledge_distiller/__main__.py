import argparse
import os
import socket
from pathlib import Path

from .v1.app import AppPaths, create_application
from .v1.file_lock import acquire


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 57740


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("端口必须是 1–65535 的整数") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须是 1–65535 的整数")
    return port


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="启动知识蒸馏器本地开发服务")
    parser.add_argument("--port", type=_port, default=os.environ.get("PORT", DEFAULT_PORT))
    parser.add_argument("--data-dir", type=Path, default=AppPaths.system_default().data_root,
                        help="SQLite 与运行文件目录；验收时指定独立目录")
    args = parser.parse_args(argv)
    paths = AppPaths(args.data_dir.expanduser().resolve())

    # Reject an occupied port before a worker can claim or recover any items.
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((DEFAULT_HOST, args.port))
    except OSError:
        parser.error(f"端口 {args.port} 已被占用；请使用已打开的服务或用 --port 指定其他端口")

    paths.data_root.mkdir(parents=True, exist_ok=True)
    # Two ports must not start two workers against the same SQLite queue.
    try:
        lock = acquire(paths.data_root / '.instance.lock')
    except BlockingIOError:
        parser.error(f"此数据目录已有服务运行：{paths.data_root}")
    with lock:
        app = create_application(paths)
        print(f"页面：http://{DEFAULT_HOST}:{args.port}", flush=True)
        print(f"数据目录：{paths.data_root}", flush=True)
        try:
            app.run(host=DEFAULT_HOST, port=args.port, use_reloader=False)
        finally:
            app.config['KNOWLEDGE_DISTILLER_CLOSE_FEISHU']()
            app.config["KNOWLEDGE_DISTILLER_WORKER"].stop()
            app.config["KNOWLEDGE_DISTILLER_CLOSE_BROWSERS"]()


if __name__ == "__main__":
    main()
