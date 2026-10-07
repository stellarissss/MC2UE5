# QA-S5 report — frame-level A/B and Shipping cook

**Author:** 严守真 (quality-lead) · **Date:** 2026-10-06 · **Project:** `Q:/MC2UE5/repo`
**Verdict: FAIL — not verified.** The bake numbers are real and reproduce exactly. The claim
*"the baked atlas reaches the screen"* is **NOT VERIFIED**, and it is not verifiable with the
current build: the atlas is not in the packaged build at all, so the A/B cannot exercise it.

I did not fix anything. The atlas PNG in the UE tree was restored byte-identical after the test
(`sha256 1a5433d0…`, verified). No processes left running. No commits made.

---

## Headline

| # | Claim | Result |
|---|-------|--------|
| 1 | Baked atlas reaches the renderer (frame A/B) | **NOT VERIFIED — test is invalid as designed.** Root cause found: atlas absent from the build. Separately, the `sha256` criterion is **unsound** — frames are not byte-reproducible. |
| 2 | Cook compile failures = 0 | **MEASURED, but VACUOUS.** Real cook ran: **0** `Failed to compile Material`. But `M_MC_Atlas` was never cooked, so the count does not cover the material in question. |
| 3 | Material graph clean (no inert `Tint`) | **VERIFIED PASS.** |
| 4 | `granite` vs `greystone` legibility | **MEASURED — report only, as instructed.** They will read as the same material on screen. |

---

## Item 1 — frame-level A/B: NOT VERIFIED

### 1a. Two path corrections (the brief's paths do not exist)

- `dist/Windows/MCReplica.exe` does not exist under `repo/`. Packaged builds live at
  `Q:/MC2UE5/dist*/Windows/`, and there are **46** of them (`dist` … `dist52`). I used
  **`dist52`**, the newest by mtime.
- `tools/focus_capture.py` does not exist under `repo/tools/`. The real one is
  **`Q:/MC2UE5/tools/focus_capture.py`**. There are two separate `tools/` trees; the atlas
  tooling (`build_atlas.py`, `verify_atlas.py`, `pick_spawn.py`) is in `repo/tools/`, the
  capture/verify tooling is in `Q:/MC2UE5/tools/`.

### 1b. The captures ran, and they are real frames

The game launches and renders. `focus_capture.py` (PrintWindow + `PW_RENDERFULLCONTENT`) works;
I got five clean 1280×720 frames. The process-name warning in the brief is accurate — the
window only appears after ~20 s and the process must be killed as
`MCReplica-Win64-Shipping.exe`. (`taskkill /F` is mangled by Git Bash into `/F:` → use
`cmd //c "taskkill /F /IM …"`.)

Frame sha256:

```
frame_A_dist52_baseline  e9a83e3e542de48410150f4234d6d985503463c7   (baked atlas in place)
frameA2_baked            3f3471e14162affcd701f43e144661ae4492505e   (baked atlas in place)
frameB_prebake           a0bb3cb4e3a73dcb1f320d03f55790268bf585ef   (pre-bake atlas swapped in)
det1                     3cdc943e64f6d8a4a62d4f093db8b6d1fde0c983   (baked, repeat run)
det2                     5ee993951fb764c696ac7825bb6399bb1f8e2543   (baked, repeat run)
```

**All five differ. That result is meaningless — see 1d.**

### 1c. Root cause: the atlas is not in the packaged build

This is the decisive finding. The A/B cannot show the bake reaching the renderer because the
baked atlas is not present in anything the packaged build loads.

- `dist52` loads content **only from `MCReplica-Windows.pak`** — there is no loose cooked
  content beside it (`dist52/Windows/MCReplica/Content/` contains only `Paks/`).
- That pak's own manifest, `Manifest_UFSFiles_Win64.txt`, contains:
  - `atlas` → **0** entries
  - `structures` → **0** entries
- Swapping `project/Content/MC/Atlas/atlas_diffuse.png` therefore cannot change a pixel: the
  file is not read at runtime, and it is not what the renderer samples anyway. **The UE-side
  texture is `T_MC_Atlas.uasset` (9.7 MB, built 16:56); the PNG is only its import source.**
  Replacing the source does not re-import the asset.

The packaged build predates the S5 atlas work entirely:

```
dist52 pak                 14:05:58
dist52 Shipping exe        13:53:03
T_MC_Atlas.uasset          16:56:00     ← atlas work, ~3 h later
M_MC_Atlas.uasset          16:56:01
atlas_diffuse.png (baked)  16:53:47
Structures/ (158 meshes)   16:56
MCReplica.umap             14:04:56     ← predates the structures it would need to place
```

The map contains no reference to any structure and no `MC_Terrain`; its only MC asset
references are `MC/Character/*`. The S5 note in `docs/s3_s4_s5_notes.md` claims
`placed 158 / 158 actors`, but that placement is **not in the saved level**. The game's own
runtime log agrees, on every one of its 1054 sample lines:

```
meshes=0 terrain(vis=0 col=0)
```

And the captured frame agrees visually — brown props and flat ground, **no buildings, no
atlas-mapped surfaces anywhere on screen.**

So the honest reading of this A/B is: *"the two frames are indistinguishable because there is
nothing in the scene for the atlas to affect."* That is not the failure mode the brief predicted
(bake silently not reaching the renderer) — it is a different and more basic one.

### 1d. The `sha256` criterion is unsound — do not use it

The brief says *"the equal-hash result is the finding."* But frames here are **not
byte-reproducible**. Two runs with byte-identical inputs and byte-identical launch flags:

| pair | max Δ | mean Δ | px >0 | px >8 | px >32 |
|------|-------|--------|-------|-------|--------|
| det1 vs det2 (same PNG) | 31 | 0.251 | 384 050 | 479 | 0 |
| det1 vs A2 (same PNG) | 26 | 0.314 | 436 918 | 300 | 0 |
| det2 vs A2 (same PNG) | 23 | 0.286 | 438 209 | 548 | 0 |
| baseline vs A2 (same PNG) | 23 | 0.288 | 439 858 | 319 | 0 |

Treatment pair, for comparison:

| pair | max Δ | mean Δ | px >0 | px >8 | px >32 |
|------|-------|--------|-------|-------|--------|
| **A2_baked vs B_prebake** | **40** | **0.228** | **292 399** | **474** | **2** |
| baseline vs B_prebake | 44 | 0.298 | 424 196 | 484 | 3 |

**The signal from swapping the atlas is statistically indistinguishable from the noise floor
of two identical runs.** Only 474 / 921 600 pixels (0.05 %) exceed Δ8, and the 2 pixels above
Δ32 sit in a 2×1 pixel box at (539, 375) — anti-aliasing jitter on a prop edge, not a material
change. Diff map: `shots/diffmap_A2_vs_B.png`.

So `sha256(A) != sha256(B)` is **guaranteed to be true regardless of the atlas**, and would have
been read as a pass. If this test had been run and the hashes compared naively, it would have
produced a **false green** — the exact class of failure `verify_atlas.py` was written to
prevent. Recommend the criterion become a **per-pixel difference threshold against a same-input
noise floor**, never a hash inequality.

---

## Item 2 — real Shipping cook: measured, but the count is vacuous

### 2a. Builds — both succeed

With the specified env (`UE_SDKS_ROOT`, `WindowsSDKDir=10.0.26100.0`, `DOTNET_ROOT`, `UBA_ROOT`,
`/q/dotnet` on PATH):

```
MCReplicaEditor Win64 Development  →  Result: Succeeded
MCReplica       Win64 Shipping     →  Result: Succeeded
```

### 2b. The documented trap is real, but the stated cause is wrong

`RunUAT BuildCookRun` failed twice with `Result: Failed (OtherCompilationError)` and **no error
line**, exactly as described. But it is **not** stale per-target intermediates, and deleting
`Intermediate/…/Shipping/MCReplica` would not have helped. The real cause is **UBA**:

```
[1/2] Link [x64] MCReplica-Win64-Shipping.exe: Exited with error code 1
UbaStorageServer - SetFileInformationByHandle (FileDispositionInfo) failed … (Access is denied)
```

Running the *identical* command by hand, outside UBA, succeeds:

```
link.exe @"…/MCReplica-Win64-Shipping.exe.rsp"   → exit 0, .lib and .exp written
```

This machine cannot write to `Q:/UBA` (ACL-locked — the same class of fault as the
`UbaStorageServer … Access is denied` lines in every log). With `-NoUBA` the link passes and the
failure simply **moves one step later** to `WriteMetadata … -Mode=WriteMetadata`, which *also*
succeeds standalone. Each step fails only under the UBA executor.

For the record, `cl.exe` compiles fine (0 compile actions needed; both targets were up to date).

### 2c. The cook itself ran to completion

Dropping `-build` (binaries were already fresh) let the cook proceed:

```
LogCook: Display: FULL COOK … recooking all packages discovered in the current cook.
LogCook: Display: Cooked packages 614 Packages Remain 0 Total 614
LogCook: Display: Cook by the book total time 52.375721
LogCook: Display: ---- Finalisation: End ----
LogCook: Display: Done!
```

**`Failed to compile Material` count = 0.** (The only shader work was
`Missing cached shadermap for /Game/MC/Materials/MC_Terrain … compiling.` → 1 MaterialShader
built, 0 failures.)

The run then failed at **staging**, on a stale Zen store — not on materials:

```
Failed reading oplog from Zen at http://[::1]:8558/… HTTP NotFound. (3 attempts)
AutomationTool exiting with ExitCode=1
```

### 2d. Why the 0 is vacuous — the atlas material is not in the cook

```
grep -c "M_MC_Atlas" cook3.log                    → 0
grep -c "/game/mc/atlas" ReferencedSet.txt        → 0
grep -c "^/game/mc/structures" ReferencedSet.txt   → 0
```

`ReferencedSet.txt` lists 112 `/game/mc/*` entries, all under `cc0/`, `character/`, `materials/`
(9), `textures/`. **No `atlas/`, no `structures/`, no `terrain/`, no `props/`.**

**So "cook compile failures = 0" is measured and true, but it does not cover `M_MC_Atlas`.**
`Materials Built = 1` and that one is `MC_Terrain`. This number must not be cited as evidence
that the atlas material compiles clean in Shipping — that claim remains **untested**.

I could not complete a packaged Shipping build on this machine, so no Shipping-config atlas
compile was verified by any route.

---

## Material graph — VERIFIED PASS

Parsed directly from `M_MC_Atlas.uasset` (13 080 bytes), not from a script's self-report.

Material expression nodes present — **exactly the five expected, nothing else**:

```
MaterialExpressionTextureSampleParameter2D
MaterialExpressionScalarParameter
MaterialExpressionMultiply
MaterialExpressionTextureCoordinate
MaterialExpressionConstant
```

- Parameters: **`Atlas`**, **`Tiling`** — both present.
- **`Tint`: ABSENT.** The inert-parameter failure mode is not present in this material.
- Texture path: `/Game/MC/Atlas/T_MC_Atlas`; material path `/Game/MC/Atlas/M_MC_Atlas`.
- Assignment is real: **158 / 158** structure meshes reference `M_MC_Atlas` (no structure uses
  `MC_Prop_structure` / `MC_Terrain` / `MC_Prop_building`).

So the material is the atlas material and it is wired to the atlas texture. It simply is not
referenced by the saved level, so it is never reached.

---

## Atlas bake numbers — independently reproduced

Measured straight from both PNGs at the cell rectangles `plan_layout()` defines (504 px tile,
4 px gutter, 512 stride, row 0 = bottom row), **not** read from `manifest.json`:

- **Baked aggregate R−B = +4.84** (requirement ≤ +10) — **PASS**
- Pre-bake aggregate R−B = **+16.81** — reproduces the stated +16.8

Per-cell, baked (R−B): `grass +17.9 · path +4.0 · soil +30.0 · asphalt +0.0 · concrete +2.0 ·
plaster +3.8 · brick +50.0 · granite −2.0 · tiles +2.1 · roof +4.0 · wood +18.0 · bark +14.0 ·
leaves +10.0 · metal −5.1 · gravel +5.7 · rock +3.8 · fabric −6.0 · quartz −4.0 · greystone +0.0 ·
other +6.0 · water −70.0 · sports +22.4`

Spot-checks against the brief: `roof` +4.0 ✓ (was +61.5) · `quartz` −4.0 ✓ (was +55.8) ·
`grass` G(104)>R(84) ✓ · `leaves` G(70)>R(62), R−B +10.0 ✓ · `metal` −5.1 ✓ ·
`water` exactly (58,106,128) unchanged ✓.

`tools/verify_atlas.py` → **PASS**, aggregate `R−B = +4.8`, and it independently confirms the
UE copy is byte-identical to `out/atlas` (`1a5433d0…` both). My figures and the tool agree.

**Note on the aggregate:** +4.84 is a mean that hides real spread. Six cells still exceed +10 —
`soil +30.0`, `brick +50.0`, `sports +22.4`, `grass +17.9`, `wood +18.0`, `bark +14.0`. If the
requirement is a *per-cell* ceiling rather than an aggregate one, six of 22 cells fail it. The
brief framed it as aggregate (≤ +10) and aggregate passes; flagging the ambiguity rather than
resolving it, since the criterion is a design call.

---

## Item 3 — `granite` vs `greystone`: they will read as one material

Reporting only, as instructed. Nothing nudged.

```
granite    (110.0, 110.0, 112.0)   luma 110.1   sd 7.5   p5–p95  98–122
greystone  (104.0, 106.0, 104.0)   luma 105.4   sd 11.6  p5–p95  90–128
```

- Max per-channel separation **8**, luma separation **4.7** — below the ≥8 guideline.
- **ΔE76 = 3.17.** Below the ~5 "clearly distinguishable side by side" threshold; above the ~2.3
  "just noticeable" threshold only under ideal conditions.
- The separation is also **below the noise the texture itself carries**: `greystone`'s internal
  contrast (sd 11.6) is *larger* than the gap between the two means, and the two luminance
  distributions overlap heavily across 90–128 vs 98–122.

**My read:** on a wall these two will not be reliably told apart. The luma difference is under
5/255 and each material's own mottling spans more than that. In a lit scene the lighting and
normal variation will dominate the distinction entirely. If granite and greystone are meant to
read as different materials on adjacent facades, this will not achieve it as-is.

---

## What would actually close Item 1

Nothing in the test harness — the build is missing the content. In order:

1. **Place the structures and terrain in the saved level.** `MCReplica.umap` (14:04) predates
   the 158 meshes (16:56) and references none of them. The `placed 158 / 158 actors` claim in
   `docs/s3_s4_s5_notes.md` describes an editor-side state that was never saved. Re-run
   `tools/import_chunks.py`, **save the level**, and confirm `grep -c bld_ … MCReplica.umap` > 0.
2. **Re-cook and re-stage.** Atlas and Structures are absent from `ReferencedSet.txt`; they can
   only enter the pak if the level references them.
3. **Fix or bypass UBA.** `Q:/UBA` is ACL-locked for this user; every UBA-hosted step fails
   silently with exit code 1 while succeeding standalone. `-NoUBA` moves the failure rather
   than removing it. This blocks any Shipping build on this machine.
4. **Then** re-run the A/B — but compare per-pixel against a same-input noise floor, not hashes
   (§1d). With ~479 pixels of unavoidable Δ>8 noise, a useful threshold is Δ>32 over a
   same-input baseline, or fix the capture to be deterministic first.

## Recommended gate

**FAIL** on S5 frame-level verification. The atlas bake itself is sound and independently
confirmed, and the material graph is clean — but "the bake reaches the screen" is unproven and
currently unprovable, and the 0-compile-failure count does not cover `M_MC_Atlas`.

Blocking items are all in the **build/level assembly**, not in the art or the bake: level not
saved with its structures (1), atlas/structures not cooked (2), UBA unwritable (3).

### Files

- Frames: `Q:/MC2UE5/shots/frame_{A_dist52_baseline,A2_baked,B_prebake}.png`, `det{1,2}.png`
- Diff map: `Q:/MC2UE5/shots/diffmap_A2_vs_B.png`
- Restored atlas backup: `Q:/MC2UE5/shots/atlas_diffuse.BAKED.png`
- Build/cook logs: `/tmp/build_editor.log`, `/tmp/build_shipping.log`, `/tmp/cook{,2,3}.log`
- Helper scripts written for this test: `Q:/MC2UE5/shots/{cap.sh,cap2.sh,build_editor.bat,build_shipping.bat,cook{,2,3}.bat}`
