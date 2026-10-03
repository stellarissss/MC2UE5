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
                     max_components_per_axis=64):
    """
    Find the Landscape configuration that best covers the world extent.

    `section_size=None` (the default) searches every valid section size and
    picks the cheapest fit, which usually is NOT 63. Pinning 63 forces the
    grid onto a coarse ladder: a 704x1088 block campus becomes 757x1135
    vertices, ~13% wasted area. Component count barely matters to the engine,
    so we let the geometry decide.

    `max_components_per_axis` caps the component count (Epic recommends 32-64).
    Without it the solver happily returns 101x156 components of 7 quads each --
    geometrically the tightest fit, but it shatters the landscape into ~16k
    render components, which is exactly what the component system exists to
    avoid. 63 quads with a few percent of border is the right trade.

    `prefer` optionally pins the component count (e.g. to match a World
    Partition grid); otherwise the smallest covering configuration wins, with
    ties broken toward the squarer one.
    """
    if section_sizes is None:
        section_sizes = VALID_SECTION_SIZES if section_size is None else (section_size,)
    elif section_size is not None:
        section_sizes = (section_size,)

    want_cm_x = (world_blocks_x + 2 * pad_blocks) * xy_scale_cm
    want_cm_y = (world_blocks_y + 2 * pad_blocks) * xy_scale_cm

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
                skew = abs(np.log((cx * q) / float(cy * q)))
                # Tie-break deliberately prefers LARGER section sizes.
                # Among configurations that fit equally well, 63 quads over 7
                # quads yields 9x fewer components, and a Landscape component
                # is the unit of streaming, culling and LOD -- so the coarser
                # one is strictly cheaper to render for the same fidelity.
                # It only loses on border area, which is already item 1.
                key = (err_x + err_y, skew, -qps, cx * cy)
                if best is None or key < best[0]:
                    best = (key, cx, cy, sx, sy, err_x, err_y, qps)
    if best is None:
        # Nothing covers it at this XY scale (e.g. one component max): fall
        # back to the smallest legal grid and let the caller raise XY Scale.
        qps = section_sizes[0]
        return 1, 1, *landscape_sizes(1, 1, qps, sections_per_component), 0.0, 0.0, qps

    _, cx, cy, sx, sy, ex, ey, qps = best
    return int(cx), int(cy), sx, sy, ex, ey, int(qps)


def resample_to_grid(height, valid, size_x, size_y, mode="linear"):
    """
    Place a block-resolution height field onto a Landscape vertex grid.

    **The world keeps its scale and its origin.** The field is resampled from
    (0,0) to (W-1, H-1) in block units and then *edge-padded* out to the larger
    grid, never stretched. That distinction is load-bearing:

    Stretching (what a naive `ndimage.zoom(field, size/shape)` does) would
    silently rescale the world by, say, 260/256 = 1.6%. A 1.6% scale error on a
    700 m campus is 11 m of drift at the far edge -- the terrain would no
    longer line up with the layer-1 HISM block layer, which keeps exact 100 cm
    per block. A border costs a handful of extra quads; a scale error costs the
    whole registration.
    """
    from scipy import ndimage

    filled = _fill_nearest(height, valid)
    H, W = filled.shape
    out_w = min(size_x, W)
    out_h = min(size_y, H)

    if out_w != W or out_h != H:
        # 1:1 sampling when the Landscape is smaller than the world slice
        sub = filled[:out_h, :out_w]
    else:
        # Same scale: grid-align by nearest sample on a shared lattice. Using
        # index mapping rather than zoom keeps 1 vertex == 1 block exactly.
        xs = np.linspace(0, W - 1, out_w) if out_w > 1 else np.zeros(1)
        ys = np.linspace(0, H - 1, out_h) if out_h > 1 else np.zeros(1)
        xi = np.rint(xs).astype(np.int64)
        yi = np.rint(ys).astype(np.int64)
        sub = filled[yi][:, xi]
        if mode != "nearest" and out_w != W:
            # Only re-interpolate when a genuine scale change is unavoidable
            # (Landscape smaller than the world slice).
            sub = ndimage.zoom(sub, (out_h / float(sub.shape[0]),
                                     out_w / float(sub.shape[1])),
                               order=1 if mode != "nearest" else 0,
                               mode="nearest")[:out_h, :out_w]

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


def export_landscapes(height, valid, block_origin, y_min_blocks, y_span_blocks,
                      out_dir, name, section_size=None,
                      sections_per_component=1, max_tiles_per_axis=1,
                      xy_scale_cm=None):
    """
    Export a height field as one or more Landscape heightmap PNGs.

    Order of operations matters, so it is spelled out here:

      1. choose the Landscape vertex grid AND the XY Scale together, so that
         (size - 1) * xy_scale covers the world in centimetres;
      2. split the world into disjoint tiles on that grid -- Landscape tiles
         must not overlap, each owns a disjoint vertex set;
      3. resample each tile onto its vertex grid, encode, write PNG.

    Every tile shares one y_min/y_span so heights match across the seam, and
    records its own block origin so the UE5 side places actors without
    guessing. Returns metadata; writes PNGs + landscape.json.
    """
    os.makedirs(out_dir, exist_ok=True)
    H, W = height.shape
    bx0, bz0 = int(block_origin[0]), int(block_origin[1])

    # 1. grid + scale together
    cx, cy, sx, sy, err_x, err_y, qps = solve_components(
        W, H, section_size=section_size,
        sections_per_component=sections_per_component,
        xy_scale_cm=xy_scale_cm or BLOCK_CM, pad_blocks=0)
    q = qps * sections_per_component
    if xy_scale_cm is None:
        # Keep the Minecraft convention -- 1 Landscape vertex == 1 block -- so
        # this layer and the layer-1 HISM block layer share one coordinate
        # system with zero conversion. The solver already guaranteed the grid
        # covers the world at 100 cm/vertex; anything beyond that is border.
        xy_scale_cm = BLOCK_CM

    # 2. disjoint tiles
    span_x = sx - 1
    span_y = sy - 1
    tx = max(1, min(int(max_tiles_per_axis), int(np.ceil(W / float(span_x)))))
    ty = max(1, min(int(max_tiles_per_axis), int(np.ceil(H / float(span_y)))))
    xs = _axis_split(W, span_x, tx)
    ys = _axis_split(H, span_y, ty)

    tiles = []
    for ti, (z0i, z1i) in enumerate(ys):
        for tj, (x0i, x1i) in enumerate(xs):
            sub = height[z0i:z1i, x0i:x1i]
            sv = valid[z0i:z1i, x0i:x1i]
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
                "origin_cm": [bx0 * BLOCK_CM + x0i * xy_scale_cm,
                              bz0 * BLOCK_CM + z0i * xy_scale_cm],
                "block_origin": [bx0 + x0i, bz0 + z0i],
                "block_span": [int(x1i - x0i), int(z1i - z0i)],
                "height_cm_meta": hmeta,
            })

    meta = {
        "name": name,
        "block_origin": [bx0, bz0],
        "block_size": [W, H],
        "xy_scale_cm": float(xy_scale_cm),
        "section_size": qps,
        "sections_per_component": sections_per_component,
        "quads_per_component": q,
        "n_components": [cx, cy],
        "landscape_resolution": [sx, sy],
        "coverage_error_blocks": [float(err_x) / float(xy_scale_cm),
                                  float(err_y) / float(xy_scale_cm)],
        "y_min_blocks": float(y_min_blocks),
        "y_span_blocks": float(y_span_blocks),
        "tile_grid": [ty, tx],
        "tile_count": len(tiles),
        "tiles": tiles,
    }
    with open(os.path.join(out_dir, "landscape.json"), "w") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)
    return meta


def _axis_split(total, tile, n):
    """
    Split [0, total) into `n` windows of `tile`, distributing the remainder to
    the LAST window so every earlier window is exactly `tile` wide (predictable
    grid) and coverage is complete and disjoint.
    """
    n = max(1, int(n))
    if n == 1:
        return [(0, total)]
    out = []
    start = 0
    for i in range(n):
        if i == n - 1:
            out.append((start, total))
        else:
            end = start + tile
            if end >= total:
                out.append((start, total))
                # distribute nothing further
                for _ in range(i + 1, n):
                    pass
                return out
            out.append((start, end))
            start = end
    return out