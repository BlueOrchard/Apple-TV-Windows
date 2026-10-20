# Setting up the bridge on another Windows PC (with PM2)

Goal: the Siri Remote (A1513) works as a trackpad on a second Windows 11 PC, with the
bridge started automatically at login and restarted if it dies.

You need: a USB Bluetooth dongle you can dedicate to this (tested: BARROT Bluetooth 5.4,
USB `33FA:0010`; any plain-HCI dongle should work), Python 3.10 or newer, Node.js, and
admin rights once for the driver swap.

## 1. Clone and install Python dependencies

```
git clone https://github.com/BlueOrchard/Apple-TV-Windows.git C:\Apple-TV-Windows
cd C:\Apple-TV-Windows	oolsumble-bridge
python -m pip install -r requirements.txt
```

All commands below run from `C:\Apple-TV-Windows	oolsumble-bridge`.

`requirements.txt` pins Bumble 0.0.235 and `libusb-package` 1.0.26.4 (the libusb DLL
Bumble loads). If `python` is not on PATH, use the full path, e.g.
`C:\Python310\python.exe -m pip install -r requirements.txt`.

## 2. Put the dongle on the WinUSB driver (one time, admin)

1. Plug in the dongle. Download and run [Zadig](https://zadig.akeo.ie/).
2. Options → **List All Devices**.
3. Pick the dongle. It shows its USB product string, not the Windows name: the BARROT
   dongle appears as **USB2.0-BT**. Check the USB ID boxes read **33FA 0010**.
   Do not pick any other Bluetooth radio (Intel ones have ID 8087).
4. Set the target driver to **WinUSB**, click **Replace Driver**, confirm.

The dongle disappears from Windows Bluetooth settings. That is intended. To undo it later:
Device Manager → Universal Serial Bus devices → the dongle → Uninstall device → tick
"Attempt to remove the driver" → unplug and replug.

If your dongle has a different VID:PID, change `USB_TRANSPORT` at the top of `bridge.py`
or pass `--usb VVVV:PPPP`.

## 3. Pair the remote once, by hand

The remote bonds with one computer at a time. Pairing it with the new PC replaces its
bond with the old one.

1. If the remote is paired with this PC's Windows Bluetooth, remove it there first
   (it shows as its serial, e.g. `DJ7PWT5HGQQT`).
2. Run the bridge in the foreground:
   ```
   python bridge.py --no-hex
   ```
3. When it prints `scanning for 78:9F:70:46:27:BC`, hold **Menu + Volume Up** on the
   remote for about 5 seconds. Expect one or two `DISCONNECTED reason=0x3E` lines;
   the bridge retries by itself. Within ~10 s you should see `paired`,
   `unlock write OK` and `ready`.
4. Move a finger on the touchpad: the pointer should move. Ctrl+C to stop.

The bond is saved in `keys.json` next to the script (ignored by git). After this, any
button press reconnects the remote; no pairing mode needed.

If the remote never shows up in the scan, restart it: hold **Menu + Home/TV** ~5 s,
then **Menu + Volume Up** ~5 s again.

## 4. Run it under PM2

PM2 keeps the bridge running and restarts it on crashes or dongle hiccups.

```
npm install -g pm2 pm2-windows-startup
cd C:\Apple-TV-Windows	oolsumble-bridge
pm2 start ecosystem.config.js
pm2 save
pm2-startup install
```

`ecosystem.config.js` runs `bridge.py --no-hex` with the `python` on PATH. To use a
specific interpreter, set it once before `pm2 start`:

```
set BRIDGE_PYTHON=C:\Python310\python.exe
pm2 start ecosystem.config.js
pm2 save
```

`pm2-startup install` registers PM2 to resurrect the saved process list when you log in.
That matters: the bridge injects input with `SendInput`, which only works from the
logged-in desktop session. Do **not** install it as a Windows service
(`pm2-windows-service`); a service runs in session 0 and its input goes nowhere.

Useful commands:

```
pm2 logs siri-remote-bridge      # live output (scan, connect, unlock, gestures)
pm2 restart siri-remote-bridge   # after editing gains in bridge.py
pm2 stop siri-remote-bridge
pm2 delete siri-remote-bridge && pm2 save
```

## 5. Tuning

Everything adjustable is at the top of `bridge.py`: pointer gain and acceleration,
scroll gain and direction, tap timing, volume-button repeat, and the requested
connection interval (7.5 ms; the remote sends one touch report per connection event,
so this sets the pointer update rate). Restart the process after editing.

## 6. Known behaviour

- First connection attempts often drop with `0x3E` ~150 ms in; the retry a second
  later works.
- The BARROT dongle occasionally drops off the USB bus and re-enumerates. The bridge
  notices, reopens it after a couple of seconds and carries on.
- The remote sleeps after a few idle minutes. Any button press wakes and reconnects
  it; the first press after a long sleep may be lost.
