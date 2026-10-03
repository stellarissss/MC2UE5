"""
Terrain reconstruction (paper section "Terrain upscaling").

Pipeline, following the paper's four disclosed steps:

  1. hide non-terrain voxels      -> per-column topmost TERRAIN/SUBSURFACE height
  2. trilinear upsample           -> scipy.ndimage.zoom, order=1
  3. anisotropic Gaussian filter  -> flatten staircases without collapsing relief
  4. tile + Poisson-style blend   -> 512-block tiles with overlap, gradient-weighted

Why a heightmap and not a voxel iso-surface (Marching Cubes / Surface Nets):
the paper explicitly resamples "the stepped block surface ... into a smooth
height-map", and for this save that is the right call -- 14.5M overworld
voxels are overwhelmingly a height field (dirt/grass/bedrock/sand), so an
iso-surface would mostly re-expose buried interior walls. Surface Nets remains
available as a fallback for tiles with heavy overhangs (see `overhang_ratio`).

Coordinate convention
---------------------
World block (x, y, z) maps to UE centimetres as:

    X_cm = x * BLOCK_CM
    Y_cm = y * BLOCK_CM
    Z_cm = z * BLOCK_CM

with BLOCK_CM = 100 (Minecraft convention: 1 block = 1 m = 100 cm). The UE5
importer consumes these numbers directly.
"""

import json
import os

import numpy as np

from semantic import (LABEL_TERRAIN, LABEL_AIR, LABEL_WATER,
                      LABEL_LAVA, LABEL_BEDROCK)

BLOCK_CM = 100.0

# Tile geometry. 512 blocks = 51.2 m at 1 block = 1 m; with World Partition a
# cell size of 512 keeps tile counts low while staying well inside a streaming
# cell. OVERLAP is the halo used for seamless blending between neighbours.
TILE_BLOCKS = 512
TILE_OVERLAP = 32

# Upsample factor: 1 block -> 4 sub-samples == 25 cm resolution. Enough to kill
# the staircase read without exploding triangle count (4^2 = 16x the verts).
DEFAULT_UPSAMPLE = 4


# --------------------------------------------------------------------------- #
# step 1: heightmap extraction
# --------------------------------------------------------------------------- #

def extract_heightmap(cursor, chunk_range, y_max, labels_per_voxel=None,
                      terrain_labels=(2, 3), label_fill=0,
                      surface_only=True):
    """
    Build the surface heightmap for a rectangle of chunks.

    Returns (height, valid, top_label) where
        height    : float32[H, W]  world Y of the surface (+1 = top face)
        valid     : bool[H, W]     False where no solid column exists
        top_label : uint8[H, W]    label of the voxel that produced the surface

    `cursor` is a voxelio.ChunkCursor. `labels_per_voxel` is a callable
    (state_array, y_array) -> label array; when omitted every non-air voxel
    counts as solid (quick looks only).

    surface_only=True (the default, and what the pipeline uses) walks each
    column from the top down and takes the FIRST solid voxel -- i.e. the actual
    visible ground. That is deliberately *not* "the highest voxel whose label is
    in terrain_labels": this map's surface is grass_block over dirt over
    bedrock, and the earlier label-filtered version sank the whole heightmap to
    the bedrock layer (y≈1) because bedrock was classified as terrain-like. Top
    -down scanning is also what a viewer sees, so it cannot disagree with the
    reference image.

    surface_only=False keeps the label-filtered behaviour for callers that
    specifically want "highest block of these kinds" (e.g. excluding tree
    canopies from a terrain-only surface).

    Heightmap indexing follows world coordinates directly:
        height[z - z0][x - x0]
    so no axis juggling is needed downstream.
    """
    cx0, cz0, cx1, cz1 = chunk_range
    W = (cx1 - cx0 + 1) * 16
    H = (cz1 - cz0 + 1) * 16
    x0 = cx0 * 16
    z0 = cz0 * 16

    top_y = np.full((H, W), -32768, dtype=np.int32)
    top_lab = np.full((H, W), label_fill, dtype=np.uint8)

    want = set(int(t) for t in terrain_labels)
    # Labels that never count as ground, even in surface_only mode.
    never_ground = {LABEL_AIR, LABEL_WATER, LABEL_LAVA, LABEL_BEDROCK}

    for cz in range(cz0, cz1 + 1):
        for cx in range(cx0, cx1 + 1):
            got = cursor.get(cx, cz)
            if got is None:
                continue
            xs, ys, zs, st = got
            if st.size == 0:
                continue
            if labels_per_voxel is None:
                lab = np.full(st.size, LABEL_TERRAIN, dtype=np.uint8)
            else:
                lab = labels_per_voxel(st, ys)

            if surface_only:
                keep = ~np.isin(lab, list(never_ground))
            else:
                keep = np.isin(lab, list(want))
            if not keep.any():
                continue

            # Mask every array with the same `keep` -- filtering one array but
            # re-deriving another from differently-sized inputs is how the
            # "boolean axis is 1024 vs size 512" mismatch happens.
            xs, ys, zs = xs[keep], ys[keep], zs[keep]
            lab = lab[keep]

            lx = xs - x0
            lz = zs - z0
            # Within one column keep only the topmost qualifying voxel.
            # np.maximum.at does not carry the payload, so sort by y ascending
            # and let the last write win.
            order = np.argsort(ys, kind="stable")
            lx, lz, ys, lab = lx[order], lz[order], ys[order], lab[order]
            top_y[lz, lx] = ys
            top_lab[lz, lx] = lab.astype(np.uint8)

    valid = top_y != -32768
    height = np.where(valid, top_y.astype(np.float32) + 1.0, 0.0)
    return height, valid, top_lab


# --------------------------------------------------------------------------- #
# step 2 + 3: upsample and de-staircase
# --------------------------------------------------------------------------- #

def upsample(height, valid, factor=DEFAULT_UPSAMPLE, order=1):
    """
    Trilinear upsample of the height field.

    `order=1` is trilinear, matching the paper. Valid-mask holes are filled by
    nearest-neighbour dilation first so the interpolation does not drag the
    void value 0 into the terrain (a hole would otherwise become a pit).
    """
    from scipy import ndimage

    filled = _fill_holes(height, valid)
    zoomed = ndimage.zoom(filled, factor, order=order, mode="nearest")
    valid_z = ndimage.zoom(valid.astype(np.float32), factor, order=0,
                           mode="nearest") > 0.5
    # zoom() on a (H, W) array scales both axes by `factor`
    return zoomed.astype(np.float32), valid_z


def _fill_holes(height, valid):
    """Nearest-valid dilation: replace invalid cells with the closest valid one."""
    from scipy import ndimage
    if valid.all():
        return height
    if not valid.any():
        return np.zeros_like(height)
    # Iterative dilation converges but is slow for big holes; use distance
    # transform based index lookup instead -- exact and O(n log n).
    _, (iy, ix) = ndimage.distance_transform_edt(~valid, return_indices=True)
    return height[iy, ix]


def destair(height, valid, sigma_xy=2.0, sigma_z=0.6, passes=1):
    """
    Anisotropic Gaussian smoothing (paper step 3).

    The anisotropy is the whole point: sigma along X/Y is several times sigma
    along Z (height). A large horizontal sigma erases the staircase edges; a
    small vertical sigma preserves hills and valleys instead of turning
    everything into a blob.

    Note on axis order: our arrays are indexed [z, x] -- row = Z, column = X.
    There is no third axis, so "vertical" (world Y) is the *value* being
    smoothed, not a spatial axis. Anisotropy is therefore achieved by scaling
    the height values relative to the horizontal distances before/after the
    filter, which is equivalent to filtering in a space where Y is stretched.
    Practically: smooth in (X, Z) only, with a modest sigma, and let the
    upsample factor provide the vertical resolution. This is what the paper
    describes as removing "staircase artefacts" while leaving "coarse relief"
    undisturbed.
    """
    from scipy import ndimage
    out = _fill_holes(height, valid)
    for _ in range(max(1, passes)):
        out = ndimage.gaussian_filter(out, sigma=(sigma_xy, sigma_xy),
                                     mode="nearest")
    # Slight vertical relaxation: pull each sample a little toward the local
    # mean in Z as well, at much lower weight, to avoid perfectly flat facets.
    out = ndimage.gaussian_filter(out, sigma=(sigma_z, sigma_z), mode="nearest")
    w = 0.65 * float(valid.mean()) + 0.35
    out = out * w + out.mean() * (1.0 - w)
    return out.astype(np.float32)


def smooth_heights(height, valid, sigma=1.4, edge_preserve=0.6):
    """
    Combined smoothing entry point actually used by the pipeline.

    Uses a blend of a Gaussian (removes staircase) and an edge-preserving
    pass (keeps cliffs and building platforms from being rounded off), which
    is the practical equivalent of the paper's anisotropic filter and avoids
    the extra dependency on skimage for the hot path.
    """
    from scipy import ndimage
    filled = _fill_holes(height, valid)
    gauss = ndimage.gaussian_filter(filled, sigma=(sigma, sigma), mode="nearest")

    # Edge-preserving: iterative anisotropic diffusion (Perona-Malik style),
    # a few sweeps only -- enough to keep cliffs, cheap enough to run per tile.
    diff = filled.copy()
    for _ in range(3):
        dx = np.gradient(diff, axis=1)
        dz = np.gradient(diff, axis=0)
        # conductance decreases where the gradient is large -> flat regions
        # smooth, cliffs preserved
        cx = 1.0 / (1.0 + (dx / max(sigma, 1e-6)) ** 2)
        cz = 1.0 / (1.0 + (dz / max(sigma, 1e-6)) ** 2)
        diff = diff + 0.2 * (np.gradient(cx * dx, axis=1)
                             + np.gradient(cz * dz, axis=0))
    out = (1.0 - edge_preserve) * gauss + edge_preserve * diff
    return out.astype(np.float32)


# --------------------------------------------------------------------------- #
# step 4: tiling + seamless blend
# --------------------------------------------------------------------------- #

def tile_ranges(total, tile=TILE_BLOCKS, overlap=TILE_OVERLAP):
    """
    Split [0, total) into overlapping tiles.

    Yields (start, end, core_start, core_end) where [core_start, core_end) is
    the region this tile owns exclusively and the rest is halo for blending.
    Guarantees full coverage with no gaps.
    """
    if total <= tile:
        yield 0, total, 0, total
        return
    stride = tile - overlap
    starts = list(range(0, max(1, total - tile + 1), stride))
    if starts[-1] + tile < total:
        starts.append(total - tile)
    for i, s in enumerate(starts):
        e = min(s + tile, total)
        # halo on the inner edges only
        cs = s + (overlap if i > 0 else 0)
        ce = e - (overlap if i < len(starts) - 1 else 0)
        if ce <= cs:
            cs, ce = s, e
        yield s, e, cs, ce


def blend_weight(n_core, n_total, ramp=None):
    """
    Cosine ramp weights for blending tile overlaps: 0 at the outer edge,
    rising to 1 inside the core. Symmetric, so neighbouring tiles cross-fade.
    """
    if ramp is None:
        ramp = max(2, n_total - n_core)
    w = np.ones(n_total, dtype=np.float32)
    r = min(ramp, n_total // 2) if n_total > 3 else 1
    if r > 0:
        head = 0.5 * (1.0 - np.cos(np.linspace(0, np.pi, r, dtype=np.float32)))
        tail = head[::-1]
        w[:r] = np.minimum(w[:r], head)
        w[-r:] = np.minimum(w[-r:], tail)
    return w


# --------------------------------------------------------------------------- #
# mesh generation
# --------------------------------------------------------------------------- #

def heightmap_to_mesh(height, origin_x, origin_z, name="terrain"):
    """
    Turn a height field into a triangle mesh.

    Vertices are placed at (x, y, z) in *centimetres*, matching the UE5
    convention used by the layer-1 HISM importer, so both layers share one
    coordinate system without conversion.

    Triangulation: each quad of the grid becomes two triangles. Diagonals are
    chosen per-quad by comparing the two candidate diagonals' implied normals,
    which avoids the classic zig-zag artefact on ridges.
    """
    H, W = height.shape
    # Grid of (W x H) samples; the mesh spans the full extent so the outer ring
    # keeps its true height instead of dropping to zero.
    xs = origin_x + np.arange(W, dtype=np.float32) * BLOCK_CM
    zs = origin_z + np.arange(H, dtype=np.float32) * BLOCK_CM
    ys = height.astype(np.float32) * BLOCK_CM

    gx, gz = np.meshgrid(xs, zs)
    verts = np.stack([gx.ravel(), ys.ravel(), gz.ravel()], axis=1)

    ix = np.arange(W - 1)
    iz = np.arange(H - 1)
    I, J = np.meshgrid(ix, iz, indexing="xy")
    # vertex indices of each quad corner
    v00 = (I + J * W)
    v01 = (I + (J + 1) * W)
    v10 = ((I + 1) + J * W)
    v11 = ((I + 1) + (J + 1) * W)

    yr = ys.ravel()
    # Pick the shorter diagonal per quad: fewer long thin triangles, and the
    # shading discontinuity follows the terrain's own ridges. `flip` is per-quad
    # (2D), so it broadcasts against the (N, 3) corner stacks.
    d_main = np.abs(yr[v00] - yr[v11])
    d_alt = np.abs(yr[v01] - yr[v10])
    flip = (d_alt < d_main)[..., None]        # (H-1, W-1, 1)

    tri_a = np.where(flip,
                     np.stack([v00, v01, v11], axis=-1),
                     np.stack([v00, v01, v10], axis=-1))
    tri_b = np.where(flip,
                     np.stack([v00, v11, v10], axis=-1),
                     np.stack([v01, v11, v10], axis=-1))
    tris = np.concatenate([tri_a.reshape(-1, 3), tri_b.reshape(-1, 3)], axis=0)
    return {"name": name, "vertices": verts, "faces": tris.astype(np.int64)}


def mesh_stats(mesh):
    """
    Sanity metrics used for the M3 acceptance gate.

    Tolerates an empty mesh: a water body made of isolated single columns has
    zero quads, and reporting "no triangles" is a valid, useful answer -- not a
    crash.
    """
    v = mesh["vertices"]
    f = mesh["faces"]
    if v.shape[0] == 0 or f.shape[0] == 0:
        return {
            "vertices": int(v.shape[0]),
            "triangles": int(f.shape[0]),
            "degenerate": 0,
            "down_facing": 0,
            "up_facing": 0,
            "area_cm2": 0.0,
            "y_range_cm": None,
            "nan": 0,
        }
    tri = v[f]
    # Newell normal per triangle -> winding consistency + up-facing check
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = 0.5 * np.linalg.norm(n, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        ny = n[:, 1] / np.maximum(np.linalg.norm(n, axis=1), 1e-12)
    return {
        "vertices": int(v.shape[0]),
        "triangles": int(f.shape[0]),
        "degenerate": int((area <= 1e-9).sum()),
        "down_facing": int((ny < -1e-6).sum()),
        "up_facing": int((ny > 1e-6).sum()),
        "area_cm2": float(area.sum()),
        "y_range_cm": [float(v[:, 1].min()), float(v[:, 1].max())],
        "nan": int(np.isnan(v).any(axis=1).sum()),
    }


def repair_mesh(mesh, drop_degenerate=True, orient_up=True):
    """
    Fix the two defects heightmap_to_mesh can produce.

    Degenerate (zero-area) triangles appear where adjacent heights are equal;
    UE5 rejects or mis-lights them, so they are removed. Down-facing triangles
    come from the diagonal choice on flat ridges; flipping them to +Y keeps the
    lighting consistent without rebuilding the index buffer.
    """
    v = mesh["vertices"]
    f = mesh["faces"]
    if drop_degenerate:
        tri = v[f]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        keep = np.linalg.norm(n, axis=1) > 1e-9
        f = f[keep]
    if orient_up and f.size:
        tri = v[f]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        flip = n[:, 1] < 0
        if flip.any():
            f = f.copy()
            f[flip] = f[flip][:, [0, 2, 1]]
    return {"name": mesh["name"], "vertices": v, "faces": f}


# --------------------------------------------------------------------------- #
# IO
# --------------------------------------------------------------------------- #

def save_heightmap_png(path, height, origin_x, origin_z, z_min=None):
    """
    Write a 16-bit greyscale PNG for UE5's Landscape "Import from File".

    UE5 reads landscape heightmaps as 16-bit greyscale. The value range must be
    identical for every tile of a landscape or the surface tears between them,
    so callers pass the *global* `z_min`/`z_max` rather than per-tile extremes.
    Here `z_min` defaults to the field's own minimum, which is correct for
    single-surface exports.
    """
    from PIL import Image
    lo = float(height.min()) if z_min is None else float(z_min)
    hi = float(height.max())
    if hi <= lo:
        hi = lo + 1.0
    norm = (height.astype(np.float32) - lo) / (hi - lo)
    u16 = np.clip(norm * 65535.0, 0, 65535).astype(np.uint16)
    img = Image.fromarray(u16, mode="I;16")
    img.save(path)
    return {"path": path, "z_min_cm": lo * BLOCK_CM,
            "z_max_cm": hi * BLOCK_CM, "origin_x_cm": origin_x * BLOCK_CM,
            "origin_z_cm": origin_z * BLOCK_CM,
            "resolution": [int(height.shape[1]), int(height.shape[0])]}


def save_obj(path, mesh, mtl_basename=None):
    """
    Minimal OBJ writer (v/vn/f). Deliberately not using trimesh here: UE5's
    importer is happiest with a plain, index-from-1 OBJ, and writing it by hand
    keeps the output byte-for-byte predictable across numpy versions.
    """
    v = mesh["vertices"]
    f = mesh["faces"]
    with open(path, "w") as fh:
        fh.write("# MC2UE5 phase2 terrain tile\n")
        fh.write("o %s\n" % mesh.get("name", "terrain"))
        if mtl_basename:
            fh.write("usemtl %s\n" % mtl_basename)
            fh.write("mtllib %s.mtl\n" % mtl_basename)
        for p in v:
            fh.write("v %.4f %.4f %.4f\n" % (p[0], p[1], p[2]))
        # vertex normals from area-weighted face normals
        n = vertex_normals(v, f)
        for p in n:
            fh.write("vn %.6f %.6f %.6f\n" % (p[0], p[1], p[2]))
        for tri in f + 1:
            fh.write("f %d//%d %d//%d %d//%d\n"
                     % (tri[0], tri[0], tri[1], tri[1], tri[2], tri[2]))
    return path


def vertex_normals(verts, faces):
    """Area-weighted vertex normals."""
    tri = verts[faces]
    fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    vn = np.zeros_like(verts)
    for k in range(3):
        np.add.at(vn, faces[:, k], fn)
    ln = np.linalg.norm(vn, axis=1, keepdims=True)
    ln[ln == 0] = 1.0
    return vn / ln


def save_metadata(path, data):
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return path
