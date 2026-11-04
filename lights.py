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
}


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

    def _range(self, role):
        return self.ranges.get(role) or LAYOUTS[self.layout or "v2"][f"{role}_range"]

    @property
    def has_colour(self):
        return "colour" in self.dps

    @property
    def has_temp(self):
        return "temp" in self.dps

    def _device(self):
        d = tinytuya.Device(self.id, self.ip, self.key, version=self.version,
                            connection_timeout=3)
        d.set_socketRetryLimit(1)
        d.set_socketTimeout(3)
        return d

    def status(self):
        info = {"id": self.id, "name": self.name, "ip": self.ip, "online": False,
                "has_colour": self.has_colour, "has_temp": self.has_temp}
        try:
            with self._lock:
                result = self._device().status()
        except Exception as e:  # network errors etc.
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

    def _send(self, values):
        error = None
        for attempt in range(2):          # one retry: Tuya bulbs occasionally drop a packet
            try:
                with self._lock:
                    result = self._device().set_multiple_values(values)
            except Exception as e:
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

    def set_colour(self, hex_colour, brightness=100):
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
            self.set_colour(state["colour"], state.get("brightness") or 100)
        else:
            self.set_white(state.get("brightness"), state.get("temp") if self.has_temp else None)


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
