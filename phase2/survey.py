"""
Locate the built-up (campus) region inside a dimension's full extent.

Phase 2 should not pay for 4320 x 3375 blocks of wilderness when the actual
deliverable is a school campus. This pass streams the MC2WV2 file once and
writes, per block column, *what the topmost solid voxel is*. From that map we
can say where the build-out is without ever holding a dense 3D volume.

Output (under out/survey/<dim>/):
    top_block.npy   uint16[H, W]  palette index of the topmost voxel
                                     (0xFFFF = no solid column)
    top_height.npy  int16[H, W]   world Y of that voxel (top face is +1)
    survey.json     summary + candidate region rectangles

Memory: two 2-D arrays over the block extent. For the overworld that is
4320 x 3375 x (2 + 2) bytes = 58 MiB -- affordable, because we store the
*palette index*, not the name, and the height, not the volume.

Note on indexing: `state` from the voxel stream is already the *global* palette
index, and `VoxelFile.palette` is the global palette, so index -> name is a
direct lookup with no remapping step.
"""

import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import voxelio as vio   # noqa: E402

NO_COLUMN = np.uint16(0xFFFF)

# Voxel word field layout (v2). Kept local so the hot loop does not attribute
# lookups; identical to voxelio._DX_MASK etc.
_DX_MASK, _DZ_MASK, _Y_MASK, _LPI_MASK = 0xF, 0xF, 0x1FF, 0x7FFF
_DZ_SHIFT, _Y_SHIFT, _LPI_SHIFT = 4, 8, 17

# Names that mean "a human built something here". A column whose topmost voxel
# is one of these is by definition part of the built-up area.
MAN_MADE_HINTS = (
    "concrete", "terracotta", "brick", "cobblestone", "planks",
    "log", "slab", "stairs", "fence", "wall", "glass", "wool", "carpet",
    "lantern", "torch", "rail", "ladder", "gravel_path", "prismarine",
    "quartz", "sandstone", "smooth_stone", "polished", "cut_", "mossy",
    "chiseled", "iron_bars", "trapdoor", "door", "sign", "banner",
    "flower_pot", "cauldron", "anvil", "enchanting_table", "crafting_table",
    "furnace", "chest", "barrel", "beacon", "bell", "lectern",
    "grindstone", "smithing", "loom", "cartography", "brewing", "bricks",
    "moss_block", "sculk", "nether_bricks", "hay_block", "sponge",
)

# Natural surfaces: dirt / grass / sand / stone. A top face made of these means
# nobody paved here.
NATURE_HINTS = (
    "dirt", "grass", "sand", "gravel", "bedrock", "podzol", "mycelium",
    "snow", "ice", "water", "lava", "stone", "andesite", "diorite",
    "granite", "deepslate", "tuff", "farmland", "clay", "end_stone",
    "netherrack", "magma_block", "obsidian", "soul_sand", "glowstone",
)


def survey(path, y_min=None, y_max=None):
    """
    One streaming pass over the voxel file.

    Returns (top_block, top_height, pal_names, bounds).
    """
    bounds = vio.world_bounds(path)
    if bounds is None:
        raise ValueError("%s has no non-air voxels" % path)
    xmin, xmax, zmin, zmax, ylo, yhi = bounds
    if y_min is not None:
        ylo = max(ylo, y_min)
    if y_max is not None:
        yhi = min(yhi, y_max)

    W = xmax - xmin + 1
    H = zmax - zmin + 1

    with vio.VoxelFile(path) as vf:
        pal_names = [nm for (nm, _pk) in vf.palette]
        air = vf.air_flags
        top_block = np.full((H, W), NO_COLUMN, dtype=np.uint16)
        top_h = np.full((H, W), -32768, dtype=np.int32)

        for (cx, cz, gmap, vcount, voff) in vf.chunk_rows():
            if vcount == 0:
                continue
            words = np.frombuffer(vf._read(voff, vcount * 4), dtype="<u4")
            lpi = (words >> _LPI_SHIFT) & _LPI_MASK
            state = gmap[lpi]
            keep = ~air[state]
            if not keep.any():
                continue

            dx = (words & _DX_MASK)[keep].astype(np.int32)
            dz = ((words >> _DZ_SHIFT) & _DZ_MASK)[keep].astype(np.int32)
            y = ((words >> _Y_SHIFT) & _Y_MASK)[keep].astype(np.int32)
            state = state[keep]

            in_y = (y >= ylo) & (y <= yhi)
            if not in_y.any():
                continue
            dx, dz, y, state = dx[in_y], dz[in_y], y[in_y], state[in_y]

            lx = cx * 16 + dx - xmin
            lz = cz * 16 + dz - zmin
            ok = (lx >= 0) & (lx < W) & (lz >= 0) & (lz < H)
            if not ok.all():
                lx, lz, y, state = lx[ok], lz[ok], y[ok], state[ok]
            if lx.size == 0:
                continue

            # Sort ascending by Y so the last write per column is the topmost.
            # Within a chunk each (lx,lz) pair is unique per voxel, and stable
            # sorting makes the result independent of chunk insertion order.
            order = np.argsort(y, kind="stable")
            lz, lx, y, state = lz[order], lx[order], y[order], state[order]
            top_h[lz, lx] = y
            top_block[lz, lx] = state.astype(np.uint16)

    return top_block, top_h.astype(np.int16), pal_names, (xmin, zmin, W, H)


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #

def classify_columns(top_block, pal_names):
    """-> bool masks: built, natural, other (air / void columns excluded)."""
    # Vectorised substring test over the palette, not over the whole grid:
    # build a per-palette-index lookup once, then index the grid with it.
    built_lut = np.zeros(len(pal_names), dtype=bool)
    nat_lut = np.zeros(len(pal_names), dtype=bool)
    for i, nm in enumerate(pal_names):
        low = nm.lower()
        built_lut[i] = any(h in low for h in MAN_MADE_HINTS)
        nat_lut[i] = any(h in low for h in NATURE_HINTS)

    valid = top_block != NO_COLUMN
    safe = np.where(valid, top_block, 0).astype(np.int32)
    return valid, built_lut[safe], nat_lut[safe]


def density_map(mask, cell=32):
    """Block counts per cell*cell block -- cheap spatial summary for eyeballing."""
    H, W = mask.shape
    ch, cw = H // cell, W // cell
    if ch == 0 or cw == 0:
        return np.zeros((1, 1), np.int32)
    trimmed = mask[:ch * cell, :cw * cell].astype(np.int32)
    return trimmed.reshape(ch, cell, cw, cell).sum(axis=(1, 3))


def find_region(mask, cell=32, threshold=0.10, min_cells=4):
    """
    Bounding box of the connected high-density area.

    Works on the cell grid (fast), then refines to blocks. Two passes:
      1. mark cells whose built-fraction clears `threshold`
      2. flood-fill (scipy) from the largest such blob, drop specks
    Falls back to the overall bounding box of qualifying cells when the map is
    too sparse for a meaningful connectivity analysis.
    """
    from scipy import ndimage

    dens = density_map(mask.astype(np.float32), cell=cell)
    frac = dens / float(cell * cell)
    seed = frac >= threshold
    if not seed.any():
        return None

    # Report the density distribution before flood-filling: it tells us whether
    # `threshold` is even the right knob, which is worth knowing before we hand
    # back a rectangle nobody can trust.
    nz = frac[frac > 0]
    diag = {
        "cells_total": int(frac.size),
        "cells_nonzero": int(nz.size),
        "frac_max": float(frac.max()),
        "frac_p99": float(np.percentile(frac, 99)) if nz.size else 0.0,
        "frac_p95": float(np.percentile(frac, 95)) if nz.size else 0.0,
        "cells_over_threshold": int(seed.sum()),
    }

    lab, n = ndimage.label(seed)
    if n == 0:
        return None
    sizes = ndimage.sum(seed, lab, index=np.arange(1, n + 1))
    keep = np.zeros(n + 1, dtype=bool)
    # Order blobs by size but require a real footprint; a 1-cell blob is noise.
    order = np.argsort(sizes)[::-1]
    total = 0
    for k in order:
        if sizes[k] * cell * cell < min_cells * cell * cell:
            continue
        keep[k + 1] = True
        total += 1
        if total >= 8:
            break
    sel = keep[lab]

    rr, cc = np.nonzero(sel)
    if rr.size == 0:
        return None
    # `rr` indexes the CELL grid along the mask's axis 0 (= world Z),
    # `cc` indexes axis 1 (= world X). Convert cells -> blocks.
    z0 = int(rr.min()) * cell
    z1 = (int(rr.max()) + 1) * cell
    x0 = int(cc.min()) * cell
    x1 = (int(cc.max()) + 1) * cell
    diag["bbox_cells"] = [int(rr.min()), int(rr.max()), int(cc.min()), int(cc.max())]
    diag["bbox_blocks_axis0_z"] = [z0, z1]
    diag["bbox_blocks_axis1_x"] = [x0, x1]
    return {"x": [x0, x1], "z": [z0, z1], "cells": int(rr.size),
            "built_fraction": float(sel.mean()), "diag": diag}


def main(argv=None):
    ap = argparse.ArgumentParser(description="survey a dimension for its built-up region")
    ap.add_argument("--dim", default="overworld")
    ap.add_argument("--voxel-dir",
                    default=os.path.join(ROOT, "voxel_data", "full"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--cell", type=int, default=32)
    ap.add_argument("--threshold", type=float, default=0.10)
    args = ap.parse_args(argv)

    vpath = os.path.join(args.voxel_dir, args.dim + ".bin")
    if not os.path.isfile(vpath):
        print("MISSING %s" % vpath, file=sys.stderr)
        return 2
    out_dir = args.out or os.path.join(ROOT, "out", "survey", args.dim)

    print("scanning %s ..." % vpath, flush=True)
    tb, th, names, bounds = survey(vpath)
    xmin, zmin, W, H = bounds
    print("  grid %d x %d  (world x[%d..%d] z[%d..%d])"
          % (W, H, xmin, xmin + W - 1, zmin, zmin + H - 1))

    valid, built, nat = classify_columns(tb, names)
    info = {
        "dimension": args.dim,
        "voxel_file": vpath,
        "grid": [W, H],
        "origin": [xmin, zmin],
        "world_x_range": [xmin, xmin + W - 1],
        "world_z_range": [zmin, zmin + H - 1],
        "y_range": [int(th[valid].min()), int(th[valid].max())],
        "columns_total": int(W * H),
        "columns_solid": int(valid.sum()),
        "columns_built": int(built.sum()),
        "columns_natural": int(nat.sum()),
        "built_pct_of_solid": 100.0 * built.sum() / max(1, valid.sum()),
        "threshold": args.threshold,
        "cell": args.cell,
    }
    reg = find_region(built, cell=args.cell, threshold=args.threshold)
    if reg:
        reg["world_x"] = [xmin + reg["x"][0], xmin + reg["x"][1] - 1]
        reg["world_z"] = [zmin + reg["z"][0], zmin + reg["z"][1] - 1]
        reg["blocks"] = [reg["x"][1] - reg["x"][0], reg["z"][1] - reg["z"][0]]
        # Pipeline-facing alias: run_phase2.py --region campus reads
        # info["regions"]["campus"]. A campus is the only built-up region this
        # save has; nether/end get their own survey runs.
        info["regions"] = {"campus": {
            "x": reg["world_x"], "z": reg["world_z"],
            "blocks": reg["blocks"],
        }}
        info["region"] = reg

    os.makedirs(out_dir, exist_ok=True)
    np.save(os.path.join(out_dir, "top_block.npy"), tb)
    np.save(os.path.join(out_dir, "top_height.npy"), th)
    np.save(os.path.join(out_dir, "built.npy"), built)
    np.save(os.path.join(out_dir, "valid.npy"), valid)
    with open(os.path.join(out_dir, "survey.json"), "w") as fh:
        json.dump(info, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, "top_names.json"), "w") as fh:
        json.dump(names, fh, ensure_ascii=False)

    print(json.dumps(info, indent=2, ensure_ascii=False))
    print("-> %s" % out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())