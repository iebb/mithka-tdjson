#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TD_REPO="${TD_REPO:-https://github.com/tdlib/td.git}"
TD_COMMIT="${TD_COMMIT:-master}"
OPENSSL_VERSION="${OPENSSL_VERSION:-3.3.2}"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/build/ohos}"
OUT_DIR="$ROOT/dist"
TD_SRC="$BUILD_ROOT/td"
TD_VERSION=""

ABI=arm64-v8a

# Locate the OpenHarmony native SDK shipped with the HarmonyOS command-line
# tools. Override with OHOS_SDK_NATIVE to point at
# .../openharmony/native directly.
find_ohos_sdk() {
  if [[ -n "${OHOS_SDK_NATIVE:-}" ]]; then
    return
  fi
  local candidate
  for candidate in \
    /opt/harmonyos-tools/command-line-tools/sdk/default/openharmony/native \
    "$HOME/Library/OpenHarmony/Sdk/default/openharmony/native" \
    "${DEVECO_SDK_HOME:-/nonexistent}/default/openharmony/native"; do
    if [[ -x "$candidate/llvm/bin/aarch64-unknown-linux-ohos-clang++" ]]; then
      OHOS_SDK_NATIVE="$candidate"
      return
    fi
  done
  echo "error: OpenHarmony native SDK not found. Set OHOS_SDK_NATIVE." >&2
  exit 1
}
find_ohos_sdk
OHOS_TOOLCHAIN_CMAKE="$OHOS_SDK_NATIVE/build/cmake/ohos.toolchain.cmake"
OHOS_LLVM_BIN="$OHOS_SDK_NATIVE/llvm/bin"
export PATH="$OHOS_LLVM_BIN:$PATH"
echo "==> OHOS native SDK: $OHOS_SDK_NATIVE"

prepare_td_source() {
  mkdir -p "$BUILD_ROOT"
  if [[ ! -d "$TD_SRC/.git" ]]; then
    rm -rf "$TD_SRC"
    git clone --depth 1 "$TD_REPO" "$TD_SRC"
  fi
  git -C "$TD_SRC" fetch --quiet origin "$TD_COMMIT"
  git -C "$TD_SRC" reset --hard HEAD >/dev/null
  git -C "$TD_SRC" clean -fdx >/dev/null
  git -C "$TD_SRC" checkout --detach --quiet FETCH_HEAD
  git -C "$TD_SRC" reset --hard FETCH_HEAD >/dev/null
  TD_VERSION="$(sed -n 's/project(TDLib VERSION \([^ ]*\).*/\1/p' "$TD_SRC/CMakeLists.txt" | head -n 1)"
  if [[ -z "$TD_VERSION" ]]; then
    echo "error: could not read TDLib version from $TD_SRC" >&2
    exit 1
  fi
  echo "==> TDLib $TD_VERSION ($(git -C "$TD_SRC" rev-parse HEAD))"
}

apply_mithka_patches() {
  "$ROOT/scripts/apply-mithka-patches.sh" "$TD_SRC"
}

# OpenSSL for aarch64-unknown-linux-ohos. The OpenHarmony clang wrappers are
# not prefix-style cross tools, so Configure gets the tools via CC/AR/RANLIB
# and no-asm skips the perl assember, which cannot target linux-ohos.
build_openssl() {
  local src="$BUILD_ROOT/openssl-$OPENSSL_VERSION"
  local prefix="$BUILD_ROOT/openssl/$ABI"

  if [[ -f "$prefix/lib/libssl.a" && -f "$prefix/lib/libcrypto.a" ]]; then
    echo "==> Reusing OpenSSL for ohos $ABI"
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

  echo "==> Building OpenSSL for ohos $ABI"
  (
    cd "$src"
    make clean >/dev/null 2>&1 || true
    CC="$OHOS_LLVM_BIN/aarch64-unknown-linux-ohos-clang" \
    AR="$OHOS_LLVM_BIN/llvm-ar" \
    RANLIB="$OHOS_LLVM_BIN/llvm-ranlib" \
    ./Configure linux-aarch64 no-asm no-shared no-tests no-apps no-docs \
      --libdir=lib --prefix="$prefix"
    make -j"$(getconf _NPROCESSORS_ONLN)" build_libs
    make install_dev
  )
}

# TDLib cross-compiling needs generated sources built by the host compiler.
# The OpenHarmony SDK ships no host clang, so a system clang must be present
# (apt install clang on the CI image) and libssl-dev for the generator build.
prepare_cross_compiling() {
  if [[ -f "$TD_SRC/tdutils/generate/auto/mime_type_to_extension.cpp" ]]; then
    return
  fi

  echo "==> Preparing TDLib cross-compiling sources"
  cmake -S "$TD_SRC" -B "$BUILD_ROOT/native-generate" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER="${HOST_C_COMPILER:-/usr/bin/clang}" \
    -DCMAKE_CXX_COMPILER="${HOST_CXX_COMPILER:-/usr/bin/clang++}" \
    -DOPENSSL_ROOT_DIR="${HOST_OPENSSL_ROOT_DIR:-/usr}"
  cmake --build "$BUILD_ROOT/native-generate" --target prepare_cross_compiling \
    -j"$(getconf _NPROCESSORS_ONLN)"
}

# The OHOS toolchain file forces CMAKE_FIND_ROOT_PATH_MODE_INCLUDE/LIBRARY to
# ONLY and roots them at the SDK dir instead of the sysroot, so CMake's zlib
# discovery misses sysroot/usr/include and sysroot/usr/lib/aarch64-linux-ohos.
# Pass the paths explicitly, like the OpenSSL settings below.
# abi lib dir: arm64-v8a -> aarch64-linux-ohos
OHOS_LIB_DIR="$OHOS_SDK_NATIVE/sysroot/usr/lib/aarch64-linux-ohos"

apply_ohos_fixes() {
  # The musl-based OHOS sysroot declares sched_setaffinity/sched_getaffinity
  # but not pthread_setaffinity_np/pthread_getaffinity_np, which TDLib uses
  # unconditionally whenever TD_LINUX is set (OHOS defines __linux__). Thread
  # affinity is optional and every call site is guarded by
  # TD_HAVE_THREAD_AFFINITY, so disable the feature for OHOS at the platform
  # guard rather than touching TDLib's scheduler logic.
  local header="$TD_SRC/tdutils/td/utils/port/detail/ThreadPthread.h"
  if grep -q '^#if TD_LINUX || TD_FREEBSD || TD_NETBSD$' "$header"; then
    sed -i \
      's,^#if TD_LINUX || TD_FREEBSD || TD_NETBSD$,#if TD_FREEBSD || TD_NETBSD || (TD_LINUX \&\& !defined(__OHOS__)),' \
      "$header"
    echo "==> Disabled TD thread affinity for OHOS"
  elif grep -q 'TD_LINUX && !defined(__OHOS__)' "$header"; then
    echo "==> OHOS thread-affinity fix already applied"
  else
    echo "error: could not apply OHOS thread-affinity fix to $header" >&2
    exit 1
  fi
}

build_tdjson() {
  local openssl="$BUILD_ROOT/openssl/$ABI"
  local td_build="$BUILD_ROOT/td-$ABI"
  local package_root="$BUILD_ROOT/package-$ABI"
  local zip="$OUT_DIR/${OHOS_ZIP_NAME:-tdjson-ohos-arm64.zip}"

  echo "==> Building tdjson for ohos $ABI"
  rm -rf "$td_build" "$package_root" "$zip"
  cmake -S "$TD_SRC" -B "$td_build" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$OHOS_TOOLCHAIN_CMAKE" \
    -DOHOS_ARCH="$ABI" \
    -DOHOS_STL=c++_static \
    -DCMAKE_BUILD_TYPE=Release \
    -DTD_ENABLE_LTO="${TD_ENABLE_LTO:-OFF}" \
    -DZLIB_INCLUDE_DIR="$OHOS_SDK_NATIVE/sysroot/usr/include" \
    -DZLIB_LIBRARY="$OHOS_LIB_DIR/libz.so" \
    -DOPENSSL_ROOT_DIR="$openssl" \
    -DOPENSSL_INCLUDE_DIR="$openssl/include" \
    -DOPENSSL_CRYPTO_LIBRARY="$openssl/lib/libcrypto.a" \
    -DOPENSSL_SSL_LIBRARY="$openssl/lib/libssl.a" \
    -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=BOTH \
    -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=BOTH
  cmake --build "$td_build" --target tdjson -j"${TD_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN)}"

  mkdir -p "$package_root/$ABI"
  cp "$td_build/libtdjson.so" "$package_root/$ABI/libtdjson.so"
  strip_bin="$OHOS_LLVM_BIN/llvm-strip"
  "$strip_bin" --strip-unneeded "$package_root/$ABI/libtdjson.so"
  nm_bin="$OHOS_LLVM_BIN/llvm-nm"
  symbols="$(  "$nm_bin" -D --defined-only "$package_root/$ABI/libtdjson.so")"
  for symbol in \
    td_create_client_id \
    td_mithka_export_session_string \
    td_mithka_import_session_string \
    td_mithka_last_error \
    td_mithka_set_transfer_boost; do
    grep " $symbol$" <<<"$symbols" >/dev/null
  done

  mkdir -p "$OUT_DIR"
  (
    cd "$package_root"
    zip -qr "$zip" "$ABI"
  )
  echo "wrote $zip"
}

prepare_td_source
apply_mithka_patches
apply_ohos_fixes
build_openssl
prepare_cross_compiling
build_tdjson
