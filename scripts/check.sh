#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python -m ruff check .
python -m pytest
bash -n install.sh scripts/install.sh scripts/check.sh scripts/install-codex.sh
python scripts/secret_scan.py
