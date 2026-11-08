#!/usr/bin/env python3
"""Turn a building's edges-only OBJ overlay into a compact 2D floor plan for the dashboard (config/floorplans/).

    python3 -I scripts/obj_to_floorplan.py basex_floor14_mesh_overlay.obj config/floorplans/basex_floor14.json

The input is the BASE-X floor 14 mesh overlay (60 State Street, Boston): OBJ 'v' vertices and 'l' line elements,
no faces, metres, Y up, +X building east, -Z building north. It is not committed (large files are linked, not
committed); config/floorplans/README.md says where it comes from. The file is data: it is parsed line by line and
nothing in it is executed.

Only these OBJ groups are kept (the "mesh_overlay_" prefix dropped), each with a kind the page styles by:

    floor_slab_L14, ceiling_slab_L15                       slab     (outline + a decimated wireframe of the mesh)
    facade_glass, facade_piers_granite, facade_spandrels   facade
    core_walls, core_elevator_shafts                       core
    core_doors                                             door
    core_stairs                                            stair
    core_lobby_ceiling                                     ceiling  (outline + a decimated wireframe of the mesh)
    columns                                                column   (outlines + filled convex hulls)

Every other group is left out, the inferred BASE-X room walls among them.

Steps, per group: project each line element to plan (x = X, y = Z, so +y is south and north is up on screen); drop
segments under 1 cm long in plan (vertical edges); deduplicate at 1 cm (the ceiling slab keeps only segments the
floor slab does not have); merge nearly collinear segments that touch or overlap into single lines (a total least
squares fit through the merged pieces); quantise to whole centimetres.

The slabs and the lobby ceiling are not grids: they are irregular triangle meshes (edges of about 27 cm, 101 k
distinct edges in plan for the floor slab), which do not merge into long lines, are several megabytes as segments
and draw as grey fog at floor scale. For those the plan keeps the mesh's outline (its boundary edges, merged) and a
decimated wireframe of the mesh ("mesh"): vertex clustering on a MESH_CELL_CM grid, each cell's mesh vertex nearest
the cell centre standing for the cell, an edge between two cells wherever the mesh has one, and of two crossing
diagonals in a 2 x 2 block only the shorter kept.

For the dashboard's solid 3D view, which extrudes the plan (it never draws edges), each group also carries:

    height    [y0, y1]: its vertical extent, the lowest and highest OBJ Y of its edges (plan lines and fills span it)
    bands     only for a group whose edges sit in separate vertical bands (the spandrels: under the glass and over
              it): [{"height": [y0, y1], "segments": [...]}, ...], each band's own plan lines, merged per band
    outline   slabs and the lobby ceiling: their outline as closed rings [[x, y, ...], ...] (the merged boundary
              lines chained end to end), for a triangulated slab
    steps     stairs: [[h, x, y, ...], ...], per level h of the treads (their horizontal edges) the convex hull of
              each connected piece, so the page can stack solid steps from the floor up to h

Output (JSON, integers in centimetres):

    {"format": "csi-floorplan", "version": 1, "name", "source", "units": "cm", "frame", "bounds": [x0, y0, x1, y1],
     "groups": {name: {"kind", "height": [y0, y1], "segments": [x0, y0, x1, y1, ...], "mesh"?: [...],
                       "fills"?: [[x, y, ...], ...], "bands"?: [...], "outline"?: [[x, y, ...], ...],
                       "steps"?: [[h, x, y, ...], ...]}},
     "stats": {name: {"obj_segments", "vertical", "unique", "segments", "mesh", "fills", "bands", "steps"}}}

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from array import array
from collections import defaultdict
from pathlib import Path

FORMAT = "csi-floorplan"
VERSION = 1
GROUP_PREFIX = "mesh_overlay_"
# OBJ group (prefix dropped) -> kind, in output order. Anything else is skipped, the inferred room walls among them.
GROUPS = {
    "floor_slab_L14": "slab",
    "ceiling_slab_L15": "slab",
    "facade_glass": "facade",
    "facade_piers_granite": "facade",
    "facade_spandrels": "facade",
    "core_walls": "core",
    "core_elevator_shafts": "core",
    "core_doors": "door",
    "core_stairs": "stair",
    "core_lobby_ceiling": "ceiling",
    "columns": "column",
}
SURFACE_KINDS = frozenset({"slab", "ceiling"})   # horizontal triangle meshes: outline + decimated wireframe
DEDUP_AGAINST = {"ceiling_slab_L15": "floor_slab_L14"}   # coincide in plan: keep the ceiling only where it differs
MIN_PLAN_M = 0.01           # shorter in plan than this: a vertical edge, dropped
ANGLE_GAP_DEG = 0.5         # merge: directions chain while consecutive ones are this close ...
ANGLE_SPAN_DEG = 3.0        # ... and a chain spans at most this much
OFFSET_GAP_CM = 1.0         # merge: carrier lines this close count as one
JOIN_GAP_CM = 1.0           # merge: pieces of one line this close (or overlapping) join
MAX_DEV_CM = 1.5            # merge: every merged piece lies this close to the merged line, else the run is split
MESH_CELL_CM = 100          # decimated wireframe: vertex-clustering cell size
BAND_GAP_CM = 2             # 3D: edge heights this close belong to one vertical band
RING_SNAP_CM = 3            # 3D: outline line ends this close are one corner of the ring
LEVEL_CM = 2                # 3D: tread levels this close are one step


class ObjError(ValueError):
    """The OBJ cannot be read; the message names the line."""


def _cm(v: float) -> int:
    return int(math.floor(v * 100.0 + 0.5))


def _index(ref: str, count: int, lineno: int) -> int:
    """0-based vertex index of an 'l' reference ("7", "7/3" or a negative relative index)."""
    try:
        i = int(ref.split("/", 1)[0])
    except ValueError:
        raise ObjError(f"line {lineno}: bad vertex reference {ref!r}") from None
    j = i - 1 if i > 0 else count + i
    if i == 0 or not 0 <= j < count:
        raise ObjError(f"line {lineno}: vertex reference {ref} is out of range (1 to {count})")
    return j


def parse_obj(path: Path, keep=GROUPS, tall=None):
    """Stream the OBJ. Returns (header comment lines, per kept group: {cm key: segment in cm as floats},
    per group raw 'l' segment counts, per kept group vertical (dropped) counts, and the heights: per kept group
    {"range": [y0, y1] cm, "intervals": {(y0, y1) cm of each edge}, "keys": {cm key: {its edges' (y0, y1)}} (not for
    the slabs), "levels": {tread level cm: {cm keys}} (stairs only)})."""
    if tall is None:
        tall = {g for g, k in keep.items() if k not in SURFACE_KINDS}
    xs, ys, zs = array("d"), array("d"), array("d")
    header: list[str] = []
    segs: dict[str, dict] = defaultdict(dict)
    raw: dict[str, int] = defaultdict(int)
    vertical: dict[str, int] = defaultdict(int)
    heights: dict[str, dict] = defaultdict(lambda: {"range": None, "intervals": set(), "keys": defaultdict(set),
                                                     "levels": defaultdict(set)})
    group = None
    seen_geometry = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            if line.startswith("v "):
                seen_geometry = True
                parts = line.split()
                try:
                    x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                except (IndexError, ValueError):
                    raise ObjError(f"line {lineno}: bad vertex {line.strip()[:60]!r}") from None
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    raise ObjError(f"line {lineno}: vertex is not finite")
                xs.append(x)
                ys.append(y)
                zs.append(z)
            elif line.startswith("l "):
                seen_geometry = True
                refs = line.split()[1:]
                raw[group] += max(0, len(refs) - 1)
                if group not in keep:
                    continue
                idx = [_index(r, len(xs), lineno) for r in refs]
                bucket = segs[group]
                h = heights[group]
                for a, b in zip(idx, idx[1:]):
                    ax, az, bx, bz = xs[a], zs[a], xs[b], zs[b]
                    lo, hi = _cm(min(ys[a], ys[b])), _cm(max(ys[a], ys[b]))
                    h["range"] = [lo, hi] if h["range"] is None else [min(h["range"][0], lo), max(h["range"][1], hi)]
                    h["intervals"].add((lo, hi))
                    if math.hypot(bx - ax, bz - az) < MIN_PLAN_M:
                        vertical[group] += 1
                        continue
                    p, q = (_cm(ax), _cm(az)), (_cm(bx), _cm(bz))
                    if p == q:
                        vertical[group] += 1
                        continue
                    key = (p, q) if p <= q else (q, p)
                    if key not in bucket:
                        bucket[key] = (ax * 100.0, az * 100.0, bx * 100.0, bz * 100.0)
                    if group in tall:
                        h["keys"][key].add((lo, hi))
                    if keep[group] == "stair" and hi - lo < 1:
                        h["levels"][_cm((ys[a] + ys[b]) / 2.0)].add(key)
            elif line.startswith(("g ", "o ")) or line.strip() in ("g", "o"):
                name = line[1:].strip()
                group = name[len(GROUP_PREFIX):] if name.startswith(GROUP_PREFIX) else name
            elif line.startswith("#") and not seen_geometry:
                header.append(line[1:].strip())
    return header, segs, raw, vertical, heights


# ------------------------------------------------------------------ merging collinear segments

def _chains(items, key, gap, span=None):
    """Split a list sorted by ``key`` where consecutive keys differ by more than ``gap`` (or a chain would span more
    than ``span``)."""
    chain = []
    for it in items:
        k = key(it)
        if chain and (k - key(chain[-1]) > gap or (span is not None and k - key(chain[0]) > span)):
            yield chain
            chain = []
        chain.append(it)
    if chain:
        yield chain


def _fit(pieces):
    """One line through the pieces (x0, y0, x1, y1): a length-weighted total least squares fit, the extreme
    projections of their endpoints as its ends."""
    if len(pieces) == 1:
        return pieces[0]
    sw = sx = sy = 0.0
    for x0, y0, x1, y1 in pieces:
        w = math.hypot(x1 - x0, y1 - y0)
        sw += 2 * w
        sx += w * (x0 + x1)
        sy += w * (y0 + y1)
    cx, cy = sx / sw, sy / sw
    sxx = syy = sxy = 0.0
    for x0, y0, x1, y1 in pieces:
        w = math.hypot(x1 - x0, y1 - y0)
        for x, y in ((x0, y0), (x1, y1)):
            dx, dy = x - cx, y - cy
            sxx += w * dx * dx
            syy += w * dy * dy
            sxy += w * dx * dy
    th = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ux, uy = math.cos(th), math.sin(th)
    ts = [(x - cx) * ux + (y - cy) * uy for x0, y0, x1, y1 in pieces for x, y in ((x0, y0), (x1, y1))]
    t0, t1 = min(ts), max(ts)
    return cx + t0 * ux, cy + t0 * uy, cx + t1 * ux, cy + t1 * uy


def _max_dev(pieces, line):
    x0, y0, x1, y1 = line
    length = math.hypot(x1 - x0, y1 - y0)
    if length == 0:
        return math.inf
    nx, ny = -(y1 - y0) / length, (x1 - x0) / length
    return max(abs((x - x0) * nx + (y - y0) * ny) for p in pieces for x, y in ((p[0], p[1]), (p[2], p[3])))


def _fit_runs(run):
    """The fitted line of a run of pieces (in order along it), or, when a piece strays more than MAX_DEV_CM from
    it, the lines of its two halves: a merge never bends the geometry."""
    line = _fit(run)
    if len(run) == 1 or _max_dev(run, line) <= MAX_DEV_CM:
        return [line]
    mid = len(run) // 2
    return _fit_runs(run[:mid]) + _fit_runs(run[mid:])


def merge_collinear(segments):
    """Nearly collinear segments that touch or overlap, merged into single lines.

    ``segments``: (x0, y0, x1, y1) in cm (floats). Returns sorted (x0, y0, x1, y1) integer tuples, each line from its
    smaller endpoint.
    """
    items = []
    for x0, y0, x1, y1 in segments:
        th = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180.0
        if th > 180.0 - ANGLE_SPAN_DEG / 2:
            th -= 180.0                       # nearly horizontal both ways: together near 0
        items.append((th, x0, y0, x1, y1))
    items.sort()
    lines = set()
    for cluster in _chains(items, key=lambda it: it[0], gap=ANGLE_GAP_DEG, span=ANGLE_SPAN_DEG):
        w = [math.hypot(it[3] - it[1], it[4] - it[2]) for it in cluster]
        th = math.radians(sum(wi * it[0] for wi, it in zip(w, cluster)) / sum(w))
        ux, uy, nx, ny = math.cos(th), math.sin(th), -math.sin(th), math.cos(th)
        carriers = []
        for _, x0, y0, x1, y1 in cluster:
            r = (nx * (x0 + x1) + ny * (y0 + y1)) / 2
            t0, t1 = ux * x0 + uy * y0, ux * x1 + uy * y1
            carriers.append((r, min(t0, t1), max(t0, t1), (x0, y0, x1, y1)))
        carriers.sort()
        for line in _chains(carriers, key=lambda c: c[0], gap=OFFSET_GAP_CM):
            line.sort(key=lambda c: c[1])
            run, end = [], None
            for _, t0, t1, seg in line:
                if run and t0 > end + JOIN_GAP_CM:
                    lines.update(_rounded(s) for s in _fit_runs(run))
                    run, end = [], None
                run.append(seg)
                end = t1 if end is None else max(end, t1)
            if run:
                lines.update(_rounded(s) for s in _fit_runs(run))
    return sorted(s for s in lines if s[:2] != s[2:])


def _rounded(seg):
    p, q = (_cm(seg[0] / 100.0), _cm(seg[1] / 100.0)), (_cm(seg[2] / 100.0), _cm(seg[3] / 100.0))
    return p + q if p <= q else q + p


# ------------------------------------------------------------------ surfaces: outline and decimated wireframe

def _adjacency(keys):
    adj = defaultdict(set)
    for p, q in keys:
        adj[p].add(q)
        adj[q].add(p)
    return adj


def outline_keys(keys):
    """The boundary of a triangle mesh in plan: the edges that only one triangle has."""
    adj = _adjacency(keys)
    return [(p, q) for p, q in keys if len(adj[p] & adj[q]) == 1]


def decimate(keys, boundary, cell=MESH_CELL_CM):
    """A coarse wireframe of a triangle mesh by vertex clustering: per ``cell`` square, the mesh vertex nearest its
    centre (a boundary vertex when the cell has one); an edge between two cells wherever the mesh has one; of two
    crossing diagonals in a 2 x 2 block, only the shorter. Returns (x0, y0, x1, y1) integer tuples."""
    on_edge = {p for pq in boundary for p in pq}
    best = {}
    for p in {p for pq in keys for p in pq}:
        c = (p[0] // cell, p[1] // cell)
        d = math.hypot(p[0] - (c[0] + 0.5) * cell, p[1] - (c[1] + 0.5) * cell)
        rank = (0 if p in on_edge else 1, d, p)
        if c not in best or rank < best[c][0]:
            best[c] = (rank, p)
    rep = {c: v[1] for c, v in best.items()}
    pairs = set()
    for p, q in keys:
        a, b = (p[0] // cell, p[1] // cell), (q[0] // cell, q[1] // cell)
        if a != b:
            pairs.add((a, b) if a <= b else (b, a))

    def length(pair):
        (a, b) = pair
        return math.dist(rep[a], rep[b])

    for a, b in list(pairs):   # a down-right diagonal crossed by the up-right one in the same 2 x 2 block
        if b == (a[0] + 1, a[1] + 1):
            other = ((a[0], a[1] + 1), (a[0] + 1, a[1]))
            if other in pairs and (a, b) in pairs:
                pairs.discard(other if length(other) > length((a, b)) else (a, b))
    out = set()
    for a, b in pairs:
        p, q = rep[a], rep[b]
        if p != q:
            out.add(p + q if p <= q else q + p)
    return sorted(out)


# ------------------------------------------------------------------ columns: filled hulls

def _hull(points):
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def component_hulls(keys):
    """The convex hull of each connected piece (segments sharing an endpoint), as flat [x, y, ...] lists."""
    parent = {}

    def find(p):
        while parent.setdefault(p, p) != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    for p, q in keys:
        ra, rb = find(p), find(q)
        if ra != rb:
            parent[ra] = rb
    pieces = defaultdict(list)
    for p in list(parent):
        pieces[find(p)].append(p)
    hulls = []
    for pts in pieces.values():
        h = _hull(pts)
        if len(h) >= 3:
            hulls.append([v for pt in h for v in pt])
    return sorted(hulls)


# ------------------------------------------------------------------ 3D: bands, outline rings, steps

def bands(intervals, gap=BAND_GAP_CM):
    """The vertical bands of a group: its edges' (y0, y1) intervals merged where they overlap or come within
    ``gap``. Returns sorted [y0, y1] lists."""
    out = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def rings(lines, snap=RING_SNAP_CM):
    """Closed rings from outline lines (x0, y0, x1, y1): line ends within ``snap`` are one corner; each ring is
    walked line by line until it closes. Returns flat [x, y, ...] lists of 3 or more corners."""
    ends = [pt for ln in lines for pt in ((ln[0], ln[1]), (ln[2], ln[3]))]
    corner = list(range(len(ends)))

    def find(i):
        while corner[i] != i:
            corner[i] = corner[corner[i]]
            i = corner[i]
        return i

    for i in range(len(ends)):
        for j in range(i):
            if math.dist(ends[i], ends[j]) <= snap:
                corner[find(i)] = find(j)
    at = defaultdict(list)          # corner -> [(line, its other corner)]
    for n in range(len(lines)):
        a, b = find(2 * n), find(2 * n + 1)
        if a != b:
            at[a].append((n, b))
            at[b].append((n, a))
    used, out = set(), []
    for start in sorted(at):
        for n, nxt in at[start]:
            if n in used:
                continue
            used.add(n)
            ring, here = [start], nxt
            while here != start:
                ring.append(here)
                step = next(((m, o) for m, o in at[here] if m not in used), None)
                if step is None:
                    break
                used.add(step[0])
                here = step[1]
            if here == start and len(ring) >= 3:
                out.append([v for c in ring for v in ends[c]])
    return sorted(out)


def steps(levels, gap=LEVEL_CM):
    """Solid steps of a stair: tread levels (cm) within ``gap`` of each other joined; per level, the convex hull of
    each connected piece of its horizontal edges. Returns [h, x, y, ...] lists, the floor level left out."""
    out, run, keys = [], None, set()

    def flush():
        if run is not None and run > gap:
            out.extend([run] + hull for hull in component_hulls(keys))

    for level in sorted(levels):
        if run is not None and level - run > gap:
            flush()
            run, keys = None, set()
        run = level if run is None else max(run, level)
        keys |= levels[level]
    flush()
    return out


# ------------------------------------------------------------------ the plan

def convert(path: Path, name: str = "") -> dict:
    header, segs, raw, vertical, heights = parse_obj(path)
    groups, stats = {}, {}
    for group, kind in GROUPS.items():
        bucket = segs.get(group, {})
        keys = set(bucket)
        base = DEDUP_AGAINST.get(group)
        if base is not None:
            keys -= set(segs.get(base, {}))
        entry = {"kind": kind}
        h = heights.get(group)
        if h is not None and h["range"] is not None:
            entry["height"] = list(h["range"])
        mesh = []
        if kind in SURFACE_KINDS and base is None:
            boundary = outline_keys(keys)
            lines = merge_collinear(bucket[k] for k in boundary)
            mesh = decimate(keys, boundary)
        else:
            lines = merge_collinear(bucket[k] for k in keys)
        entry["segments"] = [v for s in lines for v in s]
        if mesh:
            entry["mesh"] = [v for s in mesh for v in s]
        if kind == "column":
            entry["fills"] = component_hulls(keys)
        if h is not None and kind not in SURFACE_KINDS:
            spans = bands(h["intervals"])
            if len(spans) > 1:      # separate bands: each band's own lines, merged on their own
                entry["bands"] = []
                for lo, hi in spans:
                    mine = [k for k in keys if any(lo <= a and b <= hi for a, b in h["keys"].get(k, ()))]
                    band_lines = merge_collinear(bucket[k] for k in mine)
                    entry["bands"].append({"height": [lo, hi], "segments": [v for s in band_lines for v in s]})
        if kind in SURFACE_KINDS and base is None and lines:
            entry["outline"] = rings(lines)
        if kind == "stair" and h is not None:
            entry["steps"] = steps(h["levels"])
        groups[group] = entry
        stats[group] = {"obj_segments": raw.get(group, 0), "vertical": vertical.get(group, 0),
                        "unique": len(keys), "segments": len(lines), "mesh": len(mesh),
                        "fills": len(entry.get("fills", [])), "bands": len(entry.get("bands", [])),
                        "steps": len(entry.get("steps", []))}
    skipped = {g: n for g, n in raw.items() if g not in GROUPS}
    xs = [v for e in groups.values() for k in ("segments", "mesh") for v in e.get(k, [])[0::2]]
    ys = [v for e in groups.values() for k in ("segments", "mesh") for v in e.get(k, [])[1::2]]
    bounds = [min(xs), min(ys), max(xs), max(ys)] if xs else [0, 0, 0, 0]
    return {
        "format": FORMAT,
        "version": VERSION,
        "name": name or path.stem,
        "source": (f"{path.name}, converted by scripts/obj_to_floorplan.py. OBJ header: " + " ".join(header)).strip(),
        "units": "cm",
        "frame": ("plan x = OBJ X (building east), plan y = OBJ Z (+y = building south, so north is up on screen), "
                  "integer centimetres from the OBJ origin"),
        "bounds": bounds,
        "groups": groups,
        "stats": stats,
        "skipped": skipped,
    }


def dumps(plan: dict) -> str:
    """Compact JSON with one line per group, so a diff shows which group changed."""
    head = {k: v for k, v in plan.items() if k not in ("groups", "stats")}
    lines = ["{"]
    for k, v in head.items():
        lines.append(f"  {json.dumps(k)}: {json.dumps(v, separators=(',', ':'))},")
    lines.append('  "groups": {')
    names = list(plan["groups"])
    for i, g in enumerate(names):
        sep = "," if i < len(names) - 1 else ""
        lines.append(f"    {json.dumps(g)}: {json.dumps(plan['groups'][g], separators=(',', ':'))}{sep}")
    lines.append("  },")
    lines.append(f'  "stats": {json.dumps(plan["stats"], separators=(",", ":"))}')
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Convert an edges-only OBJ floor overlay into a compact floor plan JSON.")
    ap.add_argument("obj", type=Path, help="the OBJ overlay (e.g. basex_floor14_mesh_overlay.obj)")
    ap.add_argument("output", type=Path, help="the plan to write (e.g. config/floorplans/basex_floor14.json)")
    ap.add_argument("--name", default="BASE-X, 60 State Street, floor 14", help="the plan's display name")
    args = ap.parse_args(argv)
    try:
        plan = convert(args.obj, args.name)
    except (OSError, ObjError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    text = dumps(plan)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output} ({len(text.encode())} bytes), bounds {plan['bounds']} cm")
    print(f"{'group':24} {'obj':>7} {'vert':>6} {'unique':>7} {'lines':>6} {'mesh':>6} {'fills':>5} {'bands':>5} "
          f"{'steps':>5}  height cm")
    for g, s in plan["stats"].items():
        print(f"{g:24} {s['obj_segments']:7} {s['vertical']:6} {s['unique']:7} {s['segments']:6} {s['mesh']:6} "
              f"{s['fills']:5} {s['bands']:5} {s['steps']:5}  {plan['groups'][g].get('height')}")
    for g, n in plan["skipped"].items():
        print(f"skipped {g}: {n} segments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
