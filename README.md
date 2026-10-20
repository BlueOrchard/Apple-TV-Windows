# Apple-TV-Windows

Making a 1st-generation Apple Siri Remote (model A1513, Bluetooth LE, glass touchpad)
work as a trackpad on Windows 11.

Windows' own Bluetooth stack pairs the remote but stalls during GATT discovery and
never exposes its HID service, so the working approach drives a dedicated USB
Bluetooth dongle directly over raw HCI, bypassing Windows Bluetooth entirely.

## Contents

- [`tools/bumble-bridge/`](tools/bumble-bridge/) — **the working bridge.** Python,
  Google's [Bumble](https://github.com/google/bumble) stack over a WinUSB dongle.
  Pointer, tap/click, two-finger scroll, pad click, Play/Pause and volume as wheel.
  See its [README](tools/bumble-bridge/README.md) and
  [SETUP.md](tools/bumble-bridge/SETUP.md) (fresh install on another PC, with PM2).

Protocol reference: the report layout and the `0xAF` unlock write come from
[open-appleremote](https://github.com/a-funk/open-appleremote/blob/main/docs/PROTOCOL.md).
