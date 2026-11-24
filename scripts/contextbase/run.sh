#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
if [[ -n "${CONTEXTBASE_PYTHON:-}" ]]; then
  exec uv run --no-project "$CONTEXTBASE_PYTHON" -m memory_bench.contextbase "$@"
fi
exec uv run --no-project --python 3.11 --with-requirements "$repo_root/scripts/contextbase/requirements.txt" python -m memory_bench.contextbase "$@"
