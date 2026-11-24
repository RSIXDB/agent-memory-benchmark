"""Resolve the deployed service config without starting API/Worker or invoking models."""

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source-root", type=Path, required=True)
parser.add_argument("--env-file", type=Path, required=True)
parser.add_argument("--runtime-pid", type=int)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

if args.runtime_pid:
    values = dict(
        x.split("=", 1)
        for x in Path(f"/proc/{args.runtime_pid}/environ")
        .read_bytes()
        .decode()
        .split("\0")
        if "=" in x
    )
    basis = "running process environment"
else:
    values = {}
    for line in args.env_file.read_text().splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        parsed = shlex.split(value)
        values[key] = parsed[0] if len(parsed) == 1 else value
    basis = "resolved environment file; service stopped, not attested as live"
os.environ.update(values)
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
sys.path.insert(0, str(args.source_root / "hindsight-api-slim"))
from hindsight_api.config import HindsightConfig  # noqa: E402 - resolve the explicitly selected deployment

config = HindsightConfig.from_env()
secret = re.compile(
    r"api_key|password|secret|oauth|credential|headers|database_url|pepper|access_token",
    re.I,
)
groups = re.compile(
    r"retain|recall|embedding|rerank|text_search|bm25|worker|observations|consolidation|document_text|llm"
)
resolved = {
    f.name: getattr(config, f.name)
    for f in dataclasses.fields(config)
    if groups.search(f.name) and not secret.search(f.name)
}
revision = subprocess.check_output(
    [
        "git",
        "-c",
        "safe.directory=" + str(args.source_root),
        "-C",
        str(args.source_root),
        "rev-parse",
        "HEAD",
    ],
    text=True,
).strip()
hashes = {
    str(p.relative_to(args.source_root)): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted(
        (args.source_root / "hindsight-api-slim/hindsight_api").rglob("*.py")
    )
}
args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
args.output.write_text(
    json.dumps(
        {
            "basis": basis,
            "source_commit": revision,
            "resolved": resolved,
            "source_hashes": hashes,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    + "\n"
)
os.chmod(args.output, 0o600)
print(
    json.dumps(
        {
            "output": str(args.output),
            "basis": basis,
            "fields": len(resolved),
            "source_files": len(hashes),
        }
    )
)
