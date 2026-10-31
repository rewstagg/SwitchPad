#!/usr/bin/env python3
"""SwitchPad - control SwitchBot devices from the Librem 5 (GTK4 + libadwaita).

Uses the official SwitchBot Cloud API v1.1. Credentials (token + secret) are
stored in the GNOME keyring via libsecret, falling back to a 0600 file.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("Secret", "1")
    from gi.repository import Secret  # noqa: E402
except (ValueError, ImportError):
    Secret = None

APP_ID = "io.switchpad.SwitchPad"
APP_NAME = "SwitchPad"
VERSION = "1.0.0"
API = "https://api.switch-bot.com/v1.1"

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "switchpad")
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "switchpad")
CRED_FILE = os.path.join(CONFIG_DIR, "credentials.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
CACHE_FILE = os.path.join(CACHE_DIR, "devices.json")

STATUS_MESSAGES = {
    151: "Device type does not support this command",
    152: "Device not found",
    160: "Command not supported by this device",
    161: "Device is offline",
    171: "Hub is offline",
    190: "Device internal error (check the device or parameters)",
}


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------
class ApiError(Exception):
    pass


class SwitchBotClient:
    def __init__(self, token, secret):
        self.token = token.strip()
        self.secret = secret.strip()

    def _headers(self):
        t = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        msg = (self.token + t + nonce).encode()
        sign = base64.b64encode(
            hmac.new(self.secret.encode(), msg, hashlib.sha256).digest()
        ).decode()
        return {
            "Authorization": self.token,
            "sign": sign,
            "t": t,
            "nonce": nonce,
            "Content-Type": "application/json; charset=utf8",
            "User-Agent": f"{APP_NAME}/{VERSION}",
        }

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method,
                                     headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ApiError("Authentication failed - check token and secret")
            if e.code == 429:
                raise ApiError("Daily API request limit reached")
            raise ApiError(f"Server error (HTTP {e.code})")
        except urllib.error.URLError as e:
            raise ApiError(f"Network error: {e.reason}")
        except (TimeoutError, OSError) as e:
            raise ApiError(f"Network error: {e}")
        except ValueError:
            raise ApiError("Invalid response from server")
        code = payload.get("statusCode")
        if code != 100:
            raise ApiError(STATUS_MESSAGES.get(code) or payload.get("message")
                           or f"API error {code}")
        return payload.get("body")

    def devices(self):
        return self._request("GET", "/devices") or {}

    def status(self, device_id):
        return self._request("GET", f"/devices/{device_id}/status") or {}

    def command(self, device_id, command, parameter="default",
                command_type="command"):
        return self._request("POST", f"/devices/{device_id}/commands", {
            "command": command,
            "parameter": parameter,
            "commandType": command_type,
        })

    def scenes(self):
        return self._request("GET", "/scenes") or []

    def run_scene(self, scene_id):
        return self._request("POST", f"/scenes/{scene_id}/execute")


# --------------------------------------------------------------------------
# Credential storage
# --------------------------------------------------------------------------
SCHEMA = None
if Secret is not None:
    SCHEMA = Secret.Schema.new(APP_ID, Secret.SchemaFlags.NONE,
                               {"account": Secret.SchemaAttributeType.STRING})


def load_creds():
    if SCHEMA is not None:
        try:
            s = Secret.password_lookup_sync(SCHEMA, {"account": "default"}, None)
            if s:
                return json.loads(s)
        except (GLib.Error, ValueError):
            pass
    try:
        with open(CRED_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_creds(token, secret):
    data = json.dumps({"token": token.strip(), "secret": secret.strip()})
    if SCHEMA is not None:
        try:
            Secret.password_store_sync(SCHEMA, {"account": "default"},
                                       Secret.COLLECTION_DEFAULT,
                                       "SwitchPad - SwitchBot API credentials",
                                       data, None)
            if os.path.exists(CRED_FILE):
                os.remove(CRED_FILE)
            return "keyring"
        except GLib.Error:
            pass
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    fd = os.open(CRED_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    return "file"


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_cache(data):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    try:
        os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Bluetooth LE (direct Bot control via BlueZ D-Bus, no extra dependencies)
# Protocol: https://github.com/OpenWonderLabs/SwitchBotAPI-BLE
# --------------------------------------------------------------------------
BLUEZ = "org.bluez"
ADAPTER = "/org/bluez/hci0"
DEVICE_IFACE = "org.bluez.Device1"
CHAR_IFACE = "org.bluez.GattCharacteristic1"
SB_SERVICE_UUIDS = ("0000fd3d-0000-1000-8000-00805f9b34fb",
                    "00000d00-0000-1000-8000-00805f9b34fb")
WOAN_COMPANY_ID = 0x0969
SB_TX = "cba20002-224d-11e6-9fb8-0002a5d5c51b"   # write commands
SB_RX = "cba20003-224d-11e6-9fb8-0002a5d5c51b"   # notify replies
BOT_BLE_COMMANDS = {"press": b"\x57\x01\x00", "turnOn": b"\x57\x01\x01",
                    "turnOff": b"\x57\x01\x02"}
BLE_FRESH_SECS = 120     # a Bot heard this recently counts as "in range"
BLE_STATUS_SECS = 60     # BLE status this fresh beats cloud status


class BleError(Exception):
    pass


def parse_bot_adv(svc):
    """Decode Bot service data -> (status dict, encrypted) or None."""
    if not svc or len(svc) < 3 or (svc[0] & 0x7F) != 0x48:  # 'H' = Bot
        return None
    switch = bool(svc[1] & 0x80)
    st = {"deviceMode": "switchMode" if switch else "pressMode",
          "battery": svc[2] & 0x7F}
    if switch:
        st["power"] = "off" if svc[1] & 0x40 else "on"
    return st, bool(svc[0] & 0x80)


class Ble:
    """Listens for SwitchBot advertisements and sends Bot commands over GATT.

    Scanning callbacks run on the GTK main thread; bot_command() is blocking
    and must run in a worker thread.
    """

    def __init__(self, on_adv):
        self.on_adv = on_adv          # on_adv(mac, status_or_None)
        self.seen = {}                # mac -> {"t", "rssi", "path", "encrypted"}
        self.wanted = False
        self.sub = None
        self.lock = threading.Lock()
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        except GLib.Error:
            self.bus = None

    # ---- scanning (main thread) ----------------------------------------
    def _acall(self, path, iface, method, args=None):
        self.bus.call(BLUEZ, path, iface, method, args, None,
                      Gio.DBusCallFlags.NONE, 10000, None, self._adone)

    @staticmethod
    def _adone(bus, res):
        try:
            bus.call_finish(res)
        except GLib.Error:
            pass  # adapter off, already discovering, etc.

    def start(self):
        if not self.bus:
            return
        self.wanted = True
        if self.sub is None:
            self.sub = self.bus.signal_subscribe(
                BLUEZ, None, None, None, None, Gio.DBusSignalFlags.NONE,
                self._signal)
        self._resume()

    def _resume(self):
        if not self.wanted:
            return False
        self._acall(ADAPTER, "org.bluez.Adapter1", "SetDiscoveryFilter",
                    GLib.Variant("(a{sv})", ({"Transport":
                                              GLib.Variant("s", "le")},)))
        self._acall(ADAPTER, "org.bluez.Adapter1", "StartDiscovery")
        return False

    def stop(self):
        if not self.bus:
            return
        self.wanted = False
        self._acall(ADAPTER, "org.bluez.Adapter1", "StopDiscovery")

    def _signal(self, conn, sender, path, iface, signal, params):
        if signal == "InterfacesAdded":
            obj, ifaces = params.unpack()
            if DEVICE_IFACE in ifaces:
                self._handle(obj, ifaces[DEVICE_IFACE])
        elif signal == "PropertiesChanged":
            i, changed, _ = params.unpack()
            if i == DEVICE_IFACE:
                self._handle(path, changed)
        elif signal == "InterfacesRemoved":
            obj, ifaces = params.unpack()
            if DEVICE_IFACE in ifaces and "/dev_" in obj:
                self.seen.pop(self.mac_of(obj), None)

    @staticmethod
    def mac_of(path):
        return path.rsplit("dev_", 1)[1].replace("_", "").upper()

    def _handle(self, path, props):
        if not path.startswith(ADAPTER + "/dev_") or path.count("/") != 4:
            return
        mac = self.mac_of(path)
        sd = props.get("ServiceData") or {}
        md = props.get("ManufacturerData") or {}
        svc = next((bytes(sd[u]) for u in SB_SERVICE_UUIDS if u in sd), None)
        if svc is None and WOAN_COMPANY_ID not in md and mac not in self.seen:
            return
        if svc is None and WOAN_COMPANY_ID not in md and "RSSI" not in props:
            return  # unrelated property change (e.g. Connected)
        e = self.seen.setdefault(mac, {})
        e["t"] = time.monotonic()
        e["path"] = path
        if "RSSI" in props:
            e["rssi"] = props["RSSI"]
        st = None
        parsed = parse_bot_adv(svc)
        if parsed:
            st, e["encrypted"] = parsed
        self.on_adv(mac, st)

    def in_range(self, mac):
        e = self.seen.get(mac)
        return bool(e and not e.get("encrypted")
                    and time.monotonic() - e.get("t", 0) < BLE_FRESH_SECS)

    def rssi(self, mac):
        return (self.seen.get(mac) or {}).get("rssi")

    def mark_unreachable(self, mac):
        e = self.seen.get(mac)
        if e:
            e["t"] = 0

    # ---- commands (worker thread) --------------------------------------
    def _call(self, path, iface, method, args=None, timeout=10000):
        return self.bus.call_sync(BLUEZ, path, iface, method, args, None,
                                  Gio.DBusCallFlags.NONE, timeout, None)

    def _prop(self, path, iface, name):
        return self._call(path, "org.freedesktop.DBus.Properties", "Get",
                          GLib.Variant("(ss)", (iface, name))).unpack()[0]

    def bot_command(self, mac, command):
        e = self.seen.get(mac)
        if not self.bus or not e or not e.get("path"):
            raise BleError("Bot not in Bluetooth range")
        path = e["path"]
        with self.lock:
            # Many controllers can't connect reliably while scanning.
            try:
                self._call(ADAPTER, "org.bluez.Adapter1", "StopDiscovery")
            except GLib.Error:
                pass
            try:
                self._bot_command(path, BOT_BLE_COMMANDS[command])
            finally:
                try:
                    self._call(path, DEVICE_IFACE, "Disconnect", timeout=5000)
                except GLib.Error:
                    pass
                GLib.idle_add(self._resume)

    def _bot_command(self, path, data):
        last = None
        for _ in range(2):
            try:
                self._call(path, DEVICE_IFACE, "Connect", timeout=10000)
                break
            except GLib.Error as err:
                last = err
                time.sleep(0.5)
        else:
            raise BleError(f"Couldn't connect ({last.message.split(':')[-1].strip()})")
        deadline = time.monotonic() + 8
        while not self._prop(path, DEVICE_IFACE, "ServicesResolved"):
            if time.monotonic() > deadline:
                raise BleError("Timed out reading the Bot's services")
            time.sleep(0.1)
        tx = rx = None
        objs = self._call("/", "org.freedesktop.DBus.ObjectManager",
                          "GetManagedObjects").unpack()[0]
        for p, ifaces in objs.items():
            c = ifaces.get(CHAR_IFACE)
            if c and p.startswith(path + "/"):
                if c["UUID"] == SB_TX:
                    tx = p
                elif c["UUID"] == SB_RX:
                    rx = p
        if not tx:
            raise BleError("Bot has no command channel")

        reply, got = {}, threading.Event()
        sub = None
        if rx:
            def on_notify(conn, sender, p, iface, sig, params):
                _i, changed, _ = params.unpack()
                if "Value" in changed:
                    reply["v"] = bytes(changed["Value"])
                    got.set()
            # Delivered on the main loop, which is running; we just wait.
            sub = self.bus.signal_subscribe(
                BLUEZ, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                rx, CHAR_IFACE, Gio.DBusSignalFlags.NONE, on_notify)
            try:
                self._call(rx, CHAR_IFACE, "StartNotify")
            except GLib.Error:
                pass
        try:
            self._call(tx, CHAR_IFACE, "WriteValue",
                       GLib.Variant("(aya{sv})",
                                    (data, {"type": GLib.Variant("s", "request")})))
            answered = got.wait(5) if rx else False
        except GLib.Error as err:
            raise BleError(f"Write failed ({err.message.split(':')[-1].strip()})")
        finally:
            if sub is not None:
                self.bus.signal_unsubscribe(sub)
        if answered and reply.get("v") and reply["v"][0] not in (1, 5):
            raise BleError(f"Bot replied with error code {reply['v'][0]}")


# --------------------------------------------------------------------------
# Device capability model
# --------------------------------------------------------------------------
def caps_for(dtype):
    t = (dtype or "").lower()
    c = set()
    if t == "bot":
        c |= {"press", "power"}
    if any(k in t for k in ("plug", "bulb", "strip light", "ceiling light",
                            "floor lamp", "humidifier", "fan", "relay switch",
                            "air purifier", "heater", "rgbic", "light")) \
            and "sensor" not in t:
        c.add("power")
    if any(k in t for k in ("bulb", "strip light", "ceiling light",
                            "floor lamp", "rgbic")):
        c.add("brightness")
    if any(k in t for k in ("bulb", "strip light", "floor lamp", "rgbic")):
        c.add("color")
    if any(k in t for k in ("bulb", "ceiling light", "floor lamp")):
        c.add("colortemp")
    if "curtain" in t:
        c |= {"cover", "curtain"}
    elif "roller shade" in t:
        c |= {"cover", "shade"}
    elif "blind" in t:
        c |= {"cover", "blind"}
    if "lock" in t and "keypad" not in t:
        c.add("lock")
    if "vacuum" in t or t.startswith(("k10", "k20", "s10", "s20")):
        c.add("vacuum")
        c.discard("power")
    return c


def icon_for(dtype, remote=False):
    t = (dtype or "").lower()
    if remote:
        if any(k in t for k in ("tv", "box", "projector", "dvd")):
            return "video-display-symbolic"
        if "light" in t:
            return "display-brightness-symbolic"
        return "network-transmit-symbolic"
    for keys, icon in (
        (("vacuum", "k10", "k20", "s10", "s20"), "edit-clear-all-symbolic"),
        (("lock",), "changes-prevent-symbolic"),
        (("curtain", "shade", "blind"), "view-dual-symbolic"),
        (("bulb", "light", "lamp", "rgbic"), "display-brightness-symbolic"),
        (("meter", "wosensor", "wo", "hub 2", "co2"), "temperature-symbolic"),
        (("contact", "motion", "presence", "water leak"), "security-medium-symbolic"),
        (("hub",), "network-wireless-symbolic"),
        (("plug", "relay", "switch"), "system-shutdown-symbolic"),
        (("bot",), "input-touchpad-symbolic"),
    ):
        if any(k in t for k in keys):
            return icon
    return "emblem-system-symbolic"


def vacuum_commands(dtype):
    t = dtype.lower()
    if any(k in t for k in ("s10", "s20", "k10+ pro combo", "k20", "k11")):
        return (("Start", "startClean",
                 {"action": "sweep", "param": {"fanLevel": 1, "times": 1}}),
                ("Pause", "pause", "default"),
                ("Dock", "dock", "default"))
    return (("Start", "start", "default"),
            ("Stop", "stop", "default"),
            ("Dock", "dock", "default"))


def is_on(st):
    return str(st.get("power", "")).lower() == "on"


def bot_switch_mode(st):
    """True if a Bot is in switch mode. Press and custom modes, or an
    unknown mode, use the Press control (press is the factory default)."""
    return str((st or {}).get("deviceMode", "")).lower() == "switchmode"


BOT_MODES = {"pressmode": "Press", "switchmode": "Switch",
             "customizemode": "Custom"}


STATUS_LABELS = {
    "power": "Power", "battery": "Battery", "temperature": "Temperature",
    "humidity": "Humidity", "CO2": "CO₂", "lockState": "Lock",
    "doorState": "Door", "slidePosition": "Position (0 = open)",
    "moving": "Moving", "openState": "Contact", "moveDetected": "Motion",
    "brightness": "Brightness", "color": "Color",
    "colorTemperature": "Color temperature", "voltage": "Voltage",
    "electricCurrent": "Current", "weight": "Power", "electricityOfDay":
    "Usage today (min)", "workingStatus": "Status", "onlineStatus":
    "Connection", "lightLevel": "Light level", "version": "Firmware",
    "calibrate": "Calibrated", "deviceMode": "Mode",
    "nebulizationEfficiency": "Mist level", "auto": "Auto mode",
    "childLock": "Child lock", "sound": "Sound", "lackWater": "Water low",
    "direction": "Direction", "mode": "Mode", "fanSpeed": "Fan speed",
    "nightStatus": "Night mode", "oscillation": "Oscillation",
    "chargingStatus": "Charging", "taskType": "Task", "status": "Status",
}
STATUS_UNITS = {"battery": "%", "temperature": " °C", "humidity": "%",
                "slidePosition": "%", "voltage": " V", "CO2": " ppm",
                "colorTemperature": " K"}
STATUS_SKIP = {"deviceId", "deviceType", "hubDeviceId", "deviceName"}


def fmt_value(key, val):
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, (dict, list)):
        return json.dumps(val)
    s = str(val)
    if key == "deviceMode":
        return BOT_MODES.get(s.lower(), s)
    if key in ("power", "lockState", "doorState", "openState",
               "workingStatus", "onlineStatus"):
        s = s[:1].upper() + s[1:]
    return s + STATUS_UNITS.get(key, "")


def summarize(st, caps):
    p = []
    if "press" in caps:
        if bot_switch_mode(st):
            if "power" in st:
                p.append("On" if is_on(st) else "Off")
        elif "deviceMode" in st:
            p.append(fmt_value("deviceMode", st["deviceMode"]) + " mode")
    elif "power" in st and "power" in caps:
        p.append("On" if is_on(st) else "Off")
    if "lockState" in st:
        p.append(fmt_value("lockState", st["lockState"]))
    if "slidePosition" in st and "cover" in caps:
        p.append(f"{st['slidePosition']}% closed")
    if "temperature" in st:
        p.append(f"{st['temperature']} °C")
    if "humidity" in st:
        p.append(f"{st['humidity']}%")
    if "CO2" in st:
        p.append(f"{st['CO2']} ppm")
    if "openState" in st:
        p.append(fmt_value("openState", st["openState"]))
    if "moveDetected" in st:
        p.append("Motion" if st["moveDetected"] else "No motion")
    if "workingStatus" in st:
        p.append(fmt_value("workingStatus", st["workingStatus"]))
    if "battery" in st:
        p.append(f"Battery {st['battery']}%")
    return " · ".join(p)


# --------------------------------------------------------------------------
# Small widgets
# --------------------------------------------------------------------------
class Debouncer:
    def __init__(self, delay_ms, fn):
        self.delay, self.fn, self.src = delay_ms, fn, 0

    def __call__(self, *args):
        if self.src:
            GLib.source_remove(self.src)
        self.src = GLib.timeout_add(self.delay, self._fire, args)

    def _fire(self, args):
        self.src = 0
        self.fn(*args)
        return False


class PowerSwitch(Gtk.Switch):
    def __init__(self, win, dev_id):
        super().__init__(valign=Gtk.Align.CENTER)
        self.win, self.dev_id = win, dev_id
        self._h = self.connect("notify::active", self._toggled)

    def set_quiet(self, active):
        self.handler_block(self._h)
        self.set_active(active)
        self.handler_unblock(self._h)

    def _toggled(self, *_):
        on = self.get_active()
        self.win.send(self.dev_id, "turnOn" if on else "turnOff",
                      optimistic={"power": "on" if on else "off"},
                      on_error=lambda e: self.set_quiet(not on))


class QuietScale(Gtk.Scale):
    def __init__(self, lo, hi, step, on_change):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL,
                         adjustment=Gtk.Adjustment(lower=lo, upper=hi,
                                                   step_increment=step,
                                                   page_increment=step * 10),
                         hexpand=True, digits=0, draw_value=True,
                         value_pos=Gtk.PositionType.LEFT)
        self.set_size_request(150, -1)
        deb = Debouncer(500, lambda: on_change(int(self.get_value())))
        self._h = self.connect("value-changed", lambda *_: deb())

    def set_quiet(self, v):
        self.handler_block(self._h)
        self.set_value(float(v))
        self.handler_unblock(self._h)


def button(label=None, icon=None, tooltip=None, css=None, cb=None):
    b = Gtk.Button(valign=Gtk.Align.CENTER)
    if icon:
        b.set_icon_name(icon)
    if label:
        b.set_label(label)
    if tooltip:
        b.set_tooltip_text(tooltip)
    for c in (css or []):
        b.add_css_class(c)
    if cb:
        b.connect("clicked", lambda *_: cb())
    return b


def info_row(title, value, with_label=False):
    r = Adw.ActionRow(title=title, use_markup=False)
    # Values here are short (IDs, connection); keep each on one line.
    label = Gtk.Label(label=value, selectable=True, xalign=1,
                      css_classes=["dim-label"])
    r.add_suffix(label)
    return (r, label) if with_label else r


def clear_box(box):
    while (c := box.get_first_child()) is not None:
        box.remove(c)


# --------------------------------------------------------------------------
# List rows
# --------------------------------------------------------------------------
class DeviceRow(Adw.ActionRow):
    def __init__(self, win, dev):
        self.dev = dev
        self.dtype = dev.get("deviceType", "Device")
        self.caps = caps_for(self.dtype)
        self.cloud = dev.get("enableCloudService", True) is not False
        super().__init__(title=dev.get("deviceName", "Unnamed"),
                         subtitle=self.dtype, activatable=True,
                         use_markup=False, subtitle_lines=1)
        self.win = win
        self.add_prefix(Gtk.Image(icon_name=icon_for(self.dtype)))
        self.switch = None
        self.press_btn = None
        self.lock_btn = None
        did = dev["deviceId"]
        if not self.cloud:
            self.set_subtitle(f"{self.dtype} · cloud service off")
        elif "press" in self.caps:
            # Bot: both controls exist; update() shows the one for its mode.
            self.press_btn = button("Press", css=["flat"],
                                    cb=lambda: win.send(did, "press"))
            self.add_suffix(self.press_btn)
            self.switch = PowerSwitch(win, did)
            self.switch.set_visible(False)
            self.add_suffix(self.switch)
        elif "power" in self.caps:
            self.switch = PowerSwitch(win, did)
            self.add_suffix(self.switch)
        elif "lock" in self.caps:
            self.lock_btn = button(icon="changes-prevent-symbolic",
                                   tooltip="Lock / unlock", css=["flat"],
                                   cb=lambda: win.toggle_lock(dev))
            self.add_suffix(self.lock_btn)
        elif "cover" in self.caps:
            box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
            box.append(button(icon="go-up-symbolic", tooltip="Open",
                              cb=lambda: win.cover(dev, "open")))
            box.append(button(icon="go-down-symbolic", tooltip="Close",
                              cb=lambda: win.cover(dev, "close")))
            self.add_suffix(box)
        self.ble_icon = Gtk.Image(icon_name="bluetooth-active-symbolic",
                                  tooltip_text="In Bluetooth range",
                                  css_classes=["dim-label"], visible=False)
        self.add_suffix(self.ble_icon)
        self.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.connect("activated", lambda *_: win.open_device(dev))

    def set_ble(self, in_range):
        self.ble_icon.set_visible(in_range)

    def update(self, st):
        s = summarize(st, self.caps)
        self.set_subtitle(f"{self.dtype} · {s}" if s else self.dtype)
        if self.press_btn:
            sw = bot_switch_mode(st)
            self.press_btn.set_visible(not sw)
            self.switch.set_visible(sw)
        if self.switch and "power" in st:
            self.switch.set_quiet(is_on(st))
        if self.lock_btn and "lockState" in st:
            locked = str(st["lockState"]).lower() == "locked"
            self.lock_btn.set_icon_name("changes-prevent-symbolic" if locked
                                        else "changes-allow-symbolic")

    def show_error(self, msg):
        self.set_subtitle(f"{self.dtype} · {msg}")


class RemoteRow(Adw.ActionRow):
    def __init__(self, win, rem):
        rtype = rem.get("remoteType", "IR remote")
        super().__init__(title=rem.get("deviceName", "Remote"),
                         subtitle=rtype, activatable=True, use_markup=False,
                         subtitle_lines=1)
        self.add_prefix(Gtk.Image(icon_name=icon_for(rtype, remote=True)))
        rid = rem["deviceId"]
        if not rtype.lower().startswith("diy") and rtype != "Others":
            box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
            box.append(button("On", cb=lambda: win.send(rid, "turnOn", ir=True)))
            box.append(button("Off", cb=lambda: win.send(rid, "turnOff", ir=True)))
            self.add_suffix(box)
        self.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.connect("activated", lambda *_: win.open_remote(rem))


# --------------------------------------------------------------------------
# Detail pages
# --------------------------------------------------------------------------
class DetailPage(Gtk.Box):
    def __init__(self, win, title, subtitle, on_refresh=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        hb = Adw.HeaderBar(show_start_title_buttons=False)
        hb.set_title_widget(Adw.WindowTitle(title=title, subtitle=subtitle))
        back = button(icon="go-previous-symbolic", tooltip="Back",
                      cb=win.go_back)
        hb.pack_start(back)
        if on_refresh:
            hb.pack_end(button(icon="view-refresh-symbolic",
                               tooltip="Refresh status", cb=on_refresh))
        self.append(hb)
        scroll = Gtk.ScrolledWindow(vexpand=True,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=600)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                            margin_top=12, margin_bottom=24,
                            margin_start=12, margin_end=12)
        clamp.set_child(self.body)
        scroll.set_child(clamp)
        self.append(scroll)

    def group(self, title=None, description=None):
        g = Adw.PreferencesGroup(title=title or "", description=description)
        self.body.append(g)
        return g


class DevicePage(DetailPage):
    def __init__(self, win, dev):
        self.dev = dev
        self.dev_id = dev["deviceId"]
        self.dtype = dev.get("deviceType", "Device")
        self.caps = caps_for(self.dtype)
        super().__init__(win, dev.get("deviceName", "Device"), self.dtype,
                         on_refresh=lambda: win.fetch_status(self.dev_id))
        self.switch = self.bright = self.ctemp = self.color = self.pos = None
        self.press_row = self.power_row = None
        cloud = dev.get("enableCloudService", True) is not False
        if not cloud:
            self.group(description="Cloud service is disabled for this "
                       "device. Enable it in the SwitchBot app to control it "
                       "from here.")
        else:
            self._build_controls()
        self.status_group = self.group("Status")
        self.status_rows = []
        info = self.group("Device")
        for label, val in (("Device ID", self.dev_id),
                           ("Hub", dev.get("hubDeviceId") or "—")):
            info.add(info_row(label, val))
        self.conn_label = None
        if "press" in self.caps:  # Bots can be reached over Bluetooth
            r, self.conn_label = info_row("Connection", "", with_label=True)
            info.add(r)
            self.set_connection(win.connection_text(self.dev_id))
        st = win.status.get(self.dev_id)
        if st:
            self.update(st)
        else:
            self._placeholder("Loading…")

    def set_connection(self, text):
        if self.conn_label is not None:
            self.conn_label.set_label(text)

    def _placeholder(self, text):
        r = Adw.ActionRow(title=text, use_markup=False)
        self.status_group.add(r)
        self.status_rows.append(r)

    def _build_controls(self):
        win, did, c = self.win, self.dev_id, self.caps
        g = self.group("Controls")
        if "press" in c:
            # Bot: only the row matching its mode is shown (see update()).
            r = Adw.ActionRow(title="Press")
            r.add_suffix(button("Press", css=["suggested-action"],
                                cb=lambda: win.send(did, "press")))
            g.add(r)
            self.press_row = r
        if "power" in c:
            r = Adw.ActionRow(title="Power")
            self.switch = PowerSwitch(win, did)
            r.add_suffix(self.switch)
            r.set_activatable_widget(self.switch)
            g.add(r)
            if "press" in c:
                self.power_row = r
                r.set_visible(False)
        if "brightness" in c:
            r = Adw.ActionRow(title="Brightness")
            self.bright = QuietScale(1, 100, 1, lambda v: win.send(
                did, "setBrightness", str(v), optimistic={"brightness": v}))
            r.add_suffix(self.bright)
            g.add(r)
        if "colortemp" in c:
            r = Adw.ActionRow(title="White temperature", subtitle="Kelvin")
            self.ctemp = QuietScale(2700, 6500, 100, lambda v: win.send(
                did, "setColorTemperature", str(v),
                optimistic={"colorTemperature": v}))
            r.add_suffix(self.ctemp)
            g.add(r)
        if "color" in c:
            r = Adw.ActionRow(title="Color")
            self.color = Gtk.ColorButton(valign=Gtk.Align.CENTER,
                                         use_alpha=False)
            self.color.connect("color-set", self._color_set)
            r.add_suffix(self.color)
            g.add(r)
        if "cover" in c:
            r = Adw.ActionRow(title="Open / close")
            box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
            box.append(button("Open", cb=lambda: win.cover(self.dev, "open")))
            if "curtain" in c:
                box.append(button(icon="media-playback-pause-symbolic",
                                  tooltip="Pause",
                                  cb=lambda: win.send(did, "pause")))
            box.append(button("Close", cb=lambda: win.cover(self.dev, "close")))
            r.add_suffix(box)
            g.add(r)
            if "blind" not in c:
                r = Adw.ActionRow(title="Position",
                                  subtitle="0 = open, 100 = closed")
                self.pos = QuietScale(0, 100, 1, lambda v: win.cover(
                    self.dev, "position", v))
                r.add_suffix(self.pos)
                g.add(r)
        if "lock" in c:
            r = Adw.ActionRow(title="Door lock")
            box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
            box.append(button("Lock", icon=None,
                              cb=lambda: win.send(did, "lock",
                                                  optimistic={"lockState": "locked"})))
            box.append(button("Unlock", css=["destructive-action"],
                              cb=lambda: win.confirm_unlock(self.dev)))
            r.add_suffix(box)
            g.add(r)
        if "vacuum" in c:
            r = Adw.ActionRow(title="Cleaning")
            box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
            for label, cmd, param in vacuum_commands(self.dtype):
                box.append(button(label, cb=lambda cmd=cmd, param=param:
                                  win.send(did, cmd, param)))
            r.add_suffix(box)
            g.add(r)
        if not c:  # sensors / hubs have nothing to control
            self.body.remove(g)

    def _color_set(self, btn):
        rgba = btn.get_rgba()
        val = ":".join(str(round(x * 255)) for x in (rgba.red, rgba.green,
                                                    rgba.blue))
        self.win.send(self.dev_id, "setColor", val, optimistic={"color": val})

    def update(self, st):
        for r in self.status_rows:
            self.status_group.remove(r)
        self.status_rows = []
        for k, v in st.items():
            if k in STATUS_SKIP or v is None or v == "":
                continue
            r = Adw.ActionRow(title=STATUS_LABELS.get(k, k), use_markup=False)
            r.add_suffix(Gtk.Label(label=fmt_value(k, v), selectable=True,
                                   css_classes=["dim-label"], wrap=True))
            self.status_group.add(r)
            self.status_rows.append(r)
        if not self.status_rows:
            self._placeholder("No status reported")
        if self.press_row and self.power_row:
            sw = bot_switch_mode(st)
            self.press_row.set_visible(not sw)
            self.power_row.set_visible(sw)
        if self.switch and "power" in st:
            self.switch.set_quiet(is_on(st))
        if self.bright and isinstance(st.get("brightness"), (int, float)):
            self.bright.set_quiet(st["brightness"])
        if self.ctemp and isinstance(st.get("colorTemperature"), (int, float)) \
                and st["colorTemperature"] >= 2700:
            self.ctemp.set_quiet(st["colorTemperature"])
        if self.pos and isinstance(st.get("slidePosition"), (int, float)):
            self.pos.set_quiet(st["slidePosition"])
        if self.color and isinstance(st.get("color"), str):
            try:
                r, g, b = (int(x) / 255 for x in st["color"].split(":"))
                rgba = Gdk.RGBA()
                rgba.red, rgba.green, rgba.blue, rgba.alpha = r, g, b, 1.0
                self.color.set_rgba(rgba)
            except ValueError:
                pass

    def show_error(self, msg):
        for r in self.status_rows:
            self.status_group.remove(r)
        self.status_rows = []
        self._placeholder(msg)


IR_EXTRAS = {
    "tv": (("Vol +", "volumeAdd"), ("Vol −", "volumeSub"),
           ("Ch +", "channelAdd"), ("Ch −", "channelSub")),
    "light": (("Brighter", "brightnessUp"), ("Dimmer", "brightnessDown")),
    "fan": (("Swing", "swing"), ("Timer", "timer"), ("Low", "lowSpeed"),
            ("Mid", "middleSpeed"), ("High", "highSpeed")),
    "speaker": (("Vol +", "volumeAdd"), ("Vol −", "volumeSub"),
                ("Play", "Play"), ("Pause", "Pause"), ("Next", "Next"),
                ("Prev", "Previous")),
}


class RemotePage(DetailPage):
    def __init__(self, win, rem):
        rtype = rem.get("remoteType", "IR remote")
        super().__init__(win, rem.get("deviceName", "Remote"), rtype)
        rid = rem["deviceId"]
        t = rtype.lower()
        custom_only = t.startswith("diy") or rtype == "Others"
        if not custom_only:
            g = self.group("Controls")
            r = Adw.ActionRow(title="Power")
            box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
            box.append(button("On", cb=lambda: win.send(rid, "turnOn", ir=True)))
            box.append(button("Off", cb=lambda: win.send(rid, "turnOff", ir=True)))
            r.add_suffix(box)
            g.add(r)
            key = next((k for k in ("tv", "box", "iptv", "light", "fan",
                                    "speaker") if k in t), None)
            key = {"box": "tv", "iptv": "tv"}.get(key, key)
            if key:
                flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                   max_children_per_line=4,
                                   column_spacing=6, row_spacing=6,
                                   margin_top=6, homogeneous=True)
                for label, cmd in IR_EXTRAS[key]:
                    flow.append(button(label, cb=lambda cmd=cmd:
                                       win.send(rid, cmd, ir=True)))
                g.add(flow)
            if "air conditioner" in t:
                self._ac_controls(rid)
        g = self.group("Custom button",
                       "Send a button you learned in the SwitchBot app "
                       "(exact button name).")
        e = Adw.EntryRow(title="Button name", show_apply_button=True)
        e.connect("apply", lambda row: row.get_text().strip() and win.send(
            rid, row.get_text().strip(), "default", "customize", ir=True))
        g.add(e)
        info = self.group("Device")
        info.add(info_row("Device ID", rid))

    def _ac_controls(self, rid):
        g = self.group("Air conditioner")
        temp = Adw.ActionRow(title="Temperature", subtitle="°C")
        spin = Gtk.SpinButton.new_with_range(16, 30, 1)
        spin.set_value(24)
        spin.set_valign(Gtk.Align.CENTER)
        temp.add_suffix(spin)
        g.add(temp)
        modes = Gtk.StringList.new(["Auto", "Cool", "Dry", "Fan", "Heat"])
        mode = Adw.ComboRow(title="Mode", model=modes, selected=1)
        g.add(mode)
        fans = Gtk.StringList.new(["Auto", "Low", "Medium", "High"])
        fan = Adw.ComboRow(title="Fan", model=fans)
        g.add(fan)
        r = Adw.ActionRow()
        r.add_suffix(button("Send", css=["suggested-action"], cb=lambda:
                            self.win.send(rid, "setAll",
                                          f"{int(spin.get_value())},"
                                          f"{mode.get_selected() + 1},"
                                          f"{fan.get_selected() + 1},on",
                                          ir=True)))
        g.add(r)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME,
                         default_width=360, default_height=720)
        self.set_icon_name(APP_ID)
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.client = None
        self.devices, self.remotes, self.scenes = [], [], []
        self.status = {}
        self.rows = {}
        self.detail = None
        self.busy = 0
        self.bot_ids = set()
        self.ble_status = {}   # mac -> (monotonic time, status from adverts)
        self.ble_hold = {}     # mac -> ignore advertised power until this time
        self.settings = load_settings()
        self.ble = Ble(self._on_adv)

        self.toasts = Adw.ToastOverlay()
        self.leaflet = Adw.Leaflet(can_unfold=False, can_navigate_back=True)
        self.leaflet.connect("notify::child-transition-running",
                             self._transition_done)
        self.toasts.set_child(self.leaflet)
        self.set_content(self.toasts)
        self._build_list_page()

        for name, cb in (("settings", lambda *_: self.open_settings()),
                         ("about", lambda *_: self.show_about()),
                         ("refresh", lambda *_: self.refresh())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        app.set_accels_for_action("win.refresh", ["F5", "<Ctrl>r"])

        self.connect("close-request", self._on_close)
        # Scan only while the app is in the foreground, to spare the battery.
        self.connect("notify::is-active", lambda *_: self._update_scanning())
        GLib.timeout_add_seconds(15, self._refresh_ble_indicators)

        creds = load_creds()
        if creds and creds.get("token") and creds.get("secret"):
            self.client = SwitchBotClient(creds["token"], creds["secret"])
            cache = load_cache()
            if cache:
                self.devices = cache.get("devices", [])
                self.remotes = cache.get("remotes", [])
                self.scenes = cache.get("scenes", [])
            self.populate()
            GLib.idle_add(lambda: self.refresh() and False)
        else:
            self.populate()

    # ---- async plumbing -------------------------------------------------
    def run_async(self, fn, on_done=None, on_error=None):
        self._busy(1)

        def work():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._fail, on_error, e)
            else:
                GLib.idle_add(self._finish, on_done, res)
        self.pool.submit(work)

    def _finish(self, cb, res):
        self._busy(-1)
        if cb:
            cb(res)
        return False

    def _fail(self, cb, err):
        self._busy(-1)
        if cb:
            cb(err)
        else:
            self.toast(str(err))
        return False

    def _busy(self, d):
        self.busy = max(0, self.busy + d)
        self.spinner.set_visible(self.busy > 0)
        self.spinner.set_spinning(self.busy > 0)

    def toast(self, msg, timeout=3):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg),
                                        timeout=timeout))

    def _on_close(self, *_):
        self.ble.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        return False

    # ---- Bluetooth --------------------------------------------------------
    def ble_enabled(self):
        return self.settings.get("bluetooth", True) and self.ble.bus is not None

    def _update_scanning(self):
        if self.ble_enabled() and self.is_active() and self.bot_ids:
            self.ble.start()
        elif self.ble.wanted:
            self.ble.stop()

    def ble_usable(self, dev_id):
        return self.ble_enabled() and dev_id in self.bot_ids \
            and self.ble.in_range(dev_id)

    def connection_text(self, dev_id):
        if self.ble_usable(dev_id):
            rssi = self.ble.rssi(dev_id)
            return "Bluetooth" + (f" ({rssi} dBm)" if rssi is not None else "")
        if not self.ble_enabled():
            return "Cloud (Bluetooth off in settings)"
        return "Cloud via hub (not in Bluetooth range)"

    def _on_adv(self, mac, st):
        if mac not in self.bot_ids:
            return
        if st:
            cur = self.status.get(mac, {})
            # Bluetooth can't tell custom mode from press mode; keep cloud's.
            if cur.get("deviceMode") == "customizeMode" and \
                    st.get("deviceMode") == "pressMode":
                st = {k: v for k, v in st.items() if k != "deviceMode"}
            # Right after a command, an advert sent just before it may still
            # arrive; don't let it flip the switch back.
            if time.monotonic() < self.ble_hold.get(mac, 0):
                st = {k: v for k, v in st.items() if k != "power"}
            self.ble_status[mac] = (time.monotonic(), st)
            if any(cur.get(k) != v for k, v in st.items()):
                self.apply_status(mac, st, merge=True)
        self._show_ble(mac)

    def _show_ble(self, mac):
        row = self.rows.get(mac)
        if row:
            row.set_ble(self.ble_usable(mac))
        if isinstance(self.detail, DevicePage) and self.detail.dev_id == mac:
            self.detail.set_connection(self.connection_text(mac))

    def _refresh_ble_indicators(self):
        for mac in self.bot_ids:
            self._show_ble(mac)
        return True

    def _with_fresh_ble(self, dev_id, st):
        """Cloud status for Bots can lag; prefer recent Bluetooth readings."""
        t, ble_st = self.ble_status.get(dev_id, (0, None))
        if ble_st and time.monotonic() - t < BLE_STATUS_SECS:
            return {**st, **ble_st}
        return st

    # ---- list page ------------------------------------------------------
    def _build_list_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        hb = Adw.HeaderBar()
        hb.set_title_widget(Adw.WindowTitle(title=APP_NAME))
        hb.pack_start(button(icon="view-refresh-symbolic", tooltip="Refresh",
                             cb=self.refresh))
        self.spinner = Gtk.Spinner(visible=False)
        hb.pack_start(self.spinner)
        menu = Gio.Menu()
        menu.append("Settings", "win.settings")
        menu.append(f"About {APP_NAME}", "win.about")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic",
                                   menu_model=menu, tooltip_text="Menu"))
        box.append(hb)

        self.stack = Gtk.Stack(vexpand=True)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=600)
        self.groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                              spacing=18, margin_top=12, margin_bottom=24,
                              margin_start=12, margin_end=12)
        clamp.set_child(self.groups)
        scroll.set_child(clamp)
        self.stack.add_named(scroll, "list")

        self.empty = Adw.StatusPage(icon_name="network-wireless-symbolic")
        self.empty_btn = Gtk.Button(halign=Gtk.Align.CENTER,
                                    css_classes=["pill", "suggested-action"])
        self.empty_btn.connect("clicked", self._empty_clicked)
        self.empty.set_child(self.empty_btn)
        self.stack.add_named(self.empty, "empty")
        box.append(self.stack)
        self.list_page = box
        self.leaflet.append(box)

    def _empty_clicked(self, *_):
        if self.client:
            self.refresh()
        else:
            self.open_settings()

    def populate(self):
        clear_box(self.groups)
        self.rows = {}
        if not self.client:
            self.empty.set_title("Connect to SwitchBot")
            self.empty.set_description(
                "Enter your SwitchBot API token and secret to control your "
                "devices.")
            self.empty_btn.set_label("Set Up")
            self.stack.set_visible_child_name("empty")
            return
        if not (self.devices or self.remotes or self.scenes):
            self.empty.set_title("No Devices")
            self.empty.set_description(
                "No devices found on this account. Pull them in by tapping "
                "Refresh.")
            self.empty_btn.set_label("Refresh")
            self.stack.set_visible_child_name("empty")
            return
        key = lambda d: (d.get("deviceName") or "").lower()  # noqa: E731
        self.bot_ids = {d["deviceId"] for d in self.devices
                        if "press" in caps_for(d.get("deviceType"))}
        self._update_scanning()
        if self.devices:
            g = Adw.PreferencesGroup(title="Devices")
            for d in sorted(self.devices, key=key):
                row = DeviceRow(self, d)
                if d["deviceId"] in self.status:
                    row.update(self.status[d["deviceId"]])
                if d["deviceId"] in self.bot_ids:
                    row.set_ble(self.ble_usable(d["deviceId"]))
                self.rows[d["deviceId"]] = row
                g.add(row)
            self.groups.append(g)
        if self.remotes:
            g = Adw.PreferencesGroup(title="Infrared Remotes")
            for r in sorted(self.remotes, key=key):
                g.add(RemoteRow(self, r))
            self.groups.append(g)
        if self.scenes:
            g = Adw.PreferencesGroup(title="Scenes")
            for s in sorted(self.scenes,
                            key=lambda s: (s.get("sceneName") or "").lower()):
                row = Adw.ActionRow(title=s.get("sceneName", "Scene"),
                                    use_markup=False)
                row.add_prefix(Gtk.Image(
                    icon_name="media-playback-start-symbolic"))
                sid, name = s["sceneId"], s.get("sceneName", "Scene")
                row.add_suffix(button("Run", css=["flat"],
                                      cb=lambda sid=sid, name=name:
                                      self.run_scene(sid, name)))
                g.add(row)
            self.groups.append(g)
        self.stack.set_visible_child_name("list")

    # ---- data -----------------------------------------------------------
    def refresh(self, *_):
        if not self.client:
            self.open_settings()
            return
        client = self.client

        def work():
            return client.devices(), client.scenes()

        def done(res):
            body, scenes = res
            self.devices = body.get("deviceList") or []
            self.remotes = body.get("infraredRemoteList") or []
            self.scenes = scenes or []
            save_cache({"devices": self.devices, "remotes": self.remotes,
                        "scenes": self.scenes})
            self.populate()
            for d in self.devices:
                if d.get("enableCloudService", True) is not False:
                    self.fetch_status(d["deviceId"])
        self.run_async(work, done)

    def fetch_status(self, dev_id):
        client = self.client

        def err(e):
            row = self.rows.get(dev_id)
            if row:
                row.show_error(str(e))
            if isinstance(self.detail, DevicePage) and \
                    self.detail.dev_id == dev_id:
                self.detail.show_error(str(e))
        self.run_async(lambda: client.status(dev_id),
                       lambda st: self.apply_status(
                           dev_id, self._with_fresh_ble(dev_id, st)), err)

    def apply_status(self, dev_id, st, merge=False):
        if merge:
            st = {**self.status.get(dev_id, {}), **st}
        self.status[dev_id] = st
        row = self.rows.get(dev_id)
        if row:
            row.update(st)
        if isinstance(self.detail, DevicePage) and self.detail.dev_id == dev_id:
            self.detail.update(st)

    def send(self, dev_id, command, parameter="default",
             command_type="command", optimistic=None, on_error=None, ir=False):
        if not self.client:
            self.open_settings()
            return
        client = self.client
        use_ble = (not ir and command in BOT_BLE_COMMANDS
                   and self.ble_usable(dev_id))

        def work():
            if use_ble:
                try:
                    self.ble.bot_command(dev_id, command)
                    return "ble", None
                except BleError as e:
                    self.ble.mark_unreachable(dev_id)
                    reason = str(e)
                client.command(dev_id, command, parameter, command_type)
                return "cloud", reason
            client.command(dev_id, command, parameter, command_type)
            return "cloud", None

        def done(res):
            via, ble_failure = res
            if ble_failure:
                print(f"SwitchPad: Bluetooth command to {dev_id} failed: "
                      f"{ble_failure}", file=sys.stderr)
                self.toast("Bluetooth failed, sent via cloud", timeout=4)
                self._show_ble(dev_id)
            if ir:
                self.toast("Sent", timeout=1)
                return
            if optimistic:
                self.apply_status(dev_id, optimistic, merge=True)
            if via == "ble":
                # The Bot's own adverts will confirm; the cloud may be stale.
                self.ble_hold[dev_id] = time.monotonic() + 3
                if optimistic:
                    t, st = self.ble_status.get(dev_id, (0, {}))
                    self.ble_status[dev_id] = (time.monotonic(),
                                               {**st, **optimistic})
                return
            # Cloud status lags the device a little; re-read shortly after.
            GLib.timeout_add_seconds(3, lambda: self.fetch_status(dev_id)
                                     and False)

        def err(e):
            self.toast(str(e))
            if on_error:
                on_error(e)
        self.run_async(work, done, err)

    def run_scene(self, scene_id, name):
        client = self.client
        self.run_async(lambda: client.run_scene(scene_id),
                       lambda _: self.toast(f"Ran “{name}”"))

    # ---- device helpers -------------------------------------------------
    def cover(self, dev, action, value=None):
        did = dev["deviceId"]
        c = caps_for(dev.get("deviceType", ""))
        if "blind" in c:
            cmd = {"open": ("fullyOpen", "default"),
                   "close": ("closeDown", "default")}[action]
        elif "shade" in c:
            pos = {"open": 0, "close": 100}.get(action, value)
            cmd = ("setPosition", int(pos))
        else:  # curtain
            if action == "open":
                cmd = ("turnOn", "default")
            elif action == "close":
                cmd = ("turnOff", "default")
            else:
                cmd = ("setPosition", f"0,ff,{int(value)}")
        self.send(did, cmd[0], cmd[1])

    def toggle_lock(self, dev):
        st = self.status.get(dev["deviceId"], {})
        if str(st.get("lockState", "")).lower() == "locked":
            self.confirm_unlock(dev)
        else:
            self.send(dev["deviceId"], "lock",
                      optimistic={"lockState": "locked"})

    def confirm_unlock(self, dev):
        d = Adw.MessageDialog(transient_for=self, modal=True,
                              heading=f"Unlock “{dev.get('deviceName')}”?",
                              body="The door will be unlocked.")
        d.add_response("cancel", "Cancel")
        d.add_response("unlock", "Unlock")
        d.set_response_appearance("unlock", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")

        def resp(_d, r):
            if r == "unlock":
                self.send(dev["deviceId"], "unlock",
                          optimistic={"lockState": "unlocked"})
        d.connect("response", resp)
        d.present()

    # ---- navigation -----------------------------------------------------
    def _show_detail(self, page):
        if self.detail is not None:
            self.leaflet.remove(self.detail)
        self.detail = page
        self.leaflet.append(page)
        self.leaflet.set_visible_child(page)

    def open_device(self, dev):
        self._show_detail(DevicePage(self, dev))
        if dev.get("enableCloudService", True) is not False:
            self.fetch_status(dev["deviceId"])

    def open_remote(self, rem):
        self._show_detail(RemotePage(self, rem))

    def go_back(self):
        self.leaflet.navigate(Adw.NavigationDirection.BACK)

    def _transition_done(self, *_):
        if (not self.leaflet.get_child_transition_running()
                and self.leaflet.get_visible_child() is self.list_page
                and self.detail is not None):
            self.leaflet.remove(self.detail)
            self.detail = None

    # ---- dialogs --------------------------------------------------------
    def open_settings(self):
        win = Adw.PreferencesWindow(transient_for=self, modal=True,
                                    search_enabled=False,
                                    title="Settings")
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(
            title="SwitchBot Cloud API",
            description="In the SwitchBot app open Profile → Preferences "
                        "→ About, tap “App Version” 10 times, "
                        "then open Developer Options to copy your token and "
                        "secret.")
        creds = load_creds() or {}
        token = Adw.PasswordEntryRow(title="Token")
        token.set_text(creds.get("token", ""))
        secret = Adw.PasswordEntryRow(title="Secret key")
        secret.set_text(creds.get("secret", ""))
        g.add(token)
        g.add(secret)
        page.add(g)

        g2 = Adw.PreferencesGroup()
        save = Gtk.Button(label="Save and Connect", halign=Gtk.Align.CENTER,
                          css_classes=["pill", "suggested-action"])
        g2.add(save)
        page.add(g2)

        g3 = Adw.PreferencesGroup(
            title="Bluetooth",
            description="Control Bots directly when the phone is near them: "
                        "faster, works without the hub or internet, and "
                        "doesn't count toward the daily API limit. Falls back "
                        "to the cloud when out of range. Scans only while "
                        "SwitchPad is open.")
        bt_row = Adw.ActionRow(title="Use Bluetooth when nearby")
        bt_switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                               active=self.settings.get("bluetooth", True))
        if self.ble.bus is None:
            bt_switch.set_sensitive(False)
            bt_row.set_subtitle("Bluetooth service not available")
        bt_row.add_suffix(bt_switch)
        bt_row.set_activatable_widget(bt_switch)

        def bt_toggled(sw, _pspec):
            self.settings["bluetooth"] = sw.get_active()
            save_settings(self.settings)
            self._update_scanning()
            self._refresh_ble_indicators()
        bt_switch.connect("notify::active", bt_toggled)
        g3.add(bt_row)
        page.add(g3)
        win.add(page)

        def do_save(*_):
            t, s = token.get_text().strip(), secret.get_text().strip()
            if not t or not s:
                win.add_toast(Adw.Toast(title="Token and secret are required"))
                return
            save.set_sensitive(False)
            client = SwitchBotClient(t, s)

            def ok(_):
                where = save_creds(t, s)
                self.client = client
                self.status = {}
                win.close()
                self.toast("Connected" + (" (saved to keyring)"
                                          if where == "keyring" else ""))
                self.refresh()

            def bad(e):
                save.set_sensitive(True)
                win.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(e))))
            self.run_async(client.devices, ok, bad)
        save.connect("clicked", do_save)
        secret.connect("entry-activated", do_save)
        win.present()

    def show_about(self):
        Adw.AboutWindow(transient_for=self, application_name=APP_NAME,
                        application_icon=APP_ID, version=VERSION,
                        comments="Control SwitchBot devices, IR remotes and "
                                 "scenes from your Librem 5.",
                        license_type=Gtk.License.MIT_X11).present()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.props.active_window or MainWindow(self)
        win.present()


def main():
    GLib.set_application_name(APP_NAME)
    return App().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
