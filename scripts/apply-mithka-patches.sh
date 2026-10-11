#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 TD_SOURCE" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TD_SOURCE="$1"
SERIES="$ROOT/patches/series"

if ! git -C "$TD_SOURCE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: TDLib checkout not found: $TD_SOURCE" >&2
  exit 1
fi
if [[ ! -f "$SERIES" ]]; then
  echo "error: Mithka patch series not found: $SERIES" >&2
  exit 1
fi

patch_files=()
while IFS= read -r patch_name; do
  patch_name="${patch_name%$'\r'}"
  [[ -n "$patch_name" ]] || continue
  [[ "$patch_name" != \#* ]] || continue
  patch_file="$ROOT/patches/$patch_name"
  if [[ ! -f "$patch_file" ]]; then
    echo "error: patch listed in series is missing: $patch_file" >&2
    exit 1
  fi
  # Line-only insertions can apply successfully inside the wrong function
  # after upstream moves code. Require real anchors for existing-file hunks;
  # whole-file additions/deletions do not need surrounding context.
  if ! awk '
    /^diff --git / {
      if (hunk && !context && !whole_file) bad = 1
      hunk = 0; whole_file = 0
    }
    /^(new|deleted) file mode / { whole_file = 1 }
    /^@@ / {
      if (hunk && !context && !whole_file) bad = 1
      hunk = 1; context = 0
    }
    /^ / { if (hunk) context = 1 }
    END {
      if (hunk && !context && !whole_file) bad = 1
      exit bad
    }
  ' "$patch_file"; then
    echo "error: unanchored modification hunk in $patch_name" >&2
    exit 1
  fi
  patch_files+=("$patch_file")
done < "$SERIES"

# Later patches may edit an earlier patch's context. Reverse the complete
# series in a temporary index to recognize that state without modifying the
# source files or the checkout's real index.
series_already_applied() (
  patch_check_index="$(mktemp)"
  trap 'rm -f "$patch_check_index" "$patch_check_index.lock"' EXIT
  GIT_INDEX_FILE="$patch_check_index" git -C "$TD_SOURCE" read-tree HEAD || exit 1
  touched_files=()
  for patch_file in "${patch_files[@]}"; do
    while IFS=$'\t' read -r added removed source_path; do
      [[ -n "$source_path" ]] && touched_files+=("$source_path")
    done < <(git -C "$TD_SOURCE" apply --numstat "$patch_file")
  done
  [[ "${#touched_files[@]}" -gt 0 ]] || exit 1
  GIT_INDEX_FILE="$patch_check_index" git -C "$TD_SOURCE" add -u -- "${touched_files[@]}" || exit 1
  for ((index=${#patch_files[@]}-1; index>=0; index--)); do
    GIT_INDEX_FILE="$patch_check_index" git -C "$TD_SOURCE" apply --cached --reverse "${patch_files[$index]}" || exit 1
  done
)
if series_already_applied >/dev/null 2>&1; then
  echo "==> Mithka patch series already applied"
  exit 0
fi

for patch_file in "${patch_files[@]}"; do
  patch_name="${patch_file##*/}"
  if git -C "$TD_SOURCE" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
    echo "==> $patch_name already applied"
  elif git -C "$TD_SOURCE" apply --check "$patch_file"; then
    echo "==> Applying $patch_name directly"
    git -C "$TD_SOURCE" apply "$patch_file"
  elif git -C "$TD_SOURCE" apply --3way --check "$patch_file" >/dev/null 2>&1; then
    echo "==> Applying $patch_name with a three-way merge"
    git -C "$TD_SOURCE" apply --3way "$patch_file"
  else
    echo "==> Direct-apply diagnostics for $patch_name" >&2
    git -C "$TD_SOURCE" apply --check "$patch_file" >&2 || true
    echo "==> Three-way diagnostics for $patch_name" >&2
    git -C "$TD_SOURCE" apply --3way --check "$patch_file" >&2 || true
    echo "error: failed to apply $patch_name" >&2
    exit 1
  fi
done
