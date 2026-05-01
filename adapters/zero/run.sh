#!/usr/bin/env bash
# Launch the GomokuZero Gomocup adapter inside the GomokuZero-player venv.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZERO_REPO="$(cd "$HERE/../../../GomokuZero-player" && pwd)"
PYTHON="$ZERO_REPO/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "GomokuZero venv python not found at $PYTHON" >&2
    exit 1
fi

exec "$PYTHON" "$HERE/engine.py" "$@"
