#!/usr/bin/env bash
# One-time setup on Linux/macOS: venv, dependencies, .env, database
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env - put your LLM_API_KEY in it."; }
[ -f data/harness.db ] || .venv/bin/python -m fiscal_harness init
echo "Setup done. Next: .venv/bin/python -m fiscal_harness serve   (http://127.0.0.1:8765)"
