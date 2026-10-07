# S3 / S4 / S5 — buildings, detail, water, atlas, UE import

Step S3–S5 of the academic-route rebuild. S1 (`classify.py`) and S2
(`terrain_smooth.py`) were already done and verified; neither was modified.

The old pipeline rendered every block as an instanced cube: 1,171,144 instances
in 1,041 HISM clusters = **14,053,728 triangles**, with the cull distance cut to
80 m. That is what the SIGGRAPH '25 "Minecraft to 3D" paper calls "an absurd
triangle mess (every block turned into geometry)". These steps replace it with
smooth terrain (S2) plus extracted objects (here).

---

## Deliverables

| Path | What |
|---|---|
| `tools/extract_structures.py` | S3+S4: building meshes, detail meshes, water layer |
| `tools/build_atlas.py` | S5: pack CC0 textures into atlas PNGs, plan the UV layout |
| `tools/import_chunks.py` | S5: UE-side import via standard `unreal.AssetImportTask` |
| `tools/test_extract_structures.py` | 29 tests for the mesher and the atlas |
| `tools/probe_material_api.py`, `probe_compile_check.py`, `probe_mat_stats.py`, `probe_mesh_api.py` | Engine-API probes (see "Probes" below) |
| `out/structures/manifest.json` | Per-building bbox, quads, materials, seating |
| `out/atlas/manifest.json` | Atlas layout, per-cell UVs, licence/attribution |
| `out/structures/*.obj` | 158 meshes + `detail_kinds/` library + `water_layer.obj` |

Run order:

```
python3 tools/build_atlas.py           # 1.6 s
python3 tools/extract_structures.py     # ~60 s
python3 tools/test_extract_structures.py
UnrealEditor-Cmd.exe project/MCReplica.uproject \
    -ExecutePythonScript=tools/import_chunks.py -unattended -nopause -nosplash
```

---

## Measured results

### Triangles

| | triangles |
|---|---|
| Building meshes | **277,014** |
| Detail library (S4, art-replacement surface) | 148,196 |
| Water | 18 |
| **All outputs** | **426,394** |
| *Level only (buildings + water, excl. detail library)* | *277,032* |
| Naive 12-per-block, same 430,501 voxels | 5,124,780 |
| **Old cube layer** | **14,053,728** |

**33.0× fewer triangles than the cube layer**, 12.1× fewer than naive
per-block emission. Per building, quad count is **2.2–3.1 % of the naive
face count** (biggest: 100,833 quads for 3,868,380 naive faces, ratio 0.026).

Excluding the detail library — which is duplicate geometry kept for art
replacement, not for the level — the level total is **277,032 triangles**,
i.e. **50.8× fewer than the cube layer**.

Faces per building: median 74, max 100,833 (id 1), min 10.

### Volume and position: unchanged

All **88 / 88** buildings have mesh bbox exactly equal to their
`structures.json` bbox + campus origin. Nothing was smoothed, shrunk or moved.
The manifest asserts this (`bbox_check.matching_structures_json = 88`), and a
test fails the build if it ever stops holding.

### Blocks partitioned

```
labelled_total            430,501
in_reported_buildings     427,065   = structure 313,162 + detail 113,903
unclaimed_fragments         3,436   in 334 fragments (see S1 finding below)
campus_total            1,949,579
```

Structure and detail meshes partition the 88 buildings exactly and never
overlap, so there is no z-fighting between them.

### Atlas

- **4096 × 2048** PNG, 8.4 MB, **22 cells** (8 cols × 4 rows), 504 px tiles,
  4 px gutter. Power-of-two in both dimensions.
- 20 families sourced from Poly Haven (CC0), 2 generated (`other`, `water`).
- Material slots actually used across all buildings: **14**.
- The cell list is derived from `block_families.FAMILIES` at run time, so when
  the art-director added a `sports` family (for the wool sports field) both the
  atlas and the mesher picked it up with no edit here — verified on a full
  clean rerun (22 cells, 0 missing, 29/29 tests still passing).

### UE import

```
material compile: ok=True no compile errors in MCReplica.log
material ready: M_MC_Atlas  BaseColor <- MaterialExpressionTextureSampleParameter2D
vertex/triangle counts read for 157 meshes
imported 158 meshes, 0 problems
meshes that lost over half their geometry: 0
placed 158 / 158 actors
```

Compile failures: **0**. Geometry loss: **0** (worst vertex ratio 0.93, most
1.00 — the importer welds coincident duplicates).

---

## Design decisions worth knowing

**Face culling is against campus-wide occupancy, not the object alone.** Two
touching buildings therefore do not grow interior walls into each other, and a
wall does not emit a face where a window will be meshed.

**Greedy meshing is capped at `--max-quad` 4 blocks.** Greedy merging alone
stretches one atlas cell across an entire 40 m facade. The cap bounds texel
density at the cost of some merging. The cap is recorded in the manifest so the
trade is visible rather than buried in a constant. Lower it for sharper
facades, raise it for fewer triangles.

**UVs stretch onto a cell, never tile across it.** A repeating cell pulls in its
neighbour's texels under mip reduction and bilinear sampling — a colour fringe
crawling along a wall at distance. `plan_layout()` in `build_atlas.py` is the
single source of truth for cell rectangles; `extract_structures.py` imports it
rather than recomputing, because two literals that disagree by a texel corrupt
every facade and nothing errors.

**`tile = 504`, not 512, so the atlas is POT.** `tile + 2*pad` must be a power
of two for the whole atlas to be. 504 is still a multiple of 8, so BC1/BC3
compression wastes no edge.

**The coordinate contract lives in one place.** MC (x, y, z) → UE
(x\*100, z\*100, y\*100) cm, Z up, as `MC_TO_UE` in `extract_structures.py`,
matching `terrain_smooth.build_tiles`. Meshes are written in **absolute world
cm**, so UE actors all sit at the origin — re-deriving a transform on the UE
side would be a second implementation of the contract.

**The detail library is duplicate geometry and is not placed.**
`out/structures/detail_kinds/*.obj` re-meshes the same voxels as the
per-building `*_detail.obj`, grouped by trim kind (window / door / fence /
railing / stairs / trim) so an artist can replace all fences at once. Only the
per-building meshes are authoritative; adding both z-fights. This is stated in
the OBJ header of each library file and in the manifest.

**8,599 detail components** were found (6-connectivity): stairs 52,086 blocks,
trim 30,854, windows 24,421, doors 5,544, fences 2,913, railings 197.

**Water is emitted, not skipped.** 13 blocks in 1,949,579 (0.0000067 %).
The layer exists so the pipeline has a water slot the engine can shade as its
own surface. At this size it is noise; it is reported, not hidden.

---

## Traps hit, and what they cost

These are all now covered by tests, because each one produced a *plausible*
wrong answer rather than an error.

**1. The MC→UE swap is a reflection, and winding must account for it.**
MC (x,y,z) → UE (x,z,y) has determinant −1. Winding decided in Minecraft axes
comes out inside-out after the permutation. Symptom: every face of every box
culled, so buildings are see-through rather than obviously wrong. Fixed by
`MC_TO_UE_HANDEDNESS`, derived from the permutation rather than hardcoded.

**2. The permutation was initially applied only to normals, never to the
geometry.** Caught by a signed-volume test that returned exactly half the
expected 64 m³.

**3. `nm` leaked across loop iterations in the material scan.** The block name
was resolved only on the first chunk using a given palette index, so on every
later chunk `nm` still held the *previous* iteration's value. A voxel's `nameid`
and its material `family` then came from two different blocks. The water layer
came out carrying brick, rock and wood while still being *called* water.
Nothing cross-checked the two. Now asserted by
`test_family_matches_name_for_every_voxel`, which checks name/family agreement
for every distinct pair present — it catches the class of bug without needing
to know the right answer for all 840 block names.

**4. The material cache had no invalidation key.** Changing a mapping rule left
the cache serving the old answer, and every run still reported success. The
cache now stores its inputs (`atlas_families`, source mtime, campus) and
rebuilds when they differ.

**5. `build_terrain_material_min.py`'s compile check never worked on UE 5.8.**
It calls `mat.get_shader_map_valid()`, which does not exist on this engine, and
its `except` returns `(None, "no shader-map query")` — so the step-gated abort
that script exists to provide never fired. Probing confirmed UE 5.8's Python
`Material` exposes **no** compile-check API at all: `material_compilation_errors`,
`compile_errors` and `cached_expression_data` all raise "Failed to find
property", and `unreal.MaterialStatistics` is an output struct with no way to
compute it from a material. `import_chunks.py` therefore checks the **editor
log** for `Failed to compile Material` / `Default Material will be used`. That
is weaker than a real API call (the log is written asynchronously relative to
`recompile_material`), but it is strictly better than checking nothing.

**6. The geometry-loss guard was passing the class, not an instance.**
`sub = unreal.StaticMeshEditorSubsystem` instead of
`unreal.get_editor_subsystem(...)`, so `get_number_verts` raised for all 158
meshes. All 158 imports reported "clean" while verifying nothing — a guard that
cannot fail reads as verification in the log. Now the counts are actually read
(157 meshes), and `import_meshes` emits an explicit problem if *no* mesh had its
counts read, so the guard cannot silently go inert again.

**7. A wrong test expectation is as costly as a wrong test.** Three of my own
assertions were wrong before the code was: two touching voxels greedily merge
to 6 quads (not 12); a closed-cube volume test must sum **both** triangles per
quad or it returns exactly half — a plausible-looking wrong answer; and greedy
meshing produces **T-junctions**, so "every edge has an exact reverse" is simply
the wrong invariant. Signed volume is the correct orientation test and survives
merging.

---

## Findings in S1 / S2 (reported, not fixed — both files are done and verified)

**A. 3,436 voxels in 334 fragments are labelled but belong to no building.**
`classify.py` assigns labels to all object voxels, then keeps only components of
at least `--min-structure` (default 40) as reported structures. The 334 dropped
fragments are 1–39 blocks each and are meshed **nowhere**. Small against
430,501, but it is real geometry missing from the output. If those should be
built, lower `--min-structure` and re-run S1 → S3. Reported in the manifest as
`blocks.unclaimed_fragments`.

**B. Building id 1 is not a building.** It holds **322,365 of the 427,065**
labelled voxels (75 %) with a bbox spanning 421 × 60 × 453 blocks — essentially
the whole campus. `classify.py`'s comment says it deliberately separates
buildings that touch by labelling only voxels that rise above the ground and
growing the rest to the nearest wall; on this save that produced one campus-sized
component. It contributes 100,833 of the 138,507 building quads (73 %). It
meshes correctly and its bbox matches, so this is not a bug in S3 — but
"88 buildings" is really "87 buildings plus the campus". Worth revisiting in S1
if per-building work (LOD, culling, art replacement) is planned.

**C. 10 of the 88 "buildings" contain no structure-role voxels at all** — ids
10, 6, 8, 333, 32, 198, 401, 399, 306, 328, sized 40–456 blocks. They are pure
detail (fences, signs, fittings) that passed the 40-block threshold. They emit
a detail mesh only, which is correct, but they are trim clumps rather than
buildings and will behave oddly under any per-building LOD policy.

**D. `block_families.family()` sends water to `"other"`.** Correct for a general
mapping, wrong here — the water layer needs to be identifiable and swappable. I
overrode it locally in `extract_structures.family_of` rather than editing
`block_families.py`, but the special case arguably belongs there.

---

## Probes

Four small scripts under `tools/` record what UE 5.8 actually exposes, because
the existing project scripts assume APIs that do not exist and hide the failure
behind `try/except`. They are cheap to run and are the reason the checks in
`import_chunks.py` work:

- `probe_material_api.py` — no compile-related member on `Material`.
- `probe_compile_check.py` — the three error properties all raise.
- `probe_mat_stats.py` — `MaterialStatistics` is output-only.
- `probe_mesh_api.py` — `get_number_verts(mesh, lod_index)` is the working
  signature.

---

## Verified vs unverified

**Verified by measurement:**
- 88/88 building bboxes equal `structures.json`.
- Triangle counts, quad counts, block partition, atlas dimensions, atlas cell
  non-overlap, POT-ness.
- 29/29 tests pass (`tools/test_extract_structures.py`).
- The material compiles and saves; the editor log contains no compile errors.
- 158 meshes imported, 158 actors placed, per-mesh vertex and triangle counts
  read for 157 of them with no geometry loss (worst ratio 0.93).
- Water layer carries only the water family.
- Every voxel's material family agrees with its own block name.

**Not verified — needs a human or a screenshot:**
- **Nothing has been rendered.** No image of these meshes exists. Whether the
  facades *look* right — texel density at `--max-quad 4`, whether the atlas
  reads as plausible masonry, whether UVs land where intended on a real wall —
  is unconfirmed. The UV-inside-its-cell test proves the addressing is
  self-consistent, not that it looks correct.
- The material's appearance under the project's lighting. `MCConsoleCommands.cpp::ApplyLook()`
  is the authority for the look and was not touched.
- Cook result. No Shipping cook was run as part of this task; the compile
  failures here are **0 at import time**, which is not the same as a clean cook.
  Per the known trap, delete
  `project/Intermediate/Build/Win64/x64/UnrealGame/Shipping/MCReplica` before
  building Shipping, and build `MCReplicaEditor` (Development) and `MCReplica`
  (Shipping) — they can disagree.
- Whether `T_MC_Atlas` survives a cook at 4096 × 2048 with the chosen
  compression settings, and whether 8.2 MB of atlas is the right budget once
  terrain and props also sample it.

**Deliberately not done (out of scope by instruction):**
- S6, deleting the voxel layer from the level.
- S7, the `project/Source/MCReplica/**` C++ split.
- Any placement of geometry in a meaningful layout — actors are all at the
  origin because the OBJs carry absolute world coordinates. Turning that into a
  real level arrangement (streaming, per-building LOD, collision) is the next
  task.
- No git commit.