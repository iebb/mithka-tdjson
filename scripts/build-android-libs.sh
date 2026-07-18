#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TD_REPO="${TD_REPO:-https://github.com/tdlib/td.git}"
TD_COMMIT="${TD_COMMIT:-a17f87c4cff7b90b278d12b91ba0614383aaee82}"
OPENSSL_VERSION="${OPENSSL_VERSION:-3.3.2}"
ANDROID_API="${ANDROID_API:-23}"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/build/android-api$ANDROID_API}"
OUT_DIR="$ROOT/dist"
TD_SRC="$BUILD_ROOT/td"
TD_VERSION=""

if [[ "$#" -gt 0 ]]; then
  ABIS=("$@")
else
  ABIS=(arm64-v8a armeabi-v7a x86_64)
fi

: "${ANDROID_NDK_HOME:=}"
if [[ -z "$ANDROID_NDK_HOME" ]]; then
  SDK="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
  if [[ -d "$SDK/ndk" ]]; then
    ANDROID_NDK_HOME="$SDK/ndk/$(ls "$SDK/ndk" | sort -V | tail -1)"
  fi
fi
if [[ ! -d "$ANDROID_NDK_HOME" ]]; then
  echo "error: Android NDK not found. Install it and set ANDROID_NDK_HOME." >&2
  exit 1
fi

HOST_TAG="linux-x86_64"
if [[ "$(uname)" == "Darwin" ]]; then
  HOST_TAG="darwin-x86_64"
fi
export ANDROID_NDK_ROOT="$ANDROID_NDK_HOME"
export PATH="$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/$HOST_TAG/bin:$PATH"

prepare_td_source() {
  mkdir -p "$BUILD_ROOT"
  if [[ ! -d "$TD_SRC/.git" ]]; then
    rm -rf "$TD_SRC"
    git clone "$TD_REPO" "$TD_SRC"
  fi
  git -C "$TD_SRC" fetch --quiet origin "$TD_COMMIT"
  git -C "$TD_SRC" reset --hard HEAD >/dev/null
  git -C "$TD_SRC" clean -fdx >/dev/null
  git -C "$TD_SRC" checkout --quiet "$TD_COMMIT"
  git -C "$TD_SRC" reset --hard "$TD_COMMIT" >/dev/null
  TD_VERSION="$(sed -n 's/project(TDLib VERSION \([^ ]*\).*/\1/p' "$TD_SRC/CMakeLists.txt" | head -n 1)"
  if [[ -z "$TD_VERSION" ]]; then
    echo "error: could not read TDLib version from $TD_SRC" >&2
    exit 1
  fi
  echo "==> TDLib $TD_VERSION ($TD_COMMIT)"
}

apply_mithka_patches() {
  local patch
  for patch in \
    "$ROOT/patches/mithka-session-backup.patch" \
    "$ROOT/patches/mithka-installed-cloud-themes.patch" \
    "$ROOT/patches/mithka-community-full-info.patch" \
    "$ROOT/patches/mithka-transfer-boost.patch"; do
    if git -C "$TD_SRC" apply --unidiff-zero --check "$patch"; then
      echo "==> Applying $(basename "$patch")"
      git -C "$TD_SRC" apply --unidiff-zero "$patch"
    elif git -C "$TD_SRC" apply --unidiff-zero --reverse --check "$patch"; then
      echo "==> $(basename "$patch") already applied"
    else
      echo "error: failed to apply $(basename "$patch")" >&2
      exit 1
    fi
  done
}

build_openssl() {
  local abi="$1"
  local src="$BUILD_ROOT/openssl-$OPENSSL_VERSION"
  local prefix="$BUILD_ROOT/openssl/$abi"

  if [[ -f "$prefix/lib/libssl.a" && -f "$prefix/lib/libcrypto.a" ]]; then
    echo "==> Reusing OpenSSL for $abi"
    return
  fi

  if [[ ! -d "$src" ]]; then
    echo "==> Downloading OpenSSL $OPENSSL_VERSION"
    curl -fsSL \
      "https://github.com/openssl/openssl/releases/download/openssl-$OPENSSL_VERSION/openssl-$OPENSSL_VERSION.tar.gz" \
      -o "$BUILD_ROOT/openssl.tar.gz"
    mkdir -p "$src"
    tar xzf "$BUILD_ROOT/openssl.tar.gz" -C "$src" --strip-components=1
  fi

  local target=""
  case "$abi" in
    arm64-v8a) target="android-arm64" ;;
    armeabi-v7a) target="android-arm" ;;
    x86_64) target="android-x86_64" ;;
    x86) target="android-x86" ;;
    *) echo "error: unknown Android ABI: $abi" >&2; exit 1 ;;
  esac

  echo "==> Building OpenSSL for $abi"
  (
    cd "$src"
    make clean >/dev/null 2>&1 || true
    ./Configure "$target" "-D__ANDROID_API__=$ANDROID_API" \
      no-shared no-tests no-apps no-docs --libdir=lib --prefix="$prefix"
    make -j"$(getconf _NPROCESSORS_ONLN)" build_libs
    make install_dev
  )
}

prepare_cross_compiling() {
  if [[ -f "$TD_SRC/tdutils/generate/auto/mime_type_to_extension.cpp" ]]; then
    return
  fi

  echo "==> Preparing TDLib cross-compiling sources"
  cmake -S "$TD_SRC" -B "$BUILD_ROOT/native-generate" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=/usr/bin/clang \
    -DCMAKE_CXX_COMPILER=/usr/bin/clang++ \
    -DOPENSSL_ROOT_DIR="${HOST_OPENSSL_ROOT_DIR:-/usr}"
  cmake --build "$BUILD_ROOT/native-generate" --target prepare_cross_compiling \
    -j"$(getconf _NPROCESSORS_ONLN)"
}

build_tdjson() {
  local abi="$1"
  local openssl="$BUILD_ROOT/openssl/$abi"
  local td_build="$BUILD_ROOT/td-$abi"
  local package_root="$BUILD_ROOT/package-$abi"
  local zip="$OUT_DIR/tdjson-android-$abi.zip"

  echo "==> Building tdjson for $abi"
  rm -rf "$td_build" "$package_root" "$zip"
  cmake -S "$TD_SRC" -B "$td_build" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$ANDROID_NDK_HOME/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI="$abi" \
    -DANDROID_PLATFORM="android-$ANDROID_API" \
    -DCMAKE_BUILD_TYPE=Release \
    -DTD_ENABLE_LTO="${TD_ENABLE_LTO:-OFF}" \
    -DOPENSSL_ROOT_DIR="$openssl" \
    -DOPENSSL_INCLUDE_DIR="$openssl/include" \
    -DOPENSSL_CRYPTO_LIBRARY="$openssl/lib/libcrypto.a" \
    -DOPENSSL_SSL_LIBRARY="$openssl/lib/libssl.a" \
    -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=BOTH \
    -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=BOTH
  cmake --build "$td_build" --target tdjson -j"$(getconf _NPROCESSORS_ONLN)"

  mkdir -p "$package_root/$abi"
  cp "$td_build/libtdjson.so" "$package_root/$abi/libtdjson.so"
  strip_bin="$(ls "$ANDROID_NDK_HOME"/toolchains/llvm/prebuilt/*/bin/llvm-strip | head -n 1)"
  "$strip_bin" --strip-unneeded "$package_root/$abi/libtdjson.so"

  mkdir -p "$OUT_DIR"
  (
    cd "$package_root"
    zip -qr "$zip" "$abi"
  )
  echo "wrote $zip"
}

prepare_td_source
apply_mithka_patches
prepare_cross_compiling
for abi in "${ABIS[@]}"; do
  build_openssl "$abi"
  build_tdjson "$abi"
done
