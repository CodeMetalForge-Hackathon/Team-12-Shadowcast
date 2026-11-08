# Floor plans

Building plans for the dashboard's 3D view (`GET /api/floorplan`; the page's "Map / 3D" switch): the whole floor as
a solid, shaded model, extruded from the plan, with the room, its pucks and the heat raised in it. The 2D map shows the
room alone. A room config uses one through `"floor_plan"` (see `csi/room.py` and `config/examples/basex_room.json`).

## basex_floor14.json

BASE-X, 60 State Street, Boston, floor 14. Made by `scripts/obj_to_floorplan.py` from
`basex_floor14_mesh_overlay.obj` (17.7 MB, an edges-only overlay: OBJ `v` and `l` elements, no faces), which the
team added next to the repository in `basex_floor14_mesh_overlay_obj/basex_floor14_obj/`. The OBJ is not committed:
large files are linked, not committed. Its header says it was reconstructed from public data on 7 Nov 2026: metres,
Y up, +X building east (bearing 78 deg true), -Z building north (348 deg true), origin at the 10-ft facade-grid point
(0, 0) on the finished floor of level 14 (42.3589693 N, 71.0566597 W).

Kept: the floor and ceiling slabs, the facade (glass, granite piers, spandrels), the core (walls, elevator shafts,
doors, stairs, lobby ceiling) and the columns. Left out: `inferred_basex_room_walls`, the OBJ's guess at the room
walls.

The plan is in integer centimetres: plan x = OBJ X (east), plan y = OBJ Z (south), so north is up on screen. The slabs
and the lobby ceiling are irregular triangle meshes (about 27 cm edges, 101 k distinct edges for the floor slab), so
the plan keeps their outline and a decimated 1 m wireframe of the mesh. The ceiling slab coincides with the floor
slab in plan and adds nothing.

For the 3D view every group also carries its height (the lowest and highest OBJ Y of its edges), the slab its
outline as a closed ring, the spandrels their two bands (under and over the glass) as separate lines, and the stairs
their steps (per tread level, the convex hull of each tread). The page extrudes these into solids (walls and facade
as thin boxes on their plan lines, glass as translucent panels, columns and steps as prisms, the slab triangulated);
it never draws the OBJ's edges and leaves the ceilings out. To rebuild it:

    python3 -I scripts/obj_to_floorplan.py path/to/basex_floor14_mesh_overlay.obj config/floorplans/basex_floor14.json
