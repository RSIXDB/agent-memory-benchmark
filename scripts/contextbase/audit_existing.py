"""Read-only audit of existing benchmark banks against sealed historical receipts."""

import argparse
import asyncio
import datetime
import fcntl
import gzip
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

import asyncpg

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--beam-root", type=Path, required=True)
parser.add_argument("--lme-root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
B, L, OUT = (args.beam_root, args.lme_root, args.output)
os.umask(63)
OUT.mkdir(exist_ok=True, parents=True, mode=448)
AUDIT_LOCK = (OUT / ".audit.lock").open("a+")
fcntl.flock(AUDIT_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
RUN_ID = uuid.uuid4().hex


def load(p):
    return json.loads(p.read_text())


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def source_fingerprint(rows):
    return digest(
        [
            {
                "id": row["id"],
                "original_text": row["original_text"],
                "retain_params": json.loads(row["retain_params"])
                if isinstance(row["retain_params"], str)
                else row["retain_params"],
            }
            for row in sorted(rows, key=lambda row: row["id"])
        ]
    )


def save(name, value):
    if name.endswith("-reuse-audit.json"):
        value["audit_run_id"] = RUN_ID
    (OUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    )


def q(name):
    if not re.fullmatch("[a-zA-Z0-9_]+", name):
        raise ValueError("Invalid SQL identifier")
    return '"' + name + '"'


def safe(value):
    if isinstance(value, dict):
        return {
            k: safe(v)
            for k, v in value.items()
            if not re.search(
                "api_key|password|secret|token|headers|pepper|database_url|dsn", k, re.I
            )
        }
    if isinstance(value, list):
        return [safe(v) for v in value]
    return value


async def inventory(c):
    settings = [
        "shared_buffers",
        "work_mem",
        "maintenance_work_mem",
        "max_connections",
        "max_locks_per_transaction",
        "effective_cache_size",
        "random_page_cost",
        "effective_io_concurrency",
        "jit",
        "max_parallel_workers_per_gather",
        "shared_preload_libraries",
        "default_text_search_config",
        "statement_timeout",
        "idle_in_transaction_session_timeout",
        "TimeZone",
    ]
    result = {
        "identity": dict(
            await c.fetchrow(
                "SELECT current_database() AS database, inet_server_addr()::text AS address, inet_server_port() AS port"
            )
        ),
        "database": await c.fetchval("SELECT current_database()"),
        "version": await c.fetchval("SELECT version()"),
        "extensions": [
            dict(x)
            for x in await c.fetch(
                "SELECT extname,extversion FROM pg_extension ORDER BY extname"
            )
        ],
        "settings": [
            dict(x)
            for x in await c.fetch(
                "SELECT name,setting,unit,source,pending_restart FROM pg_settings WHERE name=ANY($1::text[]) ORDER BY name",
                settings,
            )
        ],
    }
    return result


async def lme():
    root = L / "candidates/concise-low"
    c = await asyncpg.connect(
        (root / "instance/input/database-url").read_text().strip()
    )
    result = {
        "dataset": "longmemeval-s",
        "checks": [],
        "errors": [],
        "vector_bytes_hashed": False,
    }
    try:
        await c.execute("SET default_transaction_read_only=on")
        await c.execute("SET statement_timeout='180s'")
        result["postgres"] = await inventory(c)
        receipts = sorted((root / "ingestion").glob("*/accepted.json"))
        assert len(receipts) == 500, "Expected 500 sealed ingestion receipts"
        for idx, path in enumerate(receipts):
            async with c.transaction(isolation="repeatable_read", readonly=True):
                a = load(path)
                qid = path.parent.name
                ws = a["workspace"]
                sch = q(ws["schema"])
                current = await c.fetchrow(
                    "SELECT workspace_id,schema_name FROM ctxdb_meta.workspaces WHERE bank_id=$1 AND state='active'",
                    ws["bank_id"],
                )
                errors = []
                if (
                    not current
                    or str(current["workspace_id"]) != ws["workspace_id"]
                    or current["schema_name"] != ws["schema"]
                ):
                    errors.append("workspace_identity")
                docs = json.load(gzip.open(L / "prepared" / qid / "documents.json.gz"))
                prepared_sha = hashlib.sha256(
                    json.dumps(
                        docs, sort_keys=True, ensure_ascii=False, default=str
                    ).encode()
                ).hexdigest()
                if prepared_sha != a["documents_sha256"]:
                    errors.append("prepared_source_checksum")
                rows = await c.fetch(
                    f"SELECT id,original_text,retain_params FROM {sch}.documents"
                )
                actual = {r["id"]: r for r in rows}
                if len(actual) != len(docs):
                    errors.append("document_count")
                for d in docs:
                    r = actual.get(d["document_id"])
                    if r is None:
                        errors.append("missing_document")
                        continue
                    params = (
                        json.loads(r["retain_params"])
                        if isinstance(r["retain_params"], str)
                        else r["retain_params"]
                    )
                    if (
                        r["original_text"] != d["content"]
                        or params["context"] != d["context"]
                        or datetime.datetime.fromisoformat(params["event_date"])
                        != datetime.datetime.fromisoformat(d["timestamp"])
                    ):
                        errors.append("source_text_context_date")
                counts = {
                    t: await c.fetchval(f"SELECT count(*) FROM {sch}.{q(t)}")
                    for t in a["counts"]
                }
                if counts != a["counts"]:
                    errors.append("table_counts")
                checksum = await c.fetchval(
                    f"SELECT md5(string_agg(h,'' ORDER BY h)) FROM (SELECT md5((to_jsonb(t)-'embedding')::text) h FROM {sch}.memory_units t) d"
                )
                if checksum != a["facts_checksum_without_vectors"]:
                    errors.append("facts_checksum")
                dims = [
                    dict(r)
                    for r in await c.fetch(
                        f"SELECT vector_dims(embedding) dim,count(*) n FROM {sch}.memory_units GROUP BY 1"
                    )
                ]
                if sorted(dims, key=str) != sorted(a["dimensions"], key=str):
                    errors.append("embedding_dimensions")
                ops = [
                    dict(r)
                    for r in await c.fetch(
                        f"SELECT status,count(*) n FROM {sch}.async_operations GROUP BY status"
                    )
                ]
                if any((r["status"] != "completed" for r in ops)):
                    errors.append("noncompleted_operations")
                bank_config = await c.fetchval(
                    f"SELECT config FROM {sch}.banks WHERE bank_id=$1", ws["bank_id"]
                )
                bank_config = (
                    json.loads(bank_config)
                    if isinstance(bank_config, str)
                    else bank_config
                )
                result["checks"].append(
                    {
                        "bank_config": safe(bank_config),
                        "bank_config_sha256": digest(bank_config),
                        "source_sha256": source_fingerprint(rows),
                        "question_id": qid,
                        "bank_id": ws["bank_id"],
                        "schema": ws["schema"],
                        "counts": counts,
                        "facts_checksum_without_vectors": checksum,
                        "errors": errors,
                    }
                )
                if errors:
                    result["errors"].append({"question_id": qid, "errors": errors})
                if idx % 50 == 0:
                    print(json.dumps({"audit": "lme", "checked": idx + 1}), flush=True)
        sch = q(result["checks"][0]["schema"])
        result["sample_bank_rows"] = safe(
            [
                json.loads(r["data"])
                for r in await c.fetch(
                    f"SELECT to_jsonb(b)::text data FROM {sch}.banks b"
                )
            ]
        )
        result["sample_indexes"] = [
            dict(r)
            for r in await c.fetch(
                "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=$1",
                result["checks"][0]["schema"],
            )
        ]
    finally:
        await c.close()
    result["reusable"] = not result["errors"]
    result["historical_input_variant"] = {
        "replaced_session_count": 1,
        "view_filter_affected_questions": 27,
        "official_unmodified_corpus": False,
    }
    save("longmemeval-reuse-audit.json", result)
    if not result["reusable"]:
        raise ValueError("LongMemEval banks differ from sealed receipts")
    print(
        json.dumps(
            {
                "audit": "lme",
                "complete": True,
                "reusable": result["reusable"],
                "errors": result["errors"],
            }
        ),
        flush=True,
    )


async def beam():
    c = await asyncpg.connect((B / "instance/input/database-url").read_text().strip())
    result = {
        "dataset": "beam10m",
        "checks": [],
        "errors": [],
        "vector_bytes_hashed": False,
        "embedding_representation_hashed": True,
    }
    try:
        async with c.transaction(isolation="repeatable_read", readonly=True):
            await c.execute("SET LOCAL statement_timeout='180s'")
            result["postgres"] = await inventory(c)
            entries = load(B / "conversation-runs.json")
            assert len(entries) == 10
            for entry in entries:
                root = Path(entry["root"])
                receipt = load(root / "frozen-bank.json")
                bank = receipt["bank"]
                ws = await c.fetchrow(
                    "SELECT workspace_id,schema_name FROM ctxdb_meta.workspaces WHERE bank_id=$1 AND state='active'",
                    bank,
                )
                assert ws, "missing beam bank"
                schema = ws["schema_name"]
                hashes = {}
                counts = {}
                errors = []
                for table, expected in receipt["table_hashes"].items():
                    h = hashlib.sha256()
                    count = 0
                    async for row in c.cursor(
                        f"SELECT row_to_json(t)::text body FROM {q(schema)}.{q(table)} t ORDER BY row_to_json(t)::text",
                        prefetch=500,
                    ):
                        h.update(row["body"].encode())
                        h.update(b"\n")
                        count += 1
                    hashes[table] = h.hexdigest()
                    counts[table] = count
                    if hashes[table] != expected:
                        errors.append(table + "_hash")
                ops = [
                    dict(r)
                    for r in await c.fetch(
                        f"SELECT status,count(*) n FROM {q(schema)}.async_operations GROUP BY status"
                    )
                ]
                if any((r["status"] != "completed" for r in ops)):
                    errors.append("noncompleted_operations")
                if counts["documents"] != receipt["source_documents_verified"]:
                    errors.append("source_count")
                bank_config = await c.fetchval(
                    f"SELECT config FROM {q(schema)}.banks WHERE bank_id=$1", bank
                )
                bank_config = (
                    json.loads(bank_config)
                    if isinstance(bank_config, str)
                    else bank_config
                )
                result["checks"].append(
                    {
                        "bank_config": safe(bank_config),
                        "bank_config_sha256": digest(bank_config),
                        "bank_id": bank,
                        "schema": schema,
                        "counts": counts,
                        "hashes": hashes,
                        "operations": ops,
                        "errors": errors,
                    }
                )
                if errors:
                    result["errors"].append({"bank_id": bank, "errors": errors})
                print(
                    json.dumps(
                        {
                            "audit": "beam",
                            "checked": len(result["checks"]),
                            "errors": errors,
                        }
                    ),
                    flush=True,
                )
            schema = result["checks"][0]["schema"]
            sch = q(schema)
            result["sample_bank_rows"] = safe(
                [
                    json.loads(r["data"])
                    for r in await c.fetch(
                        f"SELECT to_jsonb(b)::text data FROM {sch}.banks b"
                    )
                ]
            )
            result["sample_indexes"] = [
                dict(r)
                for r in await c.fetch(
                    "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=$1",
                    schema,
                )
            ]
    finally:
        await c.close()
    result["reusable"] = not result["errors"]
    save("beam10m-reuse-audit.json", result)
    if not result["reusable"]:
        raise ValueError("BEAM banks differ from sealed receipts")
    print(
        json.dumps(
            {
                "audit": "beam",
                "complete": True,
                "reusable": result["reusable"],
                "errors": result["errors"],
            }
        ),
        flush=True,
    )


def config():
    suffix = re.compile(
        "(MODEL|PROVIDER|EXTRA_BODY|REASONING_EFFORT|CHUNK_SIZE|EXTRACTION_MODE|MAX_COMPLETION_TOKENS|MAX_CONCURRENT|MAX_SLOTS|TEXT_SEARCH_EXTENSION|TEXT_SEARCH_CONFIG|BM25_\\w+|ENABLE_\\w+|STORE_DOCUMENT_TEXT|TEI_URL|RERANKER_\\w+|RECALL_\\w+|RETAIN_MISSION)$"
    )
    configs = {}
    for name, root in [("beam10m", B), ("longmemeval", L / "candidates/concise-low")]:
        rows = {}
        for line in (root / "instance/runtime/api.env").read_text().splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            if suffix.search(k) and (
                not re.search("API_KEY|SECRET|PASSWORD|TOKEN|HEADERS|PEPPER", k)
            ):
                rows[k] = v
        configs[name] = {"configured_environment": safe(rows)}
    save("service-configuration.json", configs)


async def main():
    start = time.time()
    save("audit-run.json", {"id": RUN_ID, "state": "running", "started": start})
    config()
    failures = []
    for func in [lme, beam]:
        try:
            await func()
        except Exception as e:
            failures.append(func.__name__)
            save(
                func.__name__ + "-audit-error.json",
                {"type": type(e).__name__, "message": str(e)},
            )
            print(func.__name__, type(e).__name__, str(e), flush=True)
    save(
        "audit-run.json",
        {
            "id": RUN_ID,
            "state": "failed" if failures else "complete",
            "reports": {
                name: hashlib.sha256((OUT / name).read_bytes()).hexdigest()
                for name in ("longmemeval-reuse-audit.json", "beam10m-reuse-audit.json")
                if (OUT / name).exists()
            },
            "started": start,
            "finished": time.time(),
            "readonly": True,
            "model_calls": 0,
            "failed_sections": failures,
        },
    )
    if failures:
        raise SystemExit(1)


asyncio.run(main())
