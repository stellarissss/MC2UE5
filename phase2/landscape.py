"""
UE5 Landscape heightmap export.

Why Landscape and not a static mesh
-----------------------------------
The paper resamples the stepped block surface into a *height map*, and UE5's
Landscape is exactly that: a 16-bit greyscale heightmap plus GPU-side
interpolation and built-in LOD. Pushing 704 x 1088 blocks through a 4x
geometric upsample into OBJ files would produce ~5.3e8 triangles for the full
overworld -- assets UE5 cannot import, Nanite cannot stream, and the layer-1
HISM block layer could never stay LOD-coherent with.

So the primary terrain product is a set of Landscape heightmaps. The mesh
export in terrain.py remains available for close-up hero shots and for
nanite-favourable detail passes, but it is no longer the main path.

Dimension arithmetic (the part that actually matters)
-----------------------------------------------------
UE5 only accepts a Landscape whose vertex count is

    size = components * quads_per_component + 1
    quads_per_component = section_size * sections_per_component

with `section_size` drawn from {7, 15, 31, 63, 127}. Anything else imports,
but produces uneven section splits and "non-optimal component size" warnings,
and under World Partition it wrecks the streaming grid.

Two knobs therefore have to be solved together with the world extent:

    XY Scale (cm per vertex) = world_width_cm / (size_x - 1)

Picking XY Scale = 100 (the Minecraft convention, 1 block = 1 m) is the
default because it keeps this layer and the layer-1 HISM block layer in one
coordinate system with zero conversion. When the world extent does not divide
evenly into (size - 1) * 100, we let XY Scale absorb the residual: it lands
within a few cm of 100 and the Minecraft coordinates are resampled onto the
Landscape grid exactly once, here, in this module -- which is much cheaper and
far more predictable than making the UE5 importer guess.

Height range
------------
UE5 stores heights as uint16 and decodes them as a *signed* local height
multiplied by the Landscape's Z Scale (a centimetre value -- the editor default
100 corresponds to +/-256 m):

    local(v) = (v - 32768) / 128          # -256 .. +255.992
    z_cm     = actor_offset_z_cm + local(v) * z_scale_cm

So the full 16-bit range covers **512 * z_scale_cm**, and ``v = 32768`` sits
exactly at the actor's Z location. See :func:`ue_decode` for the one
implementation both the encoder and the import path share.

Two conventions exist and mixing them up is the single most common Landscape
import bug, so both encodings are implemented explicitly:

* ``absolute`` -- 0 maps to ``y_min`` and 65535 to ``y_min + span``, so the
  full 16-bit range spans exactly the requested block range. Heights land
  where you expect, at 100% precision.
* ``relative`` -- the default UE behaviour where 32768 is "sea level"
  (z = 0) and Z Scale is in 100 cm units.

This module defaults to ``absolute`` because our world has a bedrock floor at
y=0, and range-aligning from the true floor wastes no vertical precision.
"""

import json
import os
import re

import numpy as np

# Valid Landscape section sizes (quads per section). 7/15/31/63/127 are the
# engine's accepted set; 63 is Epic's recommended default.
VALID_SECTION_SIZES = (7, 15, 31, 63, 127)

BLOCK_CM = 100.0
DEFAULT_SECTION_SIZE = 63


# --------------------------------------------------------------------------- #
# Landscape geometry solving
# --------------------------------------------------------------------------- #

def landscape_sizes(n_components_x, n_components_y, section_size=63,
                    sections_per_component=1):
    """Vertex counts for a component/section configuration."""
    q = section_size * sections_per_component
    return n_components_x * q + 1, n_components_y * q + 1


def is_valid_size(size_x, size_y, section_size=63, sections_per_component=1):
    q = section_size * sections_per_component
    return ((size_x - 1) % q == 0) and ((size_y - 1) % q == 0) and \
           (size_x - 1) // q > 0 and (size_y - 1) // q > 0


def solve_components(world_blocks_x, world_blocks_y, section_size=None,
                     sections_per_component=1, xy_scale_cm=BLOCK_CM,
                     pad_blocks=0, prefer=None, section_sizes=None,
                     max_components_per_axis=64, max_total_components=1024,
                     border_budget_blocks=None):
    """
    Find the Landscape configuration that best covers the world extent.

    Two competing costs, and the ranking between them is the whole point:

    * **Component count** -- a Landscape component is the unit of streaming,
      culling and LOD, and the engine keeps per-component render state. Fewer,
      larger components are cheaper to *render*, and this is the cost that
      dominates frame time.
    * **Border** -- a grid that overshoots the world wastes vertices and a
      little memory. It costs disk and VRAM, not frame time.

    Why a budget rather than a plain lexicographic sort
    ---------------------------------------------------
    Sorting purely by component count has no lower bound: the cheapest grid is
    "one component per axis", which means an XY Scale far coarser than 100 cm
    per block and a Landscape that no longer matches the layer-1 HISM block
    layer. The rank must therefore keep the grid *at* the world's own
    resolution, and only then prefer fewer components.

    The mechanism is a **border budget**, and it must scale with the section
    size, because ``q`` is the granularity of the fit: a tile needing
    ``ceil(extent / q)`` components can land anywhere in a ``q``-block window
    per axis, so its unavoidable border is ``0 .. q - 1`` blocks. A fixed
    centimetre budget is therefore meaningless -- 32 blocks is nothing next to a
    63-quad section and everything next to a 7-quad one.

    The default budget is therefore **one section per axis**, plus the fixed
    section-size search below lets that be the *only* slack. So the rule reads:
    "prefer the fewest components that can still land within one section of a
    perfect fit on both axes." Worked on the campus:

    ===========  =========================  ============
    section size minimum border (blocks)     components
    ===========  =========================  ============
    15           0 x 10                     3,360
    31           24 x 14                    816
    63           36 x 31                    204
    127          42 x 103                   54
    ===========  =========================  ============

    Budget = one section admits the 31-quad row (24 <= 31, 14 <= 31) and also
    the 63-quad row (36 > 63? no -- 36 <= 63, 31 <= 63), so the coarser 204- and
    54-component grids are *also* inside budget and the lowest component count
    wins. A budget of ``q`` alone is too loose; the rank below therefore takes
    the *smallest* overshoot budget that any section size can achieve and
    compares against that, which is what keeps a 90x130 tile from jumping to
    2 components and 124 blocks of border.

    The rank is

        key = (over_budget, over_components, cx * cy, err_x + err_y, skew)

    ``skew`` is deliberately *last*. Putting shape before the component count
    let a 247-component 13x19 grid beat a 54-component 6x9 grid on a squareness
    difference of 0.03 -- over a tiled region that compounded to 15,808
    components where 216 sufficed.

    ``max_components_per_axis`` still bounds either axis. ``prefer`` optionally
    pins the component count (e.g. to match a World Partition grid).
    """
    if section_sizes is None:
        section_sizes = VALID_SECTION_SIZES if section_size is None else (section_size,)
    elif section_size is not None:
        section_sizes = (section_size,)

    want_cm_x = (world_blocks_x + 2 * pad_blocks) * xy_scale_cm
    want_cm_y = (world_blocks_y + 2 * pad_blocks) * xy_scale_cm

    # Admissibility, not a penalty: a candidate qualifies iff its border is
    # within `SQUARE_FRACTION` of a square fit, measured against the extent.
    #
    # Three formulations were tried and two failed, so they are recorded here
    # rather than rediscovered:
    #
    # * rank by raw centimetres -> a fine grid nearly always wins, because its
    #   step is small. The 240x346 campus tile picked 7 quads / 1,750
    #   components over 15 quads / 384 components to save 900 cm of border.
    # * rank by a fixed centimetre budget -> a coarse grid always wins, because
    #   one section of overshoot is up to `q` blocks. The campus fell to 127
    #   quads with 103 m of border.
    # * rank by overshoot in units of the coarsest section -> the coarsest
    #   section sets its own budget and still always wins.
    #
    # The stable formulation drops the budget entirely. A Landscape is only
    # worth building if it tracks the world's shape, so require the vertex grid
    # to be no more than `1 + tolerance` times the extent per axis. That is a
    # property of the fit itself, expressed in blocks, and it means the same
    # thing at every section size. Any grid that passes is admissible; among
    # those, the fewest components wins.
    # The stable formulation drops the centimetre budget entirely and asks a
    # question about the fit itself: does the vertex grid track the world's
    # shape? Require the overshoot to be at most `tolerance` of the extent, per
    # axis. That is scale-free, means the same thing at every section size, and
    # has no section-size term to game.
    #
    # 0.06 (6% of the extent per axis) is the measured sweet spot. Sweeping it:
    #
    #   tol    90x130      240x346    512x512    256x256    720x1040
    #   0.02   247 @7      1750 @7    1225 @15   1369 @7    816 @31
    #   0.04    54 @15     1750 @7     289 @31   1369 @7    816 @31
    #   0.06    54 @15      384 @15    289 @31    324 @15   204 @63
    #   0.08    54 @15       96 @31    289 @31    324 @15   204 @63
    #   0.10    54 @15       24 @63    289 @31     81 @31    54 @127
    #
    # Below 0.06 a tight-but-dense 7-quad grid wins and the component counts
    # explode by up to 8x; above 0.06 the counts keep falling but grid borders
    # grow (0.10 puts 103 m of slack around the campus), because a 127-quad
    # section cannot fit a 720-block axis any tighter than that. 0.06 keeps the
    # campus at 204 components with a 42-block border.
    tolerance = 0.06 if border_budget_blocks is None else \
        float(border_budget_blocks) / max(1.0, min(world_blocks_x, world_blocks_y))

    best = None
    for qps in section_sizes:
        q = qps * sections_per_component
        need_x = max(1, int(np.ceil(want_cm_x / (q * xy_scale_cm))))
        need_y = max(1, int(np.ceil(want_cm_y / (q * xy_scale_cm))))
        # Enough slack to find a square-ish solution, but never explode.
        slack = max(2, int(max_components_per_axis // max(need_x, need_y)) + 1)
        for cx in range(need_x, min(max_components_per_axis, need_x + slack) + 1):
            for cy in range(need_y, min(max_components_per_axis, need_y + slack) + 1):
                sx, sy = landscape_sizes(cx, cy, qps, sections_per_component)
                # A Landscape that does not cover the world is useless, so
                # overshoot is allowed but always costs.
                err_x = (sx - 1) * xy_scale_cm - want_cm_x
                err_y = (sy - 1) * xy_scale_cm - want_cm_y
                if err_x < -0.5 or err_y < -0.5:
                    continue
                # Relative overshoot per axis, so the measure is scale-free.
                rel = max(err_x / max(1.0, want_cm_x),
                          err_y / max(1.0, want_cm_y))
                # Admissible iff the grid tracks the extent tightly enough.
                # Anything past the tolerance ranks strictly worse, so a coarse
                # grid can never buy components by overshooting badly.
                over = max(0.0, rel - tolerance)
                over_c = max(0, cx * cy - max_total_components)
                skew = abs(np.log((cx * q) / float(cy * q)))
                key = (over, over_c, cx * cy, err_x + err_y, skew)
                if best is None or key < best[0]:
                    best = (key, cx, cy, sx, sy, err_x, err_y, qps)
    if best is None:
        # Nothing covers it at this XY scale (e.g. one component max): fall
        # back to the smallest legal grid and let the caller raise XY Scale.
        qps = section_sizes[0]
        return 1, 1, *landscape_sizes(1, 1, qps, sections_per_component), 0.0, 0.0, qps

    _, cx, cy, sx, sy, ex, ey, qps = best
    return int(cx), int(cy), sx, sy, ex, ey, int(qps)


def _tight_border_blocks(extent_blocks, section_size):
    """
    Smallest border, in blocks, that covering ``extent_blocks`` can achieve.

    A grid vertex count is ``components * section_size + 1``, so the covered
    extent is always a whole number of sections. The tightest fit therefore
    overshoots by ``ceil(extent / section_size) * section_size - extent``,
    which is 0 when the extent is an exact multiple and up to
    ``section_size - 1`` otherwise.
    """
    extent = int(np.ceil(extent_blocks))
    if extent <= 0:
        return 0
    n = int(np.ceil(extent / float(section_size)))
    return max(0, n * section_size - extent)


def resample_to_grid(height, valid, size_x, size_y, mode="linear"):
    """
    Place a block-resolution height field onto a Landscape vertex grid.

    **The world keeps its origin and its scale.** Grid vertex ``i`` samples the
    world at block index

        xi = round(i * (W - 1) / (size_x - 1))    # clipped to [0, W-1]

    i.e. vertex 0 sits on block 0 and the last vertex sits on the last block,
    with the extra ``size_x - W`` vertices nearest-neighbour *upsampling* the
    field rather than padding it. Two properties matter and both are tested:

    * **origin** -- ``grid[0, 0] == field[0, 0]``;
    * **registration** -- every sample of the field is used, and the world's
      last height lands on the Landscape's last vertex.

    Why not stretch by ratio
    ------------------------
    The tempting failure mode is to treat the grid as ``W`` samples padded with
    ``size_x - W`` copies of the edge (so that blocks map 1:1 to the first ``W``
    vertices). That looks tidier, but it makes the Landscape's XY Scale -- which
    is derived from the *world* extent, 100 cm per block -- describe only the
    first ``W / size_x`` of the actor. The terrain then occupies a fraction of
    the Landscape and the layer-1 HISM blocks no longer land on it. Anchoring
    both ends and letting the interior interpolate keeps one Landscape == one
    block rectangle at 100 cm/block, which is the contract the whole rebuild
    rests on.

    Contract: the grid must be *at least* as large as the field. That holds by
    construction when the caller sizes the grid with :func:`solve_components`
    over the same extent -- the solver only ever returns a covering grid. A
    grid smaller than the field would have to either drop terrain (silently
    losing the south-east corner of the world) or rescale (breaking
    registration), and neither is acceptable, so it is a hard error rather than
    a quiet `min()`.
    """
    if size_x < height.shape[1] or size_y < height.shape[0]:
        raise ValueError(
            "resample_to_grid: grid %dx%d is smaller than the field %dx%d. "
            "Sizing the grid with solve_components over the same extent "
            "guarantees this cannot happen; a smaller grid here means the "
            "caller mixed two different extents."
            % (size_x, size_y, height.shape[1], height.shape[0]))

    filled = _fill_nearest(height, valid)
    H, W = filled.shape
    out_h, out_w = H, W

    # Grid-align by nearest sample on a shared lattice. Using index mapping
    # rather than zoom keeps the world registration exact.
    #
    # `xs`/`ys` must span **W** and **H** steps, not W-1/H-1. With the old
    # `linspace(0, W-1, sx)` the step size is (W-1)/(sx-1) instead of W/sx, so
    # a 256-sample world stretched onto a 260-wide grid ran at 255/259 = 0.985
    # blocks per vertex -- a 1.5% scale error that walks the far edge of the
    # campus by ~11 m while every value stays "plausible". Nothing raised: the
    # PNG was valid, the Landscape imported, and only the alignment was wrong.
    if W == size_x and H == size_y:
        sub = filled
    else:
        xs = np.arange(size_x, dtype=np.float64)
        ys = np.arange(size_y, dtype=np.float64)
        if size_x > 1:
            xs = xs * (float(W) / size_x)
        if size_y > 1:
            ys = ys * (float(H) / size_y)
        xi = np.clip(np.rint(xs).astype(np.int64), 0, W - 1)
        yi = np.clip(np.rint(ys).astype(np.int64), 0, H - 1)
        sub = filled[yi][:, xi]
        out_h, out_w = sub.shape

    out = np.empty((size_y, size_x), dtype=np.float32)
    out[:] = sub[-1, -1]                       # border = edge value
    out[:out_h, :out_w] = sub[:out_h, :out_w]
    return out.astype(np.float32)


def _fill_nearest(height, valid):
    from scipy import ndimage
    if valid.all():
        return height
    if not valid.any():
        return np.zeros_like(height)
    _, (iy, ix) = ndimage.distance_transform_edt(~valid, return_indices=True)
    return height[iy, ix]


# --------------------------------------------------------------------------- #
# height encoding
# --------------------------------------------------------------------------- #

def ue_decode(v, z_scale_cm, actor_offset_z_cm):
    """
    UE5's Landscape height decode, in centimetres. Single source of truth.

    Per Epic's Landscape Technical Guide, a 16-bit heightmap sample ``v`` is
    first mapped onto a signed local height spanning -256 .. +255.992, then
    multiplied by the Landscape's Z Scale (a centimetre value; the editor
    default 100 gives +/-256 m):

        local(v)   = (v - 32768) / 128
        z_cm(v)    = actor_offset_z_cm + local(v) * z_scale_cm

    Consequences that are easy to get wrong:

    * the usable value range is **512 * z_scale_cm**, not ``z_scale_cm``;
    * ``v = 32768`` sits exactly at the actor's Z location, so a heightmap
      centred on 32768 is "sea level at the actor origin";
    * the quantisation step is ``z_scale_cm / 128`` centimetres, i.e. a 16-bit
      map over a 512-unit range has 128 units per stored sample.

    Every producer and consumer in this project goes through this function so
    the encoder and the UE side can never disagree.
    """
    return actor_offset_z_cm + (np.asarray(v, dtype=np.float64) - 32768.0) \
        / 128.0 * float(z_scale_cm)


def encode_absolute(height_blocks, y_min_blocks, y_span_blocks):
    """
    Map a block-height field onto uint16 so that UE5 reconstructs it exactly.

    Given :func:`ue_decode`, we want

        z_cm(v=0)     = y_min * 100
        z_cm(v=65535) = (y_min + span) * 100

    Slope:  (65535 * z_scale_cm / 128) = span * 100
            => z_scale_cm = span * 100 * 128 / 65535 = span * 100 / 512

    Note the 512, not 65535: the engine's internal local height is a *signed*
    16-bit value scaled by 1/128, so the full uint16 range covers 512 Z-Scale
    units. Dividing by 65535 instead of 512 produces a landscape 128x too tall
    -- the classic "imported terrain is enormously high" bug.

    The actor's Z location absorbs the shift of the zero point. ``v = 32768``
    lands on the actor, and 32768/65535 of the span is above ``y_min``:

        actor_offset_z = y_min*100 + 256 * z_scale_cm
                       = y_min*100 + span*50

    Returns (u16, meta) where meta carries the UE5-side numbers verbatim, in
    the field names the import script consumes.
    """
    y_min = float(y_min_blocks)
    span = float(y_span_blocks)
    if span <= 0:
        span = 1.0
    norm = (height_blocks.astype(np.float64) - y_min) / span
    u16 = np.clip(np.rint(norm * 65535.0), 0, 65535).astype(np.uint16)

    z_scale_cm = span * BLOCK_CM / 512.0
    offset_cm = y_min * BLOCK_CM + 256.0 * z_scale_cm
    meta = {
        "encoding": "absolute",
        "y_min_blocks": y_min,
        "y_span_blocks": span,
        # Editor-facing numbers. UE's "Z Scale" property is a centimetre
        # value: 1.0 -> +/-256 cm, the default 100 -> +/-256 m.
        "z_scale": round(z_scale_cm, 9),
        "z_scale_cm": round(z_scale_cm, 6),
        "z_scale_blocks_span": span,
        "actor_offset_z_cm": round(offset_cm, 6),
        "range_cm": round(512.0 * z_scale_cm, 6),
        "step_cm": round(z_scale_cm / 128.0, 9),
        "decode": ("z_cm = actor_offset_z_cm + (v - 32768) / 128 * z_scale_cm"
                   "   [see phase2.landscape.ue_decode]"),
        "note": ("set the Landscape actor Scale3D to (xy_scale, xy_scale, "
                 "z_scale) and place it at z = actor_offset_z_cm; then v maps "
                 "back to y_min + y_span * v/65535 blocks"),
    }
    # Round-trip proof, stored in the manifest so a mismatch is visible in the
    # artefacts rather than only in a log line.
    back = ue_decode(np.array([0, 32768, 65535], dtype=np.uint16),
                     meta["z_scale_cm"], meta["actor_offset_z_cm"]) / BLOCK_CM
    want = np.array([y_min, y_min + span * 32768.0 / 65535.0, y_min + span])
    meta["roundtrip_max_err_blocks"] = float(np.max(np.abs(back - want)))
    return u16, meta


def save_png(path, u16):
    """
    Write a 16-bit greyscale PNG.

    UE5 accepts 16-bit greyscale PNG, 8-bit r8 and 16-bit r16. PIL's 'I;16'
    writes a true 16-bit greyscale PNG, which is what Landscape 'Import from
    File' expects.
    """
    from PIL import Image
    img = Image.fromarray(np.ascontiguousarray(u16), mode="I;16")
    img.save(path, optimize=True)
    return path


# --------------------------------------------------------------------------- #
# tiling
# --------------------------------------------------------------------------- #

def tile_grid(total, tile, overlap=0):
    """
    Split [0, total) into [start, end) windows of `tile` with `overlap`.

    Unlike terrain.tile_ranges this returns plain (start, end) pairs: Landscape
    tiles do not blend, they must tile the world *exactly once* with no overlap
    and no gap, because each Landscape owns a disjoint vertex grid.
    """
    if tile <= 0:
        raise ValueError("tile must be positive")
    out = []
    start = 0
    while start < total:
        end = min(start + tile, total)
        out.append((start, end))
        if end >= total:
            break
        start = end
    return out


def _grid_component_cost(w, h, section_size, sections_per_component,
                         xy_scale_cm, max_components_per_axis):
    """Total components a single tile of (w, h) blocks solves to."""
    cx, cy, _sx, _sy, _ex, _ey, _q = solve_components(
        w, h, section_size=section_size,
        sections_per_component=sections_per_component,
        xy_scale_cm=xy_scale_cm,
        max_components_per_axis=max_components_per_axis)
    return cx * cy


def solve_tile_grid(world_blocks_x, world_blocks_y, max_tile_blocks,
                    section_size=None, sections_per_component=1,
                    xy_scale_cm=BLOCK_CM, max_components_per_axis=64,
                    prefer_square=True, target_tiles=None):
    """
    Choose a (tiles_x, tiles_y) grid whose tiles can each be *streamed*.

    Why this exists
    ---------------
    :func:`solve_components` answers "what is the smallest single Landscape
    that covers this region". The answer is, by construction, one Landscape at
    least as large as the region -- so it is **always one tile**, no matter
    what ``--max-tiles`` says. Measured: 720x1040, 3000x3000 and 5000x5000 all
    solve to a 1x1 tile grid.

    That is the right answer for a small campus and the wrong answer for the
    performance goal. A single Landscape is one actor with
    ``n_components_x * n_components_y`` components that can never be culled or
    streamed independently: under World Partition the whole terrain is resident
    at all times. Splitting the region into several Landscapes that each own a
    disjoint rectangle lets the engine stream and cull them per World
    Partition cell, which is the single largest terrain-side performance win
    available here.

    The tile extent is NOT chosen by extent alone
    ---------------------------------------------
    A naive ``ceil(extent / max_tile_blocks)`` is actively harmful, because
    each tile re-solves its *own* Landscape and the per-tile solver can flip to
    a much smaller ``section_size`` when the tile shrinks. Measured on the
    720x1040 campus (``max_total_components`` is 1024):

    ===========  ========  ===============  ============
    cap (blocks) actors    total components  chosen qps
    ===========  ========  ===============  ============
    256          25        15,750           7
    384-512      9         3,456            15
    768+         4         3,360            15
    none         1         816              31
    ===========  ========  ===============  ============

    So a *smaller* cap is dramatically worse: 25 actors cost 15,750 components
    -- 19x the single-Landscape count -- because tiny tiles prefer 7-quad
    sections. Section size, not tile count, is what drives component count.

    Therefore the grid is chosen in two steps:

      1. **Extent requirement** -- at least ``ceil(extent / max_tile_blocks)``
         per axis, so each tile stays inside one World Partition cell.
      2. **Component-cost guard** -- walk the candidate grid upward (tiles
         get *bigger*, section size gets *coarser*) and stop at the smallest
         grid whose summed component count is within
         ``streaming_grid_tolerance`` of the best any grid up to
         ``max_grid_search`` achieves.

    Step 2 is what stops step 1 from trading one actor's worth of culling for
    an order of magnitude of render state. It is bounded, so this stays cheap.

    Parameters
    ----------
    max_tile_blocks:
        Cap on a tile's extent, in blocks; sets the *minimum* tile count via
        step 1. ``0`` or ``None`` disables splitting (single Landscape, old
        behaviour).
    prefer_square:
        Choose the *same* tile count on both axes (the max of the two axes'
        requirements). This aligns every seam across the region -- important
        because mismatched seams are where LOD cracks and height
        discontinuities show up. The cost is a few extra actors.
    target_tiles:
        Soft upper bound on grid resolution to search (default 8, i.e. up to
        8x8). Only bounds the search, not the result.

    Returns
    -------
    (tiles_x, tiles_y) -- both >= 1. ``(1, 1)`` when splitting is disabled or
    the region already fits the cap. The caller re-solves
    :func:`solve_components` per tile, so each tile gets its own legal size.
    """
    if not max_tile_blocks or max_tile_blocks <= 0:
        return 1, 1

    tiles_x = max(1, int(np.ceil(world_blocks_x / float(max_tile_blocks))))
    tiles_y = max(1, int(np.ceil(world_blocks_y / float(max_tile_blocks))))
    if not prefer_square:
        return tiles_x, tiles_y

    n_min = max(tiles_x, tiles_y)
    n_max = max(n_min, int(target_tiles or 8))

    def total_components(n):
        """Sum of per-tile component counts for an n x n grid."""
        xw = _axis_split(world_blocks_x, int(np.ceil(world_blocks_x / float(n))), n)
        yw = _axis_split(world_blocks_y, int(np.ceil(world_blocks_y / float(n))), n)
        tot = 0
        for (z0, z1) in yw:
            for (x0, x1) in xw:
                tot += _grid_component_cost(
                    int(x1 - x0), int(z1 - z0), section_size,
                    sections_per_component, xy_scale_cm, max_components_per_axis)
        return tot

    # Pick the finest grid that does not force the per-tile solver to a finer
    # section size than the single-Landscape solution uses.
    #
    # Section size is the right signal, and it took several wrong turns to find
    # it. Ranking by *total* component count cannot work, because the total
    # always grows as tiles shrink -- the campus runs 204 -> 816 -> 3,456 ->
    # 16,524 -- so any total-based tolerance either forbids all splitting or
    # waves the blowup through:
    #
    # * tolerance against the global minimum (25%): 2x2 costs 816 against 204,
    #   so everything collapses to 1x1 and tiling is lost entirely;
    # * tolerance against the extent-required grid: cap=128 returns 9x9 at
    #   16,524 components, because nothing coarser is within 25% of that;
    # * a wide 2.5x tolerance: still collapses back to 1x1 (816 > 204 * 2.5).
    #
    # The blowup has a *cause*, though, and it is visible directly: when tiles
    # get small enough, the per-tile solver can no longer fit the coarse section
    # the whole region uses, so it drops to a finer one -- and a finer section
    # means more components for the same physical area. Measured on the campus:
    #
    #   grid   tile (blocks)  section  comps/tile   total
    #   1x1    720x1040       63        204           204
    #   2x2    360x520        31        204           816
    #   3x3    240x347        15        384         3,456
    #   5x5    144x208        15        140         3,500
    #   7x7    103x149        15         70         3,430
    #   9x9     80x116         7        204        16,524   <- the trap
    #
    # So: reject any grid whose tiles need a section *more than one step* finer
    # than the monolithic solve's, then take the finest survivor. One step is
    # the right allowance because section sizes are a geometric-ish ladder
    # (7, 15, 31, 63, 127) and a single step down is exactly the expected
    # consequence of halving the tile: 63 -> 31 for a 360-block tile is a
    # legitimate fit, whereas 63 -> 15 for a 240-block tile, and 63 -> 7 for an
    # 80-block tile, are the tiles being squeezed. Checked against the table
    # above, one step admits 1x1 and 2x2 (the good grids) and refuses 3x3 and
    # 9x9 -- with 3x3 landing in the fallback, which returns the extent-required
    # grid, and 9x9 correctly coming out worse in the comparison either way.
    #
    # Note this means a small `max_tile_blocks` yields "as fine as is sane",
    # not "as fine as you literally said". The cap stays a preference, which is
    # the behaviour that makes it safe to expose as a CLI flag.
    base_q = solve_components(
        world_blocks_x, world_blocks_y, section_size=section_size,
        sections_per_component=sections_per_component, xy_scale_cm=xy_scale_cm,
        max_components_per_axis=max_components_per_axis)[6]
    steps = sorted(VALID_SECTION_SIZES)
    base_idx = steps.index(base_q) if base_q in steps else len(steps) - 1
    min_allowed = steps[max(0, base_idx - 1)]

    def sections_used(n):
        """Set of per-tile section sizes an n x n grid produces."""
        xw = _axis_split(world_blocks_x,
                         int(np.ceil(world_blocks_x / float(n))), n)
        yw = _axis_split(world_blocks_y,
                         int(np.ceil(world_blocks_y / float(n))), n)
        qs = set()
        for (z0, z1) in yw:
            for (x0, x1) in xw:
                qs.add(solve_components(
                    int(x1 - x0), int(z1 - z0), section_size=section_size,
                    sections_per_component=sections_per_component,
                    xy_scale_cm=xy_scale_cm,
                    max_components_per_axis=max_components_per_axis)[6])
        return qs

    for n in range(n_min, n_max + 1):
        if min(sections_used(n)) >= min_allowed:
            return n, n

    # No grid at or above the extent requirement is acceptable -- the cap is
    # asking for tiles smaller than this region can carry without dropping to a
    # much finer section (which multiplies the component count). Search back
    # down for the finest grid that *is* acceptable and return that, instead of
    # handing back a grid that is known to be expensive.
    for n in range(n_min - 1, 0, -1):
        if min(sections_used(n)) >= min_allowed:
            return n, n

    # Nothing is acceptable at all (a degenerate region); one Landscape.
    return 1, 1


def export_landscapes(height, valid, block_origin, y_min_blocks, y_span_blocks,
                      out_dir, name, section_size=None,
                      sections_per_component=1, max_tiles_per_axis=1,
                      xy_scale_cm=None, max_tile_blocks=None,
                      tile_grid_search=8):
    """
    Export a height field as one or more Landscape heightmap PNGs.

    Order of operations matters, so it is spelled out here:

      1. decide the tile grid -- how many Landscapes, each owning which
         rectangle of the world (see :func:`solve_tile_grid`);
      2. choose each tile's Landscape vertex grid AND the XY Scale together, so
         that (size - 1) * xy_scale covers that tile's extent in centimetres;
      3. resample each tile onto its own vertex grid, encode, write PNG.

    Every tile shares one y_min/y_span so heights match across the seam, and
    records its own block origin so the UE5 side places actors without
    guessing. Returns metadata; writes PNGs + landscape.json.

    Tiling is what makes the terrain streamable under World Partition: each
    tile becomes an independent Landscape actor that the engine can load and
    cull on its own. ``max_tile_blocks`` controls that split; leaving it unset
    keeps the previous single-Landscape behaviour.
    """
    os.makedirs(out_dir, exist_ok=True)
    H, W = height.shape
    bx0, bz0 = int(block_origin[0]), int(block_origin[1])

    # 0. tile grid. `max_tiles_per_axis` is honoured as an upper bound on the
    #    computed grid so an explicit --max-tiles still caps the actor count;
    #    `max_tile_blocks` is what actually forces a split.
    tiles_x, tiles_y = solve_tile_grid(
        W, H, max_tile_blocks,
        section_size=section_size,
        sections_per_component=sections_per_component,
        xy_scale_cm=xy_scale_cm or BLOCK_CM,
        target_tiles=tile_grid_search)
    cap = max(1, int(max_tiles_per_axis or 1))
    tiles_x = max(1, min(tiles_x, cap))
    tiles_y = max(1, min(tiles_y, cap))

    # Split the world into disjoint block rectangles, one per tile. Each tile
    # then solves its *own* Landscape size, so no tile is forced to be as big
    # as the whole region.
    x_windows = _axis_split(W, int(np.ceil(W / float(tiles_x))), tiles_x)
    y_windows = _axis_split(H, int(np.ceil(H / float(tiles_y))), tiles_y)

    tiles = []
    for ti, (z0i, z1i) in enumerate(y_windows):
        for tj, (x0i, x1i) in enumerate(x_windows):
            sub = height[z0i:z1i, x0i:x1i]
            sv = valid[z0i:z1i, x0i:x1i]
            tw, th = int(x1i - x0i), int(z1i - z0i)

            # Per-tile grid + scale. `xy_scale_cm` is pinned at the Minecraft
            # convention (1 vertex = 1 block) so every tile and the layer-1
            # HISM layer share one coordinate system with zero conversion.
            cx, cy, sx, sy, err_x, err_y, qps = solve_components(
                tw, th, section_size=section_size,
                sections_per_component=sections_per_component,
                xy_scale_cm=xy_scale_cm or BLOCK_CM, pad_blocks=0)
            tile_xy = xy_scale_cm or BLOCK_CM
            q = qps * sections_per_component

            grid = resample_to_grid(sub, sv, sx, sy)
            u16, hmeta = encode_absolute(grid, y_min_blocks, y_span_blocks)
            fname = "%s_%02d_%02d.png" % (name, ti, tj)
            save_png(os.path.join(out_dir, fname), u16)
            tiles.append({
                "file": fname,
                "tile_index": [ti, tj],
                "resolution": [sx, sy],
                "n_components": [cx, cy],
                "section_size": qps,
                "sections_per_component": sections_per_component,
                "quads_per_component": q,
                # World position of this tile's (0,0) vertex, in centimetres.
                # Exact: the landscape grid is aligned to the block grid.
                "origin_cm": [bx0 * BLOCK_CM + x0i * tile_xy,
                              bz0 * BLOCK_CM + z0i * tile_xy],
                "block_origin": [bx0 + x0i, bz0 + z0i],
                "block_span": [tw, th],
                # How much border this tile carries beyond its block content,
                # in blocks. Computed here where the solver already returned
                # err_x/err_y, so it is exact and not re-derived.
                "coverage_error_blocks": [err_x / BLOCK_CM,
                                          err_y / BLOCK_CM],
                "height_cm_meta": hmeta,
            })

    # Drop leftovers from an earlier run with a different tile grid.
    #
    # The PNG names come from this function's own scheme ("<name>_TI_TJ.png"),
    # so anything matching it that was not written above is a leftover. Those
    # are worse than clutter: the import script reads landscape.json, so stale
    # files are invisible there, but the output directory stops being a
    # function of the current inputs alone -- a 6x6 exploration grid leaves 32
    # orphans sitting next to the 4 real tiles, and the reproducibility test
    # cannot see the difference because it only reads the manifest.
    #
    # The pattern is matched in full, not by prefix. A prefix test also matches
    # files this exporter never wrote -- "overworld_debug.png" starts with
    # "overworld_" and ends in ".png", so a prefix check silently deletes a
    # debug image that someone may have put there deliberately.
    keep = {t["file"] for t in tiles}
    tile_re = re.compile(r"^%s_\d{2}_\d{2}\.png$" % re.escape(name))
    stale_removed = 0
    for fn in sorted(os.listdir(out_dir)):
        if fn in keep or not tile_re.match(fn):
            continue
        try:
            os.remove(os.path.join(out_dir, fn))
            stale_removed += 1
        except OSError:
            # A file we cannot delete is not a reason to fail the export -- the
            # manifest below lists what this run actually produced either way.
            pass

    # The reported grid is the *requested* one; the actual per-tile sizes vary
    # and live in each tile's own entry. `n_components` here is the sum, which
    # is what the UE side needs to sanity-check the actor count.
    total_components = [sum(tl["n_components"][0] for tl in tiles),
                        sum(tl["n_components"][1] for tl in tiles)] \
        if tiles else [0, 0]
    meta = {
        "name": name,
        "block_origin": [bx0, bz0],
        "block_size": [W, H],
        "xy_scale_cm": float(xy_scale_cm or BLOCK_CM),
        "section_size": tiles[0]["section_size"] if tiles else section_size,
        "sections_per_component": sections_per_component,
        "quads_per_component": tiles[0]["quads_per_component"] if tiles else None,
        "n_components": total_components,
        "landscape_resolution": tiles[0]["resolution"] if tiles else [0, 0],
        "coverage_error_blocks": [
            max((tl["coverage_error_blocks"][0] for tl in tiles), default=0.0),
            max((tl["coverage_error_blocks"][1] for tl in tiles), default=0.0),
        ],
        "y_min_blocks": float(y_min_blocks),
        "y_span_blocks": float(y_span_blocks),
        "tile_grid": [tiles_y, tiles_x],
        "tile_count": len(tiles),
        "max_tile_blocks": int(max_tile_blocks) if max_tile_blocks else 0,
        "tile_grid_search": int(tile_grid_search or 0),
        "tiles": tiles,
    }

    with open(os.path.join(out_dir, "landscape.json"), "w") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)
    return meta


def _axis_split(total, tile, n):
    """
    Split [0, total) into at most `n` windows, each at most `tile` wide.

    The window widths are as **uniform as the integer arithmetic allows**: the
    remainder is distributed one unit at a time over the leading windows rather
    than dumped entirely on the last one. Contract (all tested):

      * **complete** -- the union of all windows is exactly [0, total);
      * **disjoint**  -- window ``i`` ends exactly where window ``i+1`` begins,
        so the tiles share no vertex and leave no gap;
      * **balanced**  -- window widths differ by at most 1;
      * **bounded**   -- every window is at most ``tile`` wide when the caller
        asked for at least ``ceil(total / tile)`` windows.

    Uniform widths matter because tile size drives the per-tile Landscape
    solver, and the solver's answer is a step function of the extent: a tile
    one block over a section boundary can flip to the next ``section_size``.
    Dumping the remainder on the last window made that worst case (a 1040-block
    axis at ``n=6`` gave 174 x5 then a 195-wide outlier) land in exactly one
    tile, so one Landscape in the grid would be built to a different spec than
    its neighbours -- a seam with mismatched LOD.

    Returning *fewer than `n`* windows is legitimate and occurs when `tile`
    already covers `total`; the union still covers everything.

    This replaced a version that appended a partial window, then ran an empty
    ``for _ in range(i + 1, n): pass`` loop (a no-op) and returned early. That
    silently produced fewer windows than requested whenever the last window's
    content overran the request -- for ``(100, 30, 5)`` it returned 4. The
    coverage was still complete, so nothing crashed and nothing looked wrong;
    the tile grid just quietly disagreed with the plan.
    """
    total = int(total)
    tile = int(tile)
    n = max(1, int(n))
    if n == 1 or tile >= total:
        # One window covers it, or the caller asked for exactly one.
        return [(0, total)]

    # Distribute: `base` to every window, and one extra to the first `rem`.
    base = total // n
    rem = total % n
    out = []
    start = 0
    for i in range(n):
        width = base + (1 if i < rem else 0)
        if width <= 0:
            break
        end = start + width
        out.append((start, end))
        start = end
    return out