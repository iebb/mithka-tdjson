# Mithka Native TDLib Artifacts

This repository hosts prebuilt native `tdjson` artifacts used by Mithka.

The main Mithka app repository contains the Flutter app and its Dart TDLib
adapter. This repository exists only to keep large platform-specific TDLib
binaries and native packaging notes out of the user-facing app checkout.

## Release Assets

Current app setup expects:

- `tdjson-ios.xcframework.zip`

The zip should contain `tdjson.xcframework` at its root:

```text
tdjson.xcframework/
  Info.plist
  ios-arm64/
  ios-arm64_x86_64-simulator/
```

## Package iOS Artifact

From the Mithka app checkout, after `ios/tdjson/tdjson.xcframework` exists:

```sh
scripts/package-ios-xcframework.sh /path/to/mithka/ios/tdjson/tdjson.xcframework
```

Then upload `dist/tdjson-ios.xcframework.zip` to a GitHub Release.

## License

TDLib is developed by Telegram and distributed under its upstream license.
This repository only packages the native `tdjson` binary for Mithka builds.

