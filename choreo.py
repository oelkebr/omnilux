"""
Choreographies: a timeline of steps that the server plays on your bulbs.

A choreography looks like this (stored in settings.json under "choreos"):

{
  "id": "a1b2c3d4", "name": "Sunset", "loop": false, "restore": false,
  "steps": [
    {"action": "colour", "target": {"type": "all"}, "colour": "#ff8a00",
     "brightness": 80, "fade": 30, "cut": false, "stagger": 0, "duration": 35},
    ...
  ]
}

Step fields
  action     colour | white | effect | scene | on | off
  target     {"type": "all"} | {"type": "group", "id": "<group id>"} | {"type": "bulbs", "ids": [...]}
  duration   seconds until the next step starts
  stagger    seconds between one bulb and the next -> wave effect (0 = all together)
  colour     "#rrggbb"                         (colour)
  brightness 1-100                             (colour, white, effect)
  temp       0-100, 0 = warm                   (white)
  fade       seconds to fade into the step     (colour, white; 0 = bulb's own short fade)
  cut        true = instant hard change        (colour)
  effect     gradient | jump | breathe         (effect: built-in effect the bulb plays itself)
  colours    ["#rrggbb", ...] up to 8          (effect)
  speed      1-100, higher = faster            (effect)
  scene      scene id of the target group      (scene)
"""

import colorsys
import json
import math
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lights import run_parallel, snapshot

ACTIONS = ("colour", "white", "effect", "scene", "on", "off")
EFFECTS = ("gradient", "jump", "breathe")
HEX = re.compile(r"^#?[0-9a-fA-F]{6}$")
FADE_INTERVAL = 0.5          # seconds between intermediate values of a long fade
OFFSETS_FILE = Path(__file__).with_name("latency") / "offsets.json"   # made by latency_cam.py


# ---------- per-bulb delays (measured with latency_cam.py) ----------

def load_delays(path=OFFSETS_FILE):
    """{bulb id: {kind: seconds until the light is half-way}}, kinds: power, colour, cut, bright.
    Empty if the bulbs were never measured: then every bulb is sent at the same moment."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {i: {k: v / 1000 for k, v in d.items() if isinstance(v, (int, float))}
                for i, d in data.get("bulbs", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def delay_kind(step, bulb):
    """Which measured delay applies to this step on this bulb."""
    action = step["action"]
    if action in ("on", "off"):
        return "power"
    if action == "white" or (action == "colour" and not bulb.has_colour):
        return "bright"
    if action == "colour" and step.get("cut") and "control" in bulb.dps:
        return "cut"
    if action == "effect":
        return "power"            # not measured on a kept-open connection; on/off is the closest
    return "colour"               # colour, scene


def send_ahead(delays, step, bulbs):
    """{bulb id: seconds to hold this bulb's command back} so that all bulbs change together:
    the slowest bulb is sent first, faster ones a little later. Unmeasured bulbs get the median."""
    lat = {}
    for b in bulbs:
        kind = delay_kind(step, b)
        lat[b.id] = (kind, delays.get(b.id, {}).get(kind))
    known = {}
    for kind, v in lat.values():
        if v is not None:
            known.setdefault(kind, []).append(v)
    if not known:
        return {b.id: 0.0 for b in bulbs}
    fill = {k: statistics.median(v) for k, v in known.items()}
    secs = {i: (v if v is not None else fill.get(kind, 0.0)) for i, (kind, v) in lat.items()}
    slowest = max(secs.values())
    return {i: slowest - s for i, s in secs.items()}


# ---------- validation ----------

def _num(value, lo, hi, default):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _hex(value, default="#ffffff"):
    if isinstance(value, str) and HEX.match(value):
        return "#" + value.lstrip("#").lower()
    return default


def clean_step(raw, bulb_ids, groups):
    """Check one step and fill in defaults. Raises ValueError with a readable message."""
    if not isinstance(raw, dict):
        raise ValueError("Each step must be an object")
    action = raw.get("action")
    if action not in ACTIONS:
        raise ValueError(f"Unknown action '{action}' (use one of: {', '.join(ACTIONS)})")

    target = raw.get("target") or {"type": "all"}
    ttype = target.get("type", "all")
    if ttype == "group":
        if target.get("id") not in {g["id"] for g in groups}:
            raise ValueError("A step points to a group that no longer exists")
        target = {"type": "group", "id": target["id"]}
    elif ttype == "bulbs":
        ids = [i for i in target.get("ids") or [] if i in bulb_ids]
        if not ids:
            raise ValueError("A step points to bulbs that don't exist")
        target = {"type": "bulbs", "ids": ids}
    else:
        target = {"type": "all"}

    step = {
        "action": action,
        "target": target,
        "duration": _num(raw.get("duration"), 0.2, 3600, 5),
        "stagger": _num(raw.get("stagger"), 0, 30, 0),
    }
    if action == "colour":
        step.update(colour=_hex(raw.get("colour")), brightness=int(_num(raw.get("brightness"), 1, 100, 80)),
                    fade=_num(raw.get("fade"), 0, 3600, 0), cut=bool(raw.get("cut")))
    elif action == "white":
        step.update(brightness=int(_num(raw.get("brightness"), 1, 100, 80)),
                    temp=int(_num(raw.get("temp"), 0, 100, 30)), fade=_num(raw.get("fade"), 0, 3600, 0))
    elif action == "effect":
        colours = [_hex(c) for c in (raw.get("colours") or []) if isinstance(c, str) and HEX.match(c)][:8]
        step.update(effect=raw.get("effect") if raw.get("effect") in EFFECTS else "gradient",
                    colours=colours or ["#ff2a1a", "#2a5cff"],
                    speed=int(_num(raw.get("speed"), 1, 100, 40)),
                    brightness=int(_num(raw.get("brightness"), 1, 100, 80)))
    elif action == "scene":
        if target["type"] != "group":
            raise ValueError("A 'scene' step needs a group as target")
        group = next(g for g in groups if g["id"] == target["id"])
        if raw.get("scene") not in {s["id"] for s in group["scenes"]}:
            raise ValueError(f"Scene not found in group '{group['name']}'")
        step["scene"] = raw["scene"]
    return step


def clean_choreo(raw, bulb_ids, groups):
    if not isinstance(raw, dict):
        raise ValueError("Expected a JSON object")
    name = str(raw.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("A choreography needs a name")
    steps = raw.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ValueError("A choreography needs at least one step")
    if len(steps) > 200:
        raise ValueError("Too many steps (max 200)")
    return {"name": name, "loop": bool(raw.get("loop")), "restore": bool(raw.get("restore")),
            "steps": [clean_step(s, bulb_ids, groups) for s in steps]}


# ---------- examples (added once, the first time) ----------

def example_choreos():
    all_ = {"type": "all"}
    rainbow = ["#ff2a1a", "#ff8a00", "#ffd400", "#3ddc4a", "#00c2ff", "#2a5cff", "#9b3dff"]
    return [
        {"name": "Sunset", "loop": False, "restore": False, "steps": [
            {"action": "white", "target": all_, "brightness": 100, "temp": 20, "fade": 3, "duration": 10},
            {"action": "colour", "target": all_, "colour": "#ff8a00", "brightness": 80, "fade": 60, "duration": 70},
            {"action": "colour", "target": all_, "colour": "#ff3a1a", "brightness": 45, "fade": 60, "duration": 70},
            {"action": "colour", "target": all_, "colour": "#b0206a", "brightness": 10, "fade": 45, "duration": 50},
            {"action": "off", "target": all_, "duration": 1},
        ]},
        {"name": "Color flow", "loop": False, "restore": False, "steps": [
            {"action": "effect", "target": all_, "effect": "gradient", "colours": rainbow,
             "speed": 20, "brightness": 70, "duration": 1},
        ]},
        {"name": "Wave", "loop": True, "restore": True, "steps": [
            {"action": "colour", "target": all_, "colour": "#00c2ff", "brightness": 90, "stagger": 0.4, "duration": 6},
            {"action": "colour", "target": all_, "colour": "#9b3dff", "brightness": 90, "stagger": 0.4, "duration": 6},
            {"action": "colour", "target": all_, "colour": "#ff3da8", "brightness": 90, "stagger": 0.4, "duration": 6},
        ]},
        {"name": "Party flash", "loop": True, "restore": True, "steps": [
            {"action": "colour", "target": all_, "colour": c, "brightness": 100, "cut": True, "duration": 0.6}
            for c in ("#ff2a1a", "#2a5cff", "#3ddc4a", "#ff3da8", "#ffd400", "#9b3dff")
        ]},
        {"name": "Breathe blue", "loop": False, "restore": False, "steps": [
            {"action": "effect", "target": all_, "effect": "breathe", "colours": ["#2a5cff"],
             "speed": 15, "brightness": 90, "duration": 1},
        ]},
    ]


# ---------- colour maths for fades ----------

def _hex_to_hsv(c):
    c = c.lstrip("#")
    return colorsys.rgb_to_hsv(*(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)))


def _hsv_to_hex(h, s, v):
    return "#%02x%02x%02x" % tuple(int(round(x * 255)) for x in colorsys.hsv_to_rgb(h, s, v))


def _mix_colour(a, b, f):
    """Blend two colours along the colour wheel (shortest way), so red -> blue passes purple."""
    (h1, s1, _), (h2, s2, _) = _hex_to_hsv(a), _hex_to_hsv(b)
    if s1 < 0.05:
        h1 = h2                       # from white/grey: don't spin through the wheel
    dh = ((h2 - h1 + 0.5) % 1.0) - 0.5
    return _hsv_to_hex((h1 + dh * f) % 1.0, s1 + (s2 - s1) * f, 1.0)


def _lerp(a, b, f):
    return a + (b - a) * f


# ---------- the player ----------

class Player:
    """Plays one choreography at a time in a background thread."""

    def __init__(self, bulbs, store):
        self.bulbs = bulbs          # id -> Bulb
        self.store = store
        self._stop = threading.Event()
        self._lock = threading.Lock()          # play/stop strictly one at a time
        self._running = []                     # (thread, stop event) of every playback thread
        self._pool = ThreadPoolExecutor(max_workers=24)
        self.state = {"playing": False}
        self.delays = {}

    # ----- public -----

    def play(self, choreo):
        with self._lock:
            self._stop_all(wait=True)
            self.delays = load_delays()            # re-read: a new measurement may exist
            stop = threading.Event()
            self._stop = stop
            self.state = {"playing": True, "id": choreo.get("id"), "name": choreo["name"],
                          "step": 0, "steps": len(choreo["steps"]), "round": 1, "holding": False,
                          "loop": choreo["loop"], "errors": 0, "started": time.time()}
            thread = threading.Thread(target=self._run, args=(choreo, stop), daemon=True)
            self._running.append((thread, stop))
            thread.start()

    def stop(self, wait=False):
        with self._lock:
            self._stop_all(wait)

    def _stop_all(self, wait):
        # stop every playback thread, not only the newest one, so none can be left looping
        for _, ev in self._running:
            ev.set()
        if wait:
            for thread, _ in self._running:
                if thread.is_alive() and thread is not threading.current_thread():
                    thread.join(timeout=15)
        self._running = [(t, e) for t, e in self._running if t.is_alive()]

    def status(self):
        s = dict(self.state)
        if s.get("playing"):
            s["elapsed"] = round(time.time() - s["started"], 1)
        return s

    # ----- internals -----

    def _targets(self, step):
        t = step["target"]
        if t["type"] == "group":
            group = self.store.group(t["id"])
            ids = group["members"] if group else []
        elif t["type"] == "bulbs":
            ids = t["ids"]
        else:
            ids = sorted(self.bulbs, key=lambda i: self.bulbs[i].name.lower())
        return [self.bulbs[i] for i in ids if i in self.bulbs]

    def _run(self, choreo, stop):
        touched = {}
        for step in choreo["steps"]:
            for b in self._targets(step):
                touched[b.id] = b
        uses_effects = any(s["action"] == "effect" for s in choreo["steps"])
        # remember how the lights looked before: needed for "restore after",
        # and to end built-in effects when the choreography is stopped
        before = {}
        if choreo["restore"] or uses_effects:
            for s in run_parallel(lambda b: b.status(), touched.values()):
                if s.get("online"):
                    before[s["id"]] = snapshot(s)
        effect_bulbs = set()           # bulbs currently playing an effect started by us

        try:
            while not stop.is_set():
                for i, step in enumerate(choreo["steps"]):
                    if stop.is_set():
                        break
                    self._set_state(stop, step=i + 1)
                    self._play_step(step, stop, effect_bulbs)
                if not choreo["loop"]:
                    break
                self._set_state(stop, round=self.state.get("round", 1) + 1)
            # Finished, but some bulbs still run a built-in effect by themselves:
            # stay "playing" until Stop, so the effect can be ended from the app.
            if not stop.is_set() and effect_bulbs:
                self._set_state(stop, holding=True)
                stop.wait()
        finally:
            # bulbs changed with hard cuts still report their old colour
            run_parallel(lambda b: self._quiet(b.sync), touched.values())
            if choreo["restore"]:
                back = [b for b in touched.values() if b.id in before]
            else:
                # without "restore after", only end the effects that are still running
                back = [touched[i] for i in effect_bulbs if i in before]
            if back:
                run_parallel(lambda b: self._quiet(lambda: b.apply_state(before[b.id])), back)
            if self._stop is stop:
                self.state = {"playing": False, "last": choreo["name"],
                              "errors": self.state.get("errors", 0)}

    def _set_state(self, stop, **changes):
        """Only the current playback thread may update the shown state."""
        if self._stop is stop:
            self.state.update(changes)

    @staticmethod
    def _quiet(fn):
        try:
            fn()
        except Exception:
            pass

    def _play_step(self, step, stop, effect_bulbs):
        start = time.monotonic()
        bulbs = self._targets(step)
        scene_states = {}
        if step["action"] == "scene":
            _, scene = self.store.scene(step["target"]["id"], step["scene"])
            scene_states = scene["states"] if scene else {}

        hold = send_ahead(self.delays, step, bulbs)

        def run_bulb(index, bulb):
            delay = index * step["stagger"] + hold[bulb.id]
            if stop.wait(max(0, start + delay - time.monotonic())):
                return
            try:
                self._do(bulb, step, stop, scene_states)
                if step["action"] == "effect":
                    effect_bulbs.add(bulb.id)
                else:
                    effect_bulbs.discard(bulb.id)   # a later step ended its effect
            except Exception:
                self._set_state(stop, errors=self.state.get("errors", 0) + 1)

        futures = [self._pool.submit(run_bulb, i, b) for i, b in enumerate(bulbs)]
        for f in futures:
            f.result()
        # wait for the rest of the step's duration
        stop.wait(max(0, start + step["duration"] - time.monotonic()))

    def _do(self, bulb, step, stop, scene_states):
        action = step["action"]
        if action == "on":
            bulb.set_power(True)
        elif action == "off":
            bulb.set_power(False)
        elif action == "scene":
            if bulb.id in scene_states:
                bulb.apply_state(scene_states[bulb.id])
        elif action == "effect":
            if bulb.has_effects:
                bulb.set_effect(step["effect"], step["colours"], step["speed"], step["brightness"])
            elif bulb.has_colour:
                bulb.set_colour(step["colours"][0], step["brightness"])
        elif action == "white":
            if step["fade"] > 0:
                self._fade(bulb, step, stop)
            else:
                bulb.set_white(step["brightness"], step["temp"] if bulb.has_temp else None)
        elif action == "colour":
            if not bulb.has_colour:
                bulb.set_white(step["brightness"])
            elif step["cut"]:
                bulb.cut_colour(step["colour"], step["brightness"])
            elif step["fade"] > 0:
                self._fade(bulb, step, stop)
            else:
                bulb.set_colour(step["colour"], step["brightness"])

    def _fade(self, bulb, step, stop):
        """Fade from the bulb's current state to the step's target over step['fade'] seconds.
        The bulb's own short fade smooths the gaps between the intermediate values."""
        if not bulb.last:
            bulb.status()
        cur = dict(bulb.last)
        to_colour = step["action"] == "colour"
        if not cur.get("on"):
            cur["brightness"] = 1                         # fade in from (almost) dark
        from_b = cur.get("brightness") or 50
        if to_colour:
            from_c = cur.get("colour") if cur.get("mode") == "colour" and cur.get("colour") else step["colour"]
        else:
            from_t = cur.get("temp") if cur.get("mode") == "white" and cur.get("temp") is not None else step["temp"]

        n = max(1, math.ceil(step["fade"] / FADE_INTERVAL))
        t0 = time.monotonic()
        for k in range(1, n + 1):
            f = k / n
            br = max(1, int(round(_lerp(from_b, step["brightness"], f))))
            if to_colour:
                bulb.set_colour(_mix_colour(from_c, step["colour"], f), br)
            else:
                temp = int(round(_lerp(from_t, step["temp"], f))) if bulb.has_temp else None
                bulb.set_white(br, temp)
            if k < n and stop.wait(max(0, t0 + k * step["fade"] / n - time.monotonic())):
                return
