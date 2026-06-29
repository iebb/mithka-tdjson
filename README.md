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

## Automated Upstream Sync

`.github/workflows/sync-upstream.yml` runs weekly and can also be started
manually. It resolves the current `tdlib/td` `master` commit and publishes a
release tagged `upstream-<sha12>` only when that tag does not already exist.

The release publishes all assets consumed by the Mithka app CI:

```text
tdjson-android-arm64-v8a.zip
tdjson-android-armeabi-v7a.zip
tdjson-android-x86_64.zip
tdjson-ios.xcframework.zip
```

Use the manual workflow with `force=true` to rebuild an existing upstream commit.

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
