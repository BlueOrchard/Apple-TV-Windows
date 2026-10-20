# bumble-bridge — Siri Remote as a Windows trackpad, without the Windows Bluetooth stack

`bridge.py` drives a USB Bluetooth dongle directly over raw HCI using Google's
[Bumble](https://github.com/google/bumble) Python stack. Windows' own Bluetooth
stack never sees the dongle or the remote, so the GATT-discovery stall and the
blocked HID service do not apply. The script scans for the remote
(`78:9F:70:46:27:BC`), connects, bonds, sends the `0xAF` unlock write, decodes
input report `0xFC` and injects mouse / wheel / media-key events with `SendInput`.

The dongle used is the **BARROT Bluetooth 5.4 USB dongle** (USB `VID_33FA`,
`PID_0010`). Do not use either Intel radio (`VID_8087`).

For a from-scratch install on another PC, including running it under PM2 at login,
see [SETUP.md](SETUP.md).

## 1. One-time driver swap (you do this, by hand)

Bumble talks to the dongle through libusb, which on Windows needs the dongle to be
bound to the **WinUSB** driver instead of Microsoft's `BTHUSB` Bluetooth driver.
Swap it with [Zadig](https://zadig.akeo.ie/):

1. Plug in the BARROT dongle.
2. Run `zadig.exe` (it asks for admin rights).
3. Menu **Options → List All Devices**.
4. In the drop-down pick the BARROT dongle. Check the USB ID shown underneath
   reads **33FA 0010**. If the dongle shows up as two entries, pick interface 0
   (`MI_00`) or the composite parent, whichever shows `33FA 0010`; if it is listed
   as a plain parent without `MI_xx`, use that.
5. In the driver box on the right choose **WinUSB (v6.1.xxxx)** (use the small
   up/down arrows), then click **Replace Driver**. Confirm the warning.
6. Wait for "The driver was installed successfully", then close Zadig.

This **removes the dongle from Windows Bluetooth** (intended): it no longer appears
as a Bluetooth radio in Settings, and Windows cannot use it. Windows keeps using the
onboard Intel AX210 for its own Bluetooth.

### Reverting the swap

Device Manager → *Universal Serial Bus devices* → the BARROT device (WinUSB) →
right-click → **Uninstall device** → tick **Attempt to remove the driver for this
device** → Uninstall. Unplug and replug the dongle; Windows re-installs its own
Bluetooth driver.

## 2. Prerequisites

```
C:\Python310\python.exe -m pip install -r requirements.txt
```

`libusb-package` ships the `libusb-1.0.dll` that Bumble's `usb:` transport loads, so
no separate libusb install is needed.

## 3. Before running

- Remove the remote from **Settings → Bluetooth & devices** (it shows as
  `DJ7PWT5HGQQT`). While Windows still holds the bond the remote keeps
  reconnecting to Windows instead of advertising for us.
- Put the remote in pairing mode: hold **Menu + Volume Up** for ~5 s.
  If it is stuck and never advertises: hold **Menu + Home/TV** ~5 s (restart),
  then **Menu + Volume Up** ~5 s again.

## 4. Running

```
C:\Python310\python.exe tools\bumble-bridge\bridge.py              # full trackpad
C:\Python310\python.exe tools\bumble-bridge\bridge.py --log-only   # decode & print, inject nothing
C:\Python310\python.exe tools\bumble-bridge\bridge.py --no-hex     # trackpad, without the per-notification hex dump
C:\Python310\python.exe tools\bumble-bridge\bridge.py --forget     # drop the stored bond, then run
```

Other options: `--usb 33FA:0010` (transport spec, add `!` to force the first
interface), `--remote 78:9F:70:46:27:BC`, `--debug` (Bumble's own debug log).
Stop with Ctrl+C. Bond keys are stored in `keys.json` next to the script; delete it
(or use `--forget`) to re-pair from scratch, and re-pair the remote too (pairing mode).

### Mapping

| Remote                | Windows                                   |
| --------------------- | ----------------------------------------- |
| 1 finger slide        | pointer motion (mild acceleration)        |
| 2 finger slide        | vertical + horizontal scroll, natural     |
| 1 finger tap          | left click                                |
| 2 finger tap          | right click                               |
| Touchpad press        | left click (right with 2 fingers), held   |
| Play/Pause            | media Play/Pause key                      |
| Volume Up / Down      | wheel up / down, repeats while held       |
| Menu, Home/TV, Siri   | unassigned (logged only)                  |

All gains and timings are constants at the top of `bridge.py`.

### Smoothness

The remote sends exactly one touch report per BLE connection event, so the pointer
update rate equals the connection interval. The bridge asks for a 7.5 ms interval
(`CONN_INTERVAL_MIN_MS` / `CONN_INTERVAL_MAX_MS`), which the remote accepts, giving
about 133 reports per second. With the default 15-30 ms interval it was only 33.

## 5. What a failure looks like

- `No USB device matching 33FA:0010` — Zadig swap not done, or dongle unplugged.
- `LIBUSB_ERROR_NOT_SUPPORTED` / `LIBUSB_ERROR_ACCESS` on open — the dongle is still
  on the Bluetooth driver (or another program has it). Redo step 1.
- `HCI Reset timed out` — the dongle does not speak plain HCI without vendor
  initialisation; this route is then ruled out for this dongle.
- `USB transport lost` — the BARROT dongle occasionally drops off the USB bus and
  re-enumerates; the bridge reopens it by itself after a couple of seconds.
- `DISCONNECTED reason=0x3E` right after connecting — common with this dongle; the
  bridge retries a second later and the next attempt usually sticks.
- Remote seen in the scan but `connect` times out — remote is still bonded to
  Windows, or is asleep; see section 3.
