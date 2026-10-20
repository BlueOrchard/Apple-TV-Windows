#!/usr/bin/env python3
"""Siri Remote (A1513) -> Windows trackpad bridge over a WinUSB Bluetooth dongle.

Uses Google's Bumble stack over raw HCI (usb: transport, libusb/WinUSB), so the
Windows Bluetooth stack is not involved at all. See README.md for the one-time
driver swap and the run instructions.

Flow: open dongle -> HCI reset -> scan for the remote -> connect -> bond (or
re-encrypt with stored keys) -> discover HID service -> subscribe to Report
notifications -> write 0xAF to the feature report 0xF0 (unlock) -> decode input
report 0xFC -> inject input with SendInput. Reconnects automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field

# =============================================================================
# Tunables
# =============================================================================

REMOTE_ADDRESS = "78:9F:70:46:27:BC"      # the Siri Remote (public address)
USB_TRANSPORT = "usb:33FA:0010"           # BARROT dongle, VID:PID (append '!' to force interface 0)
KEYSTORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys.json")

# Pointer (one finger). Touch units: ~1650 per axis across the pad.
# Touch reports arrive about every 30 ms (60 ticks of the 2 kHz report clock).
MOUSE_GAIN = 0.35             # screen pixels per touch unit at low speed
MOUSE_ACCEL = 0.05            # extra multiplier per (touch unit/ms) of speed above the threshold
MOUSE_ACCEL_THRESHOLD = 3.0   # touch units per ms before acceleration starts
MOUSE_ACCEL_MAX = 1.8         # cap on the acceleration multiplier
MOUSE_DEADZONE = 1.0          # ignore per-report jitter smaller than this (touch units)

# Scrolling (two fingers). Wheel units: 120 = one notch.
SCROLL_GAIN = 0.9             # wheel units per touch unit
SCROLL_NATURAL = True         # content follows the fingers (macOS style)
SCROLL_AXIS_LOCK = True       # lock to the dominant axis once the gesture has moved a bit
SCROLL_AXIS_LOCK_UNITS = 25.0

# Taps
TAP_MAX_MS = 220              # finger(s) down for at most this long
TAP_MAX_TRAVEL = 40.0         # and moved at most this far (touch units)
TOUCH_TIMEOUT_MS = 100        # no touch report for this long => fingers lifted

# Volume buttons as wheel
VOLUME_WHEEL_NOTCHES = 1      # notches per press / per repeat
VOLUME_REPEAT_DELAY_MS = 350  # hold this long before auto-repeat begins
VOLUME_REPEAT_INTERVAL_MS = 90

ENGINE_TICK_MS = 15           # gesture engine timer resolution

# LE connection parameters requested from the remote. The remote sends one touch
# report per connection event, so a short interval means more pointer updates.
CONN_INTERVAL_MIN_MS = 7.5
CONN_INTERVAL_MAX_MS = 7.5    # measured: one touch report per connection event (15 ms interval -> 15 ms reports)
CONN_LATENCY = 0              # peripheral latency, in intervals
CONN_SUPERVISION_TIMEOUT_MS = 2000

# =============================================================================
# Logging
# =============================================================================

T0 = time.monotonic()


def ts() -> str:
    return f"{time.monotonic() - T0:9.3f}"


LOG_FILE = None


def log(msg: str) -> None:
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    if LOG_FILE is not None:
        LOG_FILE.write(line + "\n")
        LOG_FILE.flush()


# =============================================================================
# Windows input injection (ctypes SendInput)
# =============================================================================

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
VK_MEDIA_PLAY_PAUSE = 0xB3
WHEEL_DELTA = 120

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class Injector:
    """Real SendInput-based injector."""

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self.user32.SendInput.restype = wt.UINT

    def _send(self, inp: INPUT) -> None:
        sent = self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if sent != 1:
            log(f"SendInput failed, error {ctypes.get_last_error()}")

    def _mouse(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi = MOUSEINPUT(dx, dy, ctypes.c_uint32(data & 0xFFFFFFFF).value, flags, 0, 0)
        self._send(inp)

    def move(self, dx: int, dy: int) -> None:
        if dx or dy:
            self._mouse(MOUSEEVENTF_MOVE, dx, dy)

    def wheel(self, delta: int) -> None:
        if delta:
            self._mouse(MOUSEEVENTF_WHEEL, data=delta)

    def hwheel(self, delta: int) -> None:
        if delta:
            self._mouse(MOUSEEVENTF_HWHEEL, data=delta)

    def button(self, which: str, down: bool) -> None:
        if which == "left":
            self._mouse(MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP)
        else:
            self._mouse(MOUSEEVENTF_RIGHTDOWN if down else MOUSEEVENTF_RIGHTUP)

    def click(self, which: str) -> None:
        self.button(which, True)
        self.button(which, False)

    def key(self, vk: int, down: bool) -> None:
        inp = INPUT(type=INPUT_KEYBOARD)
        flags = KEYEVENTF_EXTENDEDKEY | (0 if down else KEYEVENTF_KEYUP)
        inp.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
        self._send(inp)


class LogInjector:
    """--log-only: print what would have been injected."""

    def move(self, dx: int, dy: int) -> None:
        if dx or dy:
            log(f"  >> move dx={dx:+d} dy={dy:+d}")

    def wheel(self, delta: int) -> None:
        if delta:
            log(f"  >> wheel {delta:+d}")

    def hwheel(self, delta: int) -> None:
        if delta:
            log(f"  >> hwheel {delta:+d}")

    def button(self, which: str, down: bool) -> None:
        log(f"  >> {which} button {'down' if down else 'up'}")

    def click(self, which: str) -> None:
        log(f"  >> {which} click")

    def key(self, vk: int, down: bool) -> None:
        log(f"  >> key 0x{vk:02X} {'down' if down else 'up'}")


# =============================================================================
# Report 0xFC decoding
# =============================================================================

KIND_BUTTONS = 0x00
KIND_AUDIO = 0x10
KIND_UNLOCK_REPLY = 0xE0
FINGER_OFFSET = 6
FINGER_SIZE = 7

BTN_HOME = 0x01
BTN_VOL_UP = 0x02
BTN_VOL_DOWN = 0x04
BTN_PLAY_PAUSE = 0x08
BTN_SIRI = 0x10
BTN_MENU = 0x20
BTN_PAD_CLICK = 0x80

BUTTON_NAMES = {
    BTN_HOME: "Home/TV",
    BTN_VOL_UP: "Vol+",
    BTN_VOL_DOWN: "Vol-",
    BTN_PLAY_PAUSE: "Play/Pause",
    BTN_SIRI: "Siri",
    BTN_MENU: "Menu",
    BTN_PAD_CLICK: "PadClick",
}


def button_names(mask: int) -> str:
    names = [n for b, n in BUTTON_NAMES.items() if mask & b]
    return "+".join(names) if names else "-"


def signed12(v: int) -> int:
    return v - 0x1000 if v & 0x800 else v


@dataclass
class Finger:
    x: int
    y: int          # +y is UP on the remote
    state: int      # bits 0-2 of b6: 3..6 on surface, 7 lifted, 0 gone
    raw: bytes

    @property
    def down(self) -> bool:
        return 3 <= self.state <= 6


@dataclass
class Report:
    kind: int
    buttons: int
    clock: int | None
    flags: int | None
    fingers: list[Finger]
    raw: bytes

    def __str__(self) -> str:
        s = f"kind=0x{self.kind:02X} btn=0x{self.buttons:02X}({button_names(self.buttons)})"
        if self.clock is not None:
            s += f" clk={self.clock} flags=0x{self.flags:02X}"
        for i, f in enumerate(self.fingers):
            s += f" f{i}=({f.x:+5d},{f.y:+5d},st={f.state}{'' if f.down else '^'} {f.raw[3:6].hex()})"
        return s


def decode_report(data: bytes) -> Report | None:
    if len(data) >= 1 and data[0] == 0xFC and len(data) in (3, 14, 21):
        data = data[1:]  # tolerate a leading report ID byte
    if len(data) < 2:
        return None
    kind, buttons = data[0], data[1]
    if kind in (KIND_BUTTONS, KIND_AUDIO, KIND_UNLOCK_REPLY) or len(data) < 6:
        return Report(kind, buttons, None, None, [], data)
    clock = data[3] | (data[4] << 8)
    flags = data[5]
    fingers: list[Finger] = []
    nfingers = kind if kind <= 2 else 0
    for k in range(nfingers):
        off = FINGER_OFFSET + k * FINGER_SIZE
        if off + FINGER_SIZE > len(data):
            break
        f = data[off:off + FINGER_SIZE]
        x = signed12(f[0] | ((f[1] & 0x0F) << 8))
        y = signed12((f[1] >> 4) | (f[2] << 4))
        fingers.append(Finger(x, y, f[6] & 0x07, bytes(f)))
    return Report(kind, buttons, clock, flags, fingers, data)


# =============================================================================
# Gesture engine
# =============================================================================


@dataclass
class Gesture:
    start_ms: float
    start_xy: tuple[float, float]
    max_fingers: int = 1
    max_travel: float = 0.0
    clicked: bool = False       # pad was physically pressed during this touch
    scrolled: bool = False
    axis: str | None = None     # 'x' or 'y' once locked
    travel_x: float = 0.0
    travel_y: float = 0.0


class GestureEngine:
    def __init__(self, inj) -> None:
        self.inj = inj
        self.gesture: Gesture | None = None
        self.prev_count = 0
        self.prev_xy: tuple[float, float] | None = None   # finger 0 (1 finger) or centroid (2)
        self.last_touch_ms = 0.0
        self.mouse_rem = [0.0, 0.0]
        self.wheel_rem = [0.0, 0.0]
        self.buttons = 0
        self.pad_button: str | None = None  # which mouse button the pad press is holding
        self.vol_dir = 0                     # +1 up, -1 down, 0 none
        self.vol_next_ms = 0.0

    # ---- touch -----------------------------------------------------------

    def on_touch(self, fingers: list[Finger], now_ms: float, dt_ms: float = 30.0) -> None:
        down = [f for f in fingers if f.down]
        count = len(down)
        self.last_touch_ms = now_ms

        if count == 0:
            self._lift(now_ms)
            return

        if count == 1:
            xy = (float(down[0].x), float(down[0].y))
        else:
            xy = (sum(f.x for f in down) / count, sum(f.y for f in down) / count)

        g = self.gesture
        if g is None:
            g = self.gesture = Gesture(start_ms=now_ms, start_xy=xy, max_fingers=count,
                                       clicked=self.pad_button is not None)
            self.prev_xy = xy
            self.prev_count = count
            return

        g.max_fingers = max(g.max_fingers, count)
        if count != self.prev_count or self.prev_xy is None:
            # finger count changed: re-anchor, no motion this frame
            self.prev_xy = xy
            self.prev_count = count
            return

        dx = xy[0] - self.prev_xy[0]
        dy = xy[1] - self.prev_xy[1]
        self.prev_xy = xy
        g.max_travel = max(g.max_travel, math.hypot(xy[0] - g.start_xy[0], xy[1] - g.start_xy[1]))

        if count == 1:
            self._move(dx, dy, dt_ms)
        else:
            self._scroll(g, dx, dy)

    def _move(self, dx: float, dy: float, dt_ms: float) -> None:
        if abs(dx) < MOUSE_DEADZONE and abs(dy) < MOUSE_DEADZONE:
            return
        speed = math.hypot(dx, dy) / max(dt_ms, 1.0)   # touch units per ms
        mult = 1.0 + MOUSE_ACCEL * max(0.0, speed - MOUSE_ACCEL_THRESHOLD)
        mult = min(mult, MOUSE_ACCEL_MAX)
        gain = MOUSE_GAIN * mult
        self.mouse_rem[0] += dx * gain
        self.mouse_rem[1] += -dy * gain     # remote +y is up, screen +y is down
        ix = int(self.mouse_rem[0])
        iy = int(self.mouse_rem[1])
        self.mouse_rem[0] -= ix
        self.mouse_rem[1] -= iy
        self.inj.move(ix, iy)

    def _scroll(self, g: Gesture, dx: float, dy: float) -> None:
        g.scrolled = True
        g.travel_x += abs(dx)
        g.travel_y += abs(dy)
        if SCROLL_AXIS_LOCK and g.axis is None and max(g.travel_x, g.travel_y) >= SCROLL_AXIS_LOCK_UNITS:
            g.axis = "x" if g.travel_x > g.travel_y else "y"
        if g.axis == "x":
            dy = 0.0
        elif g.axis == "y":
            dx = 0.0
        sign = -1.0 if SCROLL_NATURAL else 1.0
        # finger up (dy>0) with natural scrolling => content up => wheel down (negative)
        self.wheel_rem[1] += sign * dy * SCROLL_GAIN
        # finger right (dx>0) with natural scrolling => content right => hwheel negative
        self.wheel_rem[0] += sign * dx * SCROLL_GAIN
        iv = int(self.wheel_rem[1])
        ih = int(self.wheel_rem[0])
        self.wheel_rem[1] -= iv
        self.wheel_rem[0] -= ih
        self.inj.wheel(iv)
        self.inj.hwheel(ih)

    def _lift(self, now_ms: float) -> None:
        g = self.gesture
        if g is None:
            return
        self.gesture = None
        self.prev_xy = None
        self.prev_count = 0
        self.mouse_rem = [0.0, 0.0]
        self.wheel_rem = [0.0, 0.0]
        duration = now_ms - g.start_ms
        if g.clicked or self.pad_button:
            return
        if duration <= TAP_MAX_MS and g.max_travel <= TAP_MAX_TRAVEL and not g.scrolled:
            which = "right" if g.max_fingers >= 2 else "left"
            log(f"  tap ({g.max_fingers} finger, {duration:.0f} ms, travel {g.max_travel:.0f}) -> {which} click")
            self.inj.click(which)

    # ---- buttons ---------------------------------------------------------

    def on_buttons(self, buttons: int, now_ms: float) -> None:
        changed = buttons ^ self.buttons
        if not changed:
            return
        self.buttons = buttons
        pressed = changed & buttons
        released = changed & ~buttons

        if pressed & BTN_PAD_CLICK:
            fingers = self.prev_count
            which = "right" if fingers >= 2 else "left"
            self.pad_button = which
            if self.gesture:
                self.gesture.clicked = True
            log(f"  pad press ({fingers} finger) -> {which} down")
            self.inj.button(which, True)
        if released & BTN_PAD_CLICK and self.pad_button:
            log(f"  pad release -> {self.pad_button} up")
            self.inj.button(self.pad_button, False)
            self.pad_button = None

        if pressed & BTN_PLAY_PAUSE:
            self.inj.key(VK_MEDIA_PLAY_PAUSE, True)
        if released & BTN_PLAY_PAUSE:
            self.inj.key(VK_MEDIA_PLAY_PAUSE, False)

        if pressed & BTN_VOL_UP:
            self._volume(+1, now_ms)
        elif pressed & BTN_VOL_DOWN:
            self._volume(-1, now_ms)
        if released & (BTN_VOL_UP | BTN_VOL_DOWN):
            still = buttons & (BTN_VOL_UP | BTN_VOL_DOWN)
            if not still:
                self.vol_dir = 0
            elif still & BTN_VOL_UP:
                self._volume(+1, now_ms)
            else:
                self._volume(-1, now_ms)

        for bit in (BTN_MENU, BTN_HOME, BTN_SIRI):
            if pressed & bit:
                log(f"  {BUTTON_NAMES[bit]} pressed (unassigned)")

    def _volume(self, direction: int, now_ms: float) -> None:
        self.vol_dir = direction
        self.inj.wheel(direction * VOLUME_WHEEL_NOTCHES * WHEEL_DELTA)
        self.vol_next_ms = now_ms + VOLUME_REPEAT_DELAY_MS

    # ---- timer -----------------------------------------------------------

    def tick(self, now_ms: float) -> None:
        if self.gesture and now_ms - self.last_touch_ms > TOUCH_TIMEOUT_MS:
            log(f"  touch timeout ({now_ms - self.last_touch_ms:.0f} ms) -> lifted")
            self._lift(self.last_touch_ms)
        if self.vol_dir and now_ms >= self.vol_next_ms:
            self.inj.wheel(self.vol_dir * VOLUME_WHEEL_NOTCHES * WHEEL_DELTA)
            self.vol_next_ms = now_ms + VOLUME_REPEAT_INTERVAL_MS

    def reset(self) -> None:
        """Connection dropped: release anything held."""
        now = time.monotonic() * 1000
        if self.pad_button:
            self.inj.button(self.pad_button, False)
            self.pad_button = None
        if self.buttons & BTN_PLAY_PAUSE:
            self.inj.key(VK_MEDIA_PLAY_PAUSE, False)
        self.buttons = 0
        self.vol_dir = 0
        self.gesture = None
        self.prev_xy = None
        self.prev_count = 0
        self.last_touch_ms = now


# =============================================================================
# Bluetooth side (Bumble)
# =============================================================================

from bumble import hci  # noqa: E402
from bumble.core import AdvertisingData, PhysicalTransport  # noqa: E402
from bumble.device import ConnectionParametersPreferences, Device, DeviceConfiguration, Peer  # noqa: E402
from bumble.gatt import (  # noqa: E402
    GATT_HID_CONTROL_POINT_CHARACTERISTIC,
    GATT_HID_INFORMATION_CHARACTERISTIC,
    GATT_HUMAN_INTERFACE_DEVICE_SERVICE,
    GATT_PROTOCOL_MODE_CHARACTERISTIC,
    GATT_REPORT_CHARACTERISTIC,
    GATT_REPORT_MAP_CHARACTERISTIC,
    GATT_REPORT_REFERENCE_DESCRIPTOR,
)
from bumble.keys import JsonKeyStore  # noqa: E402
from bumble.pairing import PairingConfig, PairingDelegate  # noqa: E402
from bumble.transport import open_transport  # noqa: E402
from bumble.transport.common import TransportLostError  # noqa: E402


class RestartTransport(Exception):
    """Raised when the USB transport is gone; the caller reopens the dongle."""

REPORT_TYPE_NAMES = {1: "input", 2: "output", 3: "feature"}


RAW_EVENT_LOG = False  # set by --debug: print the raw bytes of the first event transfers


class FixupEventSplitter:
    """Works around a BARROT firmware quirk: the Command Complete reply to
    Read Local Extended Features (opcode 0x1004) is one byte longer than its
    length field says (a trailing pad byte). Bumble's stream splitter would take
    that byte as the start of the next event and misframe everything after it.
    This splitter drops that pad byte, and as a backstop skips 0x00 bytes wherever
    an event should begin (HCI event code 0x00 does not exist)."""

    PADDED_OPCODES = (0x1004,)

    def __init__(self, inner) -> None:
        self.emit = inner.emit
        self.buf = b""
        self.feeds = 0
        self.fixups = 0

    def feed(self, data: bytes) -> None:
        self.feeds += 1
        if RAW_EVENT_LOG and (self.feeds <= 30 or self.buf):
            log(f"HCI evt raw #{self.feeds} len={len(data)} pending={self.buf.hex()} data={data.hex()}")
        # Padded Command Complete reply delivered as its own transfer: drop the pad byte.
        if (
            not self.buf
            and len(data) >= 6
            and data[0] == 0x0E
            and int.from_bytes(data[3:5], "little") in self.PADDED_OPCODES
            and len(data) == 2 + data[1] + 1
        ):
            data = data[:-1]
            self.fixups += 1
            if self.fixups <= 5:
                log(f"HCI event frame fix-up #{self.fixups}: dropped pad byte of opcode 0x{int.from_bytes(data[3:5], 'little'):04X} reply")
        self.buf += data
        while True:
            z = 0
            while z < len(self.buf) and self.buf[z] == 0x00:
                z += 1
            if z:
                self.buf = self.buf[z:]
                self.fixups += 1
                if self.fixups <= 5:
                    log(f"HCI event frame fix-up #{self.fixups}: skipped {z} zero byte(s) at event boundary")
            if len(self.buf) < 2:
                return
            n = 2 + self.buf[1]
            if len(self.buf) < n:
                return
            packet, self.buf = self.buf[:n], self.buf[n:]
            self.emit(packet)


def install_event_fixup(transport) -> FixupEventSplitter | None:
    splitters = getattr(transport.source, "splitters", None)
    if not splitters or hci.HCI_EVENT_PACKET not in splitters:
        return None
    wrapper = FixupEventSplitter(splitters[hci.HCI_EVENT_PACKET])
    splitters[hci.HCI_EVENT_PACKET] = wrapper
    return wrapper


def addr_key(address) -> str:
    return str(address).split("/")[0].upper()


class Bridge:
    def __init__(self, args, engine: GestureEngine) -> None:
        self.args = args
        self.engine = engine
        self.device: Device | None = None
        self.connection = None
        self.disconnected = asyncio.Event()
        self.transport_lost = asyncio.Event()
        self.report_refs: dict[int, tuple[int, int]] = {}  # char handle -> (id, type)
        self.n_notifications = 0
        self.last_clock: int | None = None
        self.touch_dts: list[float] = []

    # ---- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        log(f"opening transport {self.args.usb}")
        try:
            transport = await open_transport(self.args.usb)
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            log(f"cannot open the dongle: {name}: {e}")
            if "NotSupported" in name or "Access" in name:
                log("The dongle is still bound to Windows' Bluetooth driver (BTHUSB) or another program holds it.")
                log("Do the one-time Zadig WinUSB driver swap described in README.md, then run again.")
            elif "ValueError" in name or "No USB device" in str(e):
                log("No USB device with that VID:PID is plugged in (check --usb).")
            raise SystemExit(2)
        async with transport:
            log("transport open")
            if install_event_fixup(transport) is None:
                log("note: could not install the HCI event frame fix-up (transport has no splitters)")
            config = DeviceConfiguration(
                name="siri-remote-bridge",
                le_enabled=True,
                classic_enabled=False,
                le_privacy_enabled=False,         # never use resolvable private addresses
                identity_address_type=hci.Address.PUBLIC_DEVICE_ADDRESS,
                io_capability=PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT,
                keystore=f"JsonKeyStore:{KEYSTORE_FILE}",
            )
            device = self.device = Device.from_config_with_hci(config, transport.source, transport.sink)
            device.keystore = JsonKeyStore(namespace="siri-remote", filename=KEYSTORE_FILE)
            device.pairing_config_factory = lambda connection: PairingConfig(
                sc=True,
                mitm=False,
                bonding=True,
                identity_address_type=PairingConfig.AddressType.PUBLIC,
                delegate=PairingDelegate(
                    io_capability=PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT,
                    local_initiator_key_distribution=PairingDelegate.KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY,
                    local_responder_key_distribution=PairingDelegate.KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY
                    | PairingDelegate.KeyDistribution.DISTRIBUTE_IDENTITY_KEY,
                ),
            )

            orig_on_lost = device.host.on_transport_lost

            def on_transport_lost():
                log("USB transport lost (dongle dropped off the bus)")
                self.transport_lost.set()
                orig_on_lost()

            device.host.on_transport_lost = on_transport_lost

            if self.args.legacy:
                # Force legacy HCI_LE_Create_Connection / legacy scanning (experiment for
                # controllers whose extended commands misbehave).
                orig_supports = device.host.supports_command

                def supports_command(opcode):
                    if opcode == hci.HCI_LE_EXTENDED_CREATE_CONNECTION_COMMAND:
                        return False
                    return orig_supports(opcode)

                device.host.supports_command = supports_command
                log("legacy mode: using HCI_LE_Create_Connection and legacy scanning")

            if self.args.forget:
                await device.keystore.delete_all()
                log("keystore cleared")

            log("powering on (HCI reset)...")
            try:
                await device.power_on()
            except Exception as e:  # noqa: BLE001
                log(f"HCI Reset / power-on FAILED: {e!r}")
                log("The dongle did not come up on plain HCI. If this is a timeout, it probably")
                log("needs vendor firmware/initialisation that Bumble does not provide for it.")
                raise
            lv = getattr(device.host, "local_version", None)
            if lv is not None:
                log(f"controller: hci_version={lv.hci_version} manufacturer=0x{lv.company_identifier:04X} "
                    f"lmp_subversion=0x{lv.lmp_subversion:04X}")
            log(f"controller public address: {device.public_address}")
            stored = await device.keystore.get_all()
            log(f"stored bonds: {[name for name, _ in stored] or 'none'}")

            try:
                while True:
                    await self.session_once()
            finally:
                await self.shutdown()

    async def shutdown(self) -> None:
        """Leave the controller idle: disconnect and reset, so the next start is clean."""
        device = self.device
        if device is None or self.transport_lost.is_set():
            return
        try:
            if self.connection is not None and not self.disconnected.is_set():
                log("shutdown: disconnecting")
                await asyncio.wait_for(self.connection.disconnect(), 3.0)
        except Exception as e:  # noqa: BLE001
            log(f"shutdown: disconnect failed: {e!r}")
        try:
            if device.scanning:
                await asyncio.wait_for(device.stop_scanning(legacy=self.args.legacy), 3.0)
            await asyncio.wait_for(device.send_sync_command(hci.HCI_Reset_Command()), 3.0)
            log("shutdown: controller reset")
        except Exception as e:  # noqa: BLE001
            log(f"shutdown: reset failed: {e!r}")

    async def until_transport_lost(self, awaitable):
        """Await `awaitable`, but bail out with TransportLostError if the USB link dies."""
        task = asyncio.ensure_future(awaitable)
        lost = asyncio.ensure_future(self.transport_lost.wait())
        try:
            done, _ = await asyncio.wait({task, lost}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            raise TransportLostError("transport lost")
        finally:
            for t in (task, lost):
                if not t.done():
                    t.cancel()

    async def session_once(self) -> None:
        try:
            await self.until_transport_lost(self.session())
        except asyncio.CancelledError:
            if not self.disconnected.is_set():
                raise  # real cancellation (Ctrl+C)
            log("session aborted by disconnection")
        except TransportLostError:
            log("USB transport lost (dongle reset?) -> reopening the dongle")
            raise RestartTransport()
        except Exception as e:  # noqa: BLE001
            log(f"session error: {e!r}")
            if self.args.debug:
                logging.exception("session error")
            if "CommandTimeout" in type(e).__name__:
                log("controller stopped answering -> reopening the dongle")
                raise RestartTransport()
        finally:
            self.engine.reset()
            if self.connection is not None and not self.transport_lost.is_set():
                try:
                    await asyncio.wait_for(self.connection.disconnect(), 3.0)
                except Exception:  # noqa: BLE001
                    pass
            self.connection = None
        log("reconnecting in 1 s")
        await asyncio.sleep(1.0)

    async def session(self) -> None:
        device = self.device
        assert device is not None
        advertisement = await self.scan_for_remote()
        log(f"connecting to {advertisement.address} ...")
        prefs = ConnectionParametersPreferences(
            connection_interval_min=CONN_INTERVAL_MIN_MS,
            connection_interval_max=CONN_INTERVAL_MAX_MS,
            max_latency=CONN_LATENCY,
            supervision_timeout=CONN_SUPERVISION_TIMEOUT_MS,
        )
        connection = await device.connect_le(
            advertisement.address,
            connection_parameters_preferences={hci.HCI_LE_1M_PHY: prefs},
            own_address_type=hci.OwnAddressType.PUBLIC,
            timeout=15.0,
        )
        self.connection = connection
        self.disconnected.clear()
        self.touch_dts = []
        log(f"connected: handle=0x{connection.handle:04X} role={connection.role_name} "
            f"peer={connection.peer_address} {self.params_str(connection)}")

        @connection.on("disconnection")
        def _on_disconnection(reason):
            log(f"DISCONNECTED reason=0x{reason:02X} ({hci.HCI_Constant.error_name(reason)})")
            self.disconnected.set()

        @connection.on("connection_parameters_update")
        def _on_params():
            log(f"connection parameters updated: {self.params_str(connection)}")

        @connection.on("connection_parameters_update_failure")
        def _on_params_failure(error):
            log(f"connection parameters update failed: {error!r}")

        await self.secure(connection)
        await self.setup_hid(connection)
        await self.ensure_fast_interval(connection)

        log("ready: use the remote. Ctrl+C to quit.")
        await self.disconnected.wait()

    @staticmethod
    def params_str(connection) -> str:
        p = getattr(connection, "parameters", None)
        if p is None:
            return "params=?"
        return (f"interval={p.connection_interval:.2f}ms latency={p.peripheral_latency} "
                f"timeout={p.supervision_timeout:.0f}ms")

    async def ensure_fast_interval(self, connection) -> None:
        p = getattr(connection, "parameters", None)
        if p is None or p.connection_interval <= CONN_INTERVAL_MAX_MS:
            return
        log(f"interval is {p.connection_interval:.2f} ms; requesting "
            f"{CONN_INTERVAL_MIN_MS}-{CONN_INTERVAL_MAX_MS} ms via connection parameter update")
        try:
            await asyncio.wait_for(connection.update_parameters(
                CONN_INTERVAL_MIN_MS, CONN_INTERVAL_MAX_MS, CONN_LATENCY, CONN_SUPERVISION_TIMEOUT_MS), 5.0)
            log(f"after update request: {self.params_str(connection)}")
        except Exception as e:  # noqa: BLE001
            log(f"connection parameter update request failed: {e!r}")

    # ---- scanning --------------------------------------------------------

    async def scan_for_remote(self):
        device = self.device
        assert device is not None
        target = addr_key(self.args.remote)
        found: asyncio.Future = asyncio.get_running_loop().create_future()
        last_log = 0.0

        seen_other: dict[str, float] = {}

        def adv_summary(adv):
            name = adv.data.get(AdvertisingData.COMPLETE_LOCAL_NAME) or adv.data.get(
                AdvertisingData.SHORTENED_LOCAL_NAME
            )
            uuids = []
            for t in (
                AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
                AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
            ):
                v = adv.data.get(t)
                if v:
                    uuids.extend(str(u) for u in v)
            hid = any("1812" in u.upper() for u in uuids)
            mfg = adv.data.get(AdvertisingData.MANUFACTURER_SPECIFIC_DATA)
            apple = bool(mfg) and mfg[0] == 0x4C  # company id 0x004C
            return name, uuids, hid, apple

        def on_advertisement(adv):
            nonlocal last_log
            key = addr_key(adv.address)
            name, uuids, hid, apple = adv_summary(adv)
            is_target = key == target
            interesting = is_target or hid or (name and "remote" in str(name).lower())
            if not interesting:
                return
            now = time.monotonic()
            if is_target:
                do_log = now - last_log > 0.5 or adv.is_connectable
                last_log = now if do_log else last_log
            else:
                do_log = now - seen_other.get(key, -10.0) > 5.0
                seen_other[key] = now if do_log else seen_other[key]
            if do_log:
                tag = "ADV REMOTE" if is_target else "ADV other-HID"
                log(f"{tag} {adv.address} rssi={adv.rssi} name={name!r} uuids={uuids} apple={apple} "
                    f"connectable={adv.is_connectable} directed={adv.is_directed} "
                    f"scan_rsp={adv.is_scan_response} data={adv.data_bytes.hex()}")
            accept = is_target or (self.args.any_hid and hid)
            if accept and adv.is_connectable and not found.done():
                found.set_result(adv)

        device.on("advertisement", on_advertisement)
        log(f"scanning for {target} (put the remote in pairing mode: Menu + Vol+ ~5 s, or press any button if bonded)")
        await device.start_scanning(active=True, own_address_type=hci.OwnAddressType.PUBLIC,
                                    legacy=self.args.legacy)
        try:
            return await found
        finally:
            device.remove_listener("advertisement", on_advertisement)
            try:
                await device.stop_scanning(legacy=self.args.legacy)
            except Exception as e:  # noqa: BLE001
                log(f"stop_scanning: {e!r}")

    # ---- security --------------------------------------------------------

    async def secure(self, connection) -> None:
        device = self.device
        assert device is not None
        keys = await device.keystore.get(str(connection.peer_address))
        if keys is not None and (keys.ltk or keys.ltk_central):
            log("have stored keys -> encrypting with existing LTK")
            try:
                await asyncio.wait_for(connection.encrypt(), 10.0)
                log(f"encrypted (key size {connection.encryption_key_size})")
                return
            except Exception as e:  # noqa: BLE001
                code = getattr(e, "error_code", None)
                name = hci.HCI_Constant.error_name(code) if code is not None else type(e).__name__
                if code in (hci.HCI_PIN_OR_KEY_MISSING_ERROR, hci.HCI_AUTHENTICATION_FAILURE_ERROR):
                    log(f"encrypt with stored keys REJECTED by the remote ({name}) -> forgetting keys, re-pairing")
                    await device.keystore.delete(str(connection.peer_address))
                else:
                    log(f"encrypt with stored keys failed ({name}); keeping keys")
                    raise
        log("pairing (bonding, NoInputNoOutput)...")
        await asyncio.wait_for(connection.pair(), 30.0)
        keys = await device.keystore.get(str(connection.peer_address))
        log(f"paired. encrypted={connection.is_encrypted} key size={connection.encryption_key_size} "
            f"keys stored={'yes' if keys else 'NO'} ({KEYSTORE_FILE})")

    # ---- HID service -----------------------------------------------------

    async def setup_hid(self, connection) -> None:
        peer = Peer(connection)
        log("discovering services...")
        services = await peer.discover_services()
        for s in services:
            log(f"  service {s.uuid} handles 0x{s.handle:04X}-0x{s.end_group_handle:04X}")
        hid_services = peer.get_services_by_uuid(GATT_HUMAN_INTERFACE_DEVICE_SERVICE)
        if not hid_services:
            raise RuntimeError("no HID service (0x1812) found")
        hid = hid_services[0]

        chars = await peer.discover_characteristics(service=hid)
        for c in chars:
            await peer.discover_descriptors(characteristic=c)
            descs = ", ".join(f"{d.type}@0x{d.handle:04X}" for d in c.descriptors)
            log(f"  char {c.uuid} handle=0x{c.handle:04X} props={c.properties!s} [{descs}]")

        for c in chars:
            try:
                if c.uuid == GATT_REPORT_MAP_CHARACTERISTIC:
                    v = await peer.read_value(c)
                    log(f"  Report Map ({len(v)} bytes): {v.hex()}")
                elif c.uuid == GATT_HID_INFORMATION_CHARACTERISTIC:
                    v = await peer.read_value(c)
                    log(f"  HID Information: {v.hex()}")
                elif c.uuid == GATT_PROTOCOL_MODE_CHARACTERISTIC:
                    v = await peer.read_value(c)
                    log(f"  Protocol Mode: {v.hex()}")
            except Exception as e:  # noqa: BLE001
                log(f"  read {c.uuid} failed: {e!r}")

        unlock_char = None
        self.report_refs = {}
        for c in chars:
            if c.uuid != GATT_REPORT_CHARACTERISTIC:
                continue
            ref = c.get_descriptor(GATT_REPORT_REFERENCE_DESCRIPTOR)
            rid, rtype = None, None
            if ref is not None:
                try:
                    v = await peer.read_value(ref)
                    if len(v) >= 2:
                        rid, rtype = v[0], v[1]
                except Exception as e:  # noqa: BLE001
                    log(f"  read Report Reference of 0x{c.handle:04X} failed: {e!r}")
            log(f"  Report char 0x{c.handle:04X}: id=0x{rid if rid is not None else 0:02X} "
                f"type={REPORT_TYPE_NAMES.get(rtype, rtype)}")
            if rid is not None:
                self.report_refs[c.handle] = (rid, rtype)
            if rid == 0xF0 and rtype == 3:
                unlock_char = c
            if c.properties & 0x10:  # NOTIFY
                handle = c.handle
                await peer.subscribe(c, lambda value, h=handle: self.on_notification(h, value))
                log(f"    subscribed to notifications on 0x{handle:04X}")

        if unlock_char is None:
            raise RuntimeError("feature report 0xF0 not found; cannot unlock the remote")
        log(f"writing 0xAF to feature report 0xF0 (handle 0x{unlock_char.handle:04X}) with response...")
        try:
            await asyncio.wait_for(peer.write_value(unlock_char, b"\xAF", with_response=True), 10.0)
            log("unlock write OK")
        except Exception as e:  # noqa: BLE001
            log(f"unlock write FAILED: {e!r}")
            raise

    # ---- notifications ---------------------------------------------------

    def on_notification(self, handle: int, value: bytes) -> None:
        self.n_notifications += 1
        now_ms = time.monotonic() * 1000
        rid, rtype = self.report_refs.get(handle, (None, None))
        report = decode_report(value) if (rid in (None, 0xFC) and rtype in (None, 1)) else None
        if not self.args.no_hex or report is None:
            tag = f"0x{handle:04X}" + (f"/id{rid:02X}" if rid is not None else "")
            log(f"NOTIFY {tag} len={len(value):2d} {value.hex()}" + (f"  | {report}" if report else ""))
        if report is None:
            return
        if report.kind in (1, 2):
            dt_ms = 30.0
            if report.clock is not None:
                if self.last_clock is not None:
                    dt_ms = ((report.clock - self.last_clock) & 0xFFFF) / 2.0  # 2000 Hz clock
                    dt_ms = min(max(dt_ms, 5.0), 100.0)
                    self.touch_dts.append(dt_ms)
                    n = len(self.touch_dts)
                    if n in (10, 50) or n % 300 == 0:
                        recent = self.touch_dts[-50:]
                        log(f"touch report interval: mean {sum(recent) / len(recent):.1f} ms over last {len(recent)} "
                            f"(min {min(recent):.1f}, max {max(recent):.1f})")
                self.last_clock = report.clock
            self.engine.on_touch(report.fingers, now_ms, dt_ms)
        else:
            self.last_clock = None
        self.engine.on_buttons(report.buttons, now_ms)


# =============================================================================
# main
# =============================================================================


async def engine_timer(engine: GestureEngine) -> None:
    while True:
        await asyncio.sleep(ENGINE_TICK_MS / 1000)
        engine.tick(time.monotonic() * 1000)


async def amain(args) -> None:
    inj = LogInjector() if args.log_only else Injector()
    engine = GestureEngine(inj)
    bridge = Bridge(args, engine)
    timer = asyncio.create_task(engine_timer(engine))
    try:
        while True:
            try:
                await bridge.run()
                return
            except RestartTransport:
                engine.reset()
                bridge = Bridge(args, engine)
                await asyncio.sleep(2.0)
    finally:
        timer.cancel()
        engine.reset()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log-only", action="store_true", help="decode and print, inject nothing")
    p.add_argument("--no-hex", action="store_true", help="do not print every notification as hex")
    p.add_argument("--forget", action="store_true", help="delete stored bond keys before starting")
    p.add_argument("--usb", default=USB_TRANSPORT, help=f"Bumble transport spec (default {USB_TRANSPORT})")
    p.add_argument("--remote", default=REMOTE_ADDRESS, help=f"remote address (default {REMOTE_ADDRESS})")
    p.add_argument("--any-hid", action="store_true",
                   help="connect to any connectable device advertising the HID service (if the remote uses another address)")
    p.add_argument("--legacy", action="store_true",
                   help="use legacy (non-extended) HCI scan and create-connection commands")
    p.add_argument("--debug", action="store_true", help="enable Bumble debug logging")
    p.add_argument("--logfile", help="also write all output (and Bumble logging) to this file")
    args = p.parse_args()

    global RAW_EVENT_LOG
    RAW_EVENT_LOG = args.debug
    handlers = [logging.StreamHandler()]
    if args.logfile:
        global LOG_FILE
        LOG_FILE = open(args.logfile, "a", encoding="utf-8")
        handlers.append(logging.FileHandler(args.logfile, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", handlers=handlers)
    mode = "LOG-ONLY" if args.log_only else "INJECT"
    log(f"siri remote bumble bridge, mode={mode}, keystore={KEYSTORE_FILE}")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
