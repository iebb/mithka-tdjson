# Mithka Native TDLib Artifacts

This repository hosts prebuilt native `tdjson` artifacts used by Mithka.

The main Mithka app repository contains the Flutter app and its Dart TDLib
adapter. This repository exists only to keep large platform-specific TDLib
binaries and native packaging notes out of the user-facing app checkout.

## Release Assets

Current app setup expects:

- `tdjson-android-arm64-v8a.zip`
- `tdjson-android-armeabi-v7a.zip`
- `tdjson-android-x86_64.zip`
- `tdjson-ios.xcframework.zip`
- `tgvoip-ios.xcframework.zip`

Each Android zip should contain its ABI directory at the root:

```text
arm64-v8a/
  libtdjson.so
```

The iOS zip should contain `tdjson.xcframework` at its root:

```text
tdjson.xcframework/
  Info.plist
  ios-arm64/
  ios-arm64_x86_64-simulator/
```

The TgVoip zip contains the official Telegram iOS group-call engine for arm64
iOS devices and Apple-silicon simulators:

```text
TgVoipWebrtc.xcframework/
  Info.plist
  ios-arm64/
  ios-arm64-simulator/
```

## Automated Upstream Sync

`.github/workflows/sync-upstream.yml` runs daily and can also be started
manually. It resolves the current `tdlib/td` `master` commit, reads the TDLib
version from upstream, and publishes an immutable release tagged
`tdlib-<version>-<sha12>` only when that exact source release does not exist.

The release publishes all assets consumed by the Mithka app CI:

```text
tdjson-android-arm64-v8a.zip
tdjson-android-armeabi-v7a.zip
tdjson-android-x86_64.zip
tdjson-ios.xcframework.zip
```

Mithka pins this exact release tag, so historical app builds keep using their
original TDLib binaries. Releases are never deleted by the sync workflow. A
manual `force=true` run publishes a separate
`tdlib-<version>-<sha12>-rebuild-<run-id>-<attempt>` release instead of replacing
the original artifacts.

## Telegram TgVoip iOS

`.github/workflows/build-tgvoip-ios.yml` builds the same `TgVoipWebrtc` Bazel
target used by the official Telegram iOS client. It publishes an immutable,
non-latest release tagged `tgvoip-telegram-ios-<sha12>` with this asset:

```text
tgvoip-ios.xcframework.zip
```

The workflow is manual because the Mithka app pins the full release URL. Run it
with `telegram_ref=master` for the current official client commit, or provide an
exact Telegram iOS commit for a reproducible rebuild.

## Package Android Artifacts

```sh
scripts/build-android-libs.sh arm64-v8a
```

The script writes `dist/tdjson-android-<abi>.zip`.

## Package iOS Artifact

From the Mithka app checkout, after `ios/tdjson/tdjson.xcframework` exists:

```sh
scripts/package-ios-xcframework.sh /path/to/mithka/ios/tdjson/tdjson.xcframework
```

Then upload `dist/tdjson-ios.xcframework.zip` to a GitHub Release.

## License

TDLib is developed by Telegram and distributed under its upstream license.
This repository only packages the native `tdjson` binary for Mithka builds.
