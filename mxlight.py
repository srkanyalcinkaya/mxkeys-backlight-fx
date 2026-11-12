"""Logitech HID++ 2.0 backlight control (feature 0x1982 Backlight2) over Bluetooth.

Works with MX Keys S (and probably other Logitech keyboards that expose 0x1982).
Only brightness is controllable — the backlight is single-color white.
"""
from __future__ import annotations

import os
import struct
import threading
import time

import hid

LOGI_VID = 0x046D
HIDPP_USAGE_PAGE = 0xFF43
DEV_INDEX = 0xFF          # Bluetooth-direct devices use index 0xFF
SW_ID = (os.getpid() % 15) + 1   # per-process id (1..15): replies meant for another
                                 # program on the same keyboard are ignored, not stolen
LONG = 0x11               # HID++ long report: 20 bytes
FEAT_ROOT = 0x0000
FEAT_BACKLIGHT2 = 0x1982

EFFECT_STATIC = 0x00
AWAKE_UNITS = 720         # 60 min (5 s units) — hands-out/hands-in timeout while effects run
FACTORY_UNITS = 6         # 30 s — factory timeout, used if a stale long value is found
MODE_AUTO = 0x01
MODE_MANUAL = 0x03


class HidppError(Exception):
    pass


def _system_lang() -> str:
    """Two-letter UI language. Override with MXKEYS_LANG=en|tr."""
    v = os.environ.get("MXKEYS_LANG")
    if v:
        return v[:2].lower()
    try:
        import subprocess
        out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                             capture_output=True, text=True, timeout=2).stdout
        for ch in "()\"\n ,":
            out = out.replace(ch, " ")
        first = out.split()[0] if out.split() else "en"
        return first[:2].lower()
    except Exception:  # noqa: BLE001
        return "en"


LANG_TR = _system_lang() == "tr"


def L(tr: str, en: str) -> str:
    """Pick the Turkish or English string for the current system language."""
    return tr if LANG_TR else en


LOG_DIR = os.path.expanduser("~/Library/Logs/mxkeys-backlight-fx")
LOG_PATH = os.path.join(LOG_DIR, "mxlight.log")


def log(msg: str):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 512_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def input_monitoring_access(request: bool = False) -> str:
    """macOS 'Input Monitoring' permission for this process: 'granted', 'denied' or 'unknown'.
    With request=True, asks macOS to show its permission prompt (adds the app to the list)."""
    import ctypes
    import sys
    if sys.platform != "darwin":
        return "granted"
    try:
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDCheckAccess.restype = ctypes.c_uint32
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        state = iokit.IOHIDCheckAccess(1)            # kIOHIDRequestTypeListenEvent
        if state == 2 and request:                   # undetermined → prompt
            iokit.IOHIDRequestAccess.restype = ctypes.c_bool
            iokit.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]
            iokit.IOHIDRequestAccess(1)
            state = iokit.IOHIDCheckAccess(1)
        return {0: "granted", 1: "denied"}.get(state, "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _disable_exclusive_open():
    """hidapi on macOS seizes devices exclusively by default; the OS already owns the
    keyboard, so that open fails. Switch to shared open. hid_init() resets this flag,
    so call it after the first hid.enumerate()."""
    import ctypes
    import sys
    if sys.platform != "darwin":
        return
    try:
        lib = ctypes.CDLL(hid.__file__)
        lib.hid_darwin_set_open_exclusive.argtypes = [ctypes.c_int]
        lib.hid_darwin_set_open_exclusive(0)
    except (OSError, AttributeError):
        pass


class Backlight:
    def __init__(self, pid: int | None = None):
        self.pid = pid
        self.dev: hid.device | None = None
        self.product = ""
        self.idx = None           # feature index of 0x1982
        self.version = 0
        self.levels = 8           # number of brightness levels (refined from device)
        self.original: dict | None = None
        self._lock = threading.Lock()

    # ---------- low level ----------
    def open(self):
        candidates = [d for d in hid.enumerate(LOGI_VID, self.pid or 0)
                      if d["usage_page"] == HIDPP_USAGE_PAGE]
        if not candidates:
            raise HidppError(L("Logitech klavye bulunamadı (bağlı mı?)", "No Logitech keyboard found (is it connected?)"))
        _disable_exclusive_open()
        last_err, asleep = None, False
        for c in candidates:
            dev = hid.device()
            try:
                dev.open_path(c["path"])
            except OSError as e:
                last_err = e
                continue
            self.dev = dev
            idx = 0
            for _attempt in range(2):          # a sleeping keyboard drops the first request
                try:
                    idx, ver = self._get_feature(FEAT_BACKLIGHT2)
                    break
                except HidppError as e:
                    last_err = e
                    asleep = True
            if idx:
                self.idx, self.version = idx, ver
                self.product = c.get("product_string") or hex(c["product_id"])
                break
            dev.close()
            self.dev = None
        if not self.dev:
            if isinstance(last_err, OSError):
                raise HidppError(L(
                    "Klavye açılamadı. Sistem Ayarları → Gizlilik ve Güvenlik → Giriş İzleme "
                    "altında bu uygulamaya izin verip yeniden başlat.",
                    "Could not open the keyboard. Allow this app under System Settings → Privacy & "
                    "Security → Input Monitoring, then restart it."))
            if asleep:
                raise HidppError(L("Klavye yanıt vermiyor (uykuda olabilir) — bir tuşa bas",
                                   "Keyboard is not responding (probably asleep) — press a key"))
            raise HidppError(L("Arka ışık özelliği (0x1982) olan Logitech cihaz bulunamadı",
                               "No Logitech device with the backlight feature (0x1982) found"))
        self.original = self.get_config()
        for k in ("dho", "dhi"):      # a crashed run may have left the long timeout behind
            if self.original[k] > 120:
                self.original[k] = FACTORY_UNITS
        self.levels = self._get_level_count() or self.levels

    def close(self):
        if self.dev:
            try:
                self.dev.close()
            finally:
                self.dev = None

    def _request(self, idx: int, fn: int, params: bytes = b"", timeout: float = 3.0) -> bytes:
        if not self.dev:
            raise HidppError("device not open")
        msg = bytes([LONG, DEV_INDEX, idx, (fn << 4) | SW_ID]) + params
        msg = msg.ljust(20, b"\0")[:20]
        with self._lock:
            self.dev.write(list(msg))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                r = self.dev.read(20, 200)
                if not r or r[0] not in (0x10, 0x11):
                    continue
                if r[2] == 0xFF and r[3] == idx and (r[4] & 0x0F) == SW_ID:
                    raise HidppError(f"HID++ error {r[5]} (feature {idx}, fn {fn})")
                if r[2] == idx and (r[3] & 0x0F) == SW_ID:
                    return bytes(r[4:])
        raise HidppError("no reply from device")

    def _get_feature(self, feature: int) -> tuple[int, int]:
        r = self._request(0x00, 0, struct.pack(">H", feature))
        return r[0], r[2]

    def _get_level_count(self) -> int | None:
        try:
            r = self._request(self.idx, 2)        # getBacklightInfo → number of levels
            if 2 <= r[0] <= 32:
                return r[0]
        except HidppError:
            pass
        return None

    # ---------- config ----------
    def get_config(self) -> dict:
        r = self._request(self.idx, 0)
        enabled, options, supported, effects, level, dho, dhi, dpow = struct.unpack("<BBBHBHHH", r[:12])
        return dict(enabled=enabled, options=options, supported=supported, effects=effects,
                    level=level, dho=dho, dhi=dhi, dpow=dpow, mode=(options >> 3) & 0x03)

    def set_config(self, cfg: dict, mode: int, level: int):
        options = (cfg["options"] & 0x07) | ((mode & 0x03) << 3)
        # Effect byte 0xFF = leave unchanged. The effect is activated with fn 3 (see
        # activate_static): writing it through this byte updates the reported effect but
        # did NOT make levels visible in tests; fn 3 did.
        # Inactivity timeouts are in 5 s units. With the factory 30 s the keyboard switches
        # the light off while an effect is running untouched, so manual mode keeps it awake.
        if mode == MODE_MANUAL:
            dho = dhi = AWAKE_UNITS
        else:
            dho, dhi = cfg["dho"], cfg["dhi"]
        data = struct.pack("<BBBBHHH", 1, options, 0xFF, level if mode == MODE_MANUAL else 0,
                           dho, dhi, cfg["dpow"])
        self._request(self.idx, 1, data)

    # ---------- high level ----------
    def activate_static(self):
        """fn 3 setBacklightEffect(0 = Static). Effect 1 ("None") keeps the LEDs off no matter
        what level is stored. RAM only: must be re-sent after the keyboard power-cycles."""
        self._request(self.idx, 3, bytes([EFFECT_STATIC]))

    def enter_manual(self):
        self.activate_static()
        self.set_config(self.original, MODE_MANUAL, self.original["level"])

    def set_level(self, level: int):
        level = max(0, min(self.levels - 1, int(level)))
        # NOTE: fn 3 is setBacklightEffect (RAM only), not setLevel — see activate_static.
        self.set_config(self.original, MODE_MANUAL, level)

    def restore(self):
        # Always return to automatic mode: `original` may itself be a leftover manual state.
        if self.dev and self.original:
            self.set_config(self.original, MODE_AUTO, 0)
            self.activate_static()      # keep the light usable in normal (auto) operation

    def describe(self) -> str:
        o = self.original or {}
        return (f"{self.product} · Backlight2 v{self.version} · {self.levels} levels · "
                f"mode={o.get('mode')} level={o.get('level')} "
                f"supported=0x{o.get('supported', 0):02x} effects=0x{o.get('effects', 0):04x}")


class BacklightWorker(threading.Thread):
    """Sends levels to the keyboard off the UI thread, always only the newest value."""

    def __init__(self, bl: Backlight, min_interval: float = 0.03):
        super().__init__(daemon=True)
        self.bl = bl
        self.min_interval = min_interval
        self._want: int | None = None
        self._sent: int | None = None
        self._fails = 0
        self._cv = threading.Condition()
        self._stopping = False
        self.on_error = None

    def set(self, level: int):
        with self._cv:
            self._want = level
            self._cv.notify()

    def stop(self):
        with self._cv:
            self._stopping = True
            self._cv.notify()

    def run(self):
        while True:
            with self._cv:
                while not self._stopping and (self._want is None or self._want == self._sent):
                    self._cv.wait()
                if self._stopping:
                    return
                level = self._want
            try:
                self.bl.set_level(level)
                self._sent = level
                self._fails = 0
            except Exception as e:  # noqa: BLE001
                # One late reply is normal on Bluetooth; only report after 3 in a row.
                self._fails += 1
                log(f"ERROR level {level} (attempt {self._fails}): {e}")
                if self._fails >= 3:
                    self._fails = 0
                    self._sent = level
                    if self.on_error:
                        self.on_error(str(e))
                else:
                    time.sleep(0.3)
            time.sleep(self.min_interval)


if __name__ == "__main__":
    # Diagnostic: python mxlight.py  → prints device info and blinks 3 times
    bl = Backlight()
    bl.open()
    print(bl.describe())
    bl.enter_manual()
    for _ in range(3):
        bl.set_level(bl.levels - 1); time.sleep(0.7)
        bl.set_level(1); time.sleep(0.7)       # level 0 takes ~5 s to apply; use dimmest
    bl.restore()
    print("ok — keyboard returned to automatic mode")
