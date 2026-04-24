#!/bin/zsh
set -euo pipefail

APP_DIR="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
CONFIG_FILE="${CONFIG_FILE:-$APP_DIR/config.toml}"
PYTHON_BIN="${PYTHON_BIN:-$APP_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "python executable not found: $PYTHON_BIN" >&2
  exit 1
fi

cd "$APP_DIR"
"$PYTHON_BIN" -m pip install -e "$APP_DIR"

INSTANCE_NAME=""
INSTANCE_NAME="$(
  "$PYTHON_BIN" -c 'from pathlib import Path; import sys; from sidae_secretary.config import load_instance_name; print(load_instance_name(config_file=Path(sys.argv[1])))' "$CONFIG_FILE" 2>/dev/null || true
)"

launchd_label() {
  local base_label="$1"
  if [[ -n "$INSTANCE_NAME" ]]; then
    echo "${base_label}.${INSTANCE_NAME}"
    return
  fi
  echo "$base_label"
}

kickstart_if_loaded() {
  local label="$1"
  local uid
  uid="$(id -u)"
  local domains=("system" "gui/$uid")
  local domain
  local launchctl_output
  local pid
  for domain in "${domains[@]}"; do
    launchctl_output="$(launchctl print "$domain/$label" 2>/dev/null || true)"
    if [[ -z "$launchctl_output" ]]; then
      continue
    fi
    if launchctl kickstart -k "$domain/$label" >/dev/null 2>&1; then
      continue
    fi
    pid="$(printf '%s\n' "$launchctl_output" | awk '/\bpid = / {gsub(/;/, "", $3); print $3; exit}')"
    if [[ -n "$pid" ]]; then
      kill -TERM "$pid" >/dev/null 2>&1 || true
    fi
  done
}

restart_matching_cli_processes() {
  local cli_command="$1"
  local pids
  pids="$(
    ps ax -o pid= -o command= \
      | awk -v cmd="$cli_command" -v cfg="$CONFIG_FILE" '
          index($0, "sidae_secretary.cli " cmd) && index($0, "--config-file " cfg) { print $1 }
        '
  )"
  local pid
  for pid in $pids; do
    kill -TERM "$pid" >/dev/null 2>&1 || true
  done
}

kickstart_if_loaded "$(launchd_label "com.sidae.secretary.uclass-poller")"
kickstart_if_loaded "$(launchd_label "com.sidae.secretary.telegram-listener")"
kickstart_if_loaded "$(launchd_label "com.sidae.secretary.publish")"
kickstart_if_loaded "$(launchd_label "com.sidae.secretary.briefings")"
kickstart_if_loaded "$(launchd_label "com.sidae.secretary.briefing-relay")"
kickstart_if_loaded "$(launchd_label "com.sidae.secretary.onboarding")"

restart_matching_cli_processes "uclass-poller"
restart_matching_cli_processes "telegram-listener"

if [[ -f "$CONFIG_FILE" ]]; then
  if [[ -n "$INSTANCE_NAME" ]]; then
    echo "redeployed instance=$INSTANCE_NAME with config: $CONFIG_FILE"
  else
    echo "redeployed with config: $CONFIG_FILE"
  fi
else
  echo "redeployed without config.toml override"
fi
