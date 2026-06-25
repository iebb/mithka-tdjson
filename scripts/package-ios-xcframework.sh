#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
XCFRAMEWORK="${1:-$ROOT/tdjson.xcframework}"
OUT_DIR="$ROOT/dist"
OUT="$OUT_DIR/tdjson-ios.xcframework.zip"

if [[ ! -d "$XCFRAMEWORK" ]]; then
  echo "error: missing tdjson.xcframework at $XCFRAMEWORK" >&2
  echo "usage: $0 /path/to/tdjson.xcframework" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
rm -f "$OUT"

parent="$(dirname "$XCFRAMEWORK")"
name="$(basename "$XCFRAMEWORK")"

(
  cd "$parent"
  /usr/bin/ditto -c -k --keepParent "$name" "$OUT"
)

echo "wrote $OUT"

