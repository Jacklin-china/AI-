"""Unified Kantoku command entry point."""

from __future__ import annotations

import argparse

from kantoku.config.logging_setup import archive_development_log, setup_logging
from kantoku.doctor import print_doctor
from kantoku.shells.web_studio import serve


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m kantoku")
    commands = parser.add_subparsers(dest="command")
    serve_parser = commands.add_parser("serve", help="启动 API、SSE 与生产工作台")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--no-browser", action="store_true")
    doctor_parser = commands.add_parser("doctor", help="检查本机配置与供应商就绪状态")
    doctor_parser.add_argument("--live", action="store_true")
    logs_parser = commands.add_parser("logs", help="仅管理开发应用日志，不触及审计与业务数据")
    logs_parser.add_argument("action", choices=["archive"])
    args = parser.parse_args()
    if args.command == "logs":
        archived = archive_development_log()
        print(
            f"Archived application log: {archived}"
            if archived else "No application log to archive"
        )
        return 0
    setup_logging()
    if args.command == "doctor":
        return print_doctor(live=args.live)
    return serve(
        port=getattr(args, "port", 8000),
        open_browser=not getattr(args, "no_browser", False),
    )


if __name__ == "__main__":
    raise SystemExit(main())
