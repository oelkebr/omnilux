# Lights

A small web app to control your Tuya / Smart Life bulbs over the local network.

## Folder layout

```
lights/
├── app.py            # web server (FastAPI)
├── lights.py         # talks to the bulbs (TinyTuya)
├── static/index.html # the control page
├── devices.json      # from `python -m tinytuya wizard` (contains your keys!)
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
- Refreshes every 10 seconds, works on phone and desktop, light and dark mode

## API (for your own scripts)

| Method | Path | Body |
|---|---|---|
| GET  | `/api/bulbs` | – (status of all bulbs) |
| POST | `/api/bulbs/{id}/power` | `{"on": true}` |
| POST | `/api/bulbs/{id}/white` | `{"brightness": 60, "temp": 30}` (0–100, both optional) |
| POST | `/api/bulbs/{id}/colour` | `{"colour": "#ff8800", "brightness": 80}` |
| POST | `/api/all/power` | `{"on": false}` |

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
