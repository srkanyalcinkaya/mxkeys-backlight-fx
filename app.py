"""MX Keys Işık — backlight effects for Logitech MX Keys S, controlled from the macOS menu bar.

Run:   ./run.sh              (starts in the background; menu bar icon appears)
       ./run.sh --hidden     (no window, menu bar only)
Remote control while running:
       curl -s localhost:8765/blink?n=3
       curl -s localhost:8765/effect?name=breath
       curl -s localhost:8765/show
       curl -s localhost:8765/quit
"""
from __future__ import annotations

import math
import os
import random
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QObject, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFormLayout, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QPushButton,
                               QSlider, QSpinBox, QSystemTrayIcon, QToolButton, QVBoxLayout,
                               QWidget)

from mxlight import Backlight, BacklightWorker, HidppError, L, input_monitoring_access, log

try:
    import psutil
except ImportError:          # CPU effect falls back to load average
    psutil = None

HTTP_PORT = 8765
IDLE_PAUSE = int(os.environ.get("MXKEYS_IDLE_PAUSE", "300"))   # seconds without user input


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input on this Mac (no permission needed)."""
    try:
        import Quartz
        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType)
    except Exception:  # noqa: BLE001
        return 0.0
TICK_MS = 33
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_LABEL = "local.mxkeys-backlight-fx"
AGENT_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist")

# key, icon, title, description, settings shown for this effect
APP_NAME = L("MX Keys Işık", "MX Keys Light")
EFFECTS = [
    ("react",  "⌨️", L("Tuşa basınca", "On keypress"), L("Her tuşta parlar, sonra yavaşça söner", "Lights up on every key, then fades"), ["hold", "fade"]),
    ("breath", "🌬️", L("Nefes", "Breathing"),          L("Sürekli yavaşça parlayıp söner", "Slowly fades in and out"),                  ["period"]),
    ("speed",  "⚡", L("Yazma hızı", "Typing speed"),  L("Ne kadar hızlı yazarsan o kadar parlak", "The faster you type, the brighter"), ["kps"]),
    ("heart",  "❤️", L("Kalp atışı", "Heartbeat"),     L("Çift vuruşlu nabız", "Double-beat pulse"),                                    ["bpm"]),
    ("strobe", "✨", L("Flaş", "Strobe"),              L("Hızlı sönük ↔ parlak", "Fast dim ↔ bright"),                                  ["hz"]),
    ("candle", "🕯️", L("Mum alevi", "Candle"),         L("Rastgele titreyen ışık", "Random flicker"),                                   []),
    ("saw",    "🌊", L("Dalga", "Wave"),               L("Yavaşça parlar, birden söner", "Ramps up slowly, drops at once"),             ["period"]),
    ("morse",  "📡", L("Mors kodu", "Morse code"),     L("Yazdığın metni mors alfabesiyle yanıp söndürür", "Blinks your text in Morse code"), ["morse", "unit"]),
    ("cpu",    "🖥️", L("CPU yükü", "CPU load"),        L("İşlemci ne kadar meşgulse o kadar parlak", "The busier the CPU, the brighter"), []),
    ("timer",  "⏳", L("Zamanlayıcı", "Timer"),        L("Süre boyunca söner, bitince yanıp söner", "Fades over the set time, blinks when done"), ["minutes"]),
    ("off",    "🌑", L("Kapalı", "Off"),               L("Arka ışık kapalı (sönmesi ~5 sn sürer)", "Backlight off (takes ~5 s to fade)"), []),
    ("auto",   "🏭", L("Otomatik", "Automatic"),       L("Klavyenin kendi sensör ayarı", "The keyboard's own sensor-driven mode"),      []),
]
EFFECT_KEYS = [e[0] for e in EFFECTS]
NEEDS_KEYS = {"react", "speed"}
DEFAULTS = {"effect": "breath", "max": 100, "min": 0, "hold": 400, "fade": 1200, "period": 4000,
            "kps": 7, "bpm": 70, "hz": 3, "morse": "SOS", "unit": 200, "minutes": 25}

MORSE = {"a": ".-", "b": "-...", "c": "-.-.", "d": "-..", "e": ".", "f": "..-.", "g": "--.",
         "h": "....", "i": "..", "j": ".---", "k": "-.-", "l": ".-..", "m": "--", "n": "-.",
         "o": "---", "p": ".--.", "q": "--.-", "r": ".-.", "s": "...", "t": "-", "u": "..-",
         "v": "...-", "w": ".--", "x": "-..-", "y": "-.--", "z": "--..", "0": "-----",
         "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....",
         "7": "--...", "8": "---..", "9": "----."}
TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosucgiosu")


def morse_timeline(text: str) -> list[tuple[bool, int]]:
    """[(on, units)] — dot 1, dash 3, symbol gap 1, letter gap 3, word gap 7."""
    seq: list[tuple[bool, int]] = []
    for word in text.translate(TR_MAP).lower().split():
        letters = [MORSE[c] for c in word if c in MORSE]
        for li, code in enumerate(letters):
            for si, sym in enumerate(code):
                seq.append((True, 1 if sym == "." else 3))
                if si < len(code) - 1:
                    seq.append((False, 1))
            if li < len(letters) - 1:
                seq.append((False, 3))
        if letters:
            seq.append((False, 7))
    return seq


# ---------------------------------------------------------------- engine
class Engine(QObject):
    """Owns the keyboard, the settings and the effect loop. Independent of any window."""
    changed = Signal()                 # effect or a setting changed
    frame = Signal(float, int)         # brightness 0..1 (or -1 in auto), device level
    status = Signal(str, bool)         # text, connected
    key = Signal()                     # emitted from the key-listener thread
    blink_req = Signal(int)            # emitted from the HTTP thread
    effect_req = Signal(str)
    show_req = Signal()
    quit_req = Signal()
    dev_error = Signal(str)            # emitted from the worker thread
    opened = Signal(object, str)       # emitted from the connect thread

    def __init__(self):
        super().__init__()
        self.store = QSettings("mxkeys-backlight-fx", "mxkeys-backlight-fx")
        self.p = {k: type(v)(self.store.value(k, v)) for k, v in DEFAULTS.items()}
        if self.p["effect"] not in EFFECT_KEYS:
            self.p["effect"] = DEFAULTS["effect"]
        self.bl: Backlight | None = None
        self.worker: BacklightWorker | None = None
        self.connected = False
        self.keys_trusted: bool | None = None
        now = time.monotonic()
        self.t0 = self.effect_t0 = now
        self.last_press = -1e9
        self.presses: deque[float] = deque()
        self.speed_val = 0.0
        self.blink_until, self.blink_period = 0.0, 0.3
        self.candle_val, self.candle_target, self.candle_next = 0.6, 0.6, 0.0
        self.cpu_val, self.cpu_target, self.cpu_next = 0.0, 0.0, 0.0
        self.timer_done = False
        self.morse_seq = morse_timeline(self.p["morse"])
        self.in_auto = False
        self.next_reconnect, self.reconnect_delay = 0.0, 5.0
        self.next_keepalive = now + 30
        self.paused = False                  # Mac idle → effect paused so the keyboard can sleep
        self.next_idle_check = 0.0

        self.key.connect(self.on_key)
        self.blink_req.connect(self.blink)
        self.effect_req.connect(self.set_effect)
        self.dev_error.connect(self.on_dev_error)
        self.opened.connect(self.on_opened)
        self.connecting = False
        self.last_err = ""
        self.access = "unknown"
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

    # ----- lifecycle
    def start(self):
        self.access = input_monitoring_access(request=True)
        log(f"input monitoring: {self.access}")
        self.changed.emit()
        self.connect_device()
        self.start_key_listener()
        self.timer.start(TICK_MS)
        log(f"--- started: effect={self.p['effect']}")

    def connect_device(self):
        """Opens the keyboard on a background thread: a sleeping keyboard can take several
        seconds to answer, and the UI must not freeze meanwhile."""
        if self.connecting:
            return
        self.connecting = True
        if self.worker:
            self.worker.stop()
            self.worker = None
        if self.bl:
            self.bl.close()
            self.bl = None

        def job():
            bl = Backlight()
            try:
                bl.open()
                if self.p["effect"] != "auto":
                    bl.enter_manual()
                self.opened.emit(bl, "")
            except (HidppError, OSError) as e:
                bl.close()
                self.opened.emit(None, str(e))

        threading.Thread(target=job, daemon=True).start()

    def on_opened(self, bl, err: str):
        self.connecting = False
        if bl is None:
            self.connected = False
            if err != self.last_err:
                log(f"connect failed: {err} (input monitoring: {self.access})")
                self.last_err = err
            self.status.emit(err, False)
            self.next_reconnect = time.monotonic() + self.reconnect_delay
            self.reconnect_delay = min(30.0, self.reconnect_delay * 1.5)
            return
        self.bl, self.connected, self.in_auto = bl, True, self.p["effect"] == "auto"
        self.worker = BacklightWorker(bl)
        self.worker.on_error = self.dev_error.emit
        self.worker.start()
        self.reconnect_delay = 5.0
        self.last_err = ""
        if self.access != "granted":
            self.access = "granted"
            self.changed.emit()
        self.status.emit(L(f"{bl.product} bağlı", f"{bl.product} connected"), True)
        log(f"keyboard connected: {bl.product}")

    def on_dev_error(self, msg: str):
        if self.connected:
            log(f"connection lost: {msg}")
        self.connected = False
        self.status.emit(L("Klavye bağlantısı koptu — yeniden deneniyor…", "Keyboard disconnected — retrying…"), False)
        self.next_reconnect = time.monotonic() + 3

    def start_key_listener(self):
        try:
            from pynput import keyboard
            self.listener = keyboard.Listener(on_press=lambda _k: self.key.emit())
            self.listener.daemon = True
            self.listener.start()
            QTimer.singleShot(1500, self.check_trust)
        except Exception as e:  # noqa: BLE001
            self.listener = None
            log(f"key listener failed to start: {e}")

    def check_trust(self):
        self.keys_trusted = bool(getattr(self.listener, "IS_TRUSTED", True))
        self.changed.emit()

    def shutdown(self):
        self.timer.stop()
        if self.worker:
            self.worker.stop()
            self.worker.join(1)
        if self.bl and self.connected:
            try:
                self.bl.restore()
            except (HidppError, OSError):
                pass
        if self.bl:
            self.bl.close()
        log("--- stopped")

    # ----- settings
    def set(self, key: str, value):
        if self.p.get(key) == value:
            return
        self.p[key] = value
        self.store.setValue(key, value)
        if key == "morse":
            self.morse_seq = morse_timeline(value)
        if key in ("morse", "minutes"):
            self.restart_effect()
        self.changed.emit()

    def set_effect(self, key: str):
        if key not in EFFECT_KEYS:
            return
        self.p["effect"] = key
        self.store.setValue("effect", key)
        self.restart_effect()
        log(f"effect: {key}")
        self.changed.emit()

    def restart_effect(self):
        self.effect_t0 = time.monotonic()
        self.timer_done = False

    def on_key(self):
        now = time.monotonic()
        self.last_press = now
        self.presses.append(now)

    def blink(self, n: int = 3):
        self.blink_until = time.monotonic() + max(1, min(n, 20)) * self.blink_period

    # ----- effect maths: returns 0..1, or -1 for "leave the keyboard alone"
    def compute(self, now: float) -> float:
        p = self.p
        lo, hi = p["min"] / 100, p["max"] / 100
        if now < self.blink_until:
            return hi if ((self.blink_until - now) / self.blink_period) % 1.0 > 0.5 else 0.0
        e = p["effect"]
        if e == "react":
            dt = (now - self.last_press) * 1000 - p["hold"]
            v = 1.0 if dt <= 0 else max(0.0, 1.0 - dt / p["fade"]) ** 2
        elif e == "breath":
            v = (1 - math.cos(2 * math.pi * (now - self.t0) / (p["period"] / 1000))) / 2
        elif e == "speed":
            while self.presses and now - self.presses[0] > 2.0:
                self.presses.popleft()
            target = min(1.0, (len(self.presses) / 2.0) / p["kps"])
            self.speed_val += (target - self.speed_val) * (0.25 if target > self.speed_val else 0.04)
            v = self.speed_val
        elif e == "heart":
            ph = ((now - self.t0) % (60.0 / p["bpm"])) / (60.0 / p["bpm"])
            v = max(math.exp(-((ph - 0.08) / 0.045) ** 2), 0.7 * math.exp(-((ph - 0.30) / 0.05) ** 2))
        elif e == "strobe":
            v = 1.0 if ((now - self.t0) * p["hz"]) % 1.0 < 0.5 else 0.0
        elif e == "candle":
            if now >= self.candle_next:
                self.candle_target = (random.uniform(0.05, 0.3) if random.random() < 0.12
                                      else random.uniform(0.45, 1.0))
                self.candle_next = now + random.uniform(0.07, 0.3)
            self.candle_val += (self.candle_target - self.candle_val) * 0.3
            v = self.candle_val
        elif e == "saw":
            v = ((now - self.t0) % (p["period"] / 1000)) / (p["period"] / 1000)
        elif e == "morse":
            v = 0.0
            if self.morse_seq:
                pos = ((now - self.effect_t0) / (p["unit"] / 1000)) % sum(u for _o, u in self.morse_seq)
                for on, u in self.morse_seq:
                    if pos < u:
                        v = 1.0 if on else 0.0
                        break
                    pos -= u
        elif e == "cpu":
            if now >= self.cpu_next:
                self.cpu_target = (psutil.cpu_percent(interval=None) / 100 if psutil
                                   else min(1.0, os.getloadavg()[0] / (os.cpu_count() or 1)))
                self.cpu_next = now + 0.5
            self.cpu_val += (self.cpu_target - self.cpu_val) * 0.08
            v = self.cpu_val
        elif e == "timer":
            total, el = p["minutes"] * 60, now - self.effect_t0
            if el < total:
                v = 1 - el / total
            else:
                v = 0.0
                if not self.timer_done:
                    self.timer_done = True
                    self.blink(10)
        elif e == "off":
            return 0.0
        else:
            return -1.0
        return lo + (hi - lo) * v

    def timer_left(self) -> float | None:
        if self.p["effect"] != "timer" or self.timer_done:
            return None
        return max(0.0, self.p["minutes"] * 60 - (time.monotonic() - self.effect_t0))

    def tick(self):
        now = time.monotonic()
        if now >= self.next_idle_check:                 # pause while nobody is at the Mac
            self.next_idle_check = now + 1
            idle = idle_seconds()
            if not self.paused and idle >= IDLE_PAUSE:
                self.paused = True
                log("paused: Mac idle")
                if self.connected and not self.in_auto:
                    try:
                        self.bl.restore()
                        self.in_auto = True
                    except (HidppError, OSError):
                        pass
            elif self.paused and idle < 5:
                self.paused = False
                self.reconnect_delay, self.next_reconnect = 5.0, 0.0
                log("resumed")
        if self.paused:
            self.frame.emit(-2.0, 0)
            return
        if not self.connected:
            if now >= self.next_reconnect and not self.connecting:
                self.connect_device()
            self.frame.emit(self.compute(now), 0)
            return
        v = self.compute(now)
        try:
            if v < 0:                                   # automatic: hand control back once
                if not self.in_auto:
                    self.bl.restore()
                    self.in_auto = True
                self.frame.emit(-1.0, 0)
                return
            if self.in_auto:
                self.bl.enter_manual()
                self.in_auto = False
                self.worker._sent = None
        except (HidppError, OSError) as e:
            self.on_dev_error(str(e))
            return
        if now >= self.next_keepalive:                  # effect lives in RAM: survive power cycles
            self.next_keepalive = now + 30
            try:
                self.bl.activate_static()
            except (HidppError, OSError):
                pass                                    # the worker reports real disconnects
        # Level 0 (fully off) takes ~5 s to apply on MX Keys S, so effects use level 1
        # (dimmest) as their floor; only "off" sends a real 0.
        if self.p["effect"] == "off" and now >= self.blink_until:
            level = 0
        else:
            level = 1 + round(v * (self.bl.levels - 2))
        self.worker.set(level)
        self.frame.emit(v, level)


# ---------------------------------------------------------------- remote control / single instance
def start_http(engine: Engine):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u, q = urlparse(self.path), parse_qs(urlparse(self.path).query)
            ok = True
            try:
                if u.path == "/blink":
                    engine.blink_req.emit(int(q.get("n", ["3"])[0]))
                elif u.path == "/effect" and q.get("name", [""])[0] in EFFECT_KEYS:
                    engine.effect_req.emit(q["name"][0])
                elif u.path == "/show":
                    engine.show_req.emit()
                elif u.path == "/quit":
                    engine.quit_req.emit()
                else:
                    ok = False
            except ValueError:
                ok = False
            self.send_response(200 if ok else 404)
            self.end_headers()
            self.wfile.write(b"ok\n" if ok else b"unknown\n")

        def log_message(self, *a):
            pass

    class Server(ThreadingHTTPServer):
        def server_bind(self):
            # HTTPServer.server_bind() calls socket.getfqdn(), a reverse-DNS lookup that can
            # block ~30 s in a freshly installed app bundle. We only serve loopback: skip it.
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "localhost", self.server_address[1]

    try:
        srv = Server(("127.0.0.1", HTTP_PORT), H)
    except OSError:
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def set_dock_visible(visible: bool):
    """Menu-bar-only when the window is hidden (no Dock icon)."""
    try:
        from AppKit import NSApp
        NSApp.setActivationPolicy_(0 if visible else 1)   # Regular / Accessory
        if visible:
            NSApp.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        pass


def autostart_enabled() -> bool:
    return os.path.exists(AGENT_PLIST)


def set_autostart(on: bool):
    uid = os.getuid()
    if on:
        cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
        args = "\n".join(f"    <string>{a}</string>" for a in cmd + ["--hidden"])
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{AGENT_LABEL}</string>
  <key>ProgramArguments</key><array>
{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
"""
        os.makedirs(os.path.dirname(AGENT_PLIST), exist_ok=True)
        with open(AGENT_PLIST, "w") as f:
            f.write(plist)
    else:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{AGENT_LABEL}"], capture_output=True)
        if os.path.exists(AGENT_PLIST):
            os.remove(AGENT_PLIST)


# ---------------------------------------------------------------- widgets
ACCENT = "#ffd27a"
QSS = f"""
* {{ font-family: -apple-system, "SF Pro Text", "Helvetica Neue"; font-size: 13px; color: #e8e9ed; }}
QMainWindow, QWidget#root {{ background: #14161b; }}
QLabel#title {{ font-size: 20px; font-weight: 700; }}
QLabel#muted, QLabel#desc {{ color: #8b90a0; }}
QLabel#pill {{ border-radius: 10px; padding: 3px 10px; font-size: 12px; background: #23262e; color: #8b90a0; }}
QLabel#pill[ok="true"] {{ background: #1d3326; color: #7ee2a0; }}
QLabel#pill[ok="false"] {{ background: #3a2323; color: #ff9c9c; }}
QFrame#warnbox {{ background: #3a3020; border-radius: 10px; }}
QLabel#warn {{ color: #ffd27a; }}
QFrame#card {{ background: #1c1f26; border-radius: 14px; }}
QLabel#section {{ color: #8b90a0; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QToolButton#fx {{ background: #23262e; border: 1px solid #2c303a; border-radius: 12px; padding: 8px 4px;
                 min-width: 118px; min-height: 54px; }}
QToolButton#fx:hover {{ background: #2a2e38; }}
QToolButton#fx:checked {{ background: #3a3222; border: 1px solid {ACCENT}; color: {ACCENT}; }}
QSlider::groove:horizontal {{ height: 4px; background: #2c303a; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; background: #f4f5f7; }}
QLineEdit, QSpinBox {{ background: #23262e; border: 1px solid #2c303a; border-radius: 8px; padding: 5px 8px; }}
QLineEdit:focus, QSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QPushButton {{ background: #23262e; border: 1px solid #2c303a; border-radius: 9px; padding: 7px 14px; }}
QPushButton:hover {{ background: #2a2e38; }}
QPushButton#primary {{ background: {ACCENT}; color: #1a1408; border: none; font-weight: 600; }}
QLabel#val {{ color: #8b90a0; min-width: 64px; }}
QMenu {{ background: #1c1f26; border: 1px solid #2c303a; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 5px 22px; border-radius: 5px; }}
QMenu::item:selected {{ background: #2a2e38; }}
QMenu::item:disabled {{ color: #8b90a0; }}
QMenu::separator {{ height: 1px; background: #2c303a; margin: 4px 8px; }}
"""


class KeyboardPreview(QWidget):
    """Draws a small keyboard whose keys glow with the current brightness."""
    ROWS = [14, 14, 13, 12, 9]

    def __init__(self):
        super().__init__()
        self.v, self.caption = 0.0, ""
        self.setMinimumHeight(200)

    def set(self, v: float, caption: str):
        if abs(v - self.v) > 0.004 or caption != self.caption:
            self.v, self.caption = v, caption
            self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        kw = min(w - 80, 440)
        kh = kw * 0.30
        x0, y0 = (w - kw) / 2, (h - kh) / 2 - 12
        v = max(0.0, min(1.0, self.v))
        if v > 0.02:                                     # glow behind the board
            for i in range(6, 0, -1):
                path = QPainterPath()
                path.addRoundedRect(QRectF(x0 - i * 4, y0 - i * 4, kw + i * 8, kh + i * 8), 14 + i * 3, 14 + i * 3)
                p.fillPath(path, QColor(255, 210, 122, int(8 * v * (7 - i))))
        body = QPainterPath()
        body.addRoundedRect(QRectF(x0, y0, kw, kh), 12, 12)
        p.fillPath(body, QColor("#2a2d36"))
        pad, gap = 10, 4
        rh = (kh - 2 * pad - gap * (len(self.ROWS) - 1)) / len(self.ROWS)
        base, lit = (58, 62, 73), (255, 244, 214)
        col = QColor(*(int(base[i] + (lit[i] - base[i]) * v) for i in range(3)))
        for r, n in enumerate(self.ROWS):
            cw = (kw - 2 * pad - gap * (n - 1)) / n
            for c in range(n):
                key = QPainterPath()
                key.addRoundedRect(QRectF(x0 + pad + c * (cw + gap), y0 + pad + r * (rh + gap), cw, rh), 3, 3)
                p.fillPath(key, col)
        p.setPen(QPen(QColor("#8b90a0")))
        p.drawText(QRectF(0, y0 + kh + 12, w, 20), Qt.AlignCenter, self.caption)


def tray_icon() -> QIcon:
    pm = QPixmap(44, 44)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(Qt.black, 3))
    p.drawRoundedRect(QRectF(4, 14, 36, 22), 5, 5)
    p.setPen(Qt.NoPen)
    p.setBrush(Qt.black)
    for r in range(2):
        for c in range(5):
            p.drawRoundedRect(QRectF(9 + c * 5.6, 19 + r * 5.5, 3.6, 3.4), 1, 1)
    p.drawRoundedRect(QRectF(13, 30, 18, 2.6), 1, 1)
    for dx, dy in ((-11, 2), (0, 0), (11, 2)):            # light rays above the board
        p.drawEllipse(QRectF(22 + dx - 1.7, 5 + dy, 3.4, 3.4))
    p.end()
    pm.setDevicePixelRatio(2)
    icon = QIcon(pm)
    icon.setIsMask(True)                                  # follows light/dark menu bar
    return icon


class Window(QMainWindow):
    def __init__(self, eng: Engine):
        super().__init__()
        self.eng = eng
        self.setWindowTitle(APP_NAME)
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.addWidget(QLabel(APP_NAME, objectName="title"))
        head.addStretch()
        self.pill = QLabel(L("bağlanıyor…", "connecting…"), objectName="pill")
        head.addWidget(self.pill)
        lay.addLayout(head)

        self.preview = KeyboardPreview()
        lay.addWidget(self.preview)

        self.warn_box = QFrame(objectName="warnbox")
        wl = QHBoxLayout(self.warn_box)
        wl.setContentsMargins(12, 10, 10, 10)
        self.warn = QLabel(objectName="warn")
        self.warn.setWordWrap(True)
        wl.addWidget(self.warn, 1)
        self.warn_btn = QPushButton(L("Ayarları aç", "Open Settings"))
        self.warn_btn.clicked.connect(lambda: subprocess.run(["open", self.warn_url]))
        wl.addWidget(self.warn_btn)
        self.warn_url = ""
        self.warn_box.hide()
        lay.addWidget(self.warn_box)

        card, cl = self.card(L("EFEKT", "EFFECT"))
        grid = QGridLayout()
        grid.setSpacing(8)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QToolButton] = {}
        for i, (key, icon, title, desc, _s) in enumerate(EFFECTS):
            b = QToolButton(objectName="fx")
            b.setText(f"{icon}\n{title}")
            b.setCheckable(True)
            b.setToolTip(desc)
            b.clicked.connect(lambda _c=False, k=key: eng.set_effect(k))
            self.group.addButton(b)
            self.buttons[key] = b
            grid.addWidget(b, i // 4, i % 4)
        cl.addLayout(grid)
        self.desc = QLabel(objectName="desc")
        cl.addWidget(self.desc)
        lay.addWidget(card)

        card, cl = self.card(L("AYARLAR", "SETTINGS"))
        self.settings_card = card
        self.form = QFormLayout()
        self.form.setVerticalSpacing(10)
        self.form.setLabelAlignment(Qt.AlignLeft)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.rows: dict[str, int] = {}
        self.sliders: dict[str, QSlider] = {}
        self.add_slider("max", L("En parlak", "Brightest"), 10, 100, "%")
        self.add_slider("min", L("En sönük", "Dimmest"), 0, 90, "%")
        self.add_slider("hold", L("Yanık kalma", "Hold"), 0, 3000, " ms")
        self.add_slider("fade", L("Sönme süresi", "Fade time"), 100, 5000, " ms")
        self.add_slider("period", L("Periyot", "Period"), 1000, 10000, " ms")
        self.add_slider("kps", L("Tam parlaklık", "Full brightness at"), 2, 15, L(" tuş/sn", " keys/s"))
        self.add_slider("bpm", L("Nabız", "Pulse"), 40, 140, " BPM")
        self.add_slider("hz", L("Flaş hızı", "Strobe rate"), 1, 6, " Hz")
        self.e_morse = QLineEdit(eng.p["morse"])
        self.e_morse.textChanged.connect(lambda t: eng.set("morse", t))
        self.rows["morse"] = self.form.rowCount()
        self.form.addRow(L("Mors metni", "Morse text"), self.e_morse)
        self.add_slider("unit", L("Mors birimi", "Morse unit"), 100, 500, " ms")
        self.n_minutes = QSpinBox()
        self.n_minutes.setRange(1, 180)
        self.n_minutes.setSuffix(L(" dk", " min"))
        self.n_minutes.setValue(eng.p["minutes"])
        self.n_minutes.valueChanged.connect(lambda v: eng.set("minutes", v))
        self.rows["minutes"] = self.form.rowCount()
        self.form.addRow(L("Süre", "Duration"), self.n_minutes)
        cl.addLayout(self.form)
        lay.addWidget(card)
        lay.addStretch()

        foot = QHBoxLayout()
        b = QPushButton(L("Yanıp sön", "Blink"), objectName="primary")
        b.clicked.connect(lambda: eng.blink(3))
        foot.addWidget(b)
        foot.addStretch()
        foot.addWidget(QLabel(L("Pencereyi kapatınca menü çubuğunda çalışmaya devam eder", "Keeps running in the menu bar when you close this window"), objectName="muted"))
        lay.addLayout(foot)

        eng.changed.connect(self.sync)
        eng.frame.connect(self.on_frame)
        eng.status.connect(self.on_status)
        eng.status.connect(lambda *_a: self.sync())
        self.sync()

    def card(self, title: str):
        f = QFrame(objectName="card")
        v = QVBoxLayout(f)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)
        v.addWidget(QLabel(title, objectName="section"))
        return f, v

    def add_slider(self, key: str, label: str, lo: int, hi: int, suffix: str):
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(self.eng.p[key])
        val = QLabel(f"{self.eng.p[key]}{suffix}", objectName="val")
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        s.valueChanged.connect(lambda v, k=key, l=val, sf=suffix: (l.setText(f"{v}{sf}"), self.eng.set(k, v)))
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(s)
        h.addWidget(val)
        self.rows[key] = self.form.rowCount()
        self.sliders[key] = s
        self.form.addRow(label, box)

    def sync(self):
        e = self.eng.p["effect"]
        self.buttons[e].setChecked(True)
        spec = next(x for x in EFFECTS if x[0] == e)
        self.desc.setText(spec[3])
        shown = set(spec[4]) | ({"max", "min"} if e not in ("off", "auto") else set())
        for key, row in self.rows.items():
            self.form.setRowVisible(row, key in shown)
        self.settings_card.setVisible(bool(shown))
        for key, s in self.sliders.items():
            if s.value() != self.eng.p[key]:
                s.setValue(self.eng.p[key])
        if not self.eng.connected and self.eng.access == "denied":
            self.warn.setText(L(
                "Klavyeye erişim izni yok. Sistem Ayarları → Gizlilik ve Güvenlik → Giriş İzleme altında "
                "“MX Keys Light”ı aç, sonra uygulamadan çıkıp yeniden başlat.",
                "No access to the keyboard. Turn on “MX Keys Light” under System Settings → Privacy & "
                "Security → Input Monitoring, then quit and reopen the app."))
            self.warn_url = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
            self.warn_box.show()
        elif e in NEEDS_KEYS and self.eng.keys_trusted is False:
            self.warn_url = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
            self.warn.setText(L(
                "Bu efekt tuş basışlarını sayar. Sistem Ayarları → Gizlilik ve Güvenlik → Erişilebilirlik "
                "altında bu uygulamaya (terminalden çalıştırıyorsan terminaline) izin ver, sonra yeniden başlat.",
                "This effect counts key presses. Allow this app (or your terminal, if you run it from one) "
                "under System Settings → Privacy & Security → Accessibility, then restart it."))
            self.warn_box.show()
        else:
            self.warn_box.hide()

    def on_frame(self, v: float, level: int):
        if not self.isVisible():
            return
        if v <= -2:
            self.preview.set(0.0, L("Duraklatıldı — Mac boşta", "Paused — Mac is idle"))
        elif v < 0:
            self.preview.set(0.35, L("Otomatik — klavye kendi ayarında", "Automatic — keyboard manages itself"))
        elif not self.eng.connected:
            self.preview.set(v, L("Önizleme (klavye bağlı değil)", "Preview (keyboard not connected)"))
        else:
            left = self.eng.timer_left()
            extra = f"   ·   {L('kalan', 'left')} {int(left // 60)}:{int(left % 60):02d}" if left is not None else ""
            self.preview.set(level / 7, f"{L('Seviye', 'Level')} {level} / 7{extra}")

    def on_status(self, text: str, ok: bool):
        self.pill.setText(text if len(text) < 44 else text[:42] + "…")
        self.pill.setToolTip(text)
        self.pill.setProperty("ok", "true" if ok else "false")
        self.pill.style().unpolish(self.pill)
        self.pill.style().polish(self.pill)

    def closeEvent(self, ev):                             # keep running in the menu bar
        ev.ignore()
        self.hide()
        set_dock_visible(False)


class Tray(QObject):
    def __init__(self, eng: Engine, win: Window, app: QApplication):
        super().__init__()
        self.eng, self.win, self.app = eng, win, app
        self.icon = QSystemTrayIcon(tray_icon())
        self.icon.setToolTip(APP_NAME)
        menu = QMenu()
        self.head = menu.addAction(APP_NAME)
        self.head.setEnabled(False)
        menu.addSeparator()
        self.group = QActionGroup(menu)
        self.acts: dict[str, QAction] = {}
        for key, icon, title, _d, _s in EFFECTS:
            if key == "off":
                menu.addSeparator()
            a = menu.addAction(f"{icon}  {title}")
            a.setCheckable(True)
            a.triggered.connect(lambda _c=False, k=key: eng.set_effect(k))
            self.group.addAction(a)
            self.acts[key] = a
        menu.addSeparator()
        bm = menu.addMenu(L("Parlaklık", "Brightness"))
        self.bgroup = QActionGroup(bm)
        self.bacts: dict[int, QAction] = {}
        for pct in (25, 50, 75, 100):
            a = bm.addAction(f"%{pct}")
            a.setCheckable(True)
            a.triggered.connect(lambda _c=False, v=pct: eng.set("max", v))
            self.bgroup.addAction(a)
            self.bacts[pct] = a
        menu.addAction(L("Yanıp sön (3 kez)", "Blink (3 times)")).triggered.connect(lambda: eng.blink(3))
        menu.addSeparator()
        menu.addAction(L("Pencereyi aç…", "Open window…")).triggered.connect(self.show_window)
        self.auto = menu.addAction(L("Oturum açılınca başlat", "Start at login"))
        self.auto.setCheckable(True)
        self.auto.setChecked(autostart_enabled())
        self.auto.triggered.connect(set_autostart)
        menu.addSeparator()
        menu.addAction(L("Çıkış", "Quit")).triggered.connect(self.quit)
        self.menu = menu
        self.icon.setContextMenu(menu)
        self.icon.show()
        eng.changed.connect(self.sync)
        eng.status.connect(self.on_status)
        eng.show_req.connect(self.show_window)
        eng.quit_req.connect(self.quit)
        self.sync()

    def on_status(self, text: str, ok: bool):
        self.head.setText(("● " if ok else "○ ") + (text if len(text) < 40 else text[:38] + "…"))

    def sync(self):
        self.acts[self.eng.p["effect"]].setChecked(True)
        for pct, a in self.bacts.items():
            a.setChecked(self.eng.p["max"] == pct)

    def show_window(self):
        set_dock_visible(True)
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()

    def quit(self):
        self.eng.shutdown()
        self.icon.hide()
        self.app.quit()


def main():
    hidden = "--hidden" in sys.argv
    marks = [("start", time.monotonic())]
    app = QApplication(sys.argv)
    marks.append(("qt", time.monotonic()))
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(QSS)
    eng = Engine()
    srv = start_http(eng)
    marks.append(("http", time.monotonic()))
    if not srv:                                           # already running → just bring it forward
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}/show", timeout=2).read()
            print(L("MX Keys Işık zaten çalışıyor — penceresi öne getirildi.", "MX Keys Light is already running — brought its window forward."))
        except OSError:
            print(L(f"Port {HTTP_PORT} başka bir program tarafından kullanılıyor.", f"Port {HTTP_PORT} is in use by another program."))
        return
    win = Window(eng)
    win.resize(600, 760)
    tray = Tray(eng, win, app)
    marks.append(("ui", time.monotonic()))
    signal.signal(signal.SIGTERM, lambda *_a: eng.quit_req.emit())   # restore keyboard on kill
    signal.signal(signal.SIGINT, lambda *_a: eng.quit_req.emit())
    log("startup: " + ", ".join(f"{n} {t - marks[i][1]:.1f}s" for i, (n, t) in enumerate(marks[1:])))
    if hidden:
        set_dock_visible(False)
    else:
        tray.show_window()
    QTimer.singleShot(50, eng.start)          # connect after the window is on screen
    app.aboutToQuit.connect(srv.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
