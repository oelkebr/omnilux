# Lights

A small web app to control your Tuya / Smart Life bulbs over the local network.

## Folder layout

```
lights/
├── app.py            # web server (FastAPI)
├── lights.py         # talks to the bulbs (TinyTuya)
├── store.py          # saves names, groups, scenes and choreographies
├── choreo.py         # plays choreographies (timeline engine)
├── effects_check.py  # lists which features/effects your bulbs support
├── effects_test.py   # interactive experiments with fades and effects
├── latency_cam.py    # measures bulb reaction times with the laptop camera
├── static/index.html # the control page
├── devices.json      # from `python -m tinytuya wizard` (contains your keys!)
├── settings.json     # created automatically: your names, groups and scenes
└── requirements.txt
```

## Start

In the VS Code terminal, inside the `lights` folder with `(.venv)` active:

```
uvicorn app:app --host 0.0.0.0 --port 8000
```

- On this PC: http://localhost:8000
- On your phone (same Wi-Fi): http://<your-PC-IP>:8000
  (find the IP with `ipconfig` → "IPv4-Adresse")

Stop the server with Ctrl+C.

While developing, add `--reload` so the server restarts when you save a file.

## Features

- On/off per bulb, plus "All on" / "All off"
- Brightness slider
- Warm–cold white slider (for bulbs that support it)
- White/Color mode, color swatches and a custom color picker (for RGB bulbs)
- Detects automatically which bulbs have color support
- **Rename bulbs** with the ✎ next to the name (empty name = back to the app name).
  The names are only used in this webapp; the Smart Life app keeps its own.
- **Groups**: "+ New group", pick the bulbs. A bulb can be in several groups.
  - The group's switch, brightness, white tone and colors act on all its bulbs.
    Brightness keeps each bulb's own color; colors skip white-only bulbs.
  - Groups are folded to one line (name, how many are on, switch). Tap the name
    or ▸ to unfold the controls and scenes; "Show bulbs" inside reveals the
    single bulbs to adjust them one by one.
  - Tap a section heading (Groups, Choreographies, Not in a group) to fold it.
    The page remembers what you folded, per browser.
  - ✎ on the group: rename, change bulbs, delete.
  - The list at the bottom ("Not in a group") only shows bulbs that are in no
    group. Bulbs from a deleted group, or removed from a group, show up there again.
- **Scenes** (snapshots per group): "+ Save current as scene" stores exactly how
  every bulb in the group looks right now: on/off, brightness, white tone or color.
  So you can set the whole group the same, or tune each bulb individually, then save.
  - Tap a scene to restore it.
  - ⋯ on a scene: overwrite it with the current state, rename, delete.
  - Bulbs that are offline while saving are not included (you get a notice).
- Refreshes every 10 seconds, works on phone and desktop, light and dark mode

- **Choreographies**: timelines of steps the server plays on your bulbs. They keep
  running when you close the page. Only one plays at a time; the header shows it
  with a Stop button. Five examples are added on first start (Sunset, Color flow,
  Wave, Party flash, Breathe blue) - edit or delete them freely.
  Each step has an action, a target (all lights, a group or one bulb), a
  **duration** (time until the next step) and an optional **delay between bulbs**
  (they change one after another = wave). Actions:
  - **Color**: color + brightness, with a **fade** in seconds (0 = the bulb's own
    quick soft fade) or a **hard cut** (instant change, for flashes).
  - **White**: brightness + warm/cold tone, with fade.
  - **Built-in effect**: up to 8 colors as smooth flow, jump or breathe (max 4
    colors), with speed. The bulbs play it by themselves and keep going after the
    step, until a later step changes them. If the choreography ends while an effect
    is still running, it stays "playing" ("effect running" in the header) until you
    press Stop; Stop ends the effect and returns those bulbs to how they were.
  - **Group scene**: applies one of a group's saved scenes.
  - **On / Off**.
  Options: **Loop** (repeat until Stop) and **Restore after** (when it ends or is
  stopped, every light goes back to how it was before).
  In the editor, ▶ on a step tries just that step, "▶ Try" plays the whole draft
  without saving.

Back up `settings.json` if you care about your groups and scenes. It's plain
JSON, so you can also edit it by hand while the server is stopped.

## Measuring bulb latency (laptop camera)

`latency_cam.py` measures how long each bulb takes to visibly react, so
choreographies can be timed. Stop the webapp first, make the room dark, keep the
laptop still and stay out of the picture.

```
python latency_cam.py preview      # aim the camera: +/- exposure, l bulbs on/off, q closes
python latency_cam.py calibrate    # finds each bulb in the picture, ~3 min
python latency_cam.py run          # the measurements, ~1 h (Ctrl+C keeps data so far)
python latency_cam.py analyze      # summary of all runs -> latency/summary.txt
```

Check `latency/calibration.png` after calibrating: one tile per bulb showing
where the camera sees its light. `run --bulbs "st,diele" --phases steps`
tests only some bulbs or parts. Lamps = your webapp groups (Stehlampe, Decke, Diele);
each bulb is timed separately even when all switch at once, so the summary shows the
spread within each lamp and between the lamps. Results stay in `latency/` (not in git).

The summary also writes `latency/offsets.json` (each bulb's delay per kind of
command). The choreography player reads it when a choreography starts and sends to
slow bulbs a little earlier, so all bulbs change together. Without the file every bulb
is sent at the same moment. `run --phases player` (~5 min) checks the result: all bulbs
switch together, sent exactly like a choreography, with and without the compensation.
If the camera was moved, run `calibrate` again first.

## API (for your own scripts)

| Method | Path | Body |
|---|---|---|
| GET  | `/api/bulbs` | – (status of all bulbs) |
| POST | `/api/bulbs/{id}/power` | `{"on": true}` |
| POST | `/api/bulbs/{id}/white` | `{"brightness": 60, "temp": 30}` (0–100, both optional) |
| POST | `/api/bulbs/{id}/colour` | `{"colour": "#ff8800", "brightness": 80}` |
| PUT  | `/api/bulbs/{id}/name` | `{"name": "Sofa"}` |
| POST | `/api/all/power` | `{"on": false}` |
| GET  | `/api/groups` | – (groups with their scenes) |
| POST | `/api/groups` | `{"name": "Living room", "members": ["id1", "id2"]}` |
| PUT / DELETE | `/api/groups/{gid}` | `{"name": ..., "members": [...]}` |
| POST | `/api/groups/{gid}/power` | `{"on": true}` |
| POST | `/api/groups/{gid}/brightness` | `{"brightness": 50}` |
| POST | `/api/groups/{gid}/white` | `{"brightness": 60, "temp": 30}` |
| POST | `/api/groups/{gid}/colour` | `{"colour": "#ff8800", "brightness": 80}` |
| POST | `/api/groups/{gid}/scenes` | `{"name": "Movie night"}` (saves current state) |
| POST | `/api/groups/{gid}/scenes/{sid}/apply` | – |
| PUT  | `/api/groups/{gid}/scenes/{sid}` | `{"name": "...", "recapture": true}` |
| DELETE | `/api/groups/{gid}/scenes/{sid}` | – |

| GET  | `/api/choreos` | – (all choreographies) |
| POST | `/api/choreos` | `{"name": "...", "loop": false, "restore": false, "steps": [...]}` |
| PUT / DELETE | `/api/choreos/{cid}` | same as POST |
| POST | `/api/choreos/{cid}/play` | – |
| POST | `/api/choreo/preview` | a choreography object (plays it without saving) |
| POST | `/api/choreo/stop` | – |
| GET  | `/api/choreo/status` | – (what's playing, current step, round) |

The step format is documented at the top of `choreo.py`.

Example: apply a scene from a Python script (e.g. on a schedule):

```python
import requests
requests.post("http://localhost:8000/api/groups/<gid>/scenes/<sid>/apply")
```
The group and scene ids are in `settings.json`.

Interactive API docs: http://localhost:8000/docs

## Troubleshooting

- **Phone can't open the page:** Windows Firewall blocks it. Allow Python on
  *private networks*, and make sure your Wi-Fi is set to "Private" in Windows.
- **Bulb shows "Offline":** it's switched off at the wall, or its IP changed.
  Give the bulbs fixed IPs in your router (FRITZ!Box: Heimnetz → Netzwerk →
  device → "Diesem Netzwerkgerät immer die gleiche IPv4-Adresse zuweisen"),
  or re-run `python -m tinytuya scan` and update devices.json.
- **Bulb re-paired / reset:** its local key changed. Re-run
  `python -m tinytuya wizard`.
