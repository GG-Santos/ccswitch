#!/usr/bin/env bash
# Launch claude with an optional account switch first.
#   ./launch.sh            -> start claude with whatever account is active
#   ./launch.sh work       -> switch to "work", then start claude
#   ./launch.sh work -d    -> switch, run the daemon in the background, then claude
set -euo pipefail

ACCOUNT="${1:-}"
DAEMON="${2:-}"

if [ -n "$ACCOUNT" ]; then
    python -m ccswitch.cli use "$ACCOUNT"
fi

if [ "$DAEMON" = "-d" ]; then
    python -m ccswitch.cli daemon &
    echo "ccswitch daemon started (pid $!)"
fi

exec claude
