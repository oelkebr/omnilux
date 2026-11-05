"""
Measures how long the bulbs take to visibly react to commands, with the laptop camera,
so choreographies can be synchronised: the bulbs within a lamp, and the lamps with each other.

Run inside the activated .venv, in the lights folder, with the webapp server STOPPED:

    python latency_cam.py preview        # live camera view for aiming: +/- exposure, l = bulbs on/off, q closes
    python latency_cam.py calibrate      # finds every bulb in the picture + the camera's own delay
    python latency_cam.py run            # the measurements (about 1 h for all 11 bulbs)
    python latency_cam.py analyze        # (re)writes the summary from all runs so far

`calibrate` and `run` ask how many seconds you need to leave the picture before they
start (Enter = the suggestion shown). --delay 120 skips the question, --delay 0 starts
right away.

Lamps are taken from the webapp's groups (settings.json): the smallest groups that don't
share bulbs, so "all" is skipped. Or name them: --lamps "Stehlampe,Decke,Diele".

Useful options for `run`:
    --phases steps,burst,effects,effectsync   which parts to run (default: all four)
    --phases player                all bulbs at once, sent exactly like the choreography player,
                                   with and without the per-bulb delays (needs latency/offsets.json)
    --bulbs "st1,de"               only bulbs whose name contains one of these texts
    --reps 6                       repetitions per command, connection type and direction
    --modes new,persistent         connection types to compare
    --effect-bulbs all             single-bulb effect tests on every bulb (default: one per lamp)

Setup: room as dark as possible (other lights off, curtains closed), laptop not moved
between `calibrate` and `run`, and nobody moving in the picture. The screen
should show the terminal; `calibrate` briefly flashes it white full-screen.
Warning: the effect tests at high speed flash quickly between colours.

What is measured (all times in ms, from the moment the command is sent):
  steps       every bulb alone (others off): on/off, dim/brighten, soft colour change,
              hard colour cut - over a new connection per command (what the app does) and
              over a kept-open connection. t10/t50/t90 = 10/50/90% of the visible change,
              ack = when the bulb confirmed the command.
  burst       all bulbs commanded at the same moment (like a choreography step on "all").
              Every bulb is timed separately -> spread within each lamp and between lamps.
  effects     built-in effects jump/gradient/breathe at several speeds on single bulbs:
              how long until the effect starts, and how long one colour step lasts.
  effectsync  the same effect started on all bulbs at once, watched for 40 s:
              how far apart the bulbs start and how fast they drift apart.
Camera frames arrive every ~33 ms; crossing times are interpolated between frames, and
the camera's own delay (measured with the screen flash) is subtracted. When several
bulbs are lit, each frame is split into the bulbs' contributions using the light pattern
every bulb showed alone during calibration (works even where their light overlaps).

Results go to latency/run_<date>/ (events.jsonl, traces.npz) and latency/summary.txt.
"""

import argparse
import json
import os
import random
import socket
import statistics
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
import cv2  # noqa: E402
import numpy as np  # noqa: E402
import tinytuya  # noqa: E402

from lights import _to_raw, encode_colour, hex_to_hsv, load_bulbs  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "latency"
CALIB_FILE = OUT / "calibration.json"
MASK_FILE = OUT / "masks.npz"
SETTINGS = HERE / "settings.json"

SMALL = (160, 120)          # analysis resolution (w, h); masks live at this size
BUFFER_S = 50               # seconds of camera frames kept in memory
PRE = 0.6                   # seconds of baseline before each command
POST = 3.0                  # watch at most this long after each command (usually stops much earlier:
AFTER_ACK = 1.5             # ... or this long after the confirmation)  as soon as the light has settled
STABLE = 0.4                # a change counts as finished after holding still this long
EFFECT_CROSSINGS = 9        # single-bulb effect test stops after this many colour steps
SETTLE = 1.6                # wait after preparing a bulb's start state
PAIRS_PER_BLOCK = 2         # each block: prepare once, then A,B,A,B
SPEEDS = (10, 30, 50, 75, 100)
EFFECT_SECONDS = {10: 30, 30: 20, 50: 15, 75: 12, 100: 10}
EFFECTS = ("jump", "gradient", "breathe")
SYNC_EFFECTS = (("jump", 50), ("jump", 100), ("gradient", 50), ("breathe", 50))
SYNC_SECONDS = 40
SYNC_REPS = 2
RED, BLUE, GREEN = "#ff0000", "#0000ff", "#00ff00"
LIN = ((np.arange(256) / 255.0) ** 2.2 * 255).astype(np.float32)   # camera value -> linear light

# test -> (start state, [(event name, command), (event name, command)])
STEP_TESTS = {
    "power":  (("white", 100), [("off", ("power", False)), ("on", ("power", True))]),
    "bright": (("white", 100), [("dim", ("white", 10)), ("brighten", ("white", 100))]),
    "colour": (("colour", RED), [("to_blue", ("colour", BLUE)), ("to_red", ("colour", RED))]),
    "cut":    (("colour", RED), [("to_blue", ("cut", BLUE)), ("to_red", ("cut", RED))]),
}
BURST_TESTS = ("power", "colour", "cut")


def now():
    return time.perf_counter()


def pause(seconds):
    if seconds > 0:
        time.sleep(seconds)


def ask_delay(given, suggested):
    """Seconds to wait before starting: --delay if given, else asked (Enter = suggested)."""
    if given is not None:
        return max(0, given)
    if not sys.stdin.isatty():
        return suggested
    while True:
        try:
            answer = input(f"Seconds to get out of the picture before it starts [{suggested}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            sys.exit("\nCancelled.")
        if not answer:
            return suggested
        try:
            return max(0, int(float(answer)))
        except ValueError:
            print("   Please type a number of seconds, e.g. 90 (or just Enter).")


SUGGESTED_DELAY = {"calibrate": 60, "run": 300}


def countdown(seconds):
    """Time to leave the picture before anything starts. Ctrl+C cancels."""
    if seconds <= 0:
        return
    print(f"Starting in {seconds // 60} min {seconds % 60} s - time to get out of the picture "
          "(Ctrl+C cancels).")
    end = time.monotonic() + seconds
    try:
        while (left := end - time.monotonic()) > 0:
            print(f"\r   {int(left) // 60}:{int(left) % 60:02d} left ", end="", flush=True)
            time.sleep(min(1.0, left))
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    print("\r   Starting now.      ")


# ---------- bulbs, names, lamps ----------

def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_named_bulbs():
    """Bulbs with the names given in the webapp (st1, de3, ...)."""
    bulbs = load_bulbs()
    names = load_settings().get("names") or {}
    for b in bulbs.values():
        b.name = names.get(b.id) or b.original_name
    return bulbs


def load_lamps(bulbs, wanted=None):
    """Lamps = webapp groups. Default: smallest groups first, skipping groups that share bulbs."""
    groups = [g for g in load_settings().get("groups") or [] if g.get("members")]
    if wanted:
        names = [w.strip().lower() for w in wanted.split(",") if w.strip()]
        groups = [g for n in names for g in groups if g["name"].lower() == n]
    else:
        groups = sorted(groups, key=lambda g: len(g["members"]))
    lamps, taken = [], set()
    for g in groups:
        members = [i for i in g["members"] if i in bulbs]
        if members and not taken & set(members):
            lamps.append({"name": g["name"], "bulbs": members})
            taken |= set(members)
    for i, b in bulbs.items():
        if i not in taken:
            lamps.append({"name": b.name, "bulbs": [i]})
    return lamps


def lamp_of(lamps):
    return {i: lamp["name"] for lamp in lamps for i in lamp["bulbs"]}


def model_of(bulb):
    return bulb.original_name.rstrip(" 0123456789")


# ---------- camera ----------

class Camera:
    """Reads frames in a background thread and keeps the last BUFFER_S seconds (downscaled)."""

    def __init__(self, index=0, exposure=None):
        self.cap = cv2.VideoCapture(index, cv2.CAP_MSMF)
        if not self.cap.isOpened():
            raise RuntimeError("Camera could not be opened (is another app using it?)")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.cap.set(cv2.CAP_PROP_AUTO_WB, 0)          # keep colours from drifting
        self.exposure = None
        if exposure is not None:
            self.set_exposure(exposure)
        self.buf = deque(maxlen=BUFFER_S * 40)
        self.full = None
        self.lock = threading.Lock()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        deadline = now() + 8
        while self.full is None and now() < deadline:
            pause(0.05)
        if self.full is None:
            raise RuntimeError("Camera delivers no frames")

    def set_exposure(self, value):
        self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)   # manual
        self.cap.set(cv2.CAP_PROP_EXPOSURE, value)
        self.exposure = value

    def _loop(self):
        while self.running:
            ok, frame = self.cap.read()
            t = now()
            if not ok:
                pause(0.01)
                continue
            small = cv2.resize(frame, SMALL, interpolation=cv2.INTER_AREA)
            with self.lock:
                self.buf.append((t, small))
                self.full = frame

    def close(self):
        self.running = False
        self.thread.join(timeout=2)
        self.cap.release()

    def frames(self, t0, t1):
        """(times, frames uint8 n x h x w x 3) captured between t0 and t1."""
        with self.lock:
            sel = [(t, f) for t, f in self.buf if t0 <= t <= t1]
        if not sel:
            return np.zeros(0), np.zeros((0, SMALL[1], SMALL[0], 3), np.uint8)
        return np.array([t for t, _ in sel]), np.stack([f for _, f in sel])

    def wait_past(self, t):
        """Wait until frames up to time t are in the buffer."""
        pause(t - now() + 0.08)

    def mean_frames(self, seconds=0.6):
        """Average picture over `seconds`: (camera values, linear light), both h x w x 3."""
        t0 = now()
        self.wait_past(t0 + seconds)
        _, fs = self.frames(t0, t0 + seconds)
        return fs.astype(np.float32).mean(0), LIN[fs].mean(0)

    def fps(self, seconds=2.0):
        t = now()
        ts, _ = self.frames(t - seconds, t)
        return (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) > 2 else 0.0


def signals(frames, masks):
    """Weighted mean linear light per mask and frame: n x k x 3 (masks: k x pixels, rows sum to 1)."""
    f = LIN[frames.reshape(len(frames), SMALL[0] * SMALL[1], 3)]     # linear light, so 50% = half the light
    return np.einsum("kp,npc->nkc", masks, f)


class Unmixer:
    """Splits each frame into the contributions of several lit bulbs.

    Light adds up linearly, so frame = dark + sum(level_i * pattern_i), with pattern_i the
    picture of bulb i alone at full white (from calibration). Solved by least squares per
    colour channel. Output: n x bulbs x 3, 100 = like that bulb alone at full white."""

    def __init__(self, resp, dark, sat, rows):
        r = resp[rows]                                    # m x P x 3 (linear)
        gray = r.mean(2)
        keep = np.zeros(r.shape[1], bool)
        for g in gray:
            top = np.percentile(g, 99.9)
            if top > 0:
                keep |= g >= 0.1 * top
        allon = dark + r.sum(0)                           # predicted picture with everything on
        keep &= ~sat & (allon.max(1) < LIN[240])          # clipped pixels can't be split
        self.pix = np.nonzero(keep)[0]
        self.dark = dark[self.pix]
        mats = [r[:, self.pix, c].T for c in range(3)]    # P' x m per channel
        self.w = [np.linalg.pinv(m) for m in mats]
        self.cond = max(np.linalg.cond(m) for m in mats)

    def linear(self, lin):
        """lin: n x P x 3 linear light (all pixels)."""
        f = lin[:, self.pix] - self.dark
        return np.stack([f[:, :, c] @ self.w[c].T for c in range(3)], axis=2) * 100

    def __call__(self, frames):
        return self.linear(LIN[frames.reshape(len(frames), SMALL[0] * SMALL[1], 3)])


def load_unmix_data():
    data = np.load(MASK_FILE)
    if "resp" not in data:
        sys.exit("The calibration is from an older version - run 'python latency_cam.py calibrate' again.")
    return data["resp"], data["dark"], data["sat"]


# ---------- bulb commands ----------

def values_for(bulb, command):
    kind, arg = command[0], command[1]
    dp = bulb.dps
    if kind == "power":
        return {dp["switch"]: bool(arg)}
    if kind == "white":
        return {dp["switch"]: True, dp["mode"]: "white", dp["bright"]: _to_raw(arg, bulb._range("bright"))}
    if kind == "colour":
        r, g, b = (int(arg[i:i + 2], 16) for i in (1, 3, 5))
        return {dp["switch"]: True, dp["mode"]: "colour", dp["colour"]: encode_colour(r, g, b, 100, bulb.layout)}
    if kind == "cut":                     # same as Bulb.cut_colour; the bulb is in colour mode already
        h, s = hex_to_hsv(arg)
        return {dp["switch"]: True, dp["control"]: f"0{h:04x}{s:04x}{1000:04x}{0:04x}{0:04x}"}
    if kind == "effect":
        effect, colours, speed = arg
        return bulb.effect_values(effect, colours, speed, 100)
    raise ValueError(kind)


class Link:
    """Sends commands to one bulb: a new connection per command, or one kept-open connection."""

    def __init__(self, bulb, persistent=False):
        self.bulb, self.persistent, self.dev = bulb, persistent, None

    def _make(self):
        b = self.bulb
        d = tinytuya.Device(b.id, b.ip, b.key, version=b.version, connection_timeout=3)
        d.set_socketRetryLimit(1)
        d.set_socketTimeout(3)
        d.set_socketPersistent(self.persistent)
        return d

    def send(self, command):
        """-> (t_send, t_ack, error or None)"""
        values = values_for(self.bulb, command)
        t0 = now()
        try:
            if self.dev is None or not self.persistent:
                self.dev = self._make()
            result = self.dev.set_multiple_values(values)
            err = result.get("Error") if isinstance(result, dict) else None
        except Exception as e:
            err = str(e) or type(e).__name__
        t1 = now()
        if err and self.persistent:
            self.close()
        return t0, t1, err

    def close(self):
        if self.dev is not None and self.persistent:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None


def send_quiet(bulb, command, tries=2):
    link = Link(bulb)
    for _ in range(tries):
        if not link.send(command)[2]:
            return True
        pause(0.3)
    return False


_pool = ThreadPoolExecutor(max_workers=16)


def all_bulbs(bulbs, command):
    list(_pool.map(lambda b: send_quiet(b, command), bulbs))


def fire_together(links, command, pool):
    """Send `command` over all links at the same moment. -> (t_go, [(t_send, t_ack, err)])"""
    go = threading.Barrier(len(links) + 1)

    def fire(link):
        go.wait()
        return link.send(command)
    futures = [pool.submit(fire, lk) for lk in links]
    go.wait()
    t_go = now()
    return t_go, [f.result() for f in futures]


RESTORE_DPS = ("20", "21", "22", "23", "24", "25")


def save_states(bulbs):
    def one(b):
        try:
            r = Link(b)._make().status()
            return b.id, (r or {}).get("dps")
        except Exception:
            return b.id, None
    return dict(_pool.map(one, bulbs))


def restore_states(bulbs, states):
    print("Restoring the bulbs...")

    def one(b):
        dps = states.get(b.id)
        if not dps:
            return
        values = {dp: dps[dp] for dp in RESTORE_DPS if dp in dps}
        if dps.get("21") != "scene":
            values.pop("25", None)
        try:
            Link(b)._make().set_multiple_values(values)
        except Exception as e:
            print(f"   {b.name}: restore failed ({e})")
    list(_pool.map(one, bulbs))


# ---------- analysis ----------

def crossing(t, p, level, start):
    """First time after index `start` where p reaches `level` (and stays there for 2 frames)."""
    for i in range(max(start, 1), len(p)):
        if p[i] >= level and (i + 1 >= len(p) or p[i + 1] >= level):
            p0, p1 = p[i - 1], p[i]
            f = (level - p0) / (p1 - p0) if p1 != p0 else 1.0
            return t[i - 1] + min(max(f, 0.0), 1.0) * (t[i] - t[i - 1])
    return None


def settled(t, sig, t_send, skip=()):
    """True when every bulb in sig (n x k x 3) has visibly changed since t_send and then held
    still for STABLE seconds. Bulbs in `skip` (failed commands) are ignored."""
    pre = (t >= t_send - PRE) & (t < t_send)
    recent = t >= t[-1] - STABLE
    if pre.sum() < 5 or recent.sum() < 8 or t[recent][0] < t_send:
        return False
    after = t >= t_send
    for j in range(sig.shape[1]):
        if j in skip:
            continue
        s = sig[:, j]
        base = s[pre].mean(0)
        noise = float(np.linalg.norm(s[pre] - base, axis=1).std()) + 0.3
        amp = float(np.linalg.norm(s[after] - base, axis=1).max())
        if amp < max(8 * noise, 3):
            return False                                  # nothing visible yet
        held = s[recent]
        if np.linalg.norm(held - held.mean(0), axis=1).max() > max(0.03 * amp, 4 * noise):
            return False                                  # still moving
        if np.linalg.norm(held.mean(0) - base) < 0.5 * amp:
            return False                                  # back at the start: a flicker, not the change
    return True


def analyse_step(t, rgb, t_send, offset):
    """Step change (on/off, brightness, colour). rgb: n x 3 for one bulb."""
    pre = (t >= t_send - PRE) & (t < t_send)
    if pre.sum() < 5:
        return {"status": "no_frames"}
    base = rgb[pre].mean(0)
    final = rgb[t >= t[-1] - 0.4].mean(0)
    d = final - base
    amp = float(np.linalg.norm(d))
    if amp < 1.5:
        return {"status": "no_change", "amp": round(amp, 2)}
    p = (rgb - base) @ d / amp ** 2
    noise = float(p[pre].std())
    out = {"amp": round(amp, 1), "noise": round(noise, 3)}
    if noise > 0.12:
        return {**out, "status": "unstable"}
    start = int(np.searchsorted(t, t_send))
    for level in (10, 50, 90):
        tc = crossing(t, p, level / 100, start)
        out[f"t{level}"] = None if tc is None else round((tc - t_send - offset) * 1000)
    out["status"] = "ok" if out["t10"] is not None and out["t90"] is not None else "no_crossing"
    out["overshoot"] = round(float(p[start:].max()) - 1, 2) if start < len(p) else None
    return out


def effect_crossings(t, rgb, t_send):
    """Effect start time and the times where the colour passes the middle between two steps.
    -> (t_on, [crossing times], amplitude) or (None, [], amplitude)"""
    pre = (t >= t_send - PRE) & (t < t_send)
    if pre.sum() < 5:
        return None, [], 0.0
    base = rgb[pre].mean(0)
    dev = np.linalg.norm(rgb - base, axis=1)
    noise = float(dev[pre].std()) + 0.3
    start = int(np.searchsorted(t, t_send))
    if start >= len(t):
        return None, [], 0.0
    maxdev = float(dev[start:].max())
    if maxdev < 3:
        return None, [], maxdev
    t_on = crossing(t, dev, max(6 * noise, 0.15 * maxdev), start)
    if t_on is None:
        return None, [], maxdev
    after = t >= t_on
    x = rgb[after] - rgb[after].mean(0)
    _, _, vt = np.linalg.svd(x, full_matrices=False)
    s = x @ vt[0]                                         # main colour axis of the effect
    ta = t[after]
    lo, hi = np.percentile(s, 10), np.percentile(s, 90)
    if hi - lo < 2:
        return t_on, [], maxdev
    mid = (lo + hi) / 2
    sign = s > mid
    clean = []
    for i in np.nonzero(sign[1:] != sign[:-1])[0]:
        f = (mid - s[i]) / (s[i + 1] - s[i])
        c = ta[i] + f * (ta[i + 1] - ta[i])
        if not clean or c - clean[-1] > 0.12:           # ignore flicker
            clean.append(c)
    return t_on, clean, maxdev


def analyse_effect(t, rgb, t_send, offset):
    """Built-in effect: start latency and the duration of one colour step."""
    t_on, cross, amp = effect_crossings(t, rgb, t_send)
    if t_on is None:
        return {"status": "no_change", "amp": round(amp, 1)}
    out = {"status": "ok", "amp": round(amp, 1), "start": round((t_on - t_send - offset) * 1000),
           "crossings": len(cross)}
    steps = np.diff(cross)
    if len(steps) >= 2:
        out["step"] = round(float(np.median(steps)) * 1000)
        out["step_spread"] = round(float(np.percentile(steps, 90) - np.percentile(steps, 10)) * 1000)
        out["first_step"] = round((cross[0] - t_on) * 1000)
    else:
        out["status"] = "too_slow"            # window shorter than ~3 colour steps
    return out


def fit_steps(cross, t_send, offset):
    """Straight line through the crossing times: crossing k happens at phase + k * step.
    Robust against a missed crossing. -> dict with phase (ms after send), step (ms), resid (ms)"""
    if len(cross) < 4:
        return None
    c = np.array(cross)
    est = float(np.median(np.diff(c)))
    k = np.round((c - c[0]) / est)
    b, a = np.polyfit(k, c, 1)
    resid = c - (a + b * k)
    return {"phase": round((a - t_send - offset) * 1000), "step": round(float(b) * 1000, 1),
            "resid": round(float(resid.std()) * 1000), "n": len(c)}


# ---------- calibration ----------

def check_server():
    with socket.socket() as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", 8000)) == 0:
            sys.exit("The webapp is running on port 8000 - stop it first (Ctrl+C in its terminal).")


def pick_exposure(cam):
    """Brightest exposure where, with all bulbs at full white, the picture stays below clipping."""
    chosen, results = None, []
    for e in range(-5, -12, -1):        # -5 = 1/32 s, the longest that keeps 30 frames/s
        cam.set_exposure(e)
        pause(0.5)
        f, _ = cam.mean_frames(0.4)
        p98 = float(np.percentile(f.max(axis=2), 98))
        results.append((e, round(float(f.mean()), 1), round(p98)))
        print(f"   exposure {e:>3}: mean {f.mean():6.1f}  bright pixels {p98:5.0f}")
        if p98 < 200:
            chosen = e
            break
    if len({r[1] for r in results}) == 1 and len(results) > 1:
        print("   ! The camera seems to ignore manual exposure - results will be less exact.")
    return chosen if chosen is not None else -11


def screen_offset(cam, flashes=12):
    """Camera delay: flash the screen white and see when the camera notices (includes ~1 screen frame)."""
    name = "latency flash"
    cv2.namedWindow(name, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    try:
        cv2.setWindowProperty(name, cv2.WND_PROP_TOPMOST, 1)
    except cv2.error:
        pass
    black = np.zeros((180, 320, 3), np.uint8)
    white = np.full_like(black, 255)

    def pump(seconds):
        end = now() + seconds
        while now() < end:
            cv2.waitKey(5)

    cv2.imshow(name, black)
    pump(2.0)
    out = []
    for _ in range(flashes):
        pump(0.6 + random.random() * 0.5)
        cv2.imshow(name, white)
        cv2.waitKey(1)
        t0 = now()
        pump(0.7)
        cv2.imshow(name, black)
        cv2.waitKey(1)
        ts, fs = cam.frames(t0 - PRE, t0 + 0.7)
        g = LIN[fs.reshape(len(fs), SMALL[0] * SMALL[1], 3)].mean(1)
        r = analyse_step(ts, g, t0, 0.0)
        if r.get("status") == "ok":
            out.append(r["t10"])
    cv2.destroyWindow(name)
    cv2.waitKey(1)
    return out


def contact_sheet(frame, responses, labels, path):
    tiles = []
    base = cv2.resize(frame, (320, 240)).astype(np.float32) * 0.6
    for resp, label in zip(responses, labels):
        heat = cv2.resize(resp / (resp.max() or 1), (320, 240))
        colour = cv2.applyColorMap((heat * 255).astype(np.uint8), cv2.COLORMAP_JET).astype(np.float32)
        a = np.clip(heat * 1.5, 0, 1)[..., None]
        tile = (base * (1 - a) + colour * a).astype(np.uint8)
        cv2.putText(tile, label, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(tile)
    cols = 4
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    cv2.imwrite(str(path), np.vstack(rows))


def cmd_calibrate(args):
    check_server()
    OUT.mkdir(exist_ok=True)
    named = load_named_bulbs()
    lamps = load_lamps(named, args.lamps)
    lamp_name = lamp_of(lamps)
    bulbs = sorted(named.values(), key=lambda b: (lamp_name.get(b.id, ""), b.name))
    print("Lamps: " + "; ".join(f"{lp['name']}: " + ", ".join(named[i].name for i in lp["bulbs"])
                                 for lp in lamps))
    countdown(ask_delay(args.delay, SUGGESTED_DELAY[args.cmd]))
    print(f"{len(bulbs)} bulbs. Saving their current state...")
    states = save_states(bulbs)
    offline = [b.name for b in bulbs if not states.get(b.id)]
    if offline:
        print("   not answering: " + ", ".join(offline))
    cam = Camera(args.camera)
    print(f"Camera: {cam.fps():.1f} frames/s")
    try:
        print("All bulbs to full white, choosing the exposure...")
        all_bulbs(bulbs, ("white", 100))
        pause(2.5)
        exposure = args.exposure if args.exposure is not None else pick_exposure(cam)
        cam.set_exposure(exposure)
        pause(0.6)
        allon_full = cam.full.copy()
        print(f"   using exposure {exposure}; {cam.fps():.1f} frames/s")

        print("All bulbs off...")
        all_bulbs(bulbs, ("power", False))
        pause(2.5)
        (g1, dark), (g2, _) = cam.mean_frames(), cam.mean_frames()
        noise = np.abs(g1 - g2).mean(2)

        shape = noise.shape
        masks, resps, ons, labels, info = [], [], [], [], []
        sat_all = np.zeros(shape, bool)
        for b in bulbs:
            if not states.get(b.id):
                masks.append(np.zeros(shape, np.float32))
                resps.append(np.zeros(shape + (3,), np.float32))
                ons.append(None)
                labels.append(f"{b.name}: offline")
                info.append({"id": b.id, "name": b.name, "visible": False})
                continue
            diffs, ldiffs, lon, sat = [], [], [], np.zeros(shape, bool)
            for _ in range(2):
                off_g, off_l = cam.mean_frames()
                send_quiet(b, ("white", 100))
                pause(SETTLE)
                on_g, on_l = cam.mean_frames()
                send_quiet(b, ("power", False))
                diffs.append((on_g - off_g).mean(2))
                ldiffs.append(on_l - off_l)
                lon.append(on_l)
                sat |= on_g.max(2) >= 245
                pause(SETTLE)
            resp = np.clip(np.mean(diffs, 0), 0, None)
            resp[sat] = 0                                  # clipped pixels can't show fades
            top = float(np.percentile(resp, 99.9))
            mask = np.where(resp >= 0.25 * top, resp, 0).astype(np.float32)
            area = int((mask > 0).sum())
            strength = float((mask * resp).sum() / max(mask.sum(), 1e-9))
            visible = strength > max(3.0, 8 * float(noise.mean())) and area >= 4
            masks.append(mask)
            resps.append(np.clip(np.mean(ldiffs, 0), 0, None).astype(np.float32))
            ons.append(np.mean(lon, 0))
            sat_all |= sat
            labels.append(f"{b.name}: {strength:.0f} lv, {area} px" + ("" if visible else " NOT SEEN"))
            info.append({"id": b.id, "name": b.name, "model": model_of(b), "visible": bool(visible),
                         "strength": round(strength, 1), "area": area, "saturated_px": int(sat.sum())})
            print(f"   {labels[-1]}")

        m = np.stack([x.ravel() for x in masks]).astype(np.float32)
        sums = m.sum(1, keepdims=True)
        m = np.where(sums > 0, m / np.maximum(sums, 1e-9), 0)
        resp = np.stack([r.reshape(-1, 3) for r in resps])
        dark = dark.reshape(-1, 3).astype(np.float32)
        sat_flat = sat_all.ravel()
        for x in info:
            x["lamp"] = lamp_name.get(x["id"])

        # check: can the bulbs be told apart when several are lit?
        vis = [n for n, x in enumerate(info) if x["visible"]]
        if len(vis) > 1:
            un = Unmixer(resp, dark, sat_flat, vis)
            seen = un.linear(np.stack([ons[n].reshape(-1, 3) for n in vis]))  # each bulb alone
            level = seen.mean(2)                                              # rows: lit bulb
            print(f"Separating the bulbs ({len(un.pix)} usable pixels, condition {un.cond:.0f}):")
            for r, n in enumerate(vis):
                others = np.delete(level[r], r)
                worst = int(np.argmax(np.abs(others)))
                worst_name = info[np.delete(np.array(vis), r)[worst]]["name"]
                info[n]["crosstalk_pct"] = round(float(np.abs(others).max()), 1)
                print(f"   {info[n]['name']:<12} alone reads {level[r, r]:5.0f}; largest leak into "
                      f"another bulb {others[worst]:+5.1f} ({worst_name})")
        contact_sheet(allon_full, masks, [f"{lab}  [{x.get('lamp')}]" for lab, x in zip(labels, info)],
                      OUT / "calibration.png")

        offset_ms, flashes = 0, []
        if not args.no_screen:
            print("Camera delay: the screen flashes white 12 times (~20 s), don't cover the screen...")
            for e in (exposure, min(exposure + 2, -5), -5):
                cam.set_exposure(e)
                pause(0.6)
                flashes = screen_offset(cam)
                if len(flashes) >= 6:
                    break
                print(f"   only {len(flashes)} flashes seen at exposure {e}, trying brighter...")
            cam.set_exposure(exposure)
            if len(flashes) >= 6:
                offset_ms = int(statistics.median(flashes))
                print(f"   camera delay {offset_ms} ms (spread {min(flashes)}-{max(flashes)} ms)")
            else:
                print("   ! screen flash not detected - camera delay is NOT subtracted")

        calib = {"date": datetime.now().isoformat(timespec="seconds"), "camera": args.camera,
                 "exposure": exposure, "camera_offset_ms": offset_ms, "screen_flashes_ms": flashes,
                 "fps": round(cam.fps(), 1), "bulbs": info, "lamps": lamps}
        CALIB_FILE.write_text(json.dumps(calib, indent=2), encoding="utf-8")
        np.savez_compressed(MASK_FILE, masks=m, ids=np.array([x["id"] for x in info]),
                            resp=resp, dark=dark, sat=sat_flat)
        print(f"\nSaved {CALIB_FILE.relative_to(HERE)} and {(OUT / 'calibration.png').relative_to(HERE)} "
              "(open the picture to check where each bulb was found).")
    finally:
        cam.close()
        restore_states(bulbs, states)


# ---------- measurement run ----------

class Run:
    def __init__(self, cam, masks, idx, offset, unmix_data):
        self.cam, self.masks, self.idx, self.offset = cam, masks, idx, offset
        self.unmix_data = unmix_data
        self.dir = OUT / f"run_{datetime.now():%Y%m%d_%H%M%S}"
        self.dir.mkdir(parents=True)
        self.events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self.traces = {}
        self.n = 0

    def unmixer(self, bulbs):
        resp, dark, sat = self.unmix_data
        return Unmixer(resp, dark, sat, [self.idx[b.id] for b in bulbs])

    def log(self, event, t, sig, t_send):
        event["n"] = self.n
        self.traces[f"e{self.n}_t"] = (t - t_send).astype(np.float32)
        self.traces[f"e{self.n}_sig"] = sig.astype(np.float32)
        self.events.write(json.dumps(event) + "\n")
        self.events.flush()
        self.n += 1

    def save(self):
        if self.traces:
            np.savez_compressed(self.dir / "traces.npz", **self.traces)

    def watch(self, t_send, t_cap, to_signal, done, every=0.1):
        """Record from just before t_send until done(times, signal) says the light has settled,
        at the latest until t_cap. -> (times, signal n x k x 3)"""
        while now() < t_cap:
            ts, fs = self.cam.frames(t_send - PRE - 0.1, now())
            if len(ts) > 5:
                sig = to_signal(fs)
                if done(ts, sig):
                    return ts, sig
            pause(every)
        self.cam.wait_past(t_cap)
        ts, fs = self.cam.frames(t_send - PRE - 0.1, t_cap)
        return ts, to_signal(fs)

    def mask_signal(self, rows):
        return lambda fs: signals(fs, self.masks[rows])


def run_steps(run, bulbs, everything, lamp_name, args):
    total = len(bulbs)
    for k, b in enumerate(bulbs, 1):
        print(f"\n[{k}/{total}] {b.name} ({lamp_name.get(b.id)})")
        all_bulbs(everything, ("power", False))       # only the tested bulb will light up
        blocks = [(test, mode) for test in STEP_TESTS for mode in args.modes
                  for _ in range(max(1, args.reps // PAIRS_PER_BLOCK))]
        random.shuffle(blocks)
        summary = {}
        for test, mode in blocks:
            prep, pair = STEP_TESTS[test]
            link = Link(b, persistent=(mode == "persistent"))
            try:
                link.send(prep)                                    # also opens a kept-open connection
                pause(SETTLE)
                for _ in range(PAIRS_PER_BLOCK):
                    for event, command in pair:
                        pause(random.uniform(0.1, 0.6))
                        t_send, t_ack, err = link.send(command)
                        t_cap = now() + 0.2 if err else max(t_send + POST, t_ack + AFTER_ACK)
                        ts, sig = run.watch(t_send, t_cap, run.mask_signal([run.idx[b.id]]),
                                            lambda t, s, t0=t_send: not err and settled(t, s, t0))
                        rec = {"phase": "steps", "bulb": b.name, "id": b.id, "model": model_of(b),
                               "lamp": lamp_name.get(b.id), "test": test, "event": event, "mode": mode,
                               "ack": round((t_ack - t_send) * 1000), "error": err,
                               "watched": round((ts[-1] - t_send) * 1000) if len(ts) else None}
                        rec.update({"status": "cmd_error"} if err else analyse_step(ts, sig[:, 0], t_send, run.offset))
                        run.log(rec, ts, sig[:, 0], t_send)
                        summary.setdefault(f"{test}/{event}/{mode}", []).append(rec)
                        if err:
                            link.close()
                            send_quiet(b, prep)
                            pause(SETTLE)
                            break
            finally:
                link.close()
        for key, recs in sorted(summary.items()):
            ok = [r["t50"] for r in recs if r.get("status") == "ok" and r.get("t50") is not None]
            acks = [r["ack"] for r in recs if not r["error"]]
            print(f"   {key:<26} t50 {fmt_med(ok):>7}  ack {fmt_med(acks):>7}   ok {len(ok)}/{len(recs)}")
        send_quiet(b, ("power", False))
        run.save()


def lamp_spreads(per_bulb, bulbs, lamps, key="t50"):
    """{lamp: latest - earliest bulb}, plus 'between lamps' (lamp medians) and 'all bulbs'."""
    by_id = {b.id: per_bulb.get(b.name, {}) for b in bulbs}
    out, medians, every = {}, [], []
    for lamp in lamps:
        xs = [by_id[i][key] for i in lamp["bulbs"]
              if i in by_id and by_id[i].get("status") == "ok" and by_id[i].get(key) is not None]
        every += xs
        if len(xs) >= 2:
            out[lamp["name"]] = max(xs) - min(xs)
        if xs:
            medians.append(statistics.median(xs))
    if len(medians) >= 2:
        out["between lamps"] = round(max(medians) - min(medians))
    if len(every) >= 2:
        out["all bulbs"] = max(every) - min(every)
    return out


def run_burst(run, bulbs, lamps, args):
    print("\nAll bulbs at the same moment (every bulb timed separately)")
    unmix = run.unmixer(bulbs)
    lamp_name = lamp_of(lamps)
    blocks = [(test, mode) for test in BURST_TESTS for mode in args.modes
              for _ in range(max(1, args.reps // PAIRS_PER_BLOCK))]
    random.shuffle(blocks)
    pool = ThreadPoolExecutor(max_workers=len(bulbs))
    for n, (test, mode) in enumerate(blocks, 1):
        prep, pair = STEP_TESTS[test]
        links = [Link(b, persistent=(mode == "persistent")) for b in bulbs]
        try:
            list(pool.map(lambda lk: lk.send(prep), links))
            pause(SETTLE + 0.5)
            for _ in range(PAIRS_PER_BLOCK):
                for event, command in pair:
                    pause(random.uniform(0.1, 0.6))
                    t_go, results = fire_together(links, command, pool)
                    t_cap = max(t_go + POST, max(r[1] for r in results) + AFTER_ACK)
                    failed = {j for j, r in enumerate(results) if r[2]}
                    ts, sig = run.watch(t_go, t_cap, unmix,
                                        lambda t, s, t0=t_go: settled(t, s, t0, skip=failed))
                    per = {b.name: analyse_step(ts, sig[:, j], t_go, run.offset) for j, b in enumerate(bulbs)}
                    for b, r in zip(bulbs, results):
                        per[b.name]["lamp"] = lamp_name.get(b.id)
                        per[b.name]["ack"] = round((r[1] - t_go) * 1000)
                        per[b.name]["sent"] = round((r[0] - t_go) * 1000)
                        if r[2]:
                            per[b.name].update(status="cmd_error", error=r[2])
                    spreads = lamp_spreads(per, bulbs, lamps)
                    rec = {"phase": "burst", "test": test, "event": event, "mode": mode,
                           "bulbs": per, "spreads": spreads,
                           "watched": round((ts[-1] - t_go) * 1000) if len(ts) else None}
                    run.log(rec, ts, sig, t_go)
                    errs = sum(1 for v in per.values() if v["status"] == "cmd_error")
                    print(f"   {n:>2}/{len(blocks)} {test}/{event}/{mode:<10} spread ms: "
                          + ", ".join(f"{k} {v}" for k, v in spreads.items())
                          + (f"   errors: {errs}" if errs else ""))
        finally:
            for lk in links:
                lk.close()
    run.save()


def quiet_call(fn, bulb, tries=2):
    for _ in range(tries):
        try:
            fn(bulb)
            return True
        except Exception:
            pause(0.3)
    return False


PLAYER_TESTS = {      # like STEP_TESTS, but as the choreography player sends them
    "power":  (lambda b: b.set_white(100), {"action": "on"},
               [("off", lambda b: b.set_power(False)), ("on", lambda b: b.set_power(True))]),
    "colour": (lambda b: b.set_colour(RED, 100), {"action": "colour", "cut": False},
               [("to_blue", lambda b: b.set_colour(BLUE, 100)), ("to_red", lambda b: b.set_colour(RED, 100))]),
    "cut":    (lambda b: b.set_colour(RED, 100), {"action": "colour", "cut": True},
               [("to_blue", lambda b: b.cut_colour(BLUE, 100)), ("to_red", lambda b: b.cut_colour(RED, 100))]),
}


def run_player(run, bulbs, lamps, args):
    """All bulbs together, sent exactly like a choreography step (lights.Bulb with its kept-open
    connection, cuts without waiting). Alternates with and without the per-bulb delays from
    latency/offsets.json, so the summary shows what the compensation gains."""
    import choreo
    delays = choreo.load_delays()
    if not delays:
        print("\n! latency/offsets.json missing - run 'analyze' on a full run first. Player test skipped.")
        return
    print("\nAll bulbs at once, sent like the choreography player (mode 'player' = no compensation, "
          "'player+delay' = with per-bulb delays)")
    unmix = run.unmixer(bulbs)
    lamp_name = lamp_of(lamps)
    pool = ThreadPoolExecutor(max_workers=len(bulbs))
    blocks = [(test, comp) for test in PLAYER_TESTS for comp in (False, True)
              for _ in range(max(1, args.reps // PAIRS_PER_BLOCK))]
    random.shuffle(blocks)
    try:
        for n, (test, comp) in enumerate(blocks, 1):
            prep, step, pair = PLAYER_TESTS[test]
            mode = "player+delay" if comp else "player"
            hold = choreo.send_ahead(delays, step, bulbs) if comp else {b.id: 0.0 for b in bulbs}
            list(pool.map(lambda b: quiet_call(prep, b), bulbs))
            pause(SETTLE + 0.5)
            for _ in range(PAIRS_PER_BLOCK):
                for event, action in pair:
                    pause(random.uniform(0.1, 0.6))
                    go = threading.Barrier(len(bulbs) + 1)

                    def fire(b, action=action):
                        go.wait()
                        t_at = t_go_box[0] + hold[b.id]
                        while now() < t_at:
                            time.sleep(0.001)
                        t0 = now()
                        try:
                            action(b)
                            err = None
                        except Exception as e:
                            err = str(e) or type(e).__name__
                        return t0, now(), err
                    t_go_box = [now() + 0.05]
                    futures = [pool.submit(fire, b) for b in bulbs]
                    go.wait()
                    t_go = t_go_box[0]
                    results = [f.result() for f in futures]
                    t_cap = max(t_go + POST, max(r[1] for r in results) + AFTER_ACK)
                    failed = {j for j, r in enumerate(results) if r[2]}
                    ts, sig = run.watch(t_go, t_cap, unmix,
                                        lambda t, s, t0=t_go: settled(t, s, t0, skip=failed))
                    per = {b.name: analyse_step(ts, sig[:, j], t_go, run.offset) for j, b in enumerate(bulbs)}
                    for b, r in zip(bulbs, results):
                        per[b.name]["lamp"] = lamp_name.get(b.id)
                        per[b.name]["ack"] = round((r[1] - t_go) * 1000)
                        per[b.name]["sent"] = round((r[0] - t_go) * 1000)
                        if r[2]:
                            per[b.name].update(status="cmd_error", error=r[2])
                    spreads = lamp_spreads(per, bulbs, lamps)
                    rec = {"phase": "burst", "test": test, "event": event, "mode": mode,
                           "bulbs": per, "spreads": spreads,
                           "watched": round((ts[-1] - t_go) * 1000) if len(ts) else None}
                    run.log(rec, ts, sig, t_go)
                    errs = len(failed)
                    print(f"   {n:>2}/{len(blocks)} {test}/{event}/{mode:<12} spread ms: "
                          + ", ".join(f"{k} {v}" for k, v in spreads.items())
                          + (f"   errors: {errs}" if errs else ""))
    finally:
        for b in bulbs:
            b.close()
    run.save()


def run_effects(run, bulbs, everything, lamp_name, args):
    print("\nBuilt-in effects, one bulb at a time")
    for b in bulbs:
        all_bulbs(everything, ("power", False))
        for effect in EFFECTS:
            colours = [RED] if effect == "breathe" else [RED, BLUE]
            for speed in SPEEDS:
                send_quiet(b, ("colour", GREEN))
                pause(SETTLE)
                t_send, t_ack, err = Link(b).send(("effect", (effect, colours, speed)))
                # stop as soon as enough colour steps were seen (slow speeds use the whole window)
                ts, sig = run.watch(
                    t_send, now() + 0.2 if err else t_send + EFFECT_SECONDS[speed],
                    run.mask_signal([run.idx[b.id]]),
                    lambda t, s, t0=t_send: len(effect_crossings(t, s[:, 0], t0)[1]) >= EFFECT_CROSSINGS,
                    every=0.3)
                rec = {"phase": "effects", "bulb": b.name, "id": b.id, "model": model_of(b),
                       "lamp": lamp_name.get(b.id), "effect": effect, "speed": speed,
                       "ack": round((t_ack - t_send) * 1000), "error": err,
                       "watched": round((ts[-1] - t_send) * 1000) if len(ts) else None}
                rec.update({"status": "cmd_error"} if err else analyse_effect(ts, sig[:, 0], t_send, run.offset))
                run.log(rec, ts, sig[:, 0], t_send)
                print(f"   {b.name:<12} {effect:<9} speed {speed:>3}: start {rec.get('start', '-'):>5} ms, "
                      f"step {rec.get('step', '-'):>5} ms  ({rec['status']})")
        send_quiet(b, ("power", False))
        run.save()


def run_effect_sync(run, bulbs, lamps, args):
    print(f"\nSame effect on all bulbs at once, {SYNC_SECONDS} s each")
    unmix = run.unmixer(bulbs)
    lamp_name = lamp_of(lamps)
    pool = ThreadPoolExecutor(max_workers=len(bulbs))
    links = [Link(b) for b in bulbs]
    for rep in range(SYNC_REPS):
        for effect, speed in SYNC_EFFECTS:
            colours = [RED] if effect == "breathe" else [RED, BLUE]
            all_bulbs(bulbs, ("colour", GREEN))
            pause(SETTLE + 0.5)
            t_go, results = fire_together(links, ("effect", (effect, colours, speed)), pool)
            run.cam.wait_past(t_go + SYNC_SECONDS)
            ts, fs = run.cam.frames(t_go - PRE - 0.1, t_go + SYNC_SECONDS)
            sig = unmix(fs)
            per = {}
            for j, (b, r) in enumerate(zip(bulbs, results)):
                d = {"lamp": lamp_name.get(b.id), "ack": round((r[1] - t_go) * 1000), "error": r[2]}
                t_on, cross, _ = effect_crossings(ts, sig[:, j], t_go)
                if r[2]:
                    d["status"] = "cmd_error"
                elif t_on is None:
                    d["status"] = "no_change"
                else:
                    d["start"] = round((t_on - t_go - run.offset) * 1000)
                    fit = fit_steps(cross, t_go, run.offset)
                    d.update(fit or {})
                    d["status"] = "ok" if fit else "too_few_steps"
                per[b.name] = d
            spreads = lamp_spreads(per, bulbs, lamps, "phase")
            rec = {"phase": "effectsync", "effect": effect, "speed": speed, "rep": rep + 1,
                   "bulbs": per, "spreads": spreads}
            run.log(rec, ts, sig, t_go)
            steps = [v["step"] for v in per.values() if v.get("status") == "ok"]
            drift = (max(steps) - min(steps)) / statistics.median(steps) * 60000 if len(steps) >= 2 else None
            print(f"   {effect:<9} speed {speed:>3}: start spread "
                  + ", ".join(f"{k} {v}" for k, v in spreads.items())
                  + (f";  drift up to {drift:.0f} ms/min" if drift is not None else ""))
    all_bulbs(bulbs, ("power", False))
    run.save()


def fmt_med(xs):
    return f"{statistics.median(xs):.0f}" if xs else "-"


def cmd_run(args):
    check_server()
    if not CALIB_FILE.exists():
        sys.exit("Run 'python latency_cam.py calibrate' first.")
    calib = json.loads(CALIB_FILE.read_text(encoding="utf-8"))
    unmix_data = load_unmix_data()
    data = np.load(MASK_FILE)
    masks, ids = data["masks"], [str(i) for i in data["ids"]]
    idx = {i: n for n, i in enumerate(ids)}
    seen = {x["id"] for x in calib["bulbs"] if x["visible"]}

    named = load_named_bulbs()
    lamps = load_lamps(named, args.lamps)
    lamp_name = lamp_of(lamps)
    everything = sorted(named.values(), key=lambda b: (lamp_name.get(b.id, ""), b.name))
    wanted = [w.strip().lower() for w in (args.bulbs or "").split(",") if w.strip()]
    chosen = [b for b in everything if b.id in seen and b.id in idx
              and (not wanted or any(w in b.name.lower() for w in wanted))]
    skipped = [b.name for b in everything if b.id not in seen]
    if skipped:
        print("Not visible in the calibration, skipped: " + ", ".join(skipped))
    if not chosen:
        sys.exit("No bulbs to test.")
    print("Lamps: " + "; ".join(f"{lp['name']}: " + ", ".join(named[i].name for i in lp["bulbs"])
                                 for lp in lamps))
    phases = [p.strip() for p in args.phases.split(",")]
    eff_bulbs = chosen
    if args.effect_bulbs != "all":
        eff_bulbs = []
        for lamp in lamps:
            first = next((b for b in chosen if b.id in lamp["bulbs"]), None)
            if first:
                eff_bulbs.append(first)
    minutes = 0
    if "steps" in phases:
        n_events = len(STEP_TESTS) * len(args.modes) * max(1, args.reps // PAIRS_PER_BLOCK) * PAIRS_PER_BLOCK * 2
        minutes += len(chosen) * (n_events * 2.0 + n_events / 4 * SETTLE) / 60
    if "burst" in phases:
        minutes += len(BURST_TESTS) * len(args.modes) * max(1, args.reps // PAIRS_PER_BLOCK) * 12 / 60
    if "effects" in phases:
        minutes += len(eff_bulbs) * len(EFFECTS) * sum(EFFECT_SECONDS[s] + 3 for s in SPEEDS) / 60
    if "player" in phases:
        minutes += len(PLAYER_TESTS) * 2 * max(1, args.reps // PAIRS_PER_BLOCK) * 10 / 60
    if "effectsync" in phases:
        minutes += SYNC_REPS * len(SYNC_EFFECTS) * (SYNC_SECONDS + 5) / 60
    print(f"Testing {len(chosen)} bulbs, phases {', '.join(phases)}: roughly {minutes:.0f} minutes. "
          "Ctrl+C stops (the data so far is kept).")
    countdown(ask_delay(args.delay, SUGGESTED_DELAY[args.cmd]))

    states = save_states(everything)
    cam = Camera(calib.get("camera", 0), calib["exposure"])
    pause(1.0)
    fps = cam.fps()
    print(f"Camera: {fps:.1f} frames/s, exposure {calib['exposure']}, "
          f"camera delay {calib['camera_offset_ms']} ms subtracted")
    if fps < 20:
        print("   ! low frame rate - timing will be coarser")
    run = Run(cam, masks, idx, calib["camera_offset_ms"] / 1000, unmix_data)
    (run.dir / "calibration.json").write_text(json.dumps({**calib, "lamps": lamps}, indent=2), encoding="utf-8")
    try:
        all_bulbs(everything, ("power", False))
        pause(2)
        if "steps" in phases:
            run_steps(run, chosen, everything, lamp_name, args)
        if "burst" in phases:
            run_burst(run, chosen, lamps, args)
        if "effects" in phases:
            run_effects(run, eff_bulbs, everything, lamp_name, args)
        if "effectsync" in phases:
            all_bulbs(everything, ("power", False))
            run_effect_sync(run, chosen, lamps, args)
        if "player" in phases:
            all_bulbs(everything, ("power", False))
            run_player(run, chosen, lamps, args)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        run.save()
        run.events.close()
        cam.close()
        restore_states(everything, states)
    print(f"\nData in {run.dir.relative_to(HERE)}")
    write_summary()


# ---------- summary ----------

def pct(xs, q):
    return float(np.percentile(xs, q)) if xs else None


def stat_cols(xs):
    if not xs:
        return f"{'-':>6}{'-':>6}{'-':>6}"
    return f"{statistics.median(xs):6.0f}{pct(xs, 10):6.0f}{pct(xs, 90):6.0f}"


def med_range(xs):
    """'median (p10-p90)'"""
    if not xs:
        return "-"
    return f"{statistics.median(xs):.0f} ({pct(xs, 10):.0f}-{pct(xs, 90):.0f})"


def load_events(dirs):
    events = []
    for d in dirs:
        f = d / "events.jsonl"
        if f.exists():
            events += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return events


def write_offsets(ev, dirs):
    """latency/offsets.json: per bulb and kind of command, the median ms until the light is half-way
    (kept-open connection). The choreography player uses it to line the bulbs up.
    Prefers the all-bulbs-at-once measurements (closest to a choreography), else single-bulb ones."""
    ids, names = {}, {}
    for e in ev:
        if e.get("id"):
            ids[e["bulb"]] = e["id"]
            names[e["id"]] = e["bulb"]
    burst, single = {}, {}
    for e in ev:
        if e.get("mode") != "persistent":
            continue
        if e["phase"] == "steps" and e.get("status") == "ok" and e.get("t50") is not None:
            single.setdefault((e["id"], e["test"]), []).append(e["t50"])
        elif e["phase"] == "burst":
            for name, v in e["bulbs"].items():
                if name in ids and v.get("status") == "ok" and v.get("t50") is not None:
                    burst.setdefault((ids[name], e["test"]), []).append(v["t50"])
    out = {}
    for key in set(burst) | set(single):
        xs = burst.get(key) if len(burst.get(key, [])) >= 3 else single.get(key, [])
        if len(xs) >= 3:
            out.setdefault(key[0], {"name": names.get(key[0], "")})[key[1]] = round(statistics.median(xs))
    data = {"note": "ms from sending a command until the light is half-way, kept-open connection. "
                    "Written by latency_cam.py; includes the camera delay, which is the same for all bulbs.",
            "runs": [d.name for d in dirs], "bulbs": out}
    (OUT / "offsets.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def even_bulbs(e):
    """Bulbs of an effectsync event whose colour steps were evenly spaced (a reliable fit)."""
    return {b: v for b, v in e["bulbs"].items()
            if v.get("status") == "ok" and v.get("resid") is not None and v["resid"] < 0.15 * v["step"]}


def sync_spreads(e):
    """Phase spreads of one effectsync event. Phases are compared modulo one step: a bulb whose first
    step was missed otherwise looks a whole step late. (Ambiguous when bulbs really differ by ~1/2 step.)"""
    ok = even_bulbs(e)
    if len(ok) < 2:
        return {}
    step = statistics.median(v["step"] for v in ok.values())
    ref = statistics.median(v["phase"] for v in ok.values())
    ph = {b: ref + (v["phase"] - ref + step / 2) % step - step / 2 for b, v in ok.items()}
    by_lamp = {}
    for b, p in ph.items():
        by_lamp.setdefault(ok[b].get("lamp") or "", []).append(p)
    out = {lamp: round(max(xs) - min(xs)) for lamp, xs in by_lamp.items() if len(xs) >= 2}
    if len(by_lamp) >= 2:
        meds = [statistics.median(xs) for xs in by_lamp.values()]
        out["between lamps"] = round(max(meds) - min(meds))
    out["all bulbs"] = round(max(ph.values()) - min(ph.values()))
    return out


def write_summary(dirs=None):
    dirs = dirs or sorted(OUT.glob("run_*"))
    ev = load_events(dirs)
    if not ev:
        print("No data yet.")
        return
    lamps = []
    for d in reversed(dirs):                      # lamps of the newest run
        f = d / "calibration.json"
        if f.exists():
            lamps = json.loads(f.read_text(encoding="utf-8")).get("lamps") or []
            if lamps:
                break
    lines = [f"Bulb latency summary ({datetime.now():%Y-%m-%d %H:%M}), runs: "
             + ", ".join(d.name for d in dirs),
             "Times in ms from sending the command: median, 10th and 90th percentile.",
             "t10 = first visible change, t50 = half way, t90 = almost done, ack = bulb confirmed.",
             "Lamps: " + "; ".join(lp["name"] for lp in lamps), ""]

    steps = [e for e in ev if e["phase"] == "steps"]
    if steps:
        lines.append("STEP COMMANDS, one bulb at a time, per model")
        head = (f"{'model':<24}{'command':<18}{'conn':<11}{'n':>4}{'fail':>5}"
                f"{'t10':>6}{'p10':>6}{'p90':>6}{'t50':>8}{'p10':>6}{'p90':>6}{'t90':>8}{'p10':>6}{'p90':>6}"
                f"{'ack':>8}{'p10':>6}{'p90':>6}")
        lines += [head, "-" * len(head)]
        groups = {}
        for e in steps:
            groups.setdefault((e["model"], f"{e['test']}/{e['event']}", e["mode"]), []).append(e)
        for (model, cmd, mode), es in sorted(groups.items()):
            ok = [e for e in es if e.get("status") == "ok"]
            col = lambda k: [e[k] for e in ok if e.get(k) is not None]  # noqa: E731
            lines.append(f"{model:<24}{cmd:<18}{mode:<11}{len(es):>4}{len(es) - len(ok):>5}"
                         f"{stat_cols(col('t10'))}  {stat_cols(col('t50'))}  {stat_cols(col('t90'))}  "
                         f"{stat_cols([e['ack'] for e in es if not e.get('error')])}")
        fails = {}
        for e in steps:
            if e.get("status") != "ok":
                fails[e["status"]] = fails.get(e["status"], 0) + 1
        if fails:
            lines.append("not counted: " + ", ".join(f"{k} {v}" for k, v in fails.items())
                         + "  (cmd_error = no answer, no_change/no_crossing = nothing seen, unstable = noisy baseline)")

        # per bulb: what to compensate (median) and what can't be compensated (spread)
        cmds = sorted({f"{e['test']}/{e['event']}" for e in steps})
        for mode in sorted({e["mode"] for e in steps}):
            lines += ["", f"Per bulb, connection '{mode}': t50 median per command; "
                      "jitter = typical p90-p10 of a bulb's t50 (can't be compensated)"]
            head = f"   {'lamp':<12}{'bulb':<12}" + "".join(f"{c[:14]:>15}" for c in cmds) + f"{'jitter':>8}{'err':>6}"
            lines.append(head)
            bulbs = sorted({(e.get("lamp") or "", e["bulb"]) for e in steps if e["mode"] == mode})
            for lamp, bulb in bulbs:
                es = [e for e in steps if e["bulb"] == bulb and e["mode"] == mode]
                row, jit = f"   {lamp:<12}{bulb:<12}", []
                for c in cmds:
                    xs = [e["t50"] for e in es if f"{e['test']}/{e['event']}" == c
                          and e.get("status") == "ok" and e.get("t50") is not None]
                    row += f"{fmt_med(xs):>15}"
                    if len(xs) >= 3:
                        jit.append(pct(xs, 90) - pct(xs, 10))
                row += f"{fmt_med(jit):>8}{sum(1 for e in es if e.get('error')):>6}"
                lines.append(row)
        lines.append("")

    burst = [e for e in ev if e["phase"] == "burst"]
    if burst:
        lines.append("ALL BULBS AT THE SAME MOMENT")
        lines.append("Spread = latest bulb - earliest bulb (t50), per event: median (p10-p90) over the repetitions")
        groups = {}
        for e in burst:
            groups.setdefault((f"{e['test']}/{e['event']}", e["mode"]), []).append(e)
        for (cmd, mode), es in sorted(groups.items()):
            keys = []
            for e in es:
                keys += [k for k in e["spreads"] if k not in keys]
            errs = sum(1 for e in es for v in e["bulbs"].values() if v.get("status") == "cmd_error")
            lines.append(f"   {cmd:<16}{mode:<11}" + "   ".join(
                f"{k}: {med_range([e['spreads'][k] for e in es if k in e['spreads']])}" for k in keys)
                + f"   errors {errs}")
        lines.append("")
        lines.append("Per bulb when all fire together: t50 median (p10-p90), ms")
        bulbs = sorted({b for e in burst for b in e["bulbs"]})
        lamp_by_bulb = {b: v.get("lamp") or "" for e in burst for b, v in e["bulbs"].items()}
        for (cmd, mode), es in sorted(groups.items()):
            lines.append(f"   {cmd} / {mode}")
            for b in sorted(bulbs, key=lambda x: (lamp_by_bulb.get(x, ""), x)):
                xs = [e["bulbs"][b]["t50"] for e in es if e["bulbs"].get(b, {}).get("status") == "ok"
                      and e["bulbs"][b].get("t50") is not None]
                acks = [e["bulbs"][b]["ack"] for e in es if b in e["bulbs"]]
                lines.append(f"      {lamp_by_bulb.get(b, ''):<12}{b:<12} t50 {med_range(xs):<20} ack {fmt_med(acks)}")
        lines.append("")

    eff = [e for e in ev if e["phase"] == "effects"]
    if eff:
        lines.append("BUILT-IN EFFECTS, single bulb (start = until the effect visibly starts; step = one colour step, ms)")
        head = f"{'model':<24}{'effect':<10}{'speed':>6}{'n':>4}{'start':>8}{'step':>8}{'spread':>8}  status"
        lines += [head, "-" * len(head)]
        groups = {}
        for e in eff:
            groups.setdefault((e["model"], e["effect"], e["speed"]), []).append(e)
        for (model, effect, speed), es in sorted(groups.items()):
            col = lambda k: [e[k] for e in es if e.get(k) is not None]  # noqa: E731
            lines.append(f"{model:<24}{effect:<10}{speed:>6}{len(es):>4}{fmt_med(col('start')):>8}"
                         f"{fmt_med(col('step')):>8}{fmt_med(col('step_spread')):>8}  "
                         + ",".join(sorted({e['status'] for e in es})))
        lines.append("")

    sync = [e for e in ev if e["phase"] == "effectsync"]
    if sync:
        lines.append("SAME EFFECT ON ALL BULBS AT ONCE")
        lines.append("phase spread = latest - earliest colour step (ms), compared modulo one step so a missed "
                     "first step doesn't count;")
        lines.append("drift = how far the slowest and fastest bulb move apart per minute. Only bulbs with "
                     "evenly spaced steps are used.")
        groups = {}
        for e in sync:
            groups.setdefault((e["effect"], e["speed"]), []).append(e)
        for (effect, speed), es in sorted(groups.items()):
            spreads = [sync_spreads(e) for e in es]
            keys = []
            for s in spreads:
                keys += [k for k in s if k not in keys]
            used = sum(len(even_bulbs(e)) for e in es)
            total = sum(len(e["bulbs"]) for e in es)
            lines.append(f"   {effect} speed {speed}: " + "   ".join(
                f"{k}: {med_range([s[k] for s in spreads if k in s])}" for k in keys)
                + f"   ({used}/{total} bulb runs usable)")
            per_bulb, lamp_by_bulb = {}, {}
            for e in es:
                for b, v in e["bulbs"].items():
                    lamp_by_bulb[b] = v.get("lamp") or ""
                for b, v in even_bulbs(e).items():
                    per_bulb.setdefault(b, []).append(v)
            all_steps = [v["step"] for vs in per_bulb.values() for v in vs]
            if all_steps:
                ref = statistics.median(all_steps)
                for lamp in sorted({lamp_by_bulb[b] for b in per_bulb}):
                    names = [b for b in per_bulb if lamp_by_bulb[b] == lamp]
                    st = [statistics.median([v["step"] for v in per_bulb[b]]) for b in names]
                    lines.append(f"      {lamp:<12} step {min(st):.0f}-{max(st):.0f} ms, "
                                 f"drift vs. all-bulb median up to "
                                 f"{max(abs(s - ref) for s in st) / ref * 60000:.0f} ms/min")
                drift = (max(all_steps) - min(all_steps)) / ref * 60000
                lines.append(f"      all bulbs    step median {ref:.0f} ms, fastest vs slowest {drift:.0f} ms/min")
        lines.append("")

    write_offsets(ev, dirs)
    text = "\n".join(lines)
    (OUT / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print("\n" + text)
    print(f"\nWritten to {(OUT / 'summary.txt').relative_to(HERE)}")


# ---------- preview ----------

def cmd_preview(args):
    """Live picture for aiming the camera.
    Keys: + / - exposure, l = all bulbs on/off, o = outlines of the calibrated bulbs, q = close."""
    exposure = args.exposure if args.exposure is not None else -5   # -5 = brightest at 30 frames/s
    cam = Camera(args.camera, exposure)
    outlines = []
    if MASK_FILE.exists():
        data = np.load(MASK_FILE)
        for m in data["masks"]:
            m = cv2.resize(m.reshape(SMALL[1], SMALL[0]).astype(np.float32), (640, 480))
            if m.max() > 0:
                found, _ = cv2.findContours((m > 0.25 * m.max()).astype(np.uint8),
                                            cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                outlines += list(found)
    show_outlines, lights_on, bulbs, states = False, False, None, None
    print("Preview: + / - exposure, l = all bulbs on/off, o = bulb outlines from the last calibration, "
          "q = close")
    try:
        while True:
            frame = cam.full.copy()
            if show_outlines:
                cv2.drawContours(frame, outlines, -1, (0, 0, 255), 1)
            text = (f"{cam.fps():.0f} fps   exposure {cam.exposure:g} (+/-)   l: bulbs "
                    f"{'on' if lights_on else 'off'}   o: old outlines   q: close")
            cv2.putText(frame, text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(frame, text, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1, cv2.LINE_AA)
            cv2.imshow("latency preview", frame)
            key = cv2.waitKey(30) & 0xFF
            if key in (ord("q"), 27):
                break
            if key in (ord("+"), ord("=")) and cam.exposure < -1:
                cam.set_exposure(cam.exposure + 1)
            elif key in (ord("-"), ord("_")) and cam.exposure > -13:
                cam.set_exposure(cam.exposure - 1)
            elif key == ord("o"):
                show_outlines = not show_outlines
                if not outlines:
                    print("   no calibration yet - nothing to outline")
            elif key == ord("l"):
                if bulbs is None:
                    check_server()
                    bulbs = list(load_named_bulbs().values())
                    states = save_states(bulbs)
                lights_on = not lights_on
                all_bulbs(bulbs, ("white", 100) if lights_on else ("power", False))
    finally:
        cv2.destroyAllWindows()
        cam.close()
        if states is not None:
            restore_states(bulbs, states)


def main():
    ap = argparse.ArgumentParser(description="Bulb latency measurement with the laptop camera")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preview")
    p.add_argument("--camera", type=int, default=0)
    p.add_argument("--exposure", type=float)
    p = sub.add_parser("calibrate")
    p.add_argument("--camera", type=int, default=0)
    p.add_argument("--exposure", type=float, help="fixed exposure (e.g. -7) instead of choosing one")
    p.add_argument("--no-screen", action="store_true", help="skip the screen-flash camera delay test")
    p.add_argument("--lamps", help="group names that are lamps, e.g. 'Stehlampe,Decke,Diele'")
    p.add_argument("--delay", type=int, help="seconds to wait before starting (default: asks, suggests 60)")
    p = sub.add_parser("run")
    p.add_argument("--delay", type=int, help="seconds to wait before starting (default: asks, suggests 300)")
    p.add_argument("--phases", default="steps,burst,effects,effectsync")
    p.add_argument("--bulbs", help="comma separated name parts")
    p.add_argument("--lamps", help="group names that are lamps, e.g. 'Stehlampe,Decke,Diele'")
    p.add_argument("--reps", type=int, default=6)
    p.add_argument("--modes", default="new,persistent", type=lambda s: [m.strip() for m in s.split(",")])
    p.add_argument("--effect-bulbs", default="lamp", choices=("lamp", "all"))
    sub.add_parser("analyze")
    argv = sys.argv[1:]
    if argv and argv[0].startswith("--") and argv[0] not in ("--help",):
        argv = ["run"] + argv              # "latency_cam.py --phases player" means run
    args = ap.parse_args(argv)
    if args.cmd == "run" and any(m not in ("new", "persistent") for m in args.modes):
        sys.exit("--modes: use new and/or persistent")
    {"preview": cmd_preview, "calibrate": cmd_calibrate, "run": cmd_run,
     "analyze": lambda a: write_summary()}[args.cmd](args)


if __name__ == "__main__":
    main()
