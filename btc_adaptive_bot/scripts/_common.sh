#!/usr/bin/env bash
# Shared bootstrap: locate the project, activate the venv, load .env.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[1]}")/.."
PROJECT_ROOT="$(pwd)"
export PYTHONPATH="$PROJECT_ROOT/src:${PYTHONPATH:-}"

if [ ! -d ".venv" ]; then
  echo "Virtual environment not found. Run ./scripts/setup_mac.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

btcbot() { python -m btcbot.app.cli "$@"; }

# caffeinate keeps macOS awake while the bot runs. Closing the laptop lid can
# still suspend the machine depending on hardware and power settings.
run_awake() {
  if command -v caffeinate >/dev/null 2>&1; then
    exec caffeinate -i -s python -m btcbot.app.cli "$@"
  else
    exec python -m btcbot.app.cli "$@"
  fi
}
