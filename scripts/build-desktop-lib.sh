#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: $0 <linux|macos|windows> OUTPUT_LIBRARY" >&2
  exit 2
fi

PLATFORM="$1"
OUTPUT_LIBRARY="$2"
case "$PLATFORM" in
  linux|macos|windows) ;;
  *)
    echo "error: unsupported desktop platform: $PLATFORM" >&2
    exit 2
    ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TD_REPO="${TD_REPO:-https://github.com/tdlib/td.git}"
TD_COMMIT="${TD_COMMIT:-master}"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/build/desktop-$PLATFORM}"
TD_SOURCE="$BUILD_ROOT/td"
TD_BUILD="$BUILD_ROOT/td-build"

mkdir -p "$(dirname "$OUTPUT_LIBRARY")" "$BUILD_ROOT"

prepare_td_source() {
  if [[ ! -d "$TD_SOURCE/.git" ]]; then
    rm -rf "$TD_SOURCE"
    git init "$TD_SOURCE"
    git -C "$TD_SOURCE" remote add origin "$TD_REPO"
  fi
  git -C "$TD_SOURCE" config core.autocrlf false
  git -C "$TD_SOURCE" fetch --depth 1 origin "$TD_COMMIT"
  git -C "$TD_SOURCE" reset --hard FETCH_HEAD
  git -C "$TD_SOURCE" clean -fdx
  echo "==> TDLib $(git -C "$TD_SOURCE" rev-parse HEAD)"
  "$ROOT/scripts/apply-mithka-patches.sh" "$TD_SOURCE"
}

build_macos_openssl() {
  local openssl_version="3.3.2"
  local openssl_root="$BUILD_ROOT/openssl-$openssl_version"
  local openssl_source="$openssl_root/source"
  local openssl_universal="$openssl_root/universal"

  if [[ -s "$openssl_universal/lib/libssl.a" && \
        -s "$openssl_universal/lib/libcrypto.a" ]]; then
    echo "==> Reusing universal OpenSSL $openssl_version"
    return
  fi

  mkdir -p "$openssl_root"
  if [[ ! -f "$openssl_root/source.tar.gz" ]]; then
    curl -fsSL \
      "https://github.com/openssl/openssl/releases/download/openssl-$openssl_version/openssl-$openssl_version.tar.gz" \
      -o "$openssl_root/source.tar.gz"
  fi
  rm -rf "$openssl_source" "$openssl_root/arm64" \
    "$openssl_root/x86_64" "$openssl_universal"
  mkdir -p "$openssl_source"
  tar xzf "$openssl_root/source.tar.gz" -C "$openssl_source" \
    --strip-components=1

  for architecture in arm64 x86_64; do
    local target="darwin64-${architecture}-cc"
    local prefix="$openssl_root/$architecture"
    echo "==> Building OpenSSL $openssl_version for macOS $architecture"
    (
      cd "$openssl_source"
      make clean >/dev/null 2>&1 || true
      ./Configure "$target" no-shared no-tests no-apps no-docs \
        "-mmacosx-version-min=10.15" --prefix="$prefix" --libdir=lib
      make -j"${TD_BUILD_JOBS:-2}" build_libs
      make install_dev
    )
  done

  mkdir -p "$openssl_universal/lib"
  cp -R "$openssl_root/arm64/include" "$openssl_universal/include"
  lipo -create \
    "$openssl_root/arm64/lib/libssl.a" \
    "$openssl_root/x86_64/lib/libssl.a" \
    -output "$openssl_universal/lib/libssl.a"
  lipo -create \
    "$openssl_root/arm64/lib/libcrypto.a" \
    "$openssl_root/x86_64/lib/libcrypto.a" \
    -output "$openssl_universal/lib/libcrypto.a"
}

verify_library() {
  local library="$1"
  local symbols
  test -s "$library"
  case "$PLATFORM" in
    linux)
      symbols="$(nm -D "$library")"
      for symbol in \
        td_create_client_id \
        td_mithka_export_session_string \
        td_mithka_import_session_string \
        td_mithka_last_error \
        td_mithka_set_transfer_boost; do
        grep " $symbol$" <<<"$symbols" >/dev/null
      done
      if ldd "$library" | grep -q 'not found'; then
        ldd "$library" >&2
        return 1
      fi
      ;;
    macos)
      symbols="$(nm -gU "$library")"
      for symbol in \
        _td_create_client_id \
        _td_mithka_export_session_string \
        _td_mithka_import_session_string \
        _td_mithka_last_error \
        _td_mithka_set_transfer_boost; do
        grep " $symbol$" <<<"$symbols" >/dev/null
      done
      test "$(lipo -archs "$library" | tr ' ' '\n' | sort | tr '\n' ' ')" = \
        "arm64 x86_64 "
      for architecture in arm64 x86_64; do
        otool -D -arch "$architecture" "$library" | \
          grep -Fx '@rpath/libtdjson.dylib' >/dev/null
      done
      if otool -L "$library" | grep -Eq '/opt/homebrew|/usr/local'; then
        otool -L "$library" >&2
        return 1
      fi
      ;;
    windows)
      # The workflow verifies exports by loading the DLL with GetProcAddress.
      ;;
  esac
}

prepare_td_source
rm -rf "$TD_BUILD"

case "$PLATFORM" in
  linux)
    cmake -S "$TD_SOURCE" -B "$TD_BUILD" -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DTD_ENABLE_LTO=OFF
    cmake --build "$TD_BUILD" --target tdjson \
      --parallel "${TD_BUILD_JOBS:-2}"
    built_library="$(find "$TD_BUILD" -type f -name 'libtdjson.so*' -print -quit)"
    ;;
  macos)
    build_macos_openssl
    openssl_universal="$BUILD_ROOT/openssl-3.3.2/universal"
    macos_sdk="$(xcrun --sdk macosx --show-sdk-path)"
    cmake -S "$TD_SOURCE" -B "$TD_BUILD" -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_OSX_ARCHITECTURES='arm64;x86_64' \
      -DCMAKE_OSX_DEPLOYMENT_TARGET=10.15 \
      -DTD_ENABLE_LTO=OFF \
      -DOPENSSL_USE_STATIC_LIBS=TRUE \
      -DOPENSSL_INCLUDE_DIR="$openssl_universal/include" \
      -DOPENSSL_SSL_LIBRARY="$openssl_universal/lib/libssl.a" \
      -DOPENSSL_CRYPTO_LIBRARY="$openssl_universal/lib/libcrypto.a" \
      -DZLIB_INCLUDE_DIR="$macos_sdk/usr/include" \
      -DZLIB_LIBRARY="$macos_sdk/usr/lib/libz.tbd"
    cmake --build "$TD_BUILD" --target tdjson \
      --parallel "${TD_BUILD_JOBS:-2}"
    built_library="$(find "$TD_BUILD" -type f -name 'libtdjson*.dylib' -print -quit)"
    ;;
  windows)
    : "${VCPKG_INSTALLATION_ROOT:?VCPKG_INSTALLATION_ROOT is required on Windows}"
    vcpkg_toolchain="$(cygpath -m "$VCPKG_INSTALLATION_ROOT/scripts/buildsystems/vcpkg.cmake")"
    cmake -S "$TD_SOURCE" -B "$TD_BUILD" \
      -G 'Visual Studio 17 2022' -A x64 \
      -DCMAKE_TOOLCHAIN_FILE="$vcpkg_toolchain" \
      -DVCPKG_TARGET_TRIPLET=x64-windows-static \
      -DTD_ENABLE_LTO=OFF
    cmake --build "$TD_BUILD" --target tdjson --config Release \
      --parallel "${TD_BUILD_JOBS:-2}"
    built_library="$(find "$TD_BUILD" -type f -iname 'tdjson.dll' -print -quit)"
    ;;
esac

if [[ -z "${built_library:-}" || ! -s "$built_library" ]]; then
  echo "error: tdjson library was not produced for $PLATFORM" >&2
  exit 1
fi

cp "$built_library" "$OUTPUT_LIBRARY"
if [[ "$PLATFORM" == macos ]]; then
  install_name_tool -id '@rpath/libtdjson.dylib' "$OUTPUT_LIBRARY"
fi
verify_library "$OUTPUT_LIBRARY"
echo "Built $PLATFORM tdjson: $OUTPUT_LIBRARY"
