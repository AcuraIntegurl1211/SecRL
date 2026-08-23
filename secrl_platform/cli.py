import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secrl-lite")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    subparsers.add_parser("run-worker")
    subparsers.add_parser("verify-artifacts")
    init_admin = subparsers.add_parser("init-admin")
    init_admin.add_argument("--username", default="admin")
    init_admin.add_argument("--password", default=None)
    backup = subparsers.add_parser("backup")
    backup.add_argument("backup_dir")
    restore = subparsers.add_parser("restore")
    restore.add_argument("backup_dir")
    restore.add_argument("target_dir")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "secrl_platform.api.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
        )
        return 0
    if args.command == "run-worker":
        from secrl_platform.runner.process import run_forever

        return run_forever()
    if args.command == "init-admin":
        import os

        from sqlalchemy import select

        from secrl_platform.auth.passwords import hash_password
        from secrl_platform.config import Settings
        from secrl_platform.storage.database import create_engine_and_session
        from secrl_platform.storage.orm import LocalUserORM

        password = args.password or os.environ.get("SECRL_INITIAL_ADMIN_PASSWORD", "")
        if not password:
            raise SystemExit("SECRL_INITIAL_ADMIN_PASSWORD is required")
        settings = Settings()
        sessions = create_engine_and_session(settings.database_path)
        with sessions.begin() as session:
            existing = session.scalar(
                select(LocalUserORM).where(LocalUserORM.username == args.username)
            )
            if existing is None:
                session.add(
                    LocalUserORM(
                        username=args.username,
                        password_hash=hash_password(password),
                        status="ACTIVE",
                    )
                )
        return 0
    if args.command == "backup":
        import os

        from secrl_platform.storage.backup import create_backup

        data_dir = Path(os.environ.get("SECRL_DATA_DIR", "/data"))
        result = create_backup(data_dir, Path(args.backup_dir))
        print(result.backup_dir)
        return 0
    if args.command == "restore":
        from secrl_platform.storage.backup import restore_backup

        restore_backup(Path(args.backup_dir), Path(args.target_dir))
        return 0
    from secrl_platform.storage.artifacts import verify_all_artifacts

    return 0 if verify_all_artifacts() else 1
