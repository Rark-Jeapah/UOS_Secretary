#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
APP_PYTHON="${SIDAE_APP_PYTHON:-}"

if [ ! -x "$APP_PYTHON" ]; then
  if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    APP_PYTHON="$REPO_ROOT/.venv/bin/python"
  else
    APP_PYTHON="$(command -v python3)"
  fi
fi

cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$APP_PYTHON" -m sidae_secretary.cli ops open-remote --config-file "$REPO_ROOT/config.toml"
