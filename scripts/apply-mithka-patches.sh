#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 TD_SOURCE" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TD_SOURCE="$1"
SERIES="$ROOT/patches/series"

if [[ ! -d "$TD_SOURCE/.git" ]]; then
  echo "error: TDLib checkout not found: $TD_SOURCE" >&2
  exit 1
fi
if [[ ! -f "$SERIES" ]]; then
  echo "error: Mithka patch series not found: $SERIES" >&2
  exit 1
fi

while IFS= read -r patch_name; do
  patch_name="${patch_name%$'\r'}"
  [[ -n "$patch_name" ]] || continue
  [[ "$patch_name" != \#* ]] || continue
  patch_file="$ROOT/patches/$patch_name"
  if [[ ! -f "$patch_file" ]]; then
    echo "error: patch listed in series is missing: $patch_file" >&2
    exit 1
  fi
  if git -C "$TD_SOURCE" apply --unidiff-zero --check "$patch_file"; then
    echo "==> Applying $patch_name"
    git -C "$TD_SOURCE" apply --unidiff-zero "$patch_file"
  elif git -C "$TD_SOURCE" apply --unidiff-zero --reverse --check "$patch_file"; then
    echo "==> $patch_name already applied"
  else
    echo "error: failed to apply $patch_name" >&2
    exit 1
  fi
done < "$SERIES"
