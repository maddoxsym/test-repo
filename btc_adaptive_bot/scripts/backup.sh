#!/usr/bin/env bash
# Back up the database (safe while the bot is running).
source "$(dirname "$0")/_common.sh"
btcbot --config "${BTCBOT_CONFIG:-config/research.yaml}" backup
