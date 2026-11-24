"""Read-only source access; no retain, bank writes, queue retry or index creation."""

import gzip
import hashlib
import json
import os
import re
from pathlib import Path

from .models import Question, Recipe, SourceDocument


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def read_json(path: Path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as stream:
            return json.load(stream)
    return json.loads(path.read_text())


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class RunIntegrityError(RuntimeError):
    """Stop a run without publishing a new aggregate score."""


def source_fingerprint(rows: list[SourceDocument]) -> str:
    return digest([x.model_dump() for x in sorted(rows, key=lambda x: x.id)])


class SourceReader:
    def __init__(self, recipe: Recipe, require_sealed: bool = False):
        self.recipe = recipe
        report = read_json(recipe.audit_report)
        self.report = report
        manifest_path = recipe.audit_report.parent / "audit-run.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        if manifest.get("failed_sections") or manifest.get("state") == "running":
            raise RunIntegrityError("The latest audit is failed or unfinished")
        if require_sealed or "audit_run_id" in report:
            expected = manifest.get("reports", {}).get(recipe.audit_report.name)
            if (
                manifest.get("state") != "complete"
                or report.get("audit_run_id") != manifest.get("id")
                or not expected
                or hashlib.sha256(recipe.audit_report.read_bytes()).hexdigest()
                != expected
            ):
                raise RunIntegrityError(
                    "Audit is unsealed, stale or incomplete; run a fresh audit"
                )
        if report.get("reusable") is not True or report.get("errors"):
            raise ValueError(
                "Reuse audit did not pass; do not import or overwrite automatically"
            )
        if report.get("dataset") != recipe.dataset:
            raise ValueError("Audit dataset mismatch")
        self.banks = {row["bank_id"]: row for row in report["checks"]}

    async def verify_database(self) -> None:
        if self.recipe.source != "postgres":
            return
        import asyncpg

        connection = await asyncpg.connect(os.environ[self.recipe.postgres_dsn_env])
        try:
            async with connection.transaction(readonly=True):
                row = await connection.fetchrow(
                    "SELECT current_database() AS database, inet_server_addr()::text AS address, inet_server_port() AS port"
                )
                if dict(row) != self.report.get("postgres", {}).get("identity"):
                    raise RunIntegrityError("Database identity differs from the audit")
            for bank, receipt in self.banks.items():
                async with connection.transaction(readonly=True):
                    schema = self.require_bank(
                        Question(id="identity", query="", bank_id=bank)
                    )
                    config = await connection.fetchval(
                        f'SELECT config FROM "{schema}".banks WHERE bank_id=$1', bank
                    )
                    if isinstance(config, str):
                        config = json.loads(config)
                    if digest(config) != receipt.get("bank_config_sha256"):
                        raise RunIntegrityError(
                            "Bank configuration differs from the audit"
                        )
        finally:
            await connection.close()

    def verify_sources(self, question: Question, rows: list[SourceDocument]) -> None:
        expected = self.banks[question.bank_id].get("source_sha256")
        if not expected or source_fingerprint(rows) != expected:
            raise RunIntegrityError(
                "Source content differs from the audit; no new aggregate score"
            )

    def require_bank(self, question: Question) -> str:
        if question.bank_id not in self.banks:
            raise RunIntegrityError("Question bank not present in verified reuse audit")
        schema = self.banks[question.bank_id]["schema"]
        if not re.fullmatch("ctxdb_ws_[a-z0-9_]+", schema):
            raise RunIntegrityError("Invalid workspace schema")
        return schema

    async def read(self, question: Question) -> list[SourceDocument]:
        schema = self.require_bank(question)
        if self.recipe.source == "json":
            path = self.recipe.source_directory / (
                hashlib.sha256(question.id.encode()).hexdigest() + ".json.gz"
            )
            data = read_json(path)
            if data["bank_id"] != question.bank_id:
                raise RunIntegrityError("Source file bank mismatch")
            output = [SourceDocument.model_validate(row) for row in data["documents"]]
            if "audit_run_id" in self.report:
                self.verify_sources(question, output)
            return output
        import asyncpg

        connection = await asyncpg.connect(os.environ[self.recipe.postgres_dsn_env])
        try:
            async with connection.transaction(
                isolation="repeatable_read", readonly=True
            ):
                actual = await connection.fetchval(
                    "SELECT schema_name FROM ctxdb_meta.workspaces WHERE bank_id=$1 AND state='active'",
                    question.bank_id,
                )
                if actual != schema:
                    raise RunIntegrityError("Bank mapping differs from audit")
                await connection.execute("SET LOCAL statement_timeout='60s'")
                # Historical #115 reads this bank's source, not just returned chunks.
                rows = await connection.fetch(
                    f'SELECT id,original_text,retain_params FROM "{schema}".documents ORDER BY id'
                )
                output = []
                for row in rows:
                    params = row["retain_params"]
                    if isinstance(params, str):
                        params = json.loads(params)
                    output.append(
                        SourceDocument(
                            id=row["id"],
                            original_text=row["original_text"],
                            retain_params=params,
                        )
                    )
                if len(output) != self.banks[question.bank_id]["counts"]["documents"]:
                    raise RunIntegrityError(
                        "Stored document count differs from reuse audit"
                    )
                if "audit_run_id" in self.report:
                    self.verify_sources(question, output)
                return output
        finally:
            await connection.close()
