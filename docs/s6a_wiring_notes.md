# S6A — cook, package, and first visual forensics

Author: engineering-lead-3 (程基岩). All numbers below were measured on this
host during this session unless marked **inferred**.

Engine: UE 5.8.3 (CL 58210709). Python: `Q:/MC2UE5/venv/Scripts/python.exe`.

---

## 0. TL;DR

The wiring fix **works**: 182 mesh actors (158 structures + 24 terrain) are in
the level, survive the cook, and are listed inside the packaged `.utoc`. The
cook builds **17 family materials** and reports **0 material compile failures**.
The build runs at ~300–400 fps.

**But the scene still does not show the family materials' colours.** Every
surface in the rendered frame is low-saturation tan/brown. Measured over the
whole lower frame, **0.0 % of pixels are green**, in a terrain that is
**82.1 % grass by surface area**, whose grass texture is `(84,104,66)` — clearly
green. The pre-existing voxel-pipeline render (`shots/final_campus.png`) shows
**36.2 % green**.

So: **the reference/cook problem is fixed; a colour problem remains, and it is
in the render path, not in the asset graph.** Details and the evidence chain in
§5.

---

## 1. What was produced

| Artifact | Path |
|---|---|
| Cook + package (2 steps, `-SkipZenStore`) | `repo/tools/package_shipping.bat` |
| Pak-content reader (engine's own index) | `repo/tools/pak_report.py` |
| Frame texture statistics | `repo/tools/frame_stats.py` |
| Launch + park + capture harness | `repo/tools/capture_run.py` |
| Game-window selector (not editor) | `repo/tools/shot_game.py` |
| Package #1 (assets as of 12:28) | `Q:/MC2UE5/dist_verify` |
| Package #2 (assets as of 12:38 rebind) | `Q:/MC2UE5/dist_verify2` |
| Screenshots | `Q:/MC2UE5/shots/s6a/*.png` |
| Cook logs | `Q:/MC2UE5/logs/s6a/cook1*.log`, `cook2*.log` |
| Pak listings | `Q:/MC2UE5/logs/s6a/pak_lists*/` |

---

## 2. Pak acceptance — the number that was 0 last time

Method: `UnrealPak -List` (parses the container index). **Not** a byte grep —
the index and payload are compressed, so a raw search is meaningless in both
directions.

`dist_verify2` (the current build):

```
MCReplica-Windows.utoc   1195 entries
    Maps/MCReplica        1     PRESENT
    MC/Structures       316     PRESENT   (158 meshes x uasset+uexp)
    MC/Terrain           48     PRESENT   (24 tiles  x uasset+uexp)
    MC/Families          34     PRESENT   (17 materials + 17 textures)
    MC/Atlas              0     *** ABSENT ***
```

`MC/Atlas` absence is **correct and expected now**: the meshes were rebound to
`M_MC_<family>` at 12:37 and `M_MC_Atlas` is no longer referenced by anything
in the level, so the cooker culls it. This is a correction to the brief's
requirement that `MC/Atlas` appear in the pak — under the per-family
architecture it must not.

`global.utoc` (0 entries) is the engine's global-shader container; UnrealPak
cannot list it standalone (`Missing directory index`) and `pak_report.py`
labels it rather than failing the build over it.

Family coverage: **17 of 22** family materials are cooked (see §10 — the family
set has since grown to 23 with `glass`, so re-derive this before quoting). The
5 that are not
(`asphalt`, `path`, `roof`, `sports`, `tiles`) are **correctly culled** — they
have **zero** mesh references. Verified by counting `usemtl` across every OBJ:

```
structures usemtl: metal wood quartz brick gravel concrete fabric greystone
                   plaster granite rock leaves other bark water   (15)
terrain    usemtl: grass soil concrete brick leaves gravel fabric
                   greystone rock metal                            (10)
union actually referenced = 17
```

Note `sports` has 0 references — consistent with the known S5.5 defect in the
brief (the sports field is mis-classified and never binds `sports`).

Cook statistics (`cook1_recook.log`):

```
Packages Cooked: 823
Failed to compile Material: 0
MaterialShader  Assets Built: 17        <- not vacuous; 17 materials really built
MaterialTranslation Assets Built: 0
```

---

## 3. Screenshots, each tied to its build and parameters

All captured with `PrintWindow` + `PW_RENDERFULLCONTENT`. **Important:** a bare
`focus_capture.py --match mcreplica` selected the *editor* window
(`MCReplica （64-位 Development PCD3D_SM5）`) whenever one was open, so
`shot_game.py` was written to reject editor/crash titles and prefer the exact
title `MCReplica`. Every image below is from the **packaged Shipping** binary
`.../Binaries/Win64/MCReplica-Win64-Shipping.exe`.

| File | Build | Args |
|---|---|---|
| `V2_on_0/1/2.png` | `dist_verify2` | `-MCdiag` |
| `V2_field_0/1.png` | `dist_verify2` | `-MCdiag -MCstop=0` (sports field) |
| `V2_none_0/1.png` | `dist_verify2` | `-MClayers=none` |
| `V2_terrainOnly_0/1.png` | `dist_verify2` | `-MClayers=-vox,-struct` (see §6 — only `-vox` applied) |
| `V2_structoff_0.png` | `dist_verify2` | `-MClayers=-struct` |
| `voxON_*`, `voxOFF_*`, `POLL_*`, `none_*`, `FIELD_*` | `dist_verify` | earlier package, same binary |

Camera: default spawn `loc=(1632,-21496,815) cam=(1632,-21496,980) rot=(p0 y-93)`
for all non-`-MCstop` shots — byte-identical across runs (verified by regex over
`logs/mc_runtime.txt`). `-MCstop=0` parks at the sports field
`(14966,-21166,2155)`.

Runtime census after the wiring fix: `meshes=182 terrain(vis=0 col=0)`.
**182 = 158 + 24, so the meshes are loaded.** `vis=0 col=0` is a **stale
detector, not a fact**: `MCConsoleCommands.cpp` identifies terrain by
`Mesh->GetName().Contains("overworld")` (lines 81-91 and 360), but the new
tiles are named `terrain_<x>_<y>`. The census is lying; the geometry is there.

---

## 4. Tiling measurement — the required numbers

Noise floor = two captures, same build, same camera, ~4 s apart.

| Comparison | max | mean | px > 8 | % > 8 |
|---|---|---|---|---|
| **noise floor** `V2_on_0` vs `V2_on_1` | 35 | 0.45 | 620 | 0.061 |
| `V2_on_2` vs `V2_none_0` | 43 | 0.81 | 1898 | 0.185 |
| `V2_on_2` vs `V2_terrainOnly_0` (voxel off) | 41 | 0.67 | 1370 | 0.134 |

The old frame-hash gate is unusable here — the renderer is not frame
deterministic and even two identical viewpoints differ by max 35. All of the
above are *small*, but the voxel-off difference (1370 px) is only ~2.2× the
noise floor (620 px), so the voxel layer contributes little that is visible:
it is almost entirely buried under the new terrain and structures. **The brown
scene is the S3/S4/S5 pipeline, not the old cube layer.**

Region statistics (`frame_stats.py`, `V2_on_2.png`):

| Region | mean RGB | local σ (8×8) | grad | Δ (Laplacian) | hue clusters >1 % |
|---|---|---|---|---|---|
| sky (top strip) | (109.6,134.0,155.3) | 1.28 | 1.08 | 1.60 | 10 |
| façade | (95.5,84.0,71.6) | 7.76 | 5.59 | 7.62 | 9 |
| ground mid | (136.4,116.6,93.7) | 2.75 | 4.23 | 6.12 | 5 |
| ground near | (105.1,88.3,69.5) | 4.95 | 3.92 | 4.17 | 14 |

Hue census over the whole lower 65 % of the frame (sky excluded):

| Frame | green % | brown % | blue % | mean sat |
|---|---|---|---|---|
| `V2_on_2` (new pipeline) | **0.0** | 85.1 | 4.2 | 0.287 |
| `POLL_2` (new, sports field) | **0.0** | 68.1 | 18.3 | 0.285 |
| `final_campus.png` (old voxel pipeline) | 36.2 | 0.0 | 42.9 | 0.328 |

### Verdict on "is it still flat colour / is tiling working?"

**Tiling cannot be confirmed, and on the colour evidence it is failing for
colour.** The specific claim to test was: does the ground show per-block
texture rather than one stretched average colour. Measured:

- The **ground's chroma matches the `soil` texture almost exactly**
  (render (105.1,88.3,69.5) vs soil (104.1,90.0,74.1)), and does **not** match
  `grass` (84.2,104.2,66.0). A 504-px-stretch average of the grass cell would
  still be *green*, so "flat colour" alone does not explain brown — the ground
  is showing a soil-like albedo.
- The **façade** has real high-frequency content (local σ 7.8, chroma σ 0.21)
  but its chroma autocorrelation is **broad and non-periodic** (r = −0.05 at a
  133 px lag) — consistent with geometric faceting (window slits, shadows)
  rather than a repeating 1-block texture.
- Source textures are healthy and distinct: `grass` chroma σ 0.175, `brick`
  0.242, `concrete` 0.019 — and all 22 source PNGs differ in size, so no
  texture-collision bug.

**Conclusion: "tiling 生效" cannot be reported — the rendered surface does not
carry the family albedos at all.** This is a stronger and more specific finding
than "still flat colour".

---

## 5. Evidence chain — the asset graph is correct, so the loss is in render

I walked the whole chain from the cooked package. Every link is correct:

1. **Cooked map** (`Saved/Cooked/.../Maps/MCReplica.umap`) references
   `MC/Structures` ×158 and `MC/Terrain` ×24. ✓
2. **Cooked mesh** `bld_001_structure.uasset` references 13 distinct
   `M_MC_*`: bark, brick, concrete, fabric, gravel, greystone, leaves, metal,
   other, plaster, quartz, rock, wood — the same 13, in the same order, as the
   OBJ's `usemtl` first-seen order. ✓
3. **Cooked mesh** `terrain_-016_-032.uasset` references `M_MC_grass` +
   `M_MC_soil`. ✓
4. **Materials**: `M_MC_grass` references `T_MC_grass`; `M_MC_soil` →
   `T_MC_soil`; `M_MC_concrete` → `T_MC_concrete`; `M_MC_brick` → `T_MC_brick`;
   `M_MC_quartz` → `T_MC_quartz`; `M_MC_wood` → `T_MC_wood`. No material points
   at a sibling's texture. ✓
5. **Textures**: `out/families/grass.png` mean `(84,104,66)` — green. The other
   21 are distinct. ✓
6. **Geometry**: terrain surface area by material is **82.1 % grass**,
   6.9 % fabric, 3.9 % brick, 3.0 % soil (computed from the terrain OBJs by
   triangle area). ✓

So a scene that is 82 % grass, bound to a grass material that samples a green
texture, renders **0 % green**. The defect is downstream of the assets — in the
material's runtime evaluation, the mesh's UV/material-index assignment, or the
look pipeline. **I did not localise it further**; that is the next step, not a
claim I am making.

**Independently cross-checked (quality-lead-2).** They rebuilt the surface
family map from their own data (not importing mine) and used the spawn data
(`logs/spawn.json`) to compute the family composition *inside the camera
frustum*: grass **45.9 %** at half-angle 30°, 43.8 % at 45°, 42.2 % at 60°
(fabric 26–39 %, brick 11–18 %). So the camera is **not** pointed away from
grass. If the family albedo reached the screen the frame should be ~42 % green;
it is **0.0 %**. This independently confirms the loss is albedo, not framing.

**Related but independent defect (do not conflate).** quality-lead-2 also
confirms the sports field's fabric (near-white) + brick (orange-red) combination
is a *separate* blocker: `pink_terracotta` → brick and `lime/green_wool` →
fabric, with the `sports` family used 0 times. So even once the albedo reaches
the screen, the field colour is **still** wrong. Fixing one will not fix the
other.


Things ruled out along the way:

- **Not exposure/lighting being off-spec**: `logs/applylook.txt` shows
  `sun=10 sky=0.250 ev=0.50 fog=0.0015 volumes=1` — the documented working
  values, and the PostProcessVolume was found (volumes=1).
- **Not a null texture sampler** (that renders white, not brown).
- **Not the voxel layer masking it**: disabling voxels changes the frame by
  ~2× the noise floor and nothing visible (§4).
- **Not the atlas**: `M_MC_Atlas` is not referenced by the level, the meshes or
  the terrain any more.

---

## 6. Defects and contradictions found (important)

### 6.1 `-MClayers` silently honours only the FIRST comma-separated token

> **KNOWN INVALIDATING CONDITION FOR THIS A/B — read before quoting any
> `-MClayers` comparison.** If a `-MClayers` spec contained more than one
> token, only the first was applied, so **every multi-token layer A/B is an
> invalid conclusion** (it measured the first token only). Single-token specs
> are fine. Confirmed independently by quality-lead-2 as the correct reading.

Verified by four launches, reading `logs/mclayers.txt`:

```
-MClayers=-struct            -> spec=... struct=0 ...            (applied)
-MClayers=-vox,-struct       -> spec=vox=0 struct=1 terrain=1    (2nd token LOST)
-MClayers=-terrain,-shadow   -> spec=... terrain=0 shadow=1      (2nd token LOST)
-MClayers=none               -> vox=0 struct=0 terrain=0         (single token, ok)
```

The cause is `FParse::Value(FCommandLine::Get(), TEXT("MClayers="), Spec)` in
`MCLayerControl.cpp:177` — the value is truncated at the comma, so
`ParseIntoArray(..., ",")` only ever sees one element. **Any multi-token A/B
already run is invalid** (it measured the first token only). Single-token specs
are fine. Suggested fix: parse the spec by hand out of `FCommandLine::Get()`,
or require repeated `-MClayers=` switches.

### 6.2 The `-MCstop=` park is unreliable at short settle times

`-MCstop=N` parks 14 s after BeginPlay. At a 22 s capture the camera was still
at spawn in some runs; at 26–30 s it parked correctly. The runtime log stops at
~11–21 s depending on run, so the diagnostic's own time series is not a reliable
"the game is alive" signal. Capture at ≥26 s.

### 6.3 Crashes in `Saved/Crashes` are real but are the EDITOR's, not Shipping's

128 crash dirs existed before my work, 129 after. Every recent one I opened is
`UnrealEditor.exe -game`, e.g.

```
Assertion failed: NumAcceptedStaticMeshes >= 0 && MDCIdx < ((uint16)0xffff)
  ShadowSetup.cpp:1611
cmd: -windowed -ResX=1280 -ResY=720 -nosplash -MClayers=-vox -MCstop=0 -log -game
```

and two `D3D12RenderTarget.cpp:599 Ensure condition failed: InRHITexture`.
**None of my packaged Shipping runs crashed**, and none produced a new crash
dir. Anyone capturing via `UnrealEditor.exe -game` is on a crashier path than
the packaged build; the packaged build is the one to trust for visual evidence.

### 6.4 Corrections to the brief's reference numbers

- **"1272 actors"** — I could not confirm actor count from a cooked build
  (labels do not survive). What the *cooked map* proves is 158 structure + 24
  terrain mesh references. The runtime census reports **182** mesh actors,
  which matches 158 + 24.
- **"terrain(vis/col)"** in the runtime line is a **stale detector** (§3); it
  is not evidence that terrain is missing.
- **"22 族材质"** — 22 exist on disk, **17 reference anything**, and only 17
  cook. The brief's reference mean list includes `sports`, which has 0 refs.
- **`MC/Atlas` in the pak** — correctly absent now (§2); the brief's
  "must appear in the pak" predates the per-family change.

### 6.5 Grey materials: it is a cluster of four, not one pair (noted, not fixed)

Brief §4 asked for granite vs greystone to be noted, not fixed. Measured on the
**source family textures** (`out/families/*.png`), converting mean sRGB to
CIELAB and taking ΔE76:

| family | mean RGB | Lab |
|---|---|---|
| granite | (110,110,112) | (46.48, 0.42, −1.10) |
| greystone | (104,106,104) | (44.56, −1.15, 0.87) |
| concrete | (112,112,110) | (47.16, −0.39, 1.08) |
| rock | (96,94,92) | (39.98, 0.38, 1.40) |
| gravel | (140,138,134) | (57.45, −0.05, 2.38) |

ΔE76 against the ≥8 separation guideline:

| pair | ΔE76 | |
|---|---|---|
| granite vs greystone | **3.17** | below (matches the brief's number exactly) |
| granite vs concrete | **2.42** | below |
| greystone vs concrete | **2.72** | below |
| greystone vs rock | **4.86** | below |
| granite vs gravel | 11.52 | ok |

**The brief's single pair is the tip of a bigger problem: granite, greystone
and concrete are mutually indistinguishable (ΔE76 ≤ 3.2), and rock is nearly so
against greystone.** These four are the dominant façade/stone families, so the
whole stone palette reads as one grey — consistent with QA reading them as the
same material on screen.

On screen, none of this is currently visible: the frame renders 0 % green and a
uniform tan, so the greys do not even separate from the ground. **I did not
nudge any target**, per the brief. When the albedo loss in §5 is fixed, re-check
this cluster — it will then be the visible defect.


---

## 7. `tools/` exists in two places — recommendation

Two trees, overlapping partially:

- **`Q:/MC2UE5/tools/`** — 17 files, **none version-controlled** (verified:
  `git ls-files | grep focus` returns nothing). Holds `focus_capture.py`,
  `shot.py`, `seq_capture.py`, `capture_game.py`, `verify_shipping.py`,
  `render_topdown.py`, `make_release.py`, `reg_sdk.py`, plus agent-written
  census/probe helpers.
- **`Q:/MC2UE5/repo/tools/`** — 34 tracked files, the pipeline
  (`classify.py`, `extract_structures.py`, `build_atlas.py`,
  `import_family_materials.py`, …). Also the documented home of `reg_sdk.py`
  (identical to the top-level copy).

Overlap: `reg_sdk.py`, `make_release.py`, `fetch_lfs_raw.py` are byte-identical;
`build_terrain_mesh.py` and `make_character.py` differ.

**Recommendation: keep `repo/tools/` as the single home, and move the capture
tools in.** The reason is not tidiness — `focus_capture.py` is the *only*
working screenshot path this project has (documented in REFACTOR_PLAN.md and
qa_s5_report.md), and it is **outside version control**. A rebuild of the
checkout loses it, and every visual claim in this project depends on it. I did
**not** move anything (per the brief); I added my own capture tooling under
`repo/tools/` and pointed it at `Q:/MC2UE5/tools/focus_capture.py` by absolute
path so nothing is duplicated.

---

## 8. Verified vs inferred

**Verified (measured or read from an artifact):**
- Pak contents and per-prefix counts (§2), via `UnrealPak -List`.
- Cook stats: 823 packages, 0 material compile failures, 17 MaterialShader
  assets built (§2).
- 17 of 22 families referenced / cooked; 5 unreferenced (§2).
- Cooked map → mesh → material → texture chain is internally consistent (§5).
- Terrain surface area 82.1 % grass (§5).
- Frame statistics, noise floor, hue census (§4).
- Ground chroma ≈ soil, not grass (§4).
- Grey-family separation: granite/greystone ΔE76 = 3.17, reproduced exactly;
  granite/concrete 2.42, greystone/concrete 2.72, greystone/rock 4.86 (§6.5).
- Camera frustum is 42–46 % grass — cross-checked independently by
  quality-lead-2 from their own family map + spawn data, so 0 % green cannot be
  a framing artefact (§5).
- `-MClayers` first-token-only behaviour (§6.1), from `logs/mclayers.txt`.
- Run-time census `meshes=182`; the `vis=0` detector is stale (§3).
- Frame time p50 2.4–3.3 ms on the packaged build (§9).
- All 129 crash dirs are editor runs (§6.3).

**Inferred / not proven:**
- *Where* the albedo is lost (material evaluation vs UV/material-index vs look
  pipeline). The evidence places it downstream of the assets; it does not name
  the line.
- That the visible brown ground is a specific terrain material. It matches the
  `soil` texture's mean and chroma, but I did not isolate a single tile in a
  frame and prove its material index.
- Actor count 1272 — unconfirmable from a cooked build as described.

**Not attempted (out of budget):** the prebake-vs-baked atlas frame A/B from the
earlier brief. Under the per-family architecture it is moot: `M_MC_Atlas` is not
in the render path any more, so swapping its source PNG cannot move a pixel.
The equivalent question — "do the family albedos reach the screen" — is what §4
and §5 answer, and the answer is no.

---

## 9. Frame rate

From `logs/mc_runtime.txt` (packaged build, `-MCdiag`):

```
ft[n=334 min=1.6 p50=3.0 p95=3.3 max=4.6 mean=3.0 class=ok]
```

Per-run p50: **2.4–3.3 ms → ~300–400 fps**. `class=ok` throughout (no
plateau, no spikes) on the packaged build. The honest figures are the ring
buffer's distribution, not the `frame=` column (that is clamped by
`MaxDeltaTime`).

---

## 10. Correction: family set grew mid-session (S5.6 + colour variants)

While this task ran, S5.6 (`2150ba8`, `b4ff4e8`, `b5f587a`) added a **`glass`**
family and re-derived family textures from CC0 sources, and a colour-variant
pass added 26 tinted variants. Current state, measured:

| | count |
|---|---|
| base family PNGs in `out/families/` | **24** |
| colour-variant PNGs in `out/families/` | **26** (brick_cyan, concrete_white, fabric_lime, glass_red, …) |
| `M_MC_*` / `T_MC_*` in the UE project | **23 / 23** |
| colour-variant uassets in the UE project | **0** |

So: `glass` **is** imported; `bars` exists on disk but is **not** imported; and
**none of the 26 colour variants are in the UE project yet** — they are a
data-side product, not yet in the render path. This is consistent with the
known `sports` defect (MC dye prefixes → wrong family) still being open.

This supersedes the "22 families" figure used in §1–§5 and in the brief's
reference list. **The conclusions do not change** — pak contents, 0 % green,
and the albedo chain were all measured against the cooked build and remain
valid; only the *denominator* moved. §2's "17 of 22" was correct for the
22-family set as it stood; `glass`, `bars` and the variants were not in the
OBJ `usemtl` census I ran (`out/structures/*.obj` predate the S5.6 work).
Re-run `pak_report.py` after the next cook for the current split.

Verified that the S5.6 re-derivation did **not** move my headline premise:
`grass.png` is still mean **(84.0, 104.0, 66.1)** — green — at 13:18
(regenerated), and `glass.png` is (58,68,74).

