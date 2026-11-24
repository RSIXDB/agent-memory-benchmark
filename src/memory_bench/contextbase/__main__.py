"""Run with python -m memory_bench.contextbase; all execution is reuse-only."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from .legacy import prepare, replay
from .models import Recipe
from .runner import run


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare-history")
    prep.add_argument("--dataset", choices=["longmemeval-s", "beam10m"], required=True)
    prep.add_argument("--historical-root", type=Path, required=True)
    prep.add_argument("--audit-directory", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    for name in ["replay", "run"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", type=Path, required=True)
        cmd.add_argument("--environment-report", type=Path)
        cmd.add_argument("--source-dsn-file", type=Path)
        cmd.add_argument("--read-key-file", type=Path)
        cmd.add_argument("--bank-keys-file", type=Path)
        if name == "replay":
            cmd.add_argument("--historical-root", type=Path, required=True)
            cmd.add_argument("--report", type=Path, required=True)
        else:
            cmd.add_argument("--execute-models", action="store_true")
            cmd.add_argument("--max-model-calls", type=int, required=True)
            cmd.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.command == "prepare-history":
        prepare(args.historical_root, args.dataset, args.audit_directory, args.output)
        return
    recipe = Recipe.model_validate_json(args.config.read_text())
    if args.environment_report:
        recipe.environment_report = args.environment_report
    if args.source_dsn_file:
        os.environ[recipe.postgres_dsn_env] = args.source_dsn_file.read_text().strip()
    if args.read_key_file:
        os.environ[recipe.api_key_env] = json.loads(args.read_key_file.read_text())[
            "secret"
        ]
    if args.bank_keys_file:
        if not recipe.api_keys_file_env:
            parser.error("Recipe does not declare per-bank credentials")
        os.environ[recipe.api_keys_file_env] = str(args.bank_keys_file)
    if args.command == "replay":
        asyncio.run(replay(recipe, args.historical_root, args.report))
    else:
        if (
            not args.execute_models
            or args.max_model_calls <= 0
            or (args.limit is not None and args.limit <= 0)
        ):
            parser.error(
                "Live execution requires --execute-models, a positive call cap and positive limit"
            )
        asyncio.run(run(recipe, args.limit, args.max_model_calls))


if __name__ == "__main__":
    main()
