"""
Stores your own settings in settings.json next to this file:
custom bulb names, groups, and the scenes (snapshots) saved for each group.

{
  "names":  {"<bulb id>": "My name", ...},
  "groups": [
    {"id": "a1b2c3d4", "name": "Living room", "members": ["<bulb id>", ...],
     "scenes": [
       {"id": "e5f6a7b8", "name": "Movie night",
        "states": {"<bulb id>": {"on": true, "mode": "colour", "colour": "#ff8800",
                                 "brightness": 30, "temp": 0}, ...}}
     ]}
  ]
}
"""

import json
import os
import threading
import uuid
from pathlib import Path

SETTINGS_FILE = Path(__file__).parent / "settings.json"


def _new_id():
    return uuid.uuid4().hex[:8]


class Store:
    def __init__(self, path=SETTINGS_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = {"names": {}, "groups": [], "choreos": None}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.data["names"] = loaded.get("names") or {}
                self.data["groups"] = loaded.get("groups") or []
                self.data["choreos"] = loaded.get("choreos")   # None = never set up
            except ValueError:
                # keep a copy of the broken file instead of silently losing it
                self.path.replace(self.path.with_suffix(".broken.json"))

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)   # atomic: never leaves a half-written file

    # ---------- names ----------

    def name_for(self, bulb_id):
        return self.data["names"].get(bulb_id)

    def set_name(self, bulb_id, name):
        with self._lock:
            if name:
                self.data["names"][bulb_id] = name
            else:
                self.data["names"].pop(bulb_id, None)
            self.save()

    # ---------- groups ----------

    @property
    def groups(self):
        return self.data["groups"]

    def group(self, group_id):
        return next((g for g in self.groups if g["id"] == group_id), None)

    def add_group(self, name, members):
        with self._lock:
            group = {"id": _new_id(), "name": name, "members": list(members), "scenes": []}
            self.groups.append(group)
            self.save()
            return group

    def update_group(self, group_id, name=None, members=None):
        with self._lock:
            group = self.group(group_id)
            if group is None:
                return None
            if name is not None:
                group["name"] = name
            if members is not None:
                group["members"] = list(members)
            self.save()
            return group

    def delete_group(self, group_id):
        with self._lock:
            before = len(self.groups)
            self.data["groups"] = [g for g in self.groups if g["id"] != group_id]
            self.save()
            return len(self.groups) < before

    # ---------- scenes ----------

    def scene(self, group_id, scene_id):
        group = self.group(group_id)
        if group is None:
            return None, None
        return group, next((s for s in group["scenes"] if s["id"] == scene_id), None)

    def add_scene(self, group_id, name, states):
        with self._lock:
            group = self.group(group_id)
            if group is None:
                return None
            scene = {"id": _new_id(), "name": name, "states": states}
            group["scenes"].append(scene)
            self.save()
            return scene

    def update_scene(self, group_id, scene_id, name=None, states=None):
        with self._lock:
            _, scene = self.scene(group_id, scene_id)
            if scene is None:
                return None
            if name is not None:
                scene["name"] = name
            if states is not None:
                scene["states"] = states
            self.save()
            return scene

    def delete_scene(self, group_id, scene_id):
        with self._lock:
            group, scene = self.scene(group_id, scene_id)
            if scene is None:
                return False
            group["scenes"].remove(scene)
            self.save()
            return True

    # ---------- choreographies ----------

    def seed_choreos(self, examples):
        """Add the example choreographies the very first time (not again after you delete them)."""
        with self._lock:
            if self.data["choreos"] is None:
                self.data["choreos"] = [dict(c, id=_new_id()) for c in examples]
                self.save()

    @property
    def choreos(self):
        return self.data["choreos"] or []

    def choreo(self, choreo_id):
        return next((c for c in self.choreos if c["id"] == choreo_id), None)

    def add_choreo(self, choreo):
        with self._lock:
            choreo = dict(choreo, id=_new_id())
            self.data["choreos"] = self.choreos + [choreo]
            self.save()
            return choreo

    def update_choreo(self, choreo_id, choreo):
        with self._lock:
            for i, c in enumerate(self.choreos):
                if c["id"] == choreo_id:
                    self.data["choreos"][i] = dict(choreo, id=choreo_id)
                    self.save()
                    return self.data["choreos"][i]
            return None

    def delete_choreo(self, choreo_id):
        with self._lock:
            before = len(self.choreos)
            self.data["choreos"] = [c for c in self.choreos if c["id"] != choreo_id]
            self.save()
            return len(self.choreos) < before
