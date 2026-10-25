#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python -m ruff check .
python -m pytest
bash -n scripts/install.sh scripts/check.sh
python scripts/secret_scan.py
