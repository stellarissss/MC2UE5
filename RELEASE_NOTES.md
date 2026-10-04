# MC2UE5 — Release Build Notes

**Tag:** `v0.1.0` · **Engine:** Unreal Engine 5.8.3 (CL 58210709) · **Platform:** Win64

A Minecraft 1.16.5 save rebuilt as a walkable UE5 world.

---

## What this build is

The repository holds a two-stage pipeline that turns a Minecraft Java save
into UE5 content:

| Stage | Input | Output | In this build |
|---|---|---|---|
| Layer 1 — parse | region `.mca` files | MC2WV2 voxel stream (144 MB) | **not included** |
| Layer 2 — semantic rebuild | MC2WV2 | Landscape heightmaps, water, prop placements | **included** |

Layer 1's output is deliberately not committed — it is a 144 MB binary that is
rebuildable from the Minecraft save, which is the source of truth. This release
is therefore built from **layer 2**, which *is* committed: the campus terrain
as four 16-bit heightmaps plus the encoding metadata, and 1297 classified prop
placements.

### The world

- **Terrain** — 720 × 1040 blocks (72 m × 104 m), heights 4–63 blocks
- **1 vertex = 1 block**, XY scale 100 cm, so the mesh occupies exactly the
  world coordinates the pipeline's `(x, y, z) × 100` convention defines
- **1,568,352 triangles** of terrain across 4 tiles
- **1,297 props** — 609 structure, 601 plant, 54 tree, 30 prop, 3 building
- Walkable: complex-as-simple collision on a decimated terrain proxy
- Directional sun, sky light, sky atmosphere, exponential height fog

---

## Running it

Unzip and run `MCReplica.exe`. Controls are the default spectator-pawn
bindings — WASD to move, mouse to look.

The map opens at the campus centre on a `PlayerStart` placed from the
heightmap's actual surface height, so the pawn starts on the ground rather than
inside or above it.

---

## How this was built, and what changed

The upstream repository targets **UE 5.5.4**. This build targets **UE 5.8.3**,
and three things had to be solved to get there. All three are recorded here
because each one is a silent failure — the kind that reports success and ships
something wrong.

### 1. Landscape is unreachable from Python in 5.8

The upstream scripts build the terrain as UE5 Landscape actors through
`LandscapeEditorSubsystem`. On 5.8 **none of that API exists** in Python:

```
>>> [n for n in dir(unreal) if "Landscape" in n and n.endswith("Subsystem")]
[]
>>> hasattr(unreal, "LandscapeInfo")
False
```

`unreal.Landscape` *is* exposed, but spawning it yields a
`LandscapePlaceholder` with **zero components** — `ALandscapeProxy::
CreateLandscapeInfo` is a C++ member with no Python binding, so the placeholder
never becomes a landscape. `get_actor_scale3d()` on it reads back `(0, 0, 0)`.

**Resolution:** `tools/build_terrain_mesh.py` converts the heightmaps to meshes
and they are imported as StaticMesh assets. The coordinate contract is
preserved exactly — same decode formula, same 100 cm XY scale, same world
positions — so the terrain lands where phase 2 said it would.

### 2. The height decode divisor is 128, not 65535

UE maps a 16-bit heightmap sample onto a **signed** −256..+255.992 range:

```
z_cm = actor_offset_z_cm + (v - 32768) / 128 * z_scale_cm
```

Normalising by 65535 instead makes the terrain **128× too tall** — and reports
no error at all. Both the mesh builder and the UE-side importer use the
`/128` formula, and every tile is decoded and range-checked against the
recorded block heights *before* anything is created, so an inverted or
mis-encoded heightmap fails the build instead of shipping.

### 3. Collision needs a proxy, and lives on the mesh

A freshly imported OBJ has a `BodySetup` with **no convex hulls** and the
default `UseSimpleAsComplex` trace flag — so it has no collision surface at
all, no matter what the component says. Setting `collision_enabled = True` on
the component is accepted and then ignored.

Two fixes:

- the trace flag is set on the **mesh's BodySetup** to `UseComplexAsSimple`,
  which makes the render triangles the collision surface;
- collision runs on a **stride-4 decimated copy** of each tile — 24,552
  triangles instead of 392,088. Complex collision over the full-resolution
  meshes would cook to a physics mesh hundreds of megabytes across four tiles,
  which is not something a character controller should pay for. The proxy
  traces identically at pawn scale; the full-resolution mesh stays visual-only
  and carries Nanite.

The build verifies collision by reading the BodySetup back, **not** by trusting
the component flag — the same check that would otherwise have passed on a level
where the pawn falls through the world.

---

## Known limitations

These are inherited from the pipeline, not introduced by this build.

1. **No layer-1 block layer.** The 37.5 M-voxel HISM cube layer is absent —
   its input is not in the repository. The world is terrain plus classified
   props, not a block-exact replica.
2. **Props are primitive stand-ins.** `prop_placements.json` records recipes
   like `builtin:tree:cone_on_cylinder`, not meshes. These are realised as
   engine primitives at the recorded height, radius, orientation and
   ground-aligned position. **Silhouettes are invented; placement, scale and
   class are real.**
3. **No buildings.** 3 building instances were classified, but at this scale
   they read as blocks.
4. **Terrain texture is blended, not per-block.** Grass / sand by height,
   stone by slope, dirt in the transition band — from the Minecraft block
   palette. It is not the source save's biome-tinted grass.
5. **No water.** The save has 6 isolated water columns, which do not form a
   2×2 quad, so phase 2 emitted no water mesh. That is the data, not a defect.
6. **Frame rate is unverified.** This was built on a headless machine with no
   GPU. Quality tiers are configured (`DefaultScalability.ini`,
   `DefaultDeviceProfiles.ini`, `apply_quality.py`) but **no FPS number in this
   document is measured.**
7. **One cvar was dropped.** `r.Nanite.MaxPixelsPerEdge` is no longer
   `ECVF_Scalability` in 5.8 — the engine rejects it from
   `DefaultScalability.ini` with an ensure and ignores the value. It was
   removed rather than left as silent dead config.

---

## Reproducing

```bash
# 1. Materialise the LFS textures (git-lfs is not required)
python3 tools/fetch_lfs_raw.py --root <checkout>

# 2. Convert the heightmaps to terrain + collision-proxy meshes
python3 tools/build_terrain_mesh.py --root <checkout> --out ../terrain

# 3. Assemble the level headless
UnrealEditor.exe project/MCReplica.uproject \
    -ExecutePythonScript=project/Content/Python/build_release_level.py \
    -unattended -nopause -nosplash -nullrhi

# 4. Package
Engine/Build/BatchFiles/RunUAT.bat BuildCookRun \
    -project=<checkout>/project/MCReplica.uproject \
    -platform=Win64 -clientconfig=Shipping -cook -build -stage -pak -archive \
    -archivedirectory=<out>
```

Every step fails loudly. The level builder validates the heightmaps against
their recorded encoding before it creates anything, and exits non-zero if a
tile decodes out of range, a mesh fails to import, a collision proxy has no
collision surface, or the GameMode or pawn class does not resolve.

---

## Attribution

Built on the pipeline in this repository, which follows *Minecraft to 3D*
(SIGGRAPH Posters '25, DOI [10.1145/3721250.3743044](https://doi.org/10.1145/3721250.3743044)).
