"""
Light control webapp.

Start (inside the activated .venv, in this folder):
    uvicorn app:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 on this PC,
or http://<this-PC's-IP>:8000 on your phone (same Wi-Fi).
"""

from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from lights import load_bulbs, run_parallel, snapshot
from store import Store

app = FastAPI(title="Lights")
BULBS = load_bulbs()
STORE = Store()
INDEX = Path(__file__).parent / "static" / "index.html"

# apply custom names from settings.json
for _bulb_id, _b in BULBS.items():
    _b.name = STORE.name_for(_bulb_id) or _b.original_name


# ---------- request bodies ----------

class Power(BaseModel):
    on: bool


class White(BaseModel):
    brightness: Optional[int] = Field(None, ge=0, le=100)
    temp: Optional[int] = Field(None, ge=0, le=100)


class Brightness(BaseModel):
    brightness: int = Field(ge=1, le=100)


class Colour(BaseModel):
    colour: str = Field(pattern=r"^#?[0-9a-fA-F]{6}$")
    brightness: int = Field(100, ge=1, le=100)


class Name(BaseModel):
    name: str = Field("", max_length=60)


class GroupIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    members: Optional[List[str]] = None


class SceneIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    recapture: bool = False     # True = overwrite the scene with the bulbs' current state


# ---------- helpers ----------

def _bulb(bulb_id):
    bulb = BULBS.get(bulb_id)
    if not bulb:
        raise HTTPException(404, "Unknown bulb")
    return bulb


def _group(group_id):
    group = STORE.group(group_id)
    if not group:
        raise HTTPException(404, "Unknown group")
    return group


def _members(group):
    return [BULBS[i] for i in group["members"] if i in BULBS]


def _do(action):
    try:
        action()
    except Exception as e:
        raise HTTPException(502, f"Bulb did not respond: {e}")
    return {"ok": True}


def _on_all(bulbs, action):
    """Run action(bulb) on several bulbs in parallel and report who failed."""
    def run(b):
        try:
            action(b)
            return None
        except Exception:
            return b.name
    failed = [name for name in run_parallel(run, bulbs) if name]
    return {"ok": not failed, "done": len(bulbs) - len(failed), "total": len(bulbs), "failed": failed}


def _clean_members(members):
    unknown = [m for m in members if m not in BULBS]
    if unknown:
        raise HTTPException(400, f"Unknown bulb ids: {', '.join(unknown)}")
    return list(dict.fromkeys(members))     # drop duplicates, keep order


def _capture(group):
    """Read the current state of every bulb in the group (for a scene)."""
    members = _members(group)
    statuses = run_parallel(lambda b: b.status(), members)
    states = {s["id"]: snapshot(s) for s in statuses if s.get("online")}
    missing = [s["name"] for s in statuses if not s.get("online")]
    if not states:
        raise HTTPException(409, "None of the group's bulbs responded - nothing to save")
    return states, missing


# ---------- page ----------

@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/state")
def full_state():
    """Everything the page needs: live bulb status plus groups and scenes."""
    return {"bulbs": run_parallel(lambda b: b.status(), BULBS.values()),
            "groups": STORE.groups}


# ---------- single bulbs ----------

@app.get("/api/bulbs")
def list_bulbs():
    return run_parallel(lambda b: b.status(), BULBS.values())


@app.get("/api/bulbs/{bulb_id}")
def get_bulb(bulb_id: str):
    return _bulb(bulb_id).status()


@app.post("/api/bulbs/{bulb_id}/power")
def set_power(bulb_id: str, body: Power):
    bulb = _bulb(bulb_id)
    return _do(lambda: bulb.set_power(body.on))


@app.post("/api/bulbs/{bulb_id}/white")
def set_white(bulb_id: str, body: White):
    bulb = _bulb(bulb_id)
    return _do(lambda: bulb.set_white(body.brightness, body.temp))


@app.post("/api/bulbs/{bulb_id}/colour")
def set_colour(bulb_id: str, body: Colour):
    bulb = _bulb(bulb_id)
    return _do(lambda: bulb.set_colour(body.colour, body.brightness))


@app.put("/api/bulbs/{bulb_id}/name")
def rename_bulb(bulb_id: str, body: Name):
    """Rename a bulb. An empty name restores the name from the Smart Life app."""
    bulb = _bulb(bulb_id)
    name = body.name.strip()
    STORE.set_name(bulb_id, name)
    bulb.name = name or bulb.original_name
    return {"ok": True, "name": bulb.name}


@app.post("/api/all/power")
def all_power(body: Power):
    return _on_all(list(BULBS.values()), lambda b: b.set_power(body.on))


# ---------- groups ----------

@app.get("/api/groups")
def list_groups():
    return STORE.groups


@app.post("/api/groups")
def create_group(body: GroupIn):
    if not body.name:
        raise HTTPException(400, "A group needs a name")
    return STORE.add_group(body.name.strip(), _clean_members(body.members or []))


@app.put("/api/groups/{group_id}")
def edit_group(group_id: str, body: GroupIn):
    _group(group_id)
    members = _clean_members(body.members) if body.members is not None else None
    return STORE.update_group(group_id, body.name.strip() if body.name else None, members)


@app.delete("/api/groups/{group_id}")
def delete_group(group_id: str):
    _group(group_id)
    STORE.delete_group(group_id)
    return {"ok": True}


@app.post("/api/groups/{group_id}/power")
def group_power(group_id: str, body: Power):
    return _on_all(_members(_group(group_id)), lambda b: b.set_power(body.on))


@app.post("/api/groups/{group_id}/brightness")
def group_brightness(group_id: str, body: Brightness):
    """Same brightness for all members; each bulb keeps its own colour / white mode."""
    return _on_all(_members(_group(group_id)), lambda b: b.set_brightness(body.brightness))


@app.post("/api/groups/{group_id}/white")
def group_white(group_id: str, body: White):
    return _on_all(_members(_group(group_id)), lambda b: b.set_white(body.brightness, body.temp))


@app.post("/api/groups/{group_id}/colour")
def group_colour(group_id: str, body: Colour):
    """Colour for all members that support it; white-only bulbs are left alone."""
    colour_bulbs = [b for b in _members(_group(group_id)) if b.has_colour]
    result = _on_all(colour_bulbs, lambda b: b.set_colour(body.colour, body.brightness))
    result["skipped"] = len(_members(_group(group_id))) - len(colour_bulbs)
    return result


# ---------- scenes (snapshots of a group) ----------

@app.post("/api/groups/{group_id}/scenes")
def save_scene(group_id: str, body: SceneIn):
    """Save how the group's bulbs look right now as a named scene."""
    group = _group(group_id)
    if not body.name:
        raise HTTPException(400, "A scene needs a name")
    states, missing = _capture(group)
    scene = STORE.add_scene(group_id, body.name.strip(), states)
    return {"scene": scene, "missing": missing}


@app.put("/api/groups/{group_id}/scenes/{scene_id}")
def update_scene(group_id: str, scene_id: str, body: SceneIn):
    """Rename a scene and/or overwrite it with the bulbs' current state."""
    group, scene = STORE.scene(group_id, scene_id)
    if not scene:
        raise HTTPException(404, "Unknown scene")
    states, missing = _capture(group) if body.recapture else (None, [])
    scene = STORE.update_scene(group_id, scene_id,
                               body.name.strip() if body.name else None, states)
    return {"scene": scene, "missing": missing}


@app.post("/api/groups/{group_id}/scenes/{scene_id}/apply")
def apply_scene(group_id: str, scene_id: str):
    group, scene = STORE.scene(group_id, scene_id)
    if not scene:
        raise HTTPException(404, "Unknown scene")
    # only bulbs that are still in the group and were saved in the scene
    targets = [b for b in _members(group) if b.id in scene["states"]]
    return _on_all(targets, lambda b: b.apply_state(scene["states"][b.id]))


@app.delete("/api/groups/{group_id}/scenes/{scene_id}")
def delete_scene(group_id: str, scene_id: str):
    if not STORE.delete_scene(group_id, scene_id):
        raise HTTPException(404, "Unknown scene")
    return {"ok": True}
