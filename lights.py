"""
Bulb control layer: reads devices.json (written by `python -m tinytuya wizard`)
and talks to each Tuya bulb directly over the local network.

Supports both common Tuya bulb layouts:
  - "v2" bulbs: DPs 20-24 (switch_led, work_mode, bright_value_v2, temp_value_v2, colour_data_v2)
  - "v1" bulbs: DPs 1-5   (switch_led, work_mode, bright_value, temp_value, colour_data)
Which features a bulb has (color, white temperature) is detected automatically.
"""

import colorsys
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import tinytuya

DEVICES_FILE = Path(__file__).parent / "devices.json"

# Default DP numbers and value ranges per layout, used when devices.json has no mapping
LAYOUTS = {
    "v2": {
        "switch": "20", "mode": "21", "bright": "22", "temp": "23", "colour": "24",
        "bright_range": (10, 1000), "temp_range": (0, 1000),
    },
    "v1": {
        "switch": "1", "mode": "2", "bright": "3", "temp": "4", "colour": "5",
        "bright_range": (25, 255), "temp_range": (0, 255),
    },
}

CODE_TO_ROLE = {
    "switch_led": "switch", "led_switch": "switch",
    "work_mode": "mode",
    "bright_value_v2": "bright", "bright_value": "bright",
    "temp_value_v2": "temp", "temp_value": "temp",
    "colour_data_v2": "colour", "colour_data": "colour",
    "scene_data_v2": "scene",          # built-in effects
    "control_data": "control",         # real-time control, used for hard cuts
}

# Built-in effect transitions (scene_data_v2), confirmed by effects_test.py
EFFECT_TRANSITIONS = {"static": 0, "jump": 1, "gradient": 2}
MAX_EFFECT_UNITS = 8


def hex_to_hsv(hex_colour):
    """'#rrggbb' -> (hue 0-360, saturation 0-1000) at full value."""
    hex_colour = hex_colour.lstrip("#")
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    return int(round(h * 360)) % 360, int(round(s * 1000))


def encode_effect(units, scene_num=1):
    """scene_data_v2: NN + per unit: hold fade transition HHHH SSSS VVVV BBBB TTTT."""
    out = f"{scene_num:02x}"
    for u in units[:MAX_EFFECT_UNITS]:
        out += (f"{u['speed']:02x}{u['speed']:02x}{u['transition']:02x}"
                f"{u['h']:04x}{u['s']:04x}{u['v']:04x}0000{0:04x}")
    return out


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _to_raw(percent, rng):
    lo, hi = rng
    return int(round(lo + (hi - lo) * _clamp(percent, 0, 100) / 100))


def _to_percent(raw, rng):
    lo, hi = rng
    if hi == lo:
        return 0
    return int(round(_clamp((raw - lo) / (hi - lo), 0, 1) * 100))


# ---------- colour encoding ----------

def encode_colour(r, g, b, brightness_pct, layout):
    """RGB (0-255) + brightness (%) -> Tuya colour string."""
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    v = max(brightness_pct / 100, 0.01)
    if layout == "v2":
        # HHHHSSSSVVVV  h:0-360  s,v:0-1000
        return "%04x%04x%04x" % (int(round(h * 360)) % 360, int(round(s * 1000)), int(round(v * 1000)))
    # v1: rrggbb0hhhssvv  h:0-360  s,v:0-255
    rr, gg, bb = (int(round(c * 255)) for c in colorsys.hsv_to_rgb(h, s, v))
    return "%02x%02x%02x%04x%02x%02x" % (
        rr, gg, bb, int(round(h * 360)) % 360, int(round(s * 255)), int(round(v * 255)))


def decode_colour(value, layout):
    """Tuya colour string -> (hex '#rrggbb' at full brightness, brightness %)."""
    try:
        if layout == "v2" and len(value) == 12:
            h = int(value[0:4], 16) / 360
            s = int(value[4:8], 16) / 1000
            v = int(value[8:12], 16) / 1000
        elif len(value) == 14:
            h = int(value[6:10], 16) / 360
            s = int(value[10:12], 16) / 255
            v = int(value[12:14], 16) / 255
        else:
            return "#ffffff", 100
    except ValueError:
        return "#ffffff", 100
    r, g, b = colorsys.hsv_to_rgb(h, s, 1.0)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)), int(round(v * 100))


# ---------- bulb ----------

class Bulb:
    def __init__(self, cfg):
        self.id = cfg["id"]
        self.original_name = cfg.get("name") or self.id
        self.name = self.original_name
        self.last = {}                    # last known state, used for group brightness
        self._lock = threading.Lock()     # bulbs handle one connection at a time
        self._dev = None                  # kept-open connection, see _device()
        self.ip = cfg.get("ip") or "Auto"
        self.key = cfg.get("key", "")
        self.version = float(cfg.get("version") or 3.3)
        self.dps = {}
        self.layout = None
        self.ranges = {}
        self._apply_mapping(cfg.get("mapping") or {})

    def _apply_mapping(self, mapping):
        """Use the DP mapping the wizard downloaded from Tuya, if present."""
        for dp, info in mapping.items():
            role = CODE_TO_ROLE.get((info or {}).get("code", ""))
            if not role or role in self.dps:
                continue
            self.dps[role] = str(dp)
            values = info.get("values") or {}
            if isinstance(values, str):
                try:
                    values = json.loads(values)
                except ValueError:
                    values = {}
            if role in ("bright", "temp") and "min" in values and "max" in values:
                self.ranges[role] = (int(values["min"]), int(values["max"]))
        if "switch" in self.dps:
            codes = {(info or {}).get("code", "") for info in mapping.values()}
            uses_v2 = self.dps["switch"] == "20" or any(c.endswith("_v2") for c in codes)
            self.layout = "v2" if uses_v2 else "v1"

    def _guess_from_status(self, status_dps):
        """No mapping in devices.json: infer the layout from the DPs the bulb reports."""
        layout = "v2" if "20" in status_dps else "v1"
        base = LAYOUTS[layout]
        self.layout = layout
        for role in ("switch", "mode", "bright", "temp", "colour"):
            if base[role] in status_dps:
                self.dps[role] = base[role]
        if layout == "v2" and "colour" in self.dps:
            # not always reported in the status, but present on v2 colour bulbs
            self.dps.setdefault("scene", "25")
            self.dps.setdefault("control", "28")

    def _range(self, role):
        return self.ranges.get(role) or LAYOUTS[self.layout or "v2"][f"{role}_range"]

    @property
    def has_colour(self):
        return "colour" in self.dps

    @property
    def has_temp(self):
        return "temp" in self.dps

    def _device(self):
        """The bulb's connection. It is kept open between commands: opening a new one for every
        command adds a random 0.1-0.5 s delay (measured with latency_cam.py). Call with _lock held."""
        if self._dev is None:
            d = tinytuya.Device(self.id, self.ip, self.key, version=self.version,
                                connection_timeout=3)
            d.set_socketRetryLimit(1)
            d.set_socketTimeout(3)
            d.set_socketPersistent(True)
            self._dev = d
        else:
            self._drain(self._dev)
        return self._dev

    @staticmethod
    def _drain(d):
        """Throw away replies to earlier commands sent without waiting, so they aren't
        mistaken for the answer to the next one."""
        sock = getattr(d, "socket", None)
        if sock is None:
            return
        try:
            sock.setblocking(False)
            while True:
                if not sock.recv(4096):           # the bulb closed the connection
                    d.close()                     # tinytuya opens a new one on the next command
                    return
        except (BlockingIOError, InterruptedError):
            pass                                  # nothing (more) waiting
        except OSError:
            d.close()                             # broken connection: tinytuya reopens it
        finally:
            try:
                if getattr(d, "socket", None) is not None:
                    d.socket.settimeout(3)
            except OSError:
                pass

    def _drop_connection(self):
        """Call with _lock held."""
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    def close(self):
        with self._lock:
            self._drop_connection()

    def status(self):
        info = {"id": self.id, "name": self.name, "ip": self.ip, "online": False,
                "has_colour": self.has_colour, "has_temp": self.has_temp}
        try:
            with self._lock:
                result = self._device().status()
                if not result or "dps" not in result:
                    self._drop_connection()
        except Exception as e:  # network errors etc.
            with self._lock:
                self._drop_connection()
            info["error"] = str(e)
            return info
        if not result or "dps" not in result:
            info["error"] = (result or {}).get("Error", "No response")
            return info

        dps = result["dps"]
        if "switch" not in self.dps:
            self._guess_from_status(dps)
        info.update(online=True, has_colour=self.has_colour, has_temp=self.has_temp)
        info["on"] = bool(dps.get(self.dps["switch"], False))
        mode = dps.get(self.dps.get("mode", ""), "white")
        info["mode"] = "colour" if mode == "colour" else "white"
        info["effect"] = mode in ("scene", "music")     # a built-in effect is running

        if "bright" in self.dps and self.dps["bright"] in dps:
            info["brightness"] = _to_percent(dps[self.dps["bright"]], self._range("bright"))
        if self.has_temp and self.dps["temp"] in dps:
            info["temp"] = _to_percent(dps[self.dps["temp"]], self._range("temp"))
        if self.has_colour and self.dps["colour"] in dps:
            info["colour"], colour_bright = decode_colour(str(dps[self.dps["colour"]]), self.layout)
            if info["mode"] == "colour":
                info["brightness"] = colour_bright
        self.last = {k: info[k] for k in SNAPSHOT_KEYS if k in info}
        return info

    def _send(self, values, wait=True):
        """wait=False: don't wait for the bulb's answer. Needed for hard cuts (control_data):
        the bulb never answers those, so waiting would block for the 3 s timeout."""
        error = None
        for attempt in range(2):          # one retry: Tuya bulbs occasionally drop a packet
            try:
                with self._lock:
                    d = self._device()
                    # After one failed command tinytuya switches the connection to "one value at a
                    # time, 1 s apart" for good - with a kept-open connection a single hiccup would
                    # make every later colour change take 2 s. Always send all values together.
                    d.max_simultaneous_dps = 0
                    result = d.set_multiple_values(values, nowait=not wait)
                    d.max_simultaneous_dps = 0
                    if isinstance(result, dict) and result.get("Error"):
                        self._drop_connection()
            except Exception as e:
                with self._lock:
                    self._drop_connection()
                result = {"Error": str(e)}
            if not (isinstance(result, dict) and result.get("Error")):
                return
            error = result["Error"]
            time.sleep(0.3)
        raise RuntimeError(error)

    def _ensure_known(self):
        if "switch" not in self.dps:
            self.status()
        if "switch" not in self.dps:
            raise RuntimeError("Bulb not reachable")

    def set_power(self, on):
        self._ensure_known()
        self._send({self.dps["switch"]: bool(on)})
        self.last["on"] = bool(on)

    def set_white(self, brightness=None, temp=None):
        self._ensure_known()
        values = {self.dps["switch"]: True}
        if "mode" in self.dps:
            values[self.dps["mode"]] = "white"
        if brightness is not None and "bright" in self.dps:
            values[self.dps["bright"]] = _to_raw(brightness, self._range("bright"))
        if temp is not None and self.has_temp:
            values[self.dps["temp"]] = _to_raw(temp, self._range("temp"))
        self._send(values)
        self.last.update(on=True, mode="white")
        if brightness is not None:
            self.last["brightness"] = brightness
        if temp is not None:
            self.last["temp"] = temp

    def set_colour(self, hex_colour, brightness=100, temp=None):
        """temp (optional) only stores the white tone for later; the bulb stays in colour mode."""
        self._ensure_known()
        if not self.has_colour:
            raise RuntimeError("This bulb has no colour support")
        hex_colour = "#" + hex_colour.lstrip("#").lower()
        r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
        brightness = max(1, brightness)
        values = {self.dps["switch"]: True,
                  self.dps["colour"]: encode_colour(r, g, b, brightness, self.layout)}
        if "mode" in self.dps:
            values[self.dps["mode"]] = "colour"
        if temp is not None and self.has_temp:
            values[self.dps["temp"]] = _to_raw(temp, self._range("temp"))
        self._send(values)
        self.last.update(on=True, mode="colour", colour=hex_colour, brightness=brightness)

    def set_brightness(self, brightness):
        """Change brightness but keep the current mode (white or colour)."""
        if not self.last:
            self.status()
        if self.last.get("mode") == "colour" and self.has_colour:
            self.set_colour(self.last.get("colour") or "#ffffff", brightness)
        else:
            self.set_white(brightness=brightness)

    def apply_state(self, state):
        """Restore a saved snapshot (see snapshot())."""
        if not state.get("on"):
            self.set_power(False)
        elif state.get("mode") == "colour" and self.has_colour and state.get("colour"):
            self.set_colour(state["colour"], state.get("brightness") or 100,
                            temp=state.get("temp") if self.has_temp else None)
        else:
            self.set_white(state.get("brightness"), state.get("temp") if self.has_temp else None)

    # ---------- choreography helpers ----------

    @property
    def has_effects(self):
        return "scene" in self.dps

    def cut_colour(self, hex_colour, brightness=100):
        """Instant colour change without the bulb's own short fade (control_data, direct)."""
        self._ensure_known()
        if "control" not in self.dps or not self.has_colour:
            return self.set_colour(hex_colour, brightness)
        h, s = hex_to_hsv(hex_colour)
        v = int(round(_clamp(brightness, 1, 100) * 10))
        values = {self.dps["switch"]: True,
                  self.dps["control"]: f"0{h:04x}{s:04x}{v:04x}{0:04x}{0:04x}"}
        if self.last.get("mode") != "colour" and "mode" in self.dps:
            values[self.dps["mode"]] = "colour"
        self._send(values, wait=False)
        self.last.update(on=True, mode="colour", colour="#" + hex_colour.lstrip("#").lower(),
                         brightness=brightness)
        self.needs_sync = True      # the bulb still reports its old colour, see sync()

    def sync(self):
        """After hard cuts the bulb reports a stale colour; write the real one once."""
        if getattr(self, "needs_sync", False) and self.last.get("mode") == "colour":
            self.needs_sync = False
            self.set_colour(self.last.get("colour") or "#ffffff", self.last.get("brightness") or 100)

    def set_effect(self, kind, colours, speed=40, brightness=80):
        """Start a built-in effect that the bulb plays by itself.
        kind: 'gradient' (smooth flow), 'jump' or 'breathe'. speed: 1-100, higher = faster."""
        self._ensure_known()
        if not self.has_effects:
            raise RuntimeError("This bulb has no built-in effects")
        self._send(self.effect_values(kind, colours, speed, brightness))
        self.last.update(on=True, mode="effect")

    def effect_values(self, kind, colours, speed=40, brightness=80):
        """The DP values that start a built-in effect (see set_effect)."""
        speed = int(_clamp(speed, 1, 100))
        v = int(round(_clamp(brightness, 1, 100) * 10))
        units = []
        for c in colours or ["#ffffff"]:
            h, s = hex_to_hsv(c)
            if kind == "breathe":
                units.append({"transition": 2, "h": h, "s": s, "v": v, "speed": speed})
                units.append({"transition": 2, "h": h, "s": s, "v": max(10, v // 15), "speed": speed})
            else:
                units.append({"transition": EFFECT_TRANSITIONS.get(kind, 2),
                              "h": h, "s": s, "v": v, "speed": speed})
        values = {self.dps["switch"]: True, self.dps["scene"]: encode_effect(units)}
        if "mode" in self.dps:
            values[self.dps["mode"]] = "scene"
        return values


SNAPSHOT_KEYS = ("on", "mode", "brightness", "temp", "colour")


def snapshot(status):
    """The part of a status dict that a scene stores."""
    return {k: status[k] for k in SNAPSHOT_KEYS if k in status}


def load_bulbs():
    if not DEVICES_FILE.exists():
        raise FileNotFoundError(
            f"{DEVICES_FILE} not found - run 'python -m tinytuya wizard' in this folder first.")
    devices = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
    bulbs = [Bulb(d) for d in devices if d.get("id") and d.get("key")]
    bulbs.sort(key=lambda b: b.name.lower())
    return {b.id: b for b in bulbs}


_pool = ThreadPoolExecutor(max_workers=16)


def run_parallel(func, items):
    """Run func on every item at the same time (bulbs answer slowly one by one)."""
    return list(_pool.map(func, items))
