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
     "groups": {name: {"kind", "segments": [x0, y0, x1, y1, ...], "mesh"?: [...], "fills"?: [[x, y, ...], ...]}}}

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
        fills = group.get("fills", [])
        if not isinstance(fills, list):
            raise ValueError(f"{path}: group {name!r} fills must be a list of polygons")
        for poly in fills:
            if len(_numbers(poly, f"{path}: group {name!r} fill")) < 6 or len(poly) % 2:
                raise ValueError(f"{path}: group {name!r} fills need at least 3 x, y points each")
    return plan


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


def room_plan(plan: dict, placement) -> dict:
    """The plan's groups in room coordinates (metres) for ``placement`` (csi/room.py FloorPlan), with their bounds."""
    groups = {}
    xs, ys = [], []
    for name, group in plan["groups"].items():
        out = {"kind": group["kind"]}
        for key in LINE_KEYS:
            if group.get(key):
                out[key] = _flat(group[key], placement)
        if group.get("fills"):
            out["fills"] = [_flat(poly, placement) for poly in group["fills"]]
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
