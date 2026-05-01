#!/usr/bin/env bash
# Launch the gomoku-minimax Gomocup adapter binary.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINIMAX_REPO="$(cd "$HERE/../../../gomoku-minimax" && pwd)"
BINARY=""

for build_dir in "$MINIMAX_REPO/build-release" "$MINIMAX_REPO/build"; do
    candidate="$build_dir/gomoku_gomocup"
    if [[ -x "$candidate" ]]; then
        BINARY="$candidate"
        break
    fi
done

if [[ -z "$BINARY" ]]; then
    echo "gomoku_gomocup binary not found in $MINIMAX_REPO/build-release or $MINIMAX_REPO/build" >&2
    echo "Build a release binary with: cmake -S $MINIMAX_REPO -B $MINIMAX_REPO/build-release -DCMAKE_BUILD_TYPE=Release -DGOMOKU_BUILD_SFML_UI=OFF && cmake --build $MINIMAX_REPO/build-release --target gomoku_gomocup" >&2
    echo "Or build the legacy default tree with: cmake --build $MINIMAX_REPO/build --target gomoku_gomocup" >&2
    exit 1
fi

exec "$BINARY" "$@"
