#!/usr/bin/env bash
# ============================================================
#  One-time setup for macOS.
#  Creates a virtual environment, installs pinned dependencies,
#  prepares folders, and creates .env from the template.
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; }

bold "BTC Adaptive Bot — macOS setup"
echo "Project: $ROOT"
echo

# --- Python ---------------------------------------------------
bold "1. Checking Python"
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    major="${version%%.*}"; minor="${version##*.}"
    if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then
      PYTHON_BIN="$candidate"; break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  fail "Python 3.11 or newer was not found."
  echo
  echo "  Install it with Homebrew:"
  echo "      /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  echo "      brew install python@3.12"
  echo "  Then run this script again."
  exit 1
fi
ok "Using $PYTHON_BIN ($($PYTHON_BIN --version))"

# --- virtual environment --------------------------------------
bold "2. Creating the virtual environment"
if [ -d ".venv" ]; then
  ok "Existing .venv reused (delete it to start fresh)"
else
  "$PYTHON_BIN" -m venv .venv
  ok "Created .venv"
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
ok "pip upgraded"

# --- dependencies ---------------------------------------------
bold "3. Installing dependencies (pinned)"
python -m pip install --quiet -r requirements.txt
ok "Runtime + test dependencies installed"

python -m pip install --quiet -e . 2>/dev/null && ok "btcbot command installed" || \
  warn "Editable install skipped — use ./scripts/*.sh, which work regardless"

# --- folders ---------------------------------------------------
bold "4. Preparing folders"
mkdir -p data/history data/backups logs reports/exports
ok "data/ logs/ reports/ ready"

# --- .env -------------------------------------------------------
bold "5. Credentials file"
if [ -f ".env" ]; then
  ok ".env already exists (left untouched)"
else
  cp .env.example .env
  chmod 600 .env
  ok "Created .env from the template (permissions 600)"
fi

if grep -qE '^BYBIT_DEMO_API_KEY=.+' .env 2>/dev/null; then
  ok "BYBIT_DEMO_API_KEY looks populated"
else
  warn "BYBIT_DEMO_API_KEY is empty — you must add your demo credentials next"
fi

# --- self-test ---------------------------------------------------
bold "6. Running the test suite"
if python -m pytest -q 2>&1 | tail -5; then
  ok "Tests completed"
else
  warn "Some tests failed — see the output above"
fi

echo
bold "Setup complete."
cat <<'NEXT'

Next steps:

  1. Create Bybit DEMO API credentials:
       - Log in at bybit.com
       - Switch to "Demo Trading" (top-right account menu)
       - Hover your avatar -> API -> create a new key
       - The key MUST be created inside Demo Trading

  2. Put them in .env:
       BYBIT_DEMO_API_KEY=...
       BYBIT_DEMO_API_SECRET=...

  3. Verify the connection (places no orders):
       ./scripts/verify_demo_connection.sh

  4. Start the 14-day research experiment:
       ./scripts/run_research.sh

  5. Open the dashboard:
       http://127.0.0.1:8787

NEXT
