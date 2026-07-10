#!/usr/bin/env bash
set -euo pipefail

# Build the same TgVoipWebrtc target used by Telegram iOS, then package its
# device and Apple-silicon simulator slices as an XCFramework for Mithka.

if [[ "$#" -lt 1 || "$#" -gt 2 ]]; then
  echo "usage: $0 TELEGRAM_IOS_SOURCE [OUTPUT_DIRECTORY]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TELEGRAM_SOURCE="$(cd "$1" && pwd)"
OUTPUT_DIRECTORY="${2:-$ROOT/dist}"
BUILD_FILE="$TELEGRAM_SOURCE/submodules/TgVoipWebrtc/BUILD"
CONFIGURATION_REPOSITORY="${TELEGRAM_BUILD_CONFIGURATION_REPOSITORY:-$TELEGRAM_SOURCE/build-input/configuration-repository}"

if [[ ! -f "$BUILD_FILE" ]]; then
  echo "error: Telegram-iOS must be cloned recursively" >&2
  exit 1
fi

if [[ -n "${BAZEL:-}" ]]; then
  BAZEL_BIN="$BAZEL"
else
  BAZEL_BIN="$(find "$TELEGRAM_SOURCE/build-input" -maxdepth 1 -type f -name 'bazel-*-darwin-*' -perm +111 2>/dev/null | head -1 || true)"
  if [[ -z "$BAZEL_BIN" ]]; then
    BAZEL_BIN="$(command -v bazel || true)"
  fi
fi
if [[ -z "$BAZEL_BIN" || ! -x "$BAZEL_BIN" ]]; then
  echo "error: no Bazel executable found; set BAZEL" >&2
  exit 1
fi
BAZEL_BIN="$(cd "$(dirname "$BAZEL_BIN")" && pwd)/$(basename "$BAZEL_BIN")"

prepare_build_configuration() {
  if [[ -f "$CONFIGURATION_REPOSITORY/variables.bzl" ]]; then
    return
  fi

  echo "==> Creating non-signing Telegram build configuration"
  mkdir -p "$CONFIGURATION_REPOSITORY/provisioning"
  : > "$CONFIGURATION_REPOSITORY/WORKSPACE"
  : > "$CONFIGURATION_REPOSITORY/BUILD"
  : > "$CONFIGURATION_REPOSITORY/provisioning/BUILD"
  cat > "$CONFIGURATION_REPOSITORY/MODULE.bazel" <<'EOF'
module(
    name = "build_configuration",
)
EOF
  cat > "$CONFIGURATION_REPOSITORY/variables.bzl" <<EOF
telegram_bazel_path = "$BAZEL_BIN"
telegram_use_xcode_managed_codesigning = False
telegram_bundle_id = "ad.neko.mithka.tgvoip"
telegram_api_id = "0"
telegram_api_hash = ""
telegram_team_id = ""
telegram_app_center_id = "0"
telegram_is_internal_build = "false"
telegram_is_appstore_build = "false"
telegram_appstore_id = "0"
telegram_app_specific_url_scheme = "tg"
telegram_premium_iap_product_id = ""
telegram_aps_environment = "development"
telegram_enable_siri = False
telegram_enable_icloud = False
telegram_enable_watch = False
EOF
}

prepare_build_configuration

TMP="$(mktemp -d "${TMPDIR:-/tmp}/mithka-tgvoip.XXXXXX")"
ORIGINAL_BUILD="$TMP/BUILD.original"
cp "$BUILD_FILE" "$ORIGINAL_BUILD"
cleanup() {
  cp "$ORIGINAL_BUILD" "$BUILD_FILE"
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

python3 - "$BUILD_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = 'load("@build_bazel_rules_apple//apple:ios.bzl", "ios_unit_test")'
new = 'load("@build_bazel_rules_apple//apple:ios.bzl", "ios_static_framework", "ios_unit_test")'
if old not in text and new not in text:
    raise SystemExit("unexpected Telegram TgVoipWebrtc BUILD load statement")
text = text.replace(old, new, 1)
if 'name = "MithkaTgVoipWebrtcFramework"' not in text:
    text += '''

ios_static_framework(
    name = "MithkaTgVoipWebrtcFramework",
    bundle_name = "TgVoipWebrtc",
    hdrs = glob(["PublicHeaders/**/*.h"]),
    minimum_os_version = "15.0",
    deps = [":TgVoipWebrtc"],
)
'''
path.write_text(text)
PY

TARGET="//submodules/TgVoipWebrtc:MithkaTgVoipWebrtcFramework"
XCODE_VERSION="${BAZEL_XCODE_VERSION:-$(xcodebuild -version | sed -n '1s/^Xcode //p')}"

build_slice() {
  local cpu="$1"
  local destination="$2"
  (
    cd "$TELEGRAM_SOURCE"
    "$BAZEL_BIN" build "$TARGET" \
      --override_repository="build_configuration=$CONFIGURATION_REPOSITORY" \
      --apple_platform_type=ios \
      --ios_multi_cpus="$cpu" \
      --xcode_version="$XCODE_VERSION" \
      -c opt
  )
  mkdir -p "$destination"
  unzip -q -o \
    "$TELEGRAM_SOURCE/bazel-bin/submodules/TgVoipWebrtc/MithkaTgVoipWebrtcFramework.zip" \
    -d "$destination"
}

build_slice arm64 "$TMP/device"
build_slice sim_arm64 "$TMP/simulator"

rm -rf "$OUTPUT_DIRECTORY/TgVoipWebrtc.xcframework" "$OUTPUT_DIRECTORY/tgvoip-ios.xcframework.zip"
mkdir -p "$OUTPUT_DIRECTORY"
xcodebuild -create-xcframework \
  -framework "$TMP/device/TgVoipWebrtc.framework" \
  -framework "$TMP/simulator/TgVoipWebrtc.framework" \
  -output "$OUTPUT_DIRECTORY/TgVoipWebrtc.xcframework"
(
  cd "$OUTPUT_DIRECTORY"
  /usr/bin/ditto -c -k --keepParent TgVoipWebrtc.xcframework tgvoip-ios.xcframework.zip
)

echo "wrote $OUTPUT_DIRECTORY/tgvoip-ios.xcframework.zip"
