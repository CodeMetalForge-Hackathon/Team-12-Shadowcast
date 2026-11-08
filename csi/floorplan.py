"""Building floor plans for the room map: load a plan file and place it in room coordinates. Standard library only.

A plan file (config/floorplans/*.json, written by scripts/obj_to_floorplan.py) holds named groups of plan lines in
integer centimetres: plan x east, plan y south (north up on screen). A room config places the room on it with
``"floor_plan": {"file", "origin_m": [X0, Z0], "rotation_deg"}`` (csi/room.py FloorPlan): room (0, 0) sits at plan
point (X0, Z0) in metres, and the room's x and y run along the plan's +x and +y turned ``rotation_deg`` clockwise as
seen on the north-up plan. So a plan point P maps to room (x, y) = ((P - O) . u, (P - O) . v), u = (cos r, sin r),
v = (-sin r, cos r).

GET /api/floorplan (csi/server.py) serves ``room_plan``: the same groups in room metres (rounded to the centimetre),
with the plan's bounds in room coordinates:

    {"name", "source", "units": "m", "frame": "room", "placement": {"file", "origin_m", "rotation_deg"},
     "bounds": [x0, y0, x1, y1],
     "groups": {name: {"kind", "segments": [x0, y0, x1, y1, ...], "mesh"?: [...], "fills"?: [[x, y, ...], ...],
                       "height"?: [y0, y1], "bands"?: [{"height": [y0, y1], "segments": [...]}, ...],
                       "outline"?: [[x, y, ...], ...], "steps"?: [[h, x, y, ...], ...]}}}

The optional keys are what the page's solid 3D view extrudes (scripts/obj_to_floorplan.py says what each holds):
heights are metres above the floor (OBJ Y, unchanged by the placement); points move like the rest of the plan.

Public API:
    load_plan(path)                -> dict: the plan file, validated (ValueError if unusable, OSError if unreadable)
    plan_path(file)                -> Path: a relative plan file is read from the repository root
    to_room(x_m, y_m, placement)   -> (x, y): a plan point (metres) in room coordinates
    room_plan(plan, placement)     -> dict: the plan in room coordinates, what GET /api/floorplan serves
    room_plan_json(placement)      -> bytes: room_plan of the placement's file, as JSON
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Union

FORMAT = "csi-floorplan"
REPO_ROOT = Path(__file__).resolve().parent.parent
DECIMALS = 2            # room metres are rounded to the centimetre
LINE_KEYS = ("segments", "mesh")
POLYGON_KEYS = ("fills", "outline")


def plan_path(file: Union[str, Path]) -> Path:
    """The plan file: an absolute path as given, a relative one from the repository root."""
    path = Path(file)
    return path if path.is_absolute() else REPO_ROOT / path


def _numbers(values, what: str) -> list:
    if not isinstance(values, list) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
        raise ValueError(f"{what} must be a list of finite numbers")
    return values


def load_plan(path: Union[str, Path]) -> dict:
    """The plan file at ``path``, checked: its format, and per group a kind and flat number lists."""
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or plan.get("format") != FORMAT:
        raise ValueError(f"{path}: not a floor plan (format {FORMAT!r} expected)")
    if plan.get("units", "cm") != "cm":
        raise ValueError(f"{path}: plan units must be cm")
    groups = plan.get("groups")
    if not isinstance(groups, dict):
        raise ValueError(f"{path}: groups must be an object")
    for name, group in groups.items():
        if not isinstance(group, dict) or not isinstance(group.get("kind"), str):
            raise ValueError(f"{path}: group {name!r} needs a kind")
        for key in LINE_KEYS:
            if len(_numbers(group.get(key, []), f"{path}: group {name!r} {key}")) % 4:
                raise ValueError(f"{path}: group {name!r} {key} must hold x0, y0, x1, y1 per line")
        for key in POLYGON_KEYS:
            polys = group.get(key, [])
            if not isinstance(polys, list):
                raise ValueError(f"{path}: group {name!r} {key} must be a list of polygons")
            for poly in polys:
                if len(_numbers(poly, f"{path}: group {name!r} {key}")) < 6 or len(poly) % 2:
                    raise ValueError(f"{path}: group {name!r} {key} need at least 3 x, y points each")
        if "height" in group:
            _height(group["height"], f"{path}: group {name!r} height")
        bands = group.get("bands", [])
        if not isinstance(bands, list) or not all(isinstance(b, dict) for b in bands):
            raise ValueError(f"{path}: group {name!r} bands must be a list of objects")
        for band in bands:
            _height(band.get("height"), f"{path}: group {name!r} band height")
            if len(_numbers(band.get("segments", []), f"{path}: group {name!r} band segments")) % 4:
                raise ValueError(f"{path}: group {name!r} band segments must hold x0, y0, x1, y1 per line")
        stairs = group.get("steps", [])
        if not isinstance(stairs, list):
            raise ValueError(f"{path}: group {name!r} steps must be a list")
        for step in stairs:
            if len(_numbers(step, f"{path}: group {name!r} step")) < 7 or len(step) % 2 == 0:
                raise ValueError(f"{path}: group {name!r} steps need a height and at least 3 x, y points each")
    return plan


def _height(value, what: str) -> list:
    if len(_numbers(value, what)) != 2 or value[0] > value[1]:
        raise ValueError(f"{what} must be [low, high]")
    return value


def to_room(x_m: float, y_m: float, placement) -> tuple[float, float]:
    """A plan point in metres as room coordinates, for a placement with ``origin_m`` and ``rotation_deg``."""
    r = math.radians(placement.rotation_deg)
    dx, dy = x_m - placement.origin_m[0], y_m - placement.origin_m[1]
    return dx * math.cos(r) + dy * math.sin(r), -dx * math.sin(r) + dy * math.cos(r)


def _flat(values, placement) -> list[float]:
    out = []
    for i in range(0, len(values) - 1, 2):
        x, y = to_room(values[i] / 100.0, values[i + 1] / 100.0, placement)
        out.append(round(x, DECIMALS))
        out.append(round(y, DECIMALS))
    return out


def _metres(values) -> list[float]:
    """Heights in centimetres as metres (heights are not moved by the placement)."""
    return [round(v / 100.0, DECIMALS) for v in values]


def room_plan(plan: dict, placement) -> dict:
    """The plan's groups in room coordinates (metres) for ``placement`` (csi/room.py FloorPlan), with their bounds."""
    groups = {}
    xs, ys = [], []
    for name, group in plan["groups"].items():
        out = {"kind": group["kind"]}
        for key in LINE_KEYS:
            if group.get(key):
                out[key] = _flat(group[key], placement)
        for key in POLYGON_KEYS:
            if group.get(key):
                out[key] = [_flat(poly, placement) for poly in group[key]]
        if "height" in group:
            out["height"] = _metres(group["height"])
        if group.get("bands"):
            out["bands"] = [{"height": _metres(b["height"]), "segments": _flat(b.get("segments", []), placement)}
                            for b in group["bands"]]
        if group.get("steps"):
            out["steps"] = [_metres(step[:1]) + _flat(step[1:], placement) for step in group["steps"]]
        for key in LINE_KEYS:
            xs.extend(out.get(key, [])[0::2])
            ys.extend(out.get(key, [])[1::2])
        for poly in out.get("fills", []):
            xs.extend(poly[0::2])
            ys.extend(poly[1::2])
        groups[name] = out
    bounds = [min(xs), min(ys), max(xs), max(ys)] if xs else [0.0, 0.0, 0.0, 0.0]
    return {
        "name": plan.get("name", ""),
        "source": plan.get("source", ""),
        "units": "m",
        "frame": "room: metres, x right and y down from the room's top-left corner",
        "placement": {"file": placement.file, "origin_m": list(placement.origin_m),
                      "rotation_deg": placement.rotation_deg},
        "bounds": bounds,
        "groups": groups,
    }


def room_plan_json(placement) -> bytes:
    """``room_plan`` of the placement's plan file as compact JSON (raises OSError or ValueError when unusable)."""
    plan = load_plan(plan_path(placement.file))
    return json.dumps(room_plan(plan, placement), separators=(",", ":")).encode()
