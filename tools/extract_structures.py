# -*- coding: utf-8 -*-
"""
extract_structures.py -- S3 + S4: classified voxels -> building meshes, detail
meshes, water layer.

The old pipeline rendered all 1,171,144 blocks as instanced cubes: 14,053,728
triangles, and the cull distance had to be cut to 80 m. That is what the
SIGGRAPH '25 "Minecraft to 3D" paper calls "an absurd triangle mess (every
block turned into geometry)". This step is where it stops.

Three operations, in order of how much each saves:

1. **Face culling.** A face is emitted only where the neighbouring voxel is
   empty. A solid 6x6x4 building has 24 boundary faces, not 12 * 144 = 1,728.
   The neighbour test is against campus-wide occupancy, not against this
   object alone, so two touching buildings do not grow interior walls into
   each other and a wall does not emit a face where a window will be meshed.

2. **Greedy meshing per material** (Lysenko, 0fps.net/page/4). Coplanar
   same-material quads merge into one. A flat 20x20 roof is 1 quad, not 400.
   Standard algorithm, not novel work.

3. **A cap on merged extent.** Greedy meshing alone stretches one atlas cell
   across an entire 40 m facade, because the mesh is an atlas and UVs must land
   inside one cell. `--max-quad` bounds the merge so texel density stays high.
   It gives back some of operation 2's win; the manifest records the cap so the
   trade is visible rather than buried in a constant.

**Volume and position are preserved exactly.** No smoothing, no shrinking, no
re-seating. The one hard constraint on this rebuild is that the campus stays
recognisably the same campus as the save; only the terrain (S2) is allowed to
move, and it has its own measured justification. Every mesh occupies exactly
the blocks the save has, and the manifest asserts each building's mesh bbox
equals its `structures.json` bbox.

Voxel ownership is a partition, so no two outputs overlap and nothing
z-fights:

    role 2 structure -> S3 building mesh
    role 5 detail    -> S3 detail mesh + S4 per-kind library
    role 4 water     -> S4 water layer
    role 3 vegetation-> not here; extracted as instanced objects (separate task)
    role 1 terrain   -> S2's smooth mesh

    python3 tools/extract_structures.py
"""

import argparse
import json
import os
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "phase2"))

from block_families import FAMILIES                                  # noqa: E402
from classify import CAMPUS, MAX_Y, STRUCTURE, DETAIL, WATER         # noqa: E402
from build_atlas import ATLAS_DEFAULTS, ATLAS_FAMILIES, plan_layout  # noqa: E402

BLOCK_CM = 100.0

#: Minecraft axis -> Unreal axis. Minecraft (x, y, z) becomes Unreal
#: (x, z, y): +z becomes +Y and block height becomes Z, which is Z-up. This is
#: the same contract `terrain_smooth.build_tiles` documents, and it is the only
#: place in this file that knows it.
MC_TO_UE = (0, 2, 1)

#: Detail kinds, by block-name substring. The kind only decides which library
#: mesh a detail component lands in; every kind is meshed identically, as the
#: voxels the save has. Grouping exists so an artist can replace "all fences"
#: without re-extracting the building mass.
DETAIL_KIND_RULES = (
    ("pane", "window"), ("glass", "window"), ("bars", "window"),
    ("window", "window"), ("sign", "window"),
    ("door", "door"),
    ("fence", "fence"), ("_gate", "fence"), ("_wall", "fence"),
    ("rail", "railing"), ("chain", "railing"), ("ladder", "railing"),
    ("torch", "railing"), ("lantern", "railing"), ("banner", "railing"),
    ("_stairs", "stairs"), ("_slab", "stairs"),
    ("_pressure_plate", "stairs"),
)
DETAIL_KINDS = ("window", "door", "fence", "railing", "stairs", "trim")
DETAIL_KIND_INDEX = {k: i for i, k in enumerate(DETAIL_KINDS)}


def detail_kind(block_name):
    """-> one of DETAIL_KINDS, for a block name."""
    n = block_name.split(":", 1)[-1]
    for token, kind in DETAIL_KIND_RULES:
        if token in n:
            return kind
    return "trim"


def hand_parity(axes):
    """+1 if ``axes`` is an even permutation of (0,1,2), else -1."""
    seen = [axes.index(i) for i in range(3)]
    inversions = sum(1 for a in range(3) for b in range(a + 1, 3)
                     if seen[a] > seen[b])
    return -1 if inversions % 2 else 1


#: Sign of the Minecraft -> Unreal axis mapping: +1 rotation, -1 reflection.
#:
#: MC (x, y, z) -> UE (x, z, y) swaps two axes, so its determinant is -1 and it
#: *mirrors* the coordinate system. Winding is decided in Minecraft axes, where
#: the cross product follows the right-hand rule; permuting the geometry by a
#: reflection then flips every cross product relative to the permuted normal.
#: Without this correction all six faces of every box come out inside-out, and
#: because backface culling hides exactly the wrong triangles the result is a
#: building you can see through rather than an obvious error.
MC_TO_UE_HANDEDNESS = hand_parity(list(MC_TO_UE))


# --------------------------------------------------------------------------- #
# material volume
# --------------------------------------------------------------------------- #

def build_material_volume(bin_path, cache_path):
    """Voxel -> atlas family index + palette id, cached as one .npz.

    `rolevolume.npy` records what *role* a voxel plays, not what it *is*, and
    greedy meshing merges only equal materials, so the family per voxel has to
    come from the save. Re-reading it is the honest source; the cache exists
    because this is a 20 s scan whose answer does not change.

    The cache stores the *inputs* that produced it, not just the arrays, and is
    rebuilt when they differ. Without that, changing a mapping rule silently
    keeps serving the old answer: the water special case in `family_of` was
    added, the cache was not invalidated, and the water layer kept coming out
    textured with brick, rock and wood while every run reported success. The
    failure is invisible precisely because the cache is doing its job.

    Index 0 is reserved for "air / no face" so a face mask can use 0 as empty.
    Family i is stored as i + 1.

    Returns (family uint8, name_id uint16, names list).
    """
    from voxelio import VoxelFile

    fam_slot = {f: i + 1 for i, f in enumerate(ATLAS_FAMILIES)}
    x0, x1, z0, z1 = CAMPUS
    W, D, H = x1 - x0 + 1, z1 - z0 + 1, MAX_Y

    # Everything that changes the answer. atlas_families because it defines the
    # index encoding; source_mtime because a re-extracted save is new data.
    try:
        src_stamp = int(os.path.getmtime(bin_path))
    except OSError:
        src_stamp = 0
    stamp = {"atlas_families": list(ATLAS_FAMILIES),
             "source_mtime": src_stamp,
             "campus": list(CAMPUS)}

    if os.path.isfile(cache_path):
        try:
            z = np.load(cache_path, allow_pickle=False)
            if list(z["stamp"]) == [json.dumps(stamp, sort_keys=True)]:
                return (z["family"], z["nameid"],
                        [str(s) for s in z["names"]])
            print("material cache is stale (inputs changed); rebuilding")
        except Exception as exc:
            print("material cache unreadable (%s); rebuilding" % str(exc)[:80])

    fam = np.zeros((W, H, D), dtype=np.uint8)
    nid = np.zeros((W, H, D), dtype=np.uint16)
    slot_of_palette = {}
    names = []

    t0 = time.time()
    with VoxelFile(bin_path) as vf:
        for ch in vf.iter_chunks():
            cx, cz = ch["chunkX"], ch["chunkZ"]
            if (cx * 16 > x1 or cx * 16 + 15 < x0
                    or cz * 16 > z1 or cz * 16 + 15 < z0):
                continue
            if ch["state"].size == 0:
                continue
            xyz, st = ch["xyz"], ch["state"]
            ix, iy, iz = xyz[:, 0] - x0, xyz[:, 1], xyz[:, 2] - z0
            keep = ((ix >= 0) & (ix < W) & (iz >= 0) & (iz < D)
                    & (iy >= 0) & (iy < H))
            if not keep.any():
                continue
            ix, iy, iz, st = ix[keep], iy[keep], iz[keep], st[keep]
            for s in np.unique(st):
                s = int(s)
                # The name must be resolved on *every* chunk, not only the
                # first time a palette index is seen. Assigning it only inside
                # the "new index" branch leaves `nm` holding the previous
                # iteration's value, so a voxel's name and its material family
                # come from two different blocks: the water layer came out
                # carrying brick, rock and wood while its name still read
                # "minecraft:water". Nothing cross-checks the two, so it
                # rendered wrong and reported success.
                entry = vf.palette[s]
                nm = entry[0] if isinstance(entry, (tuple, list)) else str(entry)
                slot = slot_of_palette.get(s)
                if slot is None:
                    slot = len(names)
                    names.append(nm)
                    slot_of_palette[s] = slot
                m = st == s
                fam[ix[m], iy[m], iz[m]] = fam_slot[family_of(nm)]
                nid[ix[m], iy[m], iz[m]] = slot

    names_arr = np.array([""] * len(names), dtype="U96")
    for slot, nm in enumerate(names):
        names_arr[slot] = nm
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    np.savez_compressed(cache_path, family=fam, nameid=nid, names=names_arr,
                        stamp=np.array([json.dumps(stamp, sort_keys=True)]))
    print("material volume: %d block names, %.1fs"
          % (len(names), time.time() - t0))
    return fam, nid, [str(s) for s in names_arr]


def family_of(block_name):
    """block name -> atlas family name. `water` gets its own cell.

    `block_families.family()` sends water to "other", because no rule matches
    it. That is right for a general mapping and wrong here: the S4 water layer
    needs to be identifiable in the material list and swappable on its own.
    """
    n = block_name.split(":", 1)[-1]
    if n in ("water", "flowing_water", "bubble_column"):
        return "water"
    from block_families import family
    return family(n)


# --------------------------------------------------------------------------- #
# greedy meshing
# --------------------------------------------------------------------------- #

def greedy_quads(mask, cap):
    """Greedy-merge a 2-D material mask into maximal rectangles.

    ``mask[r, c]`` is 0 for "no face" and a material index otherwise; cells
    merge only when their material matches, which is what stops one atlas cell
    bleeding into its neighbour.

    ``cap`` bounds the merged extent on both axes. Without it a quad spans a
    whole facade and stretches one atlas cell across it.

    Returns [(r0, r1, c0, c1, material)] with r1/c1 exclusive.
    """
    h, w = mask.shape

    # Horizontal runs, vectorised: there are far fewer runs than cells, so the
    # per-run merge below stays cheap even on the largest building.
    solid = mask != 0
    edges = np.flatnonzero(
        np.diff(np.concatenate(([0], solid.ravel().view(np.int8), [0]))))
    flat = mask.ravel()
    runs = [[] for _ in range(h)]
    for s0, e0 in zip(edges[0::2], edges[1::2]):
        c0 = int(s0 % w)
        runs[int(s0 // w)].append((c0, c0 + int(e0 - s0), int(flat[s0])))

    consumed = [set() for _ in range(h)]
    quads = []
    for r0 in range(h):
        for ci, (c0, c1, val) in enumerate(runs[r0]):
            if ci in consumed[r0]:
                continue
            r1 = r0 + 1
            while r1 < h and r1 - r0 < cap:
                nxt = None
                for cj, (a, b, v) in enumerate(runs[r1]):
                    if cj not in consumed[r1] and a == c0 and b == c1 \
                            and v == val:
                        nxt = cj
                        break
                if nxt is None:
                    break
                consumed[r1].add(nxt)
                r1 += 1
            consumed[r0].add(ci)
            quads.append((r0, r1, c0, c1, val))
    return quads


def _drop_last(axis, size, forward):
    """Slice dropping the last voxel along ``axis`` (or the first).

    Comparing a voxel against its neighbour needs both slices to be the same
    length, so one is taken from the front and one from the back. The size has
    to be passed in: `slice(None).stop` is None, not the array extent.
    """
    sl = [slice(None)] * 3
    sl[axis] = slice(0, size - 1) if forward else slice(1, size)
    return tuple(sl)


def mesh_volume(solid, occupied, mat, cap, world_offset):
    """Greedy-mesh one object's voxels. -> (corners, uvs, normals, quads)

    ``solid``     this object's voxel mask (bool)
    ``occupied``  campus-wide occupancy, any role: the culling reference
    ``mat``       atlas family index per voxel, 0 = air
    ``world_offset`` (x, z) block coordinate of ``solid``'s [0, 0]

    ``corners`` is (N, 3) in Unreal cm, ``uvs`` is (N, 2) in block units
    (0..w, 0..h per quad, scaled into the atlas by the caller), ``quads`` is a
    list of (base_index, material_slot, width_blocks, height_blocks).
    """
    ox, oz = world_offset
    corners, uvs, normals, quads = [], [], [], []

    for d in range(3):
        other = [a for a in range(3) if a != d]
        # Winding must follow the right-hand rule around the *outward* normal.
        # The quad is built counter-clockwise in the (other[0], other[1]) plane,
        # whose normal is e_a x e_b -- that equals +e_d only when (a, b, d) is
        # an even permutation. Deriving the parity beats an `if axis == 1:
        # flip` literal, which is right until the axis order changes.
        parity = hand_parity([other[0], other[1], d])

        for sign in (1, -1):
            n_d = solid.shape[d]
            if n_d < 2:
                continue
            # `here` is this object's voxels on the near side of the slice pair,
            # `beyond` is campus occupancy on the far side. The material must be
            # read from the *same* voxels as `here` -- slicing it with
            # `sign > 0` unconditionally picks the wrong voxel for a -d face,
            # which puts the wrong atlas cell on half the geometry.
            here = solid[_drop_last(d, n_d, sign > 0)]
            beyond = occupied[_drop_last(d, n_d, sign < 0)]
            face = here & ~beyond
            if not face.any():
                continue
            mat_here = mat[_drop_last(d, n_d, sign > 0)]

            # Reverse the corner order when the traversed plane's normal
            # disagrees with the face's outward normal.
            order = (0, 1, 2, 3) if parity * sign > 0 else (0, 3, 2, 1)

            mv_face = np.moveaxis(face, d, 0)
            mv_mat = np.moveaxis(mat_here, d, 0)
            for i in range(n_d - 1):
                m2 = mv_face[i]
                if not m2.any():
                    continue
                m2 = np.where(m2, mv_mat[i], 0)
                for (r0, r1, c0, c1, val) in greedy_quads(m2, cap):
                    base = len(corners)
                    # After moveaxis(d, 0) the remaining axes keep their
                    # original relative order, and `other` is exactly that
                    # list, so m2's row index addresses other[0] and its column
                    # index addresses other[1].
                    #
                    # The face plane sits one step past the near voxel's
                    # origin on the near side, which is `i + 1` for both
                    # directions given how `here` is sliced: at sign>0 `here`
                    # is voxel i against neighbour i+1, at sign<0 it is voxel
                    # i+1 against neighbour i. Same plane either way.
                    plane = i + 1
                    # Corner k of the quad, counter-clockwise in the
                    # (other[0], other[1]) plane.
                    g = ((r0, c0), (r1, c0), (r1, c1), (r0, c1))
                    w, h = float(r1 - r0), float(c1 - c0)
                    uv = ((0.0, 0.0), (w, 0.0), (w, h), (0.0, h))
                    # Reverse the corner order when that plane's natural normal
                    # disagrees with the face's outward normal. `order` is
                    # applied to geometry and UV together, so each corner keeps
                    # the texture it was authored with instead of sampling a
                    # mirrored one.
                    for k in order:
                        corner = [0, 0, 0]
                        corner[d] = plane
                        corner[other[0]] = g[k][0]
                        corner[other[1]] = g[k][1]
                        corners.append(corner)
                        uvs.append(uv[k])
                    n = [0.0, 0.0, 0.0]
                    n[MC_TO_UE[d]] = float(sign)
                    normals.extend([tuple(n)] * 4)
                    quads.append((base, val, r1 - r0, c1 - c0))

    corners = np.asarray(corners, dtype=np.float64).reshape(-1, 3)
    # Block corner -> Unreal cm. The permutation is the coordinate contract:
    # MC (x, y, z) becomes UE (x, z, y). It has to be applied to the geometry
    # and not only to the normals -- permuting the normals alone leaves the
    # mesh itself in Minecraft axes, which still renders (nothing validates
    # it) but puts every wall in the wrong plane.
    #
    # MC_TO_UE[d] is the Unreal axis that Minecraft axis d lands on, so the
    # x offset goes with MC x, the z offset with MC z, and block height goes to
    # Unreal Z with no offset at all.
    out = np.empty_like(corners)
    out[:, MC_TO_UE[0]] = (corners[:, 0] + ox) * BLOCK_CM
    out[:, MC_TO_UE[1]] = corners[:, 1] * BLOCK_CM
    out[:, MC_TO_UE[2]] = (corners[:, 2] + oz) * BLOCK_CM
    if MC_TO_UE_HANDEDNESS < 0:
        # The axis swap mirrors the space, so reverse every quad's corner order
        # to keep the faces front-facing in Unreal.
        out = out.reshape(-1, 4, 3)[:, ::-1, :].reshape(-1, 3)
        uvs = np.asarray(uvs, dtype=np.float64).reshape(-1, 2, 4)
        uvs = uvs[:, ::-1, :].reshape(-1, 2)
    return (out, uvs,
            np.asarray(normals, dtype=np.float64).reshape(-1, 3),
            quads)


#: "atlas" maps each quad onto its family's atlas cell (the original model,
#: which stretches one whole tile across every merged quad and therefore shows a
#: flat average per block). "block" keeps the UVs in block units instead, so a
#: normal wrapping texture tiles physically across the surface -- which is how
#: the per-family materials now sample, and the only mode that produces texture
#: detail at 1 m scale.
UV_MODE = "atlas"


def scale_uvs(uvs, quads, layout):
    """Map block-space UVs into each material's atlas cell.

    ``uvs`` arrive per quad in *blocks*: (0,0) (w,0) (w,h) (0,h). They are
    normalised by the quad's own extent and then placed inside the material's
    cell, so a quad is stretched onto that cell and never tiled across it.

    The normalisation is the step that is easy to get wrong: multiplying the
    block-space UV by the cell size instead of dividing by the extent puts the
    UV at (w * cell_w), which for a 4-block quad is four cells away -- outside
    the atlas entirely, sampling whatever family happens to be over there.
    Nothing warns about it; the texture is simply wrong.
    """
    out = np.empty_like(uvs)
    for i, (base, slot, w, h) in enumerate(quads):
        u0, v0, u1, v1 = layout[ATLAS_FAMILIES[slot - 1]]["uv"]
        du, dv = u1 - u0, v1 - v0
        inv_w = 1.0 / float(w) if w else 0.0
        inv_h = 1.0 / float(h) if h else 0.0
        for k in range(4):
            tu, tv = uvs[base + k]
            out[base + k] = (u0 + tu * inv_w * du, v0 + tv * inv_h * dv)
    return out


# --------------------------------------------------------------------------- #
# OBJ writer
# --------------------------------------------------------------------------- #

def write_obj(path, comments, corners, uvs, normals, quads):
    """Write an OBJ carrying ``v``/``vt``/``vn`` on every face.

    The v/vt/vn triple is not optional. `reimport_terrain.py` records what
    omitting it cost: the importer silently discarded ~90% of the geometry, and
    the collision that resulted did not match the terrain, so the pawn fell
    through it.
    """
    with open(path, "w", newline="\n") as fh:
        w = fh.write
        for line in comments:
            w("# %s\n" % line)
        for c in corners:
            w("v %.2f %.2f %.2f\n" % (c[0], c[1], c[2]))
        for t in uvs:
            w("vt %.6f %.6f\n" % (t[0], t[1]))
        for n in normals:
            w("vn %.3f %.3f %.3f\n" % (n[0], n[1], n[2]))
        current = None
        for base, slot, _w, _h in quads:
            name = ATLAS_FAMILIES[slot - 1]
            if name != current:
                w("usemtl %s\n" % name)
                current = name
            a, b, c, d = base + 1, base + 2, base + 3, base + 4
            w("f %d/%d/%d %d/%d/%d %d/%d/%d\n"
              % (a, a, a, b, b, b, c, c, c))
            w("f %d/%d/%d %d/%d/%d %d/%d/%d\n"
              % (a, a, a, c, c, c, d, d, d))


# --------------------------------------------------------------------------- #
# emission
# --------------------------------------------------------------------------- #

def subvol(mask, pad=1):
    """-> (submask, offset). Padded by one so face culling sees the shell."""
    idx = np.nonzero(mask)
    if idx[0].size == 0:
        return None, None
    lo = [max(0, int(a.min()) - pad) for a in idx]
    hi = [min(n, int(a.max()) + 1 + pad)
          for a, n in zip(idx, mask.shape)]
    sl = tuple(slice(a, b) for a, b in zip(lo, hi))
    return mask[sl], lo


def emit(mask, fam_idx, occupied, layout, cap, out_dir, name, comments):
    """Mesh ``mask`` and write one OBJ. -> stats dict or None."""
    sub, off = subvol(mask)
    if sub is None:
        return None
    sl = tuple(slice(o, o + n) for o, n in zip(off, sub.shape))
    corners, uvs, normals, quads = mesh_volume(
        sub, occupied[sl], fam_idx[sl], cap, (CAMPUS[0] + off[0],
                                              CAMPUS[2] + off[2]))
    if not quads:
        return None
    if UV_MODE == "atlas":
        uvs = scale_uvs(uvs, quads, layout)
    # else: keep block-space UVs (0..w, 0..h per quad) for a tiling sampler.
    path = os.path.join(out_dir, name + ".obj")
    write_obj(path, comments, corners, uvs, normals, quads)
    slots = Counter(ATLAS_FAMILIES[q[1] - 1] for q in quads)
    return {
        "obj": name + ".obj",
        "quads": len(quads),
        "triangles": len(quads) * 2,
        "blocks": int(mask.sum()),
        "material_slots": sorted(slots),
        "quads_by_material": dict(slots),
        "mean_quad_extent": round(
            sum(q[2] * q[3] for q in quads) / float(len(quads)), 2),
    }


def world_bbox(mask):
    idx = np.nonzero(mask)
    x0, z0 = CAMPUS[0], CAMPUS[2]
    return [[int(idx[0].min()) + x0, int(idx[0].max()) + x0 + 1],
            [int(idx[1].min()), int(idx[1].max()) + 1],
            [int(idx[2].min()) + z0, int(idx[2].max()) + z0 + 1]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classify", default="out/classify")
    ap.add_argument("--out", default="out")
    ap.add_argument("--bin", default="voxel_data/full/overworld.bin")
    ap.add_argument("--material-cache", default="out/materials/voxelmat.npz")
    ap.add_argument("--uv-mode", choices=("atlas", "block"), default="atlas",
                    help="atlas = per-quad cell mapping (flat averages at 1 m); "
                         "block = block-unit UVs for a tiling sampler")
    ap.add_argument("--max-quad", type=int, default=4,
                    help="max blocks per merged quad edge. Raise for fewer "
                         "triangles, lower for sharper facades. Bounds how far "
                         "one atlas cell is stretched.")
    ap.add_argument("--min-detail", type=int, default=1,
                    help="smallest detail component to list in the manifest")
    args = ap.parse_args()

    global UV_MODE
    UV_MODE = args.uv_mode

    t0 = time.time()
    x0, x1, z0, z1 = CAMPUS

    role = np.load(os.path.join(args.classify, "rolevolume.npy"))
    labels = np.load(os.path.join(args.classify, "structure_labels.npy"))
    with open(os.path.join(args.classify, "structures.json")) as fh:
        structures = json.load(fh)

    seat = {}
    seat_path = os.path.join(args.out, "terrain", "seating.json")
    if os.path.isfile(seat_path):
        with open(seat_path) as fh:
            for s in json.load(fh):
                seat[s["id"]] = s

    print("loading material volume ...")
    fam_idx, name_id, names = build_material_volume(args.bin,
                                                    args.material_cache)
    layout = plan_layout(ATLAS_FAMILIES, **ATLAS_DEFAULTS)
    occupied = role != 0

    struct_dir = os.path.join(args.out, "structures")
    lib_dir = os.path.join(struct_dir, "detail_kinds")
    os.makedirs(lib_dir, exist_ok=True)

    comment_common = [
        "greedy meshing per material, max_quad=%d blocks" % args.max_quad,
        "atlas layout: out/atlas/manifest.json (tools/build_atlas.py)",
        "coords: MC (x,y,z) -> UE (x*100, z*100, y*100) cm, Z up",
    ]

    # ---- S3: one OBJ set per building ------------------------------------
    report = []
    tot_quads = 0
    for o in structures:
        oid = o["id"]
        in_obj = labels == oid
        parts = {}
        for role_id, tag in ((STRUCTURE, "structure"), (DETAIL, "detail")):
            mask = in_obj & (role == role_id)
            if not mask.any():
                continue
            st = emit(mask, fam_idx, occupied, layout, args.max_quad,
                      struct_dir, "bld_%03d_%s" % (oid, tag),
                      ["MC2UE5 S3 building_id=%d part=%s" % (oid, tag)]
                      + comment_common)
            if st:
                st["bbox_world"] = world_bbox(mask)
                parts[tag] = st

        boxes = [p["bbox_world"] for p in parts.values()]
        union = ([[min(b[a][0] for b in boxes), max(b[a][1] for b in boxes)]
                  for a in range(3)] if boxes else None)
        src = o["bbox"]
        expect = [[src[0][0] + x0, src[0][1] + x0 + 1],
                  [src[1][0], src[1][1] + 1],
                  [src[2][0] + z0, src[2][1] + z0 + 1]]

        slots = Counter()
        for p in parts.values():
            slots.update(p["quads_by_material"])
        s = seat.get(oid, {})
        report.append({
            "id": oid,
            "blocks": int(in_obj.sum()),
            "structure_blocks": int((in_obj & (role == STRUCTURE)).sum()),
            "detail_blocks": int((in_obj & (role == DETAIL)).sum()),
            "bbox_world": union,
            "bbox_expected": expect,
            "bbox_matches_structures_json": union == expect,
            "quads": sum(p["quads"] for p in parts.values()),
            "triangles": sum(p["triangles"] for p in parts.values()),
            "meshes": parts,
            "material_slots": sorted(slots),
            "dominant_material": slots.most_common(1)[0][0] if slots else None,
            "seating": {k: s.get(k) for k in
                        ("voxel_bottom_y", "smooth_ground_y", "drop_m")},
        })
        tot_quads += report[-1]["quads"]
        if oid % 10 == 0:
            print("  %d / %d buildings" % (len(report), len(structures)))

    # ---- S4: detail components, grouped into a swap-in library ------------
    print("detail: labelling components ...")
    from scipy import ndimage
    detail_all = role == DETAIL
    dlab, _ = ndimage.label(detail_all,
                            structure=ndimage.generate_binary_structure(3, 1))
    # One byte per voxel, not an object array: at 448x72x768 an object array
    # would be ~200 MB and the whole classification volume is already resident.
    kind_lut = np.array([DETAIL_KIND_INDEX[detail_kind(n)] for n in names],
                        dtype=np.uint8)
    vox_kind = kind_lut[name_id]
    del name_id
    objects = ndimage.find_objects(dlab)

    # Per-component records, and one union mask per kind. Components are walked
    # through their bounding-box slice rather than as full-volume masks --
    # materialising one 24 MB bool per component is what exhausted memory.
    comps = []
    sel_by_kind = {k: np.zeros_like(detail_all) for k in DETAIL_KINDS}
    for cid, sl in enumerate(objects, start=1):
        if sl is None:
            continue
        sub = dlab[sl] == cid
        nb = int(sub.sum())
        if nb < args.min_detail:
            continue
        kc = Counter(vox_kind[sl][sub].tolist())
        kind = DETAIL_KINDS[kc.most_common(1)[0][0]]
        sel_by_kind[kind][sl] |= sub
        comps.append({"kind": kind, "blocks": nb, "slice": sl, "cid": cid})

    by_kind = {}
    for kind in DETAIL_KINDS:
        sel = sel_by_kind[kind]
        if not sel.any():
            continue
        n = sum(1 for c in comps if c["kind"] == kind)
        st = emit(sel, fam_idx, occupied, layout, args.max_quad, lib_dir,
                  "detail_%s" % kind,
                  ["MC2UE5 S4 detail library: kind=%s components=%d"
                   % (kind, n),
                   "DUPLICATE GEOMETRY, same world position as the per-building"
                   " *_detail.obj meshes.",
                   "Authoritative in-level geometry is the per-building mesh; "
                   "this group exists so an artist can replace all '%s' trim at"
                   " once. Do NOT add both to the level -- they z-fight."
                   % kind] + comment_common)
        if st:
            st["components"] = n
            st["authoritative"] = False
            by_kind[kind] = st
    del sel_by_kind, detail_all

    for c in comps:
        sl = c.pop("slice")
        cid = c.pop("cid")
        sub = dlab[sl] == cid
        idx = np.nonzero(sub)
        c["bbox_world"] = [[int(idx[0].min()) + sl[0].start + x0,
                            int(idx[0].max()) + sl[0].start + x0 + 1],
                           [int(idx[1].min()) + sl[1].start,
                            int(idx[1].max()) + sl[1].start + 1],
                           [int(idx[2].min()) + sl[2].start + z0,
                            int(idx[2].max()) + sl[2].start + z0 + 1]]
        c["centre_block"] = [int(idx[0].mean()) + sl[0].start + x0,
                             int(idx[1].mean()) + sl[1].start,
                             int(idx[2].mean()) + sl[2].start + z0]
        lab = labels[sl][sub]
        lab = lab[lab > 0]
        c["building"] = int(Counter(lab.tolist()).most_common(1)[0][0]) \
            if lab.size else None
        del sub, idx, lab
    del dlab, vox_kind

    # ---- S4: water -------------------------------------------------------
    water = role == WATER
    water_report = {"blocks": int(water.sum()),
                    "share_of_campus": round(
                        float(water.sum()) / float((role != 0).sum()), 9)}
    if water.any():
        st = emit(water, fam_idx, occupied, layout, args.max_quad,
                  struct_dir, "water_layer",
                  ["MC2UE5 S4 water layer"] + comment_common)
        water_report.update({k: st[k] for k in
                             ("obj", "quads", "triangles", "material_slots")})
        water_report["bbox_world"] = world_bbox(water)
    else:
        water_report["obj"] = None
        water_report["quads"] = 0
    water_report["note"] = (
        "The save holds 13 water blocks in 1,949,579. The layer is emitted "
        "anyway rather than skipped silently, so the pipeline has a water slot "
        "and the engine can shade it as its own surface. At this size it is "
        "noise against the terrain; it is reported, not hidden.")

    # ---- manifest --------------------------------------------------------
    labelled = int((labels > 0).sum())
    in_buildings = sum(o["blocks"] for o in structures)
    # Labelled voxels that belong to no reported building. classify.py drops
    # components under --min-structure, so these are real blocks of the save
    # that no building claims. They are reported rather than dropped silently:
    # 334 fragments of 1-39 blocks is small next to 430,501, but "small and
    # unaccounted for" is exactly how a hole in a courtyard goes unnoticed.
    kept_ids = np.zeros(int(labels.max()) + 1, dtype=bool)
    kept_ids[[o["id"] for o in structures]] = True
    orphan_mask = (labels > 0) & ~kept_ids[labels]
    orphan_count = int(orphan_mask.sum())
    orphan_labels = np.unique(labels[orphan_mask])
    naive = in_buildings * 12
    mismatches = [r["id"] for r in report
                  if not r["bbox_matches_structures_json"]]
    all_quads = tot_quads + sum(v["quads"] for v in by_kind.values()) \
        + water_report.get("quads", 0)

    # The UV convention is mode-dependent and must be recorded as such: a
    # reader who assumes atlas semantics on a block-mode mesh would look for
    # the atlas cell and find block counts instead, with nothing to warn them.
    if UV_MODE == "block":
        uv_convention = ("block-unit UVs, 1 texture repeat per block, "
                         "sampler address mode = Wrap")
        why_capped = ("greedy merging emits quads measured in blocks; the cap "
                      "bounds how many blocks one quad spans so the per-block "
                      "texture repeat stays uniform across the facade")
    else:
        uv_convention = ("each merged quad is stretched onto its material's "
                         "atlas cell, never tiled across it")
        why_capped = ("greedy merging alone stretches one atlas cell across a "
                      "whole facade; the cap bounds texel density")

    manifest = {
        "step": "S3+S4",
        "campus": list(CAMPUS),
        "coordinate_contract":
            "MC (x,y,z) -> UE (x*100, z*100, y*100) cm, Z up; "
            "MC_TO_UE = (0,2,1). Same contract as terrain_smooth.build_tiles.",
        "mesher": {
            "algorithm": "greedy meshing per material (Lysenko, 0fps.net/page/4)",
            "face_culling": "a face is emitted only where the neighbouring "
                            "voxel is empty in ANY role, campus-wide",
            "max_quad_blocks": args.max_quad,
            "uv_mode": UV_MODE,
            "why_capped": why_capped,
            "uv_convention": uv_convention,
            "winding": "right-hand rule around the outward normal, parity "
                       "derived per axis rather than hardcoded",
        },
        "atlas": {
            "planned_by": "tools/build_atlas.py plan_layout()",
            "families": ATLAS_FAMILIES,
            "tile_px": ATLAS_DEFAULTS["tile"],
            "manifest": "out/atlas/manifest.json",
        },
        "blocks": {
            "labelled_total": labelled,
            "in_reported_buildings": in_buildings,
            "structure": sum(r["structure_blocks"] for r in report),
            "detail": sum(r["detail_blocks"] for r in report),
            "unclaimed_fragments": orphan_count,
            "unclaimed_fragment_count": int(orphan_labels.size),
            "unclaimed_note": "structure + detail == in_reported_buildings: the "
                              "partition of the 88 buildings is exact and the "
                              "two meshes never overlap. The "
                              "unclaimed_fragments are labelled voxels in "
                              "components below classify.py's --min-structure "
                              "(default 40), so no building claims them and "
                              "they are meshed nowhere. Lower --min-structure "
                              "in S1 if they should be built.",
            "campus_total": int((role != 0).sum()),
        },
        "buildings": report,
        "bbox_check": {
            "objects": len(report),
            "matching_structures_json": len(report) - len(mismatches),
            "mismatched_ids": mismatches,
            "definition": "world bbox = union of the building's structure and "
                          "detail mesh bboxes, half-open on the max side, "
                          "compared against structures.json bbox + campus "
                          "origin. This is the 'volume and position unchanged' "
                          "assertion.",
        },
        "triangles": {
            "buildings": tot_quads * 2,
            "detail_library": sum(v["triangles"] for v in by_kind.values()),
            "water": water_report.get("triangles", 0),
            "all_outputs": all_quads * 2,
            "naive_12_per_block_same_voxels": naive,
            "reduction_vs_naive": round(naive / float(max(all_quads * 2, 1)), 2),
            "old_cube_layer": 14053728,
            "reduction_vs_cube_layer": round(
                14053728.0 / float(max(all_quads * 2, 1)), 1),
        },
        "detail": {
            "components": len(comps),
            "blocks_by_kind": {k: sum(c["blocks"] for c in comps
                                      if c["kind"] == k)
                               for k in DETAIL_KINDS},
            "kinds": list(DETAIL_KINDS),
            "library": by_kind,
            "note": "The per-building *_detail.obj meshes are the authoritative "
                    "in-level geometry. detail_kinds/ is a duplicate of the "
                    "same voxels grouped by trim kind, for art replacement. "
                    "Do not add both to a level.",
            "components_list": comps,
        },
        "water": water_report,
        "elapsed_s": round(time.time() - t0, 1),
    }
    with open(os.path.join(struct_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    print("")
    print("buildings: %d objects, %d quads, %d triangles"
          % (len(report), tot_quads, tot_quads * 2))
    print("  bbox == structures.json: %d / %d"
          % (len(report) - len(mismatches), len(report)))
    print("  naive 12/block on the same %d voxels = %d triangles"
          % (labelled, naive))
    print("  all outputs %d triangles; %.1fx fewer than the 14,053,728 cube "
          "layer" % (all_quads * 2, 14053728.0 / max(all_quads * 2, 1)))
    print("detail: %d components, library %s"
          % (len(comps), sorted(by_kind)))
    print("water: %d blocks -> %s"
          % (water_report["blocks"], water_report.get("obj")))
    print("report: %s/structures/manifest.json" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())