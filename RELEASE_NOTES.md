# MC2UE5 — Release Build Notes

**Tag:** `v0.4.0` · **Engine:** Unreal Engine 5.8.3 (CL 58210709) · **Platform:** Win64

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

## Playability fixes in this build (v0.4.0)

Four defects stood between "the level exists and verifies" and "a player can
walk around it". Each one reported success somewhere, which is why they survived
a green build.

### 1. The camera could not turn: no input was mapped

`Config/DefaultInput.ini` carried only the axis dead-zone defaults -- there were
**no `+AxisMappings` or `+ActionMappings` at all**. The character binds the
legacy names `MoveForward`, `MoveRight`, `Turn`, `LookUp`, `Jump` and `Sprint`
(`MCReplicaCharacter::SetupPlayerInputComponent`), so with nothing mapped to them
the mouse could not turn the view and WASD did nothing: the reported "the camera
never moves, all I can see is sky". The mappings are now present (WASD / arrows,
mouse, gamepad, space, shift). Legacy mappings are read even when Enhanced Input
is the default input class, so `DefaultPlayerInputClass` did not need to change.

### 2. Movement was not camera-relative, and the body did not face travel

`MoveForward` used `GetActorForwardVector()`. With `bUseControllerRotationYaw`
off and no `bOrientRotationToMovement`, the body kept its spawn yaw, so "forward"
was a fixed world direction rather than the way the player was looking. Movement
input is now built from `GetControlRotation().Yaw`, and the movement component
turns the body to face its travel (`bOrientRotationToMovement`, `RotationRate`
540 deg/s).

### 3. The pawn fell through the terrain: complex-as-simple collision is never cooked in an uncooked `-game` session

The collision proxies use `CTF_UseComplexAsSimple`, so the engine cooks their
physics trimesh from the mesh's render triangles. That cook never happens in an
uncooked `UnrealEditor.exe ... -game` session:

- `UBodySetup::Serialize` only writes `CookedFormatData` when `Ar.IsCooking()`,
  so an editor `.uasset` carries no baked physics;
- the same function only sets `ChaosDerivedDataReader` when loading cooked data;
- `CreatePhysicsMeshes()`'s runtime-cook branch is gated on `IsRuntime(this)`,
  which needs the BodySetup's outer to resolve to a game world -- false for a
  package asset.

So the trimesh stayed empty, a straight-down complex trace missed, and the pawn
fell to Z ~ -151000. The fix is to **ship the game cooked**: the cooker runs with
`RequiresCookedData() == false`, so it builds and bakes the trimesh into the
cooked package (the cooked collision `.uexp` files are 40-333 KB, where the old
editor assets carried no trimesh at all). `bAllowCPUAccess` is set on the
proxies as well, which the cook requires.

### 4. Dead cvars in `DefaultScalability.ini` failed the cook outright

`Scalability.ini` may only set `ECVF_Scalability` console variables. Any other
cvar raises an engine **ensure**, and a failed ensure counts as an error, so the
cook commandlet reports failure and `BuildCookRun` aborts. The file carried
several (`r.Nanite.Streaming.StreamingPoolSize`,
`r.DynamicGlobalIlluminationMethod`, `r.Shadow.Virtual.Enable`,
`r.Streaming.PoolSize`, `r.Streaming.MaxTempMemoryAllowed`,
`grass.DensityScale`). They are removed; the ones that genuinely vary per tier
at runtime are applied by `apply_quality.py` instead.

---

## Running it

Unzip and run `MCReplica.exe`. Controls are WASD / arrows to move, the mouse to
look, Space to jump and Shift to sprint.

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

# 4. Package. The toolchain lives on Q:, and the Windows SDK registry entry is
#    lost whenever the system drive resets, so the SDK root is re-registered
#    (tools/reg_sdk.py) and UBT is pointed at it first:
#      UE_SDKS_ROOT=Q:\AutoSDK  WindowsSDKDir=Q:\WindowsKits
#      WindowsSDKVersion=10.0.26100.0  DOTNET_ROOT=Q:\dotnet  UBA_ROOT=Q:\UBA
#    Cook to loose files with -SkipZenStore; without it the cooked packages go
#    into the Zen store and the staging step cannot read them back.
Engine/Binaries/Win64/UnrealEditor-Cmd.exe <checkout>/project/MCReplica.uproject \
    -run=Cook -TargetPlatform=Windows -unversioned -SkipZenStore \
    -unattended -nopause -nosplash -nullrhi -stdout
Engine/Build/BatchFiles/RunUAT.bat BuildCookRun \
    -project=<checkout>/project/MCReplica.uproject \
    -platform=Win64 -clientconfig=Shipping -nocompile -nocompileeditor -skipcook \
    -stage -pak -archive -archivedirectory=<out> \
    -unattended -nopause -nosplash -NoCodeSign
```

Every step fails loudly. The level builder validates the heightmaps against
their recorded encoding before it creates anything, and exits non-zero if a
tile decodes out of range, a mesh fails to import, a collision proxy has no
collision surface, or the GameMode or pawn class does not resolve.

---

## Attribution

Built on the pipeline in this repository, which follows *Minecraft to 3D*
(SIGGRAPH Posters '25, DOI [10.1145/3721250.3743044](https://doi.org/10.1145/3721250.3743044)).
