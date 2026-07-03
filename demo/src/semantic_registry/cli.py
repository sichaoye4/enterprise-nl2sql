from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn

from src.semantic_registry.config import get_settings
from src.semantic_registry.database import get_sessionmaker
from src.semantic_registry.sync import sync_all
from src.semantic_registry.yaml_schema import validate_all_yaml_files, validate_yaml_file


def _print_validation_errors(errors: dict[str, list[object]]) -> None:
    for file_name, file_errors in sorted(errors.items()):
        for error in file_errors:
            print(f"{file_name}: {error}", file=sys.stderr)


def validate_command(args: argparse.Namespace) -> int:
    if args.path:
        errors = {args.path: validate_yaml_file(args.path)}
        errors = {key: value for key, value in errors.items() if value}
    else:
        errors = validate_all_yaml_files(args.directory)
    if errors:
        _print_validation_errors(errors)
        print(f"Validation failed: {sum(len(value) for value in errors.values())} error(s)", file=sys.stderr)
        return 1
    print("Validation passed")
    return 0


async def _sync_command_async(args: argparse.Namespace) -> int:
    async with get_sessionmaker()() as session:
        report = await sync_all(session=session, semantic_dir=args.directory, dry_run=args.dry_run)
    print(report.model_dump_json(indent=2))
    return 1 if report.errors else 0


def sync_command(args: argparse.Namespace) -> int:
    return asyncio.run(_sync_command_async(args))


def serve_command(args: argparse.Namespace) -> int:
    settings = get_settings()
    uvicorn.run(
        "src.semantic_registry.api.main:app",
        host=args.host,
        port=args.port or settings.api_port,
        reload=args.reload,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.semantic_registry.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate semantic YAML files")
    validate_parser.add_argument("path", nargs="?", help="Optional YAML file to validate")
    validate_parser.add_argument("--directory", default=str(get_settings().semantic_dir), help="Semantic YAML root")
    validate_parser.set_defaults(func=validate_command)

    sync_parser = subparsers.add_parser("sync", help="Sync semantic YAML files to the database")
    sync_parser.add_argument("--directory", default=str(get_settings().semantic_dir), help="Semantic YAML root")
    sync_parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    sync_parser.set_defaults(func=sync_command)

    serve_parser = subparsers.add_parser("serve", help="Start the semantic registry API")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.add_argument("--reload", action="store_true")
    serve_parser.set_defaults(func=serve_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
