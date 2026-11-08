"""Building floor plans: the OBJ converter (scripts/obj_to_floorplan.py), the room config's "floor_plan" placement
(csi/room.py), the transform into room coordinates (csi/floorplan.py) and GET /api/floorplan (csi/server.py)."""
import dataclasses
import importlib.util
import json
import math
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from csi.floorplan import load_plan, room_plan, room_plan_json, to_room
from csi.room import FloorPlan, load_room, parse_floor_plan
from csi.server import DashboardServer
from csi.simulate import simulate_room

ROOT = Path(__file__).resolve().parent.parent
ROOM = load_room()


def converter():
    spec = importlib.util.spec_from_file_location("obj_to_floorplan", ROOT / "scripts" / "obj_to_floorplan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_obj(path: Path) -> Path:
    """A tiny edges-only overlay: a 2 x 2 m triangulated floor slab (top and bottom faces and the vertical edges
    between them), the same slab as the ceiling, a 3 m wall meshed in overlapping pieces at two heights, a 0.5 m
    box column and an inferred room wall that must be left out."""
    lines = ["# tiny overlay. Units: metres. Y up.", "o mesh_overlay_floor_slab_L14", "g mesh_overlay_floor_slab_L14"]
    vertices = []

    def v(x, y, z):
        vertices.append((x, y, z))
        lines.append(f"v {x:.3f} {y:.3f} {z:.3f}")
        return len(vertices)

    def slab(y_top, y_bottom):
        for y in (y_top, y_bottom):
            ids = {(i, j): v(float(i), y, -float(j)) for i in range(3) for j in range(3)}
            for i in range(3):
                for j in range(3):
                    if i < 2:
                        lines.append(f"l {ids[i, j]} {ids[i + 1, j]}")
                    if j < 2:
                        lines.append(f"l {ids[i, j]} {ids[i, j + 1]}")
                    if i < 2 and j < 2:
                        lines.append(f"l {ids[i, j]} {ids[i + 1, j + 1]}")
        top, bottom = v(0.0, y_top, 0.0), v(0.0, y_bottom, 0.0)
        lines.append(f"l {top} {bottom}")           # a vertical edge: dropped

    slab(0.0, -0.15)
    lines += ["o mesh_overlay_ceiling_slab_L15", "g mesh_overlay_ceiling_slab_L15"]
    slab(3.7, 3.55)
    lines += ["g mesh_overlay_core_walls"]
    a, b, c, d = v(10.0, 0.0, 0.0), v(11.0, 0.0, 0.0), v(12.0, 0.0, 0.0), v(11.5, 2.0, 0.003)
    e = v(13.0, 2.0, -0.002)
    lines += [f"l {a} {b} {c}", f"l {d} {e}", f"l {a} {v(10.0, 2.0, 0.0)}"]   # a polyline, an overlap, a vertical
    lines += ["g mesh_overlay_columns"]
    corners = [v(20.0, 0.0, 0.0), v(20.5, 0.0, 0.0), v(20.5, 0.0, -0.5), v(20.0, 0.0, -0.5)]
    lines += [f"l {corners[i]} {corners[(i + 1) % 4]}" for i in range(4)]
    lines += [f"l {corners[0]} {corners[2]}"]
    lines += ["g mesh_overlay_inferred_basex_room_walls", f"l {v(30.0, 0.0, 0.0)} {v(35.0, 0.0, 0.0)}"]
    path.write_text("\n".join(lines) + "\n")
    return path


def segments(flat):
    return [tuple(flat[i:i + 4]) for i in range(0, len(flat), 4)]


def test_the_converter_keeps_the_named_groups_projects_merges_and_leaves_the_room_walls_out(tmp_path):
    mod = converter()
    plan = mod.convert(write_obj(tmp_path / "tiny.obj"), "tiny")
    groups = plan["groups"]

    assert "inferred_basex_room_walls" not in groups and plan["skipped"] == {"inferred_basex_room_walls": 1}
    assert plan["units"] == "cm" and plan["format"] == "csi-floorplan"
    # the wall: 10 -> 11 -> 12 m and an overlapping piece 11.5 -> 13 m merge into one line, the vertical is dropped
    assert segments(groups["core_walls"]["segments"]) == [(1000, 0, 1300, 0)]
    assert plan["stats"]["core_walls"]["vertical"] == 1
    # the slab: its outline (plan y = Z, so the slab runs to y = -200 cm), its mesh decimated, its vertical dropped
    assert sorted(segments(groups["floor_slab_L14"]["segments"])) == [
        (0, -200, 0, 0), (0, -200, 200, -200), (0, 0, 200, 0), (200, -200, 200, 0)]
    mesh = groups["floor_slab_L14"]["mesh"]
    assert mesh and all(-200 <= y <= 0 for y in mesh[1::2]) and all(0 <= x <= 200 for x in mesh[0::2])
    # the ceiling slab coincides with the floor slab in plan: nothing of it is kept
    assert groups["ceiling_slab_L15"]["segments"] == [] and "mesh" not in groups["ceiling_slab_L15"]
    # the column: its outline and one filled hull
    hull = groups["columns"]["fills"]
    assert len(hull) == 1 and sorted(zip(hull[0][0::2], hull[0][1::2])) == [(2000, -50), (2000, 0), (2050, -50),
                                                                              (2050, 0)]
    assert plan["bounds"] == [0, -200, 2050, 0]
    assert all(isinstance(v, int) for g in groups.values() for v in g["segments"] + g.get("mesh", []))


def test_the_converter_writes_a_plan_the_loader_reads(tmp_path):
    mod = converter()
    out = tmp_path / "plans" / "tiny.json"
    assert mod.main([str(write_obj(tmp_path / "tiny.obj")), str(out)]) == 0
    plan = load_plan(out)
    assert plan["groups"]["core_walls"]["segments"] == [1000, 0, 1300, 0]
    assert "OBJ header" in plan["source"]


def test_the_converter_refuses_an_obj_with_a_bad_vertex_reference(tmp_path):
    mod = converter()
    bad = tmp_path / "bad.obj"
    bad.write_text("g mesh_overlay_core_walls\nv 0 0 0\nl 1 7\n")
    with pytest.raises(mod.ObjError, match="out of range"):
        mod.convert(bad)


def write_room(tmp_path: Path, floor_plan=None) -> Path:
    config = json.loads((ROOT / "tests" / "fixtures" / "room_rig4.json").read_text())
    if floor_plan is not None:
        config["floor_plan"] = floor_plan
    path = tmp_path / "room.json"
    path.write_text(json.dumps(config))
    return path


def test_a_room_without_floor_plan_has_none():
    assert ROOM.floor_plan is None


def test_the_room_loads_its_floor_plan_placement_and_ignores_unknown_keys(tmp_path):
    room = load_room(write_room(tmp_path, {"file": "config/floorplans/basex_floor14.json", "origin_m": [39.4, -21],
                                           "rotation_deg": 90, "note": "east wing"}))
    assert room.floor_plan == FloorPlan("config/floorplans/basex_floor14.json", (39.4, -21.0), 90.0)
    assert parse_floor_plan({"file": "a.json", "origin_m": [0, 0]}).rotation_deg == 0.0


@pytest.mark.parametrize("value", [
    "config/floorplans/basex_floor14.json",
    {"origin_m": [0, 0]},
    {"file": "", "origin_m": [0, 0]},
    {"file": "a.json"},
    {"file": "a.json", "origin_m": [0]},
    {"file": "a.json", "origin_m": [0, "1"]},
    {"file": "a.json", "origin_m": [0, True]},
    {"file": "a.json", "origin_m": [0, 0], "rotation_deg": "90"},
    {"file": "a.json", "origin_m": [0, 0], "rotation_deg": float("nan")},
])
def test_a_floor_plan_placement_with_wrong_types_is_refused(tmp_path, value):
    with pytest.raises(ValueError, match="floor_plan"):
        load_room(write_room(tmp_path, value))


TINY_PLAN = {
    "format": "csi-floorplan", "version": 1, "name": "tiny", "source": "test", "units": "cm",
    "groups": {
        "core_walls": {"kind": "core", "segments": [100, 200, 500, 200]},
        "floor_slab_L14": {"kind": "slab", "segments": [0, 0, 0, 400], "mesh": [0, 0, 100, 100]},
        "columns": {"kind": "column", "segments": [], "fills": [[300, 300, 350, 300, 350, 350]]},
    },
}


def test_plan_points_map_to_room_coordinates_by_origin_and_clockwise_rotation():
    placed = FloorPlan("p.json", (1.0, 2.0), 0.0)
    assert to_room(5.0, 2.0, placed) == (4.0, 0.0)
    turned = FloorPlan("p.json", (1.0, 2.0), 90.0)
    x, y = to_room(1.0, 5.0, turned)          # 3 m south of the origin: along the room's x when turned 90 deg
    assert math.isclose(x, 3.0) and math.isclose(y, 0.0, abs_tol=1e-12)
    x, y = to_room(0.0, 2.0, turned)          # 1 m west of the origin: along the room's y
    assert math.isclose(x, 0.0, abs_tol=1e-12) and math.isclose(y, 1.0)


def test_room_plan_moves_every_group_into_room_metres_with_its_bounds():
    out = room_plan(TINY_PLAN, FloorPlan("p.json", (1.0, 2.0), 0.0))
    assert out["units"] == "m" and out["placement"] == {"file": "p.json", "origin_m": [1.0, 2.0], "rotation_deg": 0.0}
    assert out["groups"]["core_walls"] == {"kind": "core", "segments": [0.0, 0.0, 4.0, 0.0]}
    assert out["groups"]["floor_slab_L14"]["mesh"] == [-1.0, -2.0, 0.0, -1.0]
    assert out["groups"]["columns"]["fills"] == [[2.0, 1.0, 2.5, 1.0, 2.5, 1.5]]
    assert "segments" not in out["groups"]["columns"]
    assert out["bounds"] == [-1.0, -2.0, 4.0, 2.0]


def test_the_committed_basex_plan_loads_and_places_the_example_room():
    room = load_room(ROOT / "config" / "examples" / "basex_room.json")
    body = json.loads(room_plan_json(room.floor_plan))
    assert set(body["groups"]) >= {"floor_slab_L14", "core_walls", "columns", "facade_glass"}
    assert "inferred_basex_room_walls" not in body["groups"]
    x0, y0, x1, y1 = body["bounds"]
    assert x0 < 0 < room.width_m < x1 and y0 < 0 < room.depth_m < y1   # the room lies inside the floor


def get(url: str):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.status, resp.read()


def start_server(room):
    server = DashboardServer(room, simulate_room(None, room=room, duration_s=1.0, seed=3), None, port=0)
    server.start()
    return server


def test_get_floorplan_serves_the_plan_in_room_coordinates(tmp_path):
    plan_file = tmp_path / "tiny.json"
    plan_file.write_text(json.dumps(TINY_PLAN))
    room = dataclasses.replace(ROOM, floor_plan=FloorPlan(str(plan_file), (1.0, 2.0), 0.0))
    server = start_server(room)
    try:
        assert server.wait_finished(timeout=30)
        status, body = get(server.url + "/api/floorplan")
        state = json.loads(get(server.url + "/api/state")[1])
    finally:
        server.stop()
    assert status == 200
    assert json.loads(body)["groups"]["core_walls"]["segments"] == [0.0, 0.0, 4.0, 0.0]
    assert "floor_plan" not in state["room"]      # the SSE and state payloads are unchanged


@pytest.mark.parametrize("floor_plan", [None, FloorPlan("/nonexistent/plan.json", (0.0, 0.0), 0.0)])
def test_get_floorplan_is_404_without_a_usable_plan(floor_plan):
    server = start_server(dataclasses.replace(ROOM, floor_plan=floor_plan))
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            get(server.url + "/api/floorplan")
    finally:
        server.stop()
    assert err.value.code == 404
