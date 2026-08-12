#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 3 ]]; then
  echo "usage: $0 LIBRARY ASSET_NAME OUTPUT_DIRECTORY" >&2
  exit 2
fi

LIBRARY="$1"
ASSET_NAME="$2"
OUTPUT_DIRECTORY="$3"

if [[ ! -s "$LIBRARY" ]]; then
  echo "error: desktop tdjson library is missing: $LIBRARY" >&2
  exit 1
fi

package_root="$(mktemp -d "${TMPDIR:-/tmp}/tdjson-desktop.XXXXXX")"
trap 'rm -rf "$package_root"' EXIT
mkdir -p "$OUTPUT_DIRECTORY"
OUTPUT_DIRECTORY="$(cd "$OUTPUT_DIRECTORY" && pwd)"
cp "$LIBRARY" "$package_root/$(basename "$LIBRARY")"
(
  cd "$package_root"
  cmake -E tar cf "$OUTPUT_DIRECTORY/$ASSET_NAME" \
    --format=zip "$(basename "$LIBRARY")"
)
test -s "$OUTPUT_DIRECTORY/$ASSET_NAME"
echo "wrote $OUTPUT_DIRECTORY/$ASSET_NAME"
