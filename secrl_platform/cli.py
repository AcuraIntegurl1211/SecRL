import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secrl-lite")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    subparsers.add_parser("run-worker")
    subparsers.add_parser("verify-artifacts")
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
    from secrl_platform.storage.artifacts import verify_all_artifacts

    return 0 if verify_all_artifacts() else 1
