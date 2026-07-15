#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TD_REPO="${TD_REPO:-https://github.com/tdlib/td.git}"
TD_COMMIT="${TD_COMMIT:-a8f21f5230172634becc1739050ef23ecd6ea291}"
OPENSSL_SRC_DIR="${OPENSSL_SRC_DIR:-/Users/ieb/Vibe/Nagram-iOS/submodules/openssl}"
OPENSSL_VERSION="${OPENSSL_VERSION:-1.1.1d}"
OPENSSL_TARBALL="${OPENSSL_TARBALL:-$OPENSSL_SRC_DIR/openssl-$OPENSSL_VERSION.tar.gz}"
OPENSSL_URL="${OPENSSL_URL:-https://github.com/openssl/openssl/releases/download/OpenSSL_1_1_1d/openssl-$OPENSSL_VERSION.tar.gz}"
MIN_IOS="${MIN_IOS:-13.0}"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/build/ios-min$MIN_IOS}"
OUT_DIR="$ROOT/dist"
XCFRAMEWORK="$OUT_DIR/tdjson.xcframework"
ZIP="$OUT_DIR/tdjson-ios.xcframework.zip"
UPSTREAM_TD_SRC="$BUILD_ROOT/upstream-td"
TD_SRC="${TD_SRC:-$UPSTREAM_TD_SRC}"
GENERATED_TD_SRC="$BUILD_ROOT/td-src"
TD_VERSION=""

ensure_openssl_tarball() {
  if [[ -f "$OPENSSL_TARBALL" ]]; then
    return
  fi
  mkdir -p "$BUILD_ROOT/downloads"
  OPENSSL_TARBALL="$BUILD_ROOT/downloads/openssl-$OPENSSL_VERSION.tar.gz"
  if [[ ! -f "$OPENSSL_TARBALL" ]]; then
    echo "==> Downloading OpenSSL $OPENSSL_VERSION"
    curl -fsSL "$OPENSSL_URL" -o "$OPENSSL_TARBALL"
  fi
}

prepare_td_source() {
  if [[ "$TD_SRC" == "$UPSTREAM_TD_SRC" ]]; then
    if [[ ! -d "$UPSTREAM_TD_SRC/.git" ]]; then
      rm -rf "$UPSTREAM_TD_SRC"
      git clone "$TD_REPO" "$UPSTREAM_TD_SRC"
    fi
    git -C "$UPSTREAM_TD_SRC" fetch --quiet origin "$TD_COMMIT"
    git -C "$UPSTREAM_TD_SRC" reset --hard HEAD >/dev/null
    git -C "$UPSTREAM_TD_SRC" checkout --quiet "$TD_COMMIT"
    git -C "$UPSTREAM_TD_SRC" reset --hard "$TD_COMMIT" >/dev/null
  fi
  if [[ ! -d "$TD_SRC" ]]; then
    echo "error: missing TDLib source at $TD_SRC" >&2
    exit 1
  fi
  TD_VERSION="$(sed -n 's/project(TDLib VERSION \([^ ]*\).*/\1/p' "$TD_SRC/CMakeLists.txt" | head -n 1)"
  if [[ -z "$TD_VERSION" ]]; then
    echo "error: could not read TDLib version from $TD_SRC" >&2
    exit 1
  fi
  echo "==> TDLib $TD_VERSION ($TD_COMMIT)"
}

apply_mithka_patches() {
  local src="$1"
  local patch="$ROOT/patches/mithka-session-backup.patch"
  if git -C "$src" apply --unidiff-zero --check "$patch"; then
    echo "==> Applying Mithka TDLib session backup patch"
    git -C "$src" apply --unidiff-zero "$patch"
  elif git -C "$src" apply --unidiff-zero --reverse --check "$patch"; then
    echo "==> Mithka TDLib session backup patch already applied"
  else
    echo "error: failed to apply Mithka TDLib session backup patch" >&2
    exit 1
  fi

  patch="$ROOT/patches/mithka-installed-cloud-themes.patch"
  if git -C "$src" apply --unidiff-zero --check "$patch"; then
    echo "==> Applying Mithka installed cloud themes patch"
    git -C "$src" apply --unidiff-zero "$patch"
  elif git -C "$src" apply --unidiff-zero --reverse --check "$patch"; then
    echo "==> Mithka installed cloud themes patch already applied"
  else
    echo "error: failed to apply Mithka installed cloud themes patch" >&2
    exit 1
  fi
}

patch_openssl_for_sim_arm64() {
  local conf="$1/Configurations/15-ios.conf"
  python3 - "$conf" "$MIN_IOS" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
min_ios = sys.argv[2]
text = path.read_text()
if '"iossimulator-arm64-xcrun"' not in text:
    needle = '    "iossimulator-xcrun" => {\n        inherit_from     => [ "ios-common" ],\n        CC               => "xcrun -sdk iphonesimulator cc",\n    },\n'
    replacement = needle + f'''    "iossimulator-arm64-xcrun" => {{
        inherit_from     => [ "iossimulator-xcrun", asm("no_asm") ],
        cflags           => add("-arch arm64 -target arm64-apple-ios{min_ios}-simulator -mios-simulator-version-min={min_ios} -DOPENSSL_NO_ASM -fno-common"),
        bn_ops           => "SIXTY_FOUR_BIT_LONG RC4_CHAR",
        perlasm_scheme   => "ios64",
    }},
'''
    if needle not in text:
        raise SystemExit("could not patch OpenSSL iOS simulator target")
    path.write_text(text.replace(needle, replacement))
PY
}

build_openssl() {
  local name="$1"
  local target="$2"
  local out="$BUILD_ROOT/$name/openssl"
  local src="$BUILD_ROOT/$name/openssl-src/openssl-1.1.1d"

  if [[ -f "$out/lib/libcrypto.a" && -f "$out/lib/libssl.a" ]]; then
    echo "==> Reusing OpenSSL for $name"
    return
  fi

  echo "==> Building OpenSSL for $name"
  rm -rf "$out" "$BUILD_ROOT/$name/openssl-src"
  mkdir -p "$out" "$BUILD_ROOT/$name/openssl-src"
  tar -xzf "$OPENSSL_TARBALL" -C "$BUILD_ROOT/$name/openssl-src"
  patch_openssl_for_sim_arm64 "$src"

  pushd "$src" >/dev/null
  export CROSS_COMPILE=""
  export CROSS_TOP="$(xcode-select --print-path)/Platforms/iPhoneOS.platform/Developer"
  export CROSS_SDK="iPhoneOS.sdk"
  if [[ "$name" == *simulator ]]; then
    export CROSS_TOP="$(xcode-select --print-path)/Platforms/iPhoneSimulator.platform/Developer"
    export CROSS_SDK="iPhoneSimulator.sdk"
  fi
  ./Configure "$target" no-shared no-asm no-ssl3 no-comp no-hw no-engine no-async no-tests "--prefix=$out"
  make -j"$(sysctl -n hw.ncpu)"
  make install_sw
  popd >/dev/null
}

build_tdjson_slice() {
  local name="$1"
  local sdk="$2"
  local arch="$3"
  local platform="$4"
  local openssl="$BUILD_ROOT/$name/openssl"
  local td_build="$BUILD_ROOT/$name/td"
  local framework="$BUILD_ROOT/$name/tdjson.framework"

  echo "==> Building TDLib tdjson for $name"
  rm -rf "$td_build"
  cmake -S "$GENERATED_TD_SRC" -B "$td_build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_SYSTEM_NAME=iOS \
    -DCMAKE_OSX_SYSROOT="$sdk" \
    -DCMAKE_OSX_ARCHITECTURES="$arch" \
    -DCMAKE_OSX_DEPLOYMENT_TARGET="$MIN_IOS" \
    -DCMAKE_XCODE_ATTRIBUTE_SUPPORTED_PLATFORMS="$platform" \
    -DOPENSSL_FOUND=1 \
    -DOPENSSL_CRYPTO_LIBRARY="$openssl/lib/libcrypto.a" \
    -DOPENSSL_SSL_LIBRARY="$openssl/lib/libssl.a" \
    -DOPENSSL_INCLUDE_DIR="$openssl/include" \
    -DZLIB_LIBRARY="$(xcrun --sdk "$sdk" --show-sdk-path)/usr/lib/libz.tbd" \
    -DZLIB_INCLUDE_DIR="$(xcrun --sdk "$sdk" --show-sdk-path)/usr/include"
  cmake --build "$td_build" --target tdjson

  local dylib
  dylib="$(find "$td_build" -maxdepth 2 -type f -name 'libtdjson*.dylib' | head -n 1)"
  if [[ -z "$dylib" ]]; then
    echo "error: libtdjson dylib not found in $td_build" >&2
    exit 1
  fi

  rm -rf "$framework"
  mkdir -p "$framework/Headers" "$framework/Modules"
  cp "$dylib" "$framework/tdjson"
  install_name_tool -id "@rpath/tdjson.framework/tdjson" "$framework/tdjson"
  cat > "$framework/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>tdjson</string>
  <key>CFBundleIdentifier</key>
  <string>ad.neko.tdjson</string>
  <key>CFBundleName</key>
  <string>tdjson</string>
  <key>CFBundlePackageType</key>
  <string>FMWK</string>
  <key>CFBundleShortVersionString</key>
  <string>$TD_VERSION</string>
  <key>CFBundleVersion</key>
  <string>$TD_VERSION</string>
  <key>MinimumOSVersion</key>
  <string>$MIN_IOS</string>
</dict>
</plist>
PLIST
  cat > "$framework/Modules/module.modulemap" <<'MODULEMAP'
framework module tdjson {
  umbrella header "tdjson.h"
  export *
  module * { export * }
}
MODULEMAP
  cp "$GENERATED_TD_SRC/td/telegram/td_json_client.h" "$framework/Headers/tdjson.h"
}

rm -rf "$XCFRAMEWORK" "$ZIP"
mkdir -p "$BUILD_ROOT" "$OUT_DIR"

ensure_openssl_tarball
prepare_td_source

echo "==> Preparing generated TDLib sources"
rm -rf "$GENERATED_TD_SRC" "$BUILD_ROOT/native-generate"
mkdir -p "$GENERATED_TD_SRC"
rsync -a --delete "$TD_SRC/" "$GENERATED_TD_SRC/"
apply_mithka_patches "$GENERATED_TD_SRC"
cmake -S "$GENERATED_TD_SRC" -B "$BUILD_ROOT/native-generate" -G Ninja \
  -DTD_GENERATE_SOURCE_FILES=ON \
  -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD_ROOT/native-generate"

build_openssl ios-arm64 ios64-xcrun
build_tdjson_slice ios-arm64 iphoneos arm64 iphoneos

build_openssl ios-arm64_x86_64-simulator iossimulator-arm64-xcrun
build_tdjson_slice ios-arm64_x86_64-simulator iphonesimulator arm64 iphonesimulator

xcodebuild -create-xcframework \
  -framework "$BUILD_ROOT/ios-arm64/tdjson.framework" \
  -framework "$BUILD_ROOT/ios-arm64_x86_64-simulator/tdjson.framework" \
  -output "$XCFRAMEWORK"

(
  cd "$OUT_DIR"
  /usr/bin/ditto -c -k --keepParent "$(basename "$XCFRAMEWORK")" "$ZIP"
)

echo "wrote $ZIP"
otool -l "$XCFRAMEWORK/ios-arm64/tdjson.framework/tdjson" | grep -A4 LC_BUILD_VERSION
