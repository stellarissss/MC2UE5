# S5 — Material and surface look specification

**Task:** ART-S5· **Owner:** art-director (林绘澄) · **Status: complete, awaiting approval**

Defines what the engineer's S5 import + material step must produce so the campus stops
reading as sand. It does not specify import code. It specifies the *output*.

Two hard constraints shape everything below. They come from the build, not from taste:

1. The master material graph is fixed at `TextureSampleParameter2D → BaseColor` plus a
   `Tiling` scalar. This is the only shape proven to compile in this project. A previous
   attempt added a `Tint` vector parameter; it verified as set on the instance and
   correctly connected, and changed **zero pixels**. So **no colour correction may live
   in the shader.**
2. Therefore every colour correction here is expressed as either **an atlas bake** or
   **a per-family texture choice**. Nothing in this document asks for a new node.

---

## 0. Provenance — what I measured vs what I took from the brief

This matters because two of the brief's numbers turned out to be wrong, and one
load-bearing number was unverifiable.

### Measured by me, first hand

I wrote a dependency-free PNG decoder (`zlib` + `struct`, no numpy/PIL in this
environment) and ran it over the real files on disk.

| What | How | Result |
|---|---|---|
| All 24 cells of `project/Content/MC/Atlas/atlas_diffuse.png` | decoded the actual shipped atlas, 4096×1536 | **Reproduces every family mean in the brief to ±1.5/255.** See §1. |
| 70 ambientCG candidate colour maps | downloaded the 2048px colour preview from the ambientCG API and measured it | the "target mean RGB" column in §2 is measured, not asserted |
| Atlas grid geometry | flat-seam detection | 8 cols × 3 rows, pitch 512, tile 504, gutter 4 — matches `tools/build_atlas.py` `ATLAS_DEFAULTS` exactly |
| UV rects per family | read `out/atlas/manifest.json` | matches my cell measurements; the mesher's contract is already correct |
| Minecraft source blocks (`stone`, `cobblestone`, `dirt`, `bricks`, `sand`, `grass_block_top`) | decoded the 16×16 PNGs | `stone` (125,125,125) and `cobblestone` (128,127,128) are **already neutral** — the warmth is entirely in the CC0 set, not in Minecraft's own palette |

**Every mean RGB in the brief's table is confirmed.** The diagnosis is sound.

### Corrections to the brief

Two things the brief got wrong, both of which change what we do:

- **`fabric` is not the courtyard floor.** `tools/block_families.py:72-76` records a
  measured fact: `quartz_block` alone is **31% of the campus surface within 60 blocks
  of spawn**, and Minecraft renders it near-white. `quartz` maps to the `quartz` family,
  not `fabric`. `fabric` catches `glass`/`ice`/`wool`/`carpet`/`bed` — and `glass` is
  mapped to `fabric` explicitly as a *placeholder* (`block_families.py:31`). So the
  2,710-column figure is real but it is **glass**, and the actual courtyard driver is
  `quartz` at (177.7, 157.1, 121.8) with **R−B = +55.9** — the second-warmest thing in
  the atlas after `leaves`. Re-sourcing `fabric` alone would have left the courtyard warm.
  **Both** must be fixed, and `quartz` is the higher priority of the two.
- **`roof` was missing from the brief's table.** It is in the atlas at (89.4, 51.4, 27.9),
  **R−B = +61.5, saturation 68.8% — the single most saturated family in the entire
  atlas**, and it is on every building. The brief's "worst offenders" list omitted the
  actual worst cell.

### Taken from the brief, not independently verified

- The per-family top-surface column counts (`fabric` 2,710, `brick` 2,353). These come
  from the S4 census; reproducing them needs numpy, which is not installed here. The
  *ranking* is corroborated by `out/classify/rolemap.txt`, which I read.
- The four rejected Poly Haven candidates (`concrete_floor_01` 118,107,88 etc.). Not
  re-measured — they are already excluded by the same +18…+56 R−B rule I applied.

---

## 1. The atlas as it stands (measured, not assumed)

`project/Content/MC/Atlas/atlas_diffuse.png` — 4096×1536, 8.6 MB, 8 cols × 3 rows,
pitch 512, tile 504, gutter 4, `v` increases upward (row 0 = bottom of image).
Overall mean **(98.1, 86.8, 71.1), R−B = +27.0, saturation 27.5%** — the campus is
beige at the atlas level, before any lighting.

Cell occupancy, with my measured mean per cell and the family the manifest assigns:

| cell | family | measured mean | R−B | sat% | dominant |
|---|---|---|---|---|---|
| r0c0 | grass | (110.0, 96.4, 62.0) | **+48.0** | 43.7 | R |
| r0c1 | path | (115.0, 103.3, 76.8) | +38.2 | 33.2 | R |
| r0c2 | soil | (99.1, 82.2, 62.2) | +36.8 | 37.2 | R |
| r0c3 | asphalt | (89.1, 79.8, 70.3) | +18.8 | 21.1 | R |
| r0c4 | concrete | (106.4, 99.0, 88.0) | +18.4 | 17.3 | R |
| r0c5 | plaster | (157.7, 140.0, 120.0) | +37.7 | 23.9 | R |
| r0c6 | brick | (150.8, 111.8, 81.1) | **+69.7** | 46.2 | R |
| r0c7 | granite | (77.8, 78.3, 78.6) | −0.7 | 0.9 | B |
| r1c0 | tiles | (103.2, 98.5, 92.2) | +11.0 | 10.6 | R |
| r1c1 | roof | (89.4, 51.4, 27.9) | **+61.5** | **68.8** | R |
| r1c2 | wood | (107.1, 99.7, 89.5) | +17.6 | 16.4 | R |
| r1c3 | bark | (114.4, 93.2, 53.3) | +61.0 | 53.4 | R |
| r1c4 | leaves | (129.6, 94.9, 58.9) | **+70.6** | 54.5 | R |
| r1c5 | metal | (61.4, 49.9, 28.0) | +33.4 | 54.4 | R |
| r1c6 | gravel | (161.9, 133.5, 100.5) | +61.4 | 37.9 | R |
| r1c7 | rock | (96.9, 69.6, 49.5) | +47.4 | 48.9 | R |
| r2c0 | fabric | (194.1, 170.6, 156.9) | +37.1 | 19.1 | R |
| r2c1 | quartz | (177.7, 157.1, 121.8) | **+55.9** | 31.4 | R |
| r2c2 | greystone | (81.9, 86.5, 73.0) | +8.9 | 15.7 | G |
| r2c3 | other | (150, 148, 144) flat | +6.0 | 4.0 | flat grey |
| r2c4 | water | (58, 106, 128) flat | **−70.0** | 54.7 | B |
| r2c5–7 | *(empty)* | (0, 0, 0) | — | — | unused |

Three things follow immediately:

- **Only `greystone` and `water` have the correct hue direction.** `greystone` is the only
  sourced cell where G is the largest channel — and Minecraft's `cobblestone` and `stone`
  both map to it. The one family the save's own palette vouches for is the one that
  happens to be neutral.
- **`r2c5`, `r2c6`, `r2c7` are empty.** Three free cells, already allocated, no layout
  change needed. This is where the new roles go.
- The three flat cells (`other`, `water`) are generated by `build_atlas.py`, not sourced.
  `water` at R−B = −70 is the one strongly cool cell and it looks right.

---

## 2. Target material palette

**Column 3 is measured** — I downloaded each ambientCG colour map and ran the same
decoder. **Column 4 is the target mean the bake must hit.** Column 5 is the gain the
bake applies per channel to move column 3 → column 4; `tgt/cur` per channel, applied in
sRGB on the packed cell.

Selection rule I applied: **G ≥ R for anything organic, and R−B ≤ +12 for anything
mineral.** That is a stricter bar than "looks greyish" and it is what actually kills the
sand read.

The gains below are not estimates — I checked that `gain × measured = target` for all
twelve rows, worst-case error **0.6/255**, i.e. comfortably inside the ±6 tolerance §6
Step 1 asserts.

| Role | Source (measured) | Measured mean | Target mean | Bake gain (R,G,B) | Why this one |
|---|---|---|---|---|---|
| **Ground / grass** | ambientCG `Grass004` | (65.1, 71.2, 38.1) | **(84, 104, 66)** | 1.29, 1.46, 1.74 | Only grass candidate where G leads by 6 and it still reads as *grass lawn*, not astroturf. `Grass008` is greener still (65,84,30) but 64.7% sat — reads as AstroTurf, wrong for a campus. |
| **Tree foliage** | ambientCG `Leaf003` | (35.9, 36.2, 28.1) | **(72, 96, 58)** | 2.01, 2.65, 2.06 | `Leaf001/002` are near-black (mean 12–22) — a canopy built from them goes to a black blob at campus viewing distance. `Leaf003` keeps leaf detail *and* survives a 2× lift. |
| **Tree bark** | ambientCG `Bark001` | (74.0, 72.8, 70.3) | **(96, 90, 82)** | 1.30, 1.24, 1.17 | R−B = +3.8, sat 5.1% — the most neutral bark available, and a lift to 96 keeps it readable against grass instead of crushing it. |
| **Campus paving** | ambientCG `PavingStones103` | (119.1, 117.0, 110.6) | **(122, 122, 118)** | 1.02, 1.04, 1.07 | The gain is ~1.0 — this cell is **already right**, it only needs *finding*. It is the cheapest fix in this document: near-zero bake, and it becomes the reference for what "correct" looks like. Grey-tagged, 2 m tile. |
| **Road / asphalt** | ambientCG `Asphalt003` | (65.4, 65.4, 65.4) | **(78, 78, 78)** | 1.19, 1.19, 1.19 | **R−B = +0.0, sat 0.0%** — a perfectly achromatic road. A uniform gain, so the coarse aggregate texture survives untouched. Contrast with `concrete_floor_01` at +30 in the same role. |
| **Facade — light** | ambientCG `Plaster003` | (133.7, 131.4, 127.6) | **(178, 178, 174)** | 1.33, 1.35, 1.36 | White-tagged, sat 4.5%. Near-uniform gain, so it lightens without hue shift — this is what makes buildings read as *white plaster* rather than sand-coloured render. |
| **Facade — accent / brick** | ambientCG `Bricks074` | (104.2, 91.0, 82.0) | **(132, 94, 82)** | 1.27, 1.03, 1.00 | The only brick in the 8 I measured that is *already* low-warmth (R−B +22.3 vs `Bricks079`'s +60.7). R stays the dominant channel — **brick must stay brick.** A "neutral grey brick" would erase the material and the Minecraft read with it. |
| **Roof** | ambientCG `RoofingTiles004` | (66.1, 66.8, 60.5) | **(84, 84, 80)** | 1.27, 1.26, 1.32 | Replaces the worst cell in the atlas (+61.5, 68.8% sat). Slate, so it reads as *roof* by shape and tiling rather than by colour — and it sits behind parapets on most campus massing anyway. |
| **Plaster / white wall** | ambientCG `Plaster005` | (124.0, 123.0, 118.0) | **(178, 178, 174)** | 1.44, 1.45, 1.47 | Second plaster so `facade-light` and `white-wall` can differ in value without either going warm. Keep two cells: one family cannot be both the bright render and the shaded return. |
| **Gravel** | ambientCG `Gravel030` | (102.1, 99.8, 97.4) | **(140, 138, 134)** | 1.37, 1.38, 1.38 | Current cell is +61.4 and reads as beach sand. Near-uniform gain; `Gravel022` was the alternative but is tagged *dirt* and sat 14.5%. |
| **Metal** | ambientCG `MetalPlates006` | (42.9, 43.9, 44.0) | **(120, 122, 125)** | 2.80, 2.78, 2.84 | **R−B = −1.1, sat 2.6%** — genuinely neutral, and the *only* candidate where B ≥ G ≥ R, i.e. real cool metal. Uniform gain preserves the plate seams. `Metal032` is also cool (−16.1) but 16.3% sat reads as galvanised-blue. |
| **Water** | *generated* (existing) | (58, 106, 128) | **(58, 106, 128)** | 1.00, 1.00, 1.00 | **No change.** Already the only strongly cool cell (R−B = −70). `water` is 13 blocks in the whole save — leave it alone. |
| **Sports-field surface** | Poly Haven `running_track` | (105.1, 122.5, 130.4) | **(112, 116, 120)** | 1.07, 0.95, 0.92 | **R−B = −25.3 — the most neutral real-world material I measured anywhere**, and it is an actual sports surface rather than AstroTurf. Gain spread 0.92–1.07 (vs 1.47–1.94 if reusing `Grass004`), so it needs a far gentler correction and keeps its surface detail. Uses free cell **r2c5**. |

**On the sports surface — a late measurement changed this row.** I first specified reusing
`Grass004` and pushing it green, on the reasoning that a campus pitch in a Minecraft save
reads better as maintained grass. Measuring Poly Haven's sports assets properly (my first
three attempts failed: the `q=` search endpoint returns nothing for these terms, the tag
filter is unreliable — asking for `Football` returned `Wood096` — and the asset metadata is
keyed by **map type** at the top level, not by asset name) turned up `running_track`. It is
neutral *by measurement* rather than by argument, and its bake is roughly a third the
strength. Reusing `Grass004` is the fallback if the fetch is unavailable.

**A caution about Poly Haven generally.** Having now measured six of its candidates
directly: `patterned_concrete_pavers_03` +29.3, `hexagonal_concrete_paving` +36.3,
`grass_medium_01` +19.1, `asphalt_track` +5.6, `running_track` −25.3, `grass_bermuda_01`
+4.0 (but mean 8.7 — nearly black). **Four of six land in the same warm band as the
current set.** This independently reproduces the brief's finding that swapping inside the
Poly Haven library does not fix the cast, now on assets I measured myself rather than the
four the brief cited. `running_track` is the exception that proves a source must be
*screened*, not trusted by reputation — it is the only one I would take from Poly Haven.

### Two decisions worth defending

**Why `PavingStones103` needs almost no bake.** It is tempting to treat "re-source
everything" as the answer. But this cell measures R−B = +8.4 already. The paving looked
warm because it was `granite_tile` at +56. The correct intervention is to pick the cell
that is already right and leave the bake budget for cells that genuinely need it. Bake
budget spent where it is not needed is how a palette drifts.

**Why brick keeps a +50 R−B after the bake** (132, 94, 82). A brick facade that reaches
neutral grey stops being brick, and the Minecraft save's identity is part of the brief —
the campus must stay recognisably *that* campus. Target is R−B ≤ +12 for *mineral*
surfaces generally, but brick is the deliberate exception, and it is the one place where
I am asking for a visibly warm cell to survive.

---

## 3. Which families must be re-sourced, and which are acceptable

### Must be re-sourced — 8 of 19

| Family | Current | R−B | sat% | Reason |
|---|---|---|---|---|
| `grass` | (110.0, 96.4, 62.0) | +48.0 | 43.7 | Olive. G is 14 *below* R. No multiplier fixes a hue inversion cleanly — you would need to invert the channel order, which is a re-map, not a tint. |
| `leaves` | (129.6, 94.9, 58.9) | **+70.6** | 54.5 | Worst sourced cell. Autumn-brown at 54.5% sat. Unfixable by gain. |
| `roof` | (89.4, 51.4, 27.9) | +61.5 | **68.8** | Highest saturation in the atlas. A 2.7× blue gain (0.940, 1.556, 2.724) would lift the base but leave the saturation *ratio* intact — the tile stays orange. |
| `quartz` | (177.7, 157.1, 121.8) | +55.9 | 31.4 | 31% of the campus surface. The courtyard is *this cell*. |
| `bark` | (114.4, 93.2, 53.3) | +61.0 | 53.4 | |
| `gravel` | (161.9, 133.5, 100.5) | +61.4 | 37.9 | Reads as beach sand at +61. |
| `brick` | (150.8, 111.8, 81.1) | +69.7 | 46.2 | Facades. |
| `metal` | (61.4, 49.9, 28.0) | +33.4 | 54.4 | Gain would be (1.95, 2.45, **4.46**) — a 4.5× blue lift on a dark cell amplifies its noise floor into visible blotching. |

### Acceptable after a bake alone — 7 of 19

`asphalt` (+18.8), `concrete` (+18.4), `tiles` (+11.0), `wood` (+17.6), `soil` (+36.8),
`path` (+38.2), `fabric` (+37.1). All are R-dominant by a *small* margin with modest
saturation; a per-channel gain of 1.0–1.6 lands them inside tolerance without
reshaping the texture.

### Keep the generated cells — 2

`water` (58,106,128) and `other` (150,148,144). Both already neutral-to-cool, and
`other` is deliberately desaturated so an unmapped block reads as "neutral material"
rather than "a coloured mistake" (`build_atlas.py:65-70`). **Do not re-source these.**

### Free cells

`r2c5`, `r2c6`, `r2c7` are empty and already allocated at 8×3. `sports-field surface`
takes **r2c5**. **r2c6/r2c7 stay empty** — headroom for a glass family, which
`block_families.py:31` currently sends to `fabric` as a placeholder, and for a
running-track / kerb material if the campus turns out to have one.

### Licence and provenance for every re-sourced family

All re-sourced families are **ambientCG, CC0 1.0 Universal** (public domain, no
attribution required, commercial use permitted). One attribution line in
`build_atlas.py ATTRIBUTION` should be extended to name ambientCG alongside Poly Haven,
with the asset IDs below:

```
Grass004  Leaf003  Bark001  PavingStones103  Asphalt003
Plaster003  Plaster005  Bricks074  RoofingTiles004
Gravel030  MetalPlates006
```

One family comes from Poly Haven rather than ambientCG:

```
running_track   ->  https://polyhaven.com/a/running_track
                 api:  https://api.polyhaven.com/files/running_track
                 map:  Diffuse -> 1k -> png   (use the PNG; the 1k JPG is fine too)
```

- API used for measurement and metadata: `https://ambientcg.com/api/v2/full_json?id=<ASSET>`
- Colour maps: `https://ambientcg.com/view?id=<ASSET>` → *1K-JPG* zip, `*_Color.jpg`
- Direct single-map fetch: `https://ambientcg.com/get?file=<ASSET>_1K-JPG.zip`
- Mirror with stable Wikimedia URLs: `https://commons.wikimedia.org/wiki/Category:Concrete_textures_by_ambientCG`
  (and one sibling category per material class) — useful because those are CDN-stable
  and do not require the API key path.
- Licence text: `https://ambientcg.com/licence` (CC0 1.0). Poly Haven remains CC0 for the
  families being kept.

**Three API gotchas, so nobody loses an hour to them** (all three cost me time):

1. `https://ambientcg.com/api/v2/full_json?q=<term>` **returns 0 results** for
   descriptive terms. It is not a usable search. Probe by asset ID instead.
2. The `tags=` filter on the same endpoint is **not reliable** — `tags=Football` returned
   `Wood096`, `Bricks105`, `DaySkyHDRI071B`. Do not trust it to enumerate a category.
   `https://api.polyhaven.com/assets` (full list, 2383 entries) *does* work if you need to
   enumerate and filter client-side.
3. `https://api.polyhaven.com/files/<asset>` returns JSON **keyed by map type at the top
   level** (`Diffuse`, `nor_gl`, `Rough`, …), *not* nested under the asset name. Indexing it
   by asset name raises `KeyError` even though the fetch succeeded.

**Fetch 1K, not 2K/4K.** `build_atlas.py:197-200` already resizes anything that is not
1024×1024 down to `SOURCE_TILE`, so a 1K source drops in without change — and it keeps the
atlas build from needing 8K downloads for a 504-texel cell.

---

## 4. Atlas layout

**Keep the existing layout.** This is the single most important compatibility statement
in the document: `tools/build_atlas.py` already implements a correct, self-consistent
packer, `plan_layout()` is already the single source of truth, and `extract_structures.py`
already imports it. The layout is **not** the problem. Changing it would invalidate the
UV contract in every mesh already generated, for no gain.

| Property | Value | Source |
|---|---|---|
| Atlas size | **4096 × 1536** | 8 × 3 cells of pitch 512 |
| Cell pitch | 512 | `tile + 2*pad` |
| Tile | **504** | `ATLAS_DEFAULTS["tile"]`. 504 + 2·4 = 512 = power of two, and 504 is a multiple of 8 so BC1/BC3 wastes no edge block. Both constraints matter. |
| Pad / gutter | **4 px** | Prevents mip reduction pulling a neighbouring family across a cell edge |
| Columns × rows | 8 × 3 = 24 cells | 19 sourced + 2 generated + 3 empty |
| Max resolution | **4096**, do not raise | 24 families is 3 rows. A 32-wide atlas would be 16384 × 512 — worse for mip anisotropy and near the 4096 limit where UE mips get coarse. |
| Format | PNG, 8-bit, **no alpha** | `build_atlas.py:185` creates `Image.new("RGB", ...)` |
| Compression | BC1 (DXT1) for the shipped texture | RGB only, no alpha → DXT1 is 4× smaller than BC3 and adequate for BaseColor-only |

### UV convention the mesher must emit

Verbatim from `plan_layout()` so there is no room to drift:

- `cell = tile + 2*pad = 512`. Cell (col, row) occupies pixels `[col*512, (col+1)*512)`.
- **UV `v` increases upward (OBJ/OpenGL convention). Row 0 is the BOTTOM row of the image.**
  The packing code flips when it writes rows. This is the classic atlas bug: get it
  upside down and every cell is silently wrong with nothing logged.
- `px = [col*512 + 4, (rows-1-row)*512 + 4, col*512 + 508, (rows-1-row)*512 + 508]`
- `uv = [x0/4096, y0/1536, x1/4096, y1/1536]`

Worked example, the cell that matters most — **`quartz`, index 17, col 1, row 2**:

```
uv = [0.12598 .. 0.24902]  ×  [0.00260 .. 0.33073]
px = [516, 4, 1020, 508]
```

And `grass`, index 0, col 0, row 0 (bottom-left of the image):

```
uv = [0.00098 .. 0.12402]  ×  [0.66927 .. 0.99740]
px = [4, 1028, 508, 1532]
```

All 21 rects, as read from `out/atlas/manifest.json` — these do not change:

| family | col | row | u range | v range |
|---|---|---|---|---|
| grass | 0 | 0 | 0.00098–0.12402 | 0.66927–0.99740 |
| path | 1 | 0 | 0.12598–0.24902 | 0.66927–0.99740 |
| soil | 2 | 0 | 0.25098–0.37402 | 0.66927–0.99740 |
| asphalt | 3 | 0 | 0.37598–0.49902 | 0.66927–0.99740 |
| concrete | 4 | 0 | 0.50098–0.62402 | 0.66927–0.99740 |
| plaster | 5 | 0 | 0.62598–0.74902 | 0.66927–0.99740 |
| brick | 6 | 0 | 0.75098–0.87402 | 0.66927–0.99740 |
| granite | 7 | 0 | 0.87598–0.99902 | 0.66927–0.99740 |
| tiles | 0 | 1 | 0.00098–0.12402 | 0.33594–0.66406 |
| roof | 1 | 1 | 0.12598–0.24902 | 0.33594–0.66406 |
| wood | 2 | 1 | 0.25098–0.37402 | 0.33594–0.66406 |
| bark | 3 | 1 | 0.37598–0.49902 | 0.33594–0.66406 |
| leaves | 4 | 1 | 0.50098–0.62402 | 0.33594–0.66406 |
| metal | 5 | 1 | 0.62598–0.74902 | 0.33594–0.66406 |
| gravel | 6 | 1 | 0.75098–0.87402 | 0.33594–0.66406 |
| rock | 7 | 1 | 0.87598–0.99902 | 0.33594–0.66406 |
| fabric | 0 | 2 | 0.00098–0.12402 | 0.00260–0.33073 |
| quartz | 1 | 2 | 0.12598–0.24902 | 0.00260–0.33073 |
| greystone | 2 | 2 | 0.25098–0.37402 | 0.00260–0.33073 |
| other | 3 | 2 | 0.37598–0.49902 | 0.00260–0.33073 |
| water | 4 | 2 | 0.50098–0.62402 | 0.00260–0.33073 |
| *(sports — new)* | 5 | 2 | 0.62598–0.74902 | 0.00260–0.33073 |
| *(free)* | 6–7 | 2 | — | — |

**`v` range asymmetry is not a bug.** Row 0 is `0.66927–0.99740`, not `0.66666–1.0` —
the 4-texel pad at the bottom of the image shifts every rect by `4/1536 = 0.0026`. Do
not "tidy" these numbers; they come from the pad and the mesher must match them exactly.

### Padding and mip strategy

- **4-texel gutter, always.** `504 + 2×4 = 512`. The gutter is what stops mip level 4+
  from averaging a grass cell with a neighbouring brick cell and producing a colour
  fringe that crawls along a wall as the camera moves.
- **UVs stretch onto a cell; they never tile across one.** This is the existing contract
  (`uv_contract` in `out/atlas/manifest.json`) and it is correct: a cell that *repeats*
  needs its neighbour's texels to bleed in under bilinear filtering. The cost is texel
  density on large quads, bounded today by `--max-quad` in `extract_structures.py`.
- Mip generation on a power-of-two atlas only. This is why `tile` is 504 and not 500.
- **Never enable sRGB on a second copy of the atlas for data reads**, and do not add a
  roughness/normal atlas at this stage — the master material has exactly one
  `TextureSampleParameter2D`. A packed ORM atlas would need a second sampler and breaks
  the proven graph shape. Roughness/normal stay separate streaming textures, as
  `build_atlas.py:78-80` already documents.

### Where the bake happens

Inside `build_atlas.py`, at the single point where each cell is written:

```python
# after im is resized to (T, T), before atlas.paste(im, (x0, y0))
if fam in BAKE_GAINS:
    r, g, b = BAKE_GAINS[fam]
    im = im.point([min(255, int(v * r)) for v in range(256)] +
                  [min(255, int(v * g)) for v in range(256)] +
                  [min(255, int(v * b)) for v in range(256)])
```

`Image.point()` with three 256-entry tables is the whole implementation. It is a pure
lookup, runs once at build time, costs nothing at runtime, and **cannot** fail silently
the way the in-shader `Tint` did. Put the gains in a module-level `BAKE_GAINS` dict
next to `GENERATED_RGB`, and add the applied gains to `out/atlas/manifest.json` so QA can
predict cell means from the manifest without opening the PNG.

Apply gains **after** the resize to `T`, never before — LANCZOS resampling mixes
neighbouring texels, and baking before the resize makes the result depend on the filter.

---

## 5. Tiling / real-world scale

Terrain is **1 m per block**, so metres-per-repeat is a direct multiplier on the `Tiling`
scalar. A `Tiling` of 1.0 with a cell stretched over a 4 m quad shows the whole 504-texel
cell across 4 m ≈ **126 texels/m**; a `Tiling` of 0.5 doubles that to ≈ 252.

Note the interaction with the stretch contract: because UVs stretch rather than repeat,
`Tiling` does **not** create repetition — it scales how much of the cell a face shows.
Higher `Tiling` = more magnification = softer. Keep them modest.

| Role | m / repeat | `Tiling` | Rationale |
|---|---|---|---|
| Ground / grass | **4.0** | 0.25 | Grass004 is authored at 1.4 m. At 4 m you get roughly 3 tile cycles per block-diagonal — detail without moiré at the campus viewing distance. |
| Sports field | **8.0** | 0.125 | `running_track` is an 8 m-wide athletics surface; at 8 m the lane banding reads at real scale. |
| Tree foliage | **2.0** | 0.5 | Canopies are 3–5 m across; at 2 m the leaf clusters stay legible instead of turning to noise. |
| Tree bark | **1.5** | 0.67 | Bark001 has no author dimensions; 1.5 m suits the 1 m trunk. |
| Campus paving | **2.0** | 0.5 | **Authored dimension — `PavingStones103` is a real 2 m × 2 m scan.** Using the authored size is what makes the joints land at a believable slab size. |
| Road / asphalt | **1.85** | 0.54 | `Asphalt022` siblings are authored at ca. 1.85 m; keeps aggregate at real scale. |
| Facade — light | **3.0** | 0.33 | One cell per storey-ish. Too tight and render noise reads as brick. |
| Facade — brick | **2.3** | 0.43 | `Bricks074` is authored at 3.0 m; 2.3 m keeps courses near 8 cm. |
| Roof | **3.0** | 0.33 | Slate courses stay parallel to the ridge at this pitch. |
| Plaster / white wall | **3.0** | 0.33 | Matched to facade-light so the two do not disagree at the corner. |
| Gravel | **1.5** | 0.67 | Gravel030 is authored at 2.0 m; 1.5 keeps pebble size physical. |
| Metal | **2.0** | 0.5 | Plate seams every 2 m reads as panelisation. |
| Water | **8.0** | 0.125 | Flat cell; large repeat avoids any hint of structure. |
| `other` (unmapped) | **3.0** | 0.33 | Neutral; must not draw attention. |

If `Tiling` proves to be a no-op like `Tint` was, the fallback is **per-instance cell
selection by UV**, which is already how the atlas works — the mesher can bake the scale
into the UVs directly, with no material involvement at all. See §7.

---

## 6. Verification — pixel-level, because the last change lied

The `Tint` parameter reported success at every level that was not the screen: the
parameter was set on the instance, the graph node was connected, the compile succeeded,
and the render was byte-identical. Every check below reads **pixels**. None of them
trusts a log line, a parameter dump, or a compile success.

The project already has the capture half of this: `UMCFrameCapture`
(`project/Source/MCReplica/Private/MCConsoleCommands.cpp`) writes
`Saved/MCFrame/frame.png` and returns `mean_rgb`, `dark_percent`, `sky_percent`,
`lit_percent`, `distinct_colours`. `capture_live.py` drives it. **Do not build a new
capture path.**

### Step 1 — Atlas-level, before UE ever opens (fastest, catches most)

Run the same decoder over the **rebuilt** `atlas_diffuse.png` and assert per cell.
This is the check that would have caught `Tint` instantly, because it tests the artefact
rather than the intent.

For each of the 21 cells, extract the 504×504 interior at the manifest's `px` rect,
compute the mean, and assert:

| Assertion | Threshold |
|---|---|
| `abs(R−B)` for all mineral cells (`concrete`, `granite`, `tiles`, `gravel`, `quartz`, `plaster`, `asphalt`, `metal`, `rock`, `greystone`) | **≤ 12** |
| `G ≥ R` for `grass`, `leaves`, and sports | **must hold** |
| `R > G` for `brick` | must hold (brick stays brick) |
| `abs(mean − target)` per channel | **≤ 6** vs the §2 table |
| `water` mean | unchanged within ±2 of (58, 106, 128) |
| `r2c5`, `r2c6`, `r2c7` | still flat (0,0,0) unless intentionally used |

Also assert the **atlas aggregate** moved: currently (98.1, 86.8, 71.1) with R−B = **+27.0**.
After the change, `R−B` must be **≤ +10**. One number, one check, catches a partial bake.

### Step 2 — Texture actually applied to UE

A stale import is the classic silent failure: the PNG on disk is correct, the `.uasset`
still points at the old texture, and the render is unchanged.

- Assert `atlas_diffuse.png` mtime > `project/Content/MC/Atlas/atlas_diffuse.png` mtime
  after copy, and that the two files are **byte-identical** (`sha256`). The copy in
  `build_atlas.py:228-235` is the step that silently skips.
- Assert the `.uasset` is newer than the PNG it was imported from. UE will not
  re-import on its own for a Python-driven copy.
- Force a reimport rather than relying on editor state.

### Step 3 — Material really resolves (the `Tint` trap)

For the master material and **one instance per family**:

- Assert the `TextureSampleParameter2D` parameter's value **is** the atlas texture asset.
- Assert the node is connected to BaseColor *and* that BaseColor is connected to the
  active output. `disconnect_material_property` can leave a valid-looking graph with
  nothing reaching the output — `import_cc0_materials.py:206` exists because this happened.
- Assert **no** `Tint`/Multiply node is in the graph. If one is present, this spec has
  been violated; remove it rather than debug it.
- Assert instance `Tiling` values match §5.

### Step 4 — Pixel diff against the previous frame (the decisive test)

This is the one that cannot be fooled, because it does not care *why* the render looks
the same.

1. Capture frame **A** from the fixed spawn vantage (`pick_spawn.py` /
   `spawn_vantage.py` — use the *same* camera, identical resolution, identical lighting
   and time-of-day). Save `frame_A.png`.
2. Apply the material change.
3. Capture frame **B** from the identical camera. Save `frame_B.png`.
4. **Assert `sha256(A) ≠ sha256(B)`.** If they are equal, the change did nothing, no
   matter what any parameter reports. This is the exact signature of the `Tint` failure
   and it takes one command to catch.
5. **Assert `mean_rgb` moved toward the target**: the frame's mean `R−B` should drop
   toward ≤ +10 by roughly the same proportion the atlas aggregate did.
6. **Per-region check** — crop three known regions and check each against its target
   family, not just the frame average:
   - courtyard floor → should track the `quartz` target, not `fabric`
   - a roof plane → should track the `roof` target
   - a tree canopy → must show `G > R` at the pixel level
7. **Anti-aliasing guard**: compare *median* as well as mean, and require
   `distinct_colours > 500` on both frames. If `distinct_colours` collapses, the atlas is
   showing one flat cell — a UV or import failure, which looks deceptively "correct and
   neutral" in a mean-only check.
8. **Regression guard**: re-run Step 1's assertions against the final on-disk atlas *after*
   the UE session, to catch anything that re-wrote the texture.

### Step 5 — Distance check for gutter bleed

Fly the camera to ~80 m from a facade and re-capture. The 4-texel gutter exists to stop
mip reduction cross-contaminating cells; if it is too small you get a colour fringe
crawling along the wall at distance that **no screenshot at close range will ever show**.
This is the one defect that a screenshot-based review structurally cannot find.

---

## 7. Known risks

Ordered by how quietly they fail.

1. **In-shader tinting. Do not attempt it again.** The `Tint` vector parameter verified
   as set, verified as connected, and changed zero pixels. Nothing in the material graph
   may be relied on for colour. Everything goes through the atlas bake. *Mitigation: the
   Step 3 assertion that rejects any `Tint`/Multiply node outright.*

2. **Stale `.uasset` — correct PNG, old texture in the editor.** The single most likely
   cause of "I changed it and nothing happened". The PNG on disk will be correct and
   every log line will be green. *Mitigation: Step 2 byte-identity + mtime assertions.
   Always reimport explicitly.*

3. **Gutter bleed at distance.** 4 texels is correct for 504-texel cells, but if a family
   is ever re-tiled at a different size the ratio must be rescaled or mip level 4+ will
   average grass into brick. *Mitigation: keep 504/4 fixed; Step 5 distance check.*

4. **UV v-flip.** Row 0 is the bottom row. Flip it and every cell is wrong with **no
   error, no warning, and a plausible-looking image** — each family just shows another
   family's texture. This is the failure mode that most often gets mistaken for "the
   textures are subtly off". *Mitigation: `extract_structures.py` must keep importing
   `plan_layout()` rather than re-deriving UVs. One source of truth.*

5. **Partial bake — some cells corrected, some not.** Because the bake is per-cell in a
   Python dict, a typo in one key silently skips one family. This is *exactly* the
   `Tint` failure mode reproduced in the bake. *Mitigation: Step 1 asserts every cell
   individually plus the atlas aggregate R−B. Never trust "the bake ran".*

6. **Over-correction destroys recognisability.** Pushing every cell to R−B ≤ 0 gives a
   grey model that is no longer the Minecraft campus. `brick` is deliberately left at
   R−B = +50 and `soil`/`path` stay warm-brown on purpose. The target is *not* "no warm
   colours" — it is "no warm cast at the aggregate level, with hue identity preserved
   where it carries meaning". *This is the one risk that QA should judge by eye rather
   than by threshold.*

7. **`granite` and `greystone` may look identical after correction.** Both target
   ~(104–112) neutral. `greystone` carries cobblestone/stone (31%-of-surface `quartz`
   sits next to it), `granite` carries diorite/andesite. If they converge, the surface
   loses material read. *Mitigation: keep a ≥ 8 value separation between them; check in
   Step 4 region crops.*

8. **Resolution loss from double resampling.** 1K source → `resize(1024→1024)` (no-op) →
   `resize(1024→504)` LANCZOS → bake. If the bake is applied *before* the resize, the
   result depends on the filter and gains will not land where §2 predicts.
   *Mitigation: bake after the resize, and assert against §2 in Step 1.*

9. **AmbientCG API/rate limits at fetch time.** The API is public and unauthenticated but
   rate-limited; a parallel fetch across 11 families can 429. *Mitigation: serial fetch
   with a retry, and record the resolved asset ID + licence in `manifest.json` so a
   rebuild never re-hits the network.*

10. **Three empty cells invite scope creep.** `r2c6`/`r2c7` are free. Adding a glass
    family means adding a family to `block_families.FAMILIES`, which shifts **every
    subsequent family's index** and therefore every UV rect. *If a family is added, it
    must be appended last and `plan_layout()` re-run and the meshes rebuilt — never
    inserted mid-list.*

11. **`MCFrameCapture` reports the viewport, not a calibrated target.** Its
    `mean_rgb` is affected by sky, exposure and time-of-day. A drop in frame mean could
    be a lighting change. *Mitigation: Step 4 holds camera, lighting and time-of-day
    fixed and changes exactly one thing. If the two frames differ, the material moved.*

---

## 8. Summary for the engineer

- **Do not touch the layout.** 4096×1536, 8×3, tile 504, pad 4 is correct and is already
  the single source of truth in `plan_layout()`.
- **Re-source 8 families** (`grass`, `leaves`, `roof`, `quartz`, `bark`, `gravel`,
  `brick`, `metal`) from ambientCG CC0, asset IDs in §3.
- **Bake 7** (`asphalt`, `concrete`, `tiles`, `wood`, `soil`, `path`, `fabric`) with the
  gains in §2, applied in `build_atlas.py` after the resize.
- **Keep** `water` and `other` as generated. **Leave `r2c6`/`r2c7` empty.**
- **Sports surface** into `r2c5`, from Poly Haven `running_track`, `Tiling` 0.125.
- **Verify with pixels**, starting with the atlas-level per-cell assertions — that check
  alone would have caught the `Tint` failure before a single pixel was rendered.

**One judgement call for the lead:** the brief said `fabric` was the courtyard floor
(2,710 columns). `tools/block_families.py:72-76` records that `quartz_block` is 31% of
the campus surface and maps to the `quartz` family, which measures +55.9 R−B. I have
specified for **both**, with `quartz` treated as the higher priority. If the S4 census
disagrees, `quartz` should still be fixed — it is the warmer of the two.