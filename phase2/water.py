"""
Water surface extraction (paper: "The water layer is exported separately as a
flat mesh at the recorded sea level").

We do exactly that, with one refinement: instead of a single global plane at
`level.dat`'s SeaLevel, we emit the *actual* topmost-water height per column.
Creative maps often have water at several levels (ponds, basins, a moat), and a
flat plane at SeaLevel would slice through them. Where the map really is a
single flat body of water, the two agree -- so this is a strict improvement and
still satisfies the paper's engine-side-ocean-shader use case.
"""

import json

import numpy as np

from semantic import LABEL_WATER, LABEL_LAVA

BLOCK_CM = 100.0


def extract_water(cursor, chunk_range, y_max, labels_per_voxel,
                  water_labels=(LABEL_WATER,), lava_labels=(LABEL_LAVA,)):
    """
    Returns (surface, valid, is_lava) where
        surface : float32[H, W] world Y of the topmost liquid face (+1)
        valid   : bool[H, W]
        is_lava : bool[H, W]
    """
    cx0, cz0, cx1, cz1 = chunk_range
    W = (cx1 - cx0 + 1) * 16
    H = (cz1 - cz0 + 1) * 16
    x0, z0 = cx0 * 16, cz0 * 16

    top_y = np.full((H, W), -32768, dtype=np.int32)
    top_lava = np.zeros((H, W), dtype=bool)

    want = set(int(t) for t in tuple(water_labels) + tuple(lava_labels))
    lava_set = set(int(t) for t in lava_labels)

    for cz in range(cz0, cz1 + 1):
        for cx in range(cx0, cx1 + 1):
            got = cursor.get(cx, cz)
            if got is None:
                continue
            xs, ys, zs, st = got
            if st.size == 0:
                continue
            lab = labels_per_voxel(st, ys)
            keep = np.isin(lab, list(want))
            if not keep.any():
                continue
            lx = (xs[keep] - x0).astype(np.int64)
            lz = (zs[keep] - z0).astype(np.int64)
            ly = ys[keep]
            ll = lab[keep]
            # waterlogged slabs sit below the surface block; topmost wins
            order = np.argsort(ly, kind="stable")
            lx, lz, ly, ll = lx[order], lz[order], ly[order], ll[order]
            top_y[lz, lx] = ly
            top_lava[lz, lx] = np.isin(ll, list(lava_set))

    valid = top_y != -32768
    surface = np.where(valid, top_y.astype(np.float32) + 1.0, 0.0)
    return surface, valid, top_lava


def water_mesh(surface, valid, origin_x, origin_z, name="water"):
    """
    Flat mesh over the water surface, sharing the terrain's XZ grid.

    Only quads whose four corners are all valid are emitted, so shorelines get a
    real edge instead of being stretched over dry land.

    Vertices are emitted only for the cells those quads actually touch. The
    earlier version laid down one vertex per grid cell, which for a map with a
    few isolated ponds meant ~750k vertices and zero triangles -- a real file on
    disk for no geometry at all. Compacting to the referenced subset keeps the
    OBJ proportional to the water, and the index mapping is done with a lookup
    table so it costs one pass, not a Python loop.
    """
    from terrain import BLOCK_CM as _BC

    H, W = surface.shape
    quads = _valid_quads(valid)
    if quads.size == 0:
        return {"name": name,
                "vertices": np.zeros((0, 3), dtype=np.float32),
                "faces": np.zeros((0, 3), dtype=np.int64),
                "quad_count": 0}

    i, j = quads[:, 0], quads[:, 1]          # i = column, j = row

    # Which grid cells does any quad touch?
    used = np.zeros((H, W), dtype=bool)
    for dj in (0, 1):
        for di in (0, 1):
            used[j + dj, i + di] = True
    remap = np.full((H, W), -1, dtype=np.int64)
    remap[used] = np.arange(int(used.sum()), dtype=np.int64)

    ys, xs = np.nonzero(used)
    # surface is [row=z, col=x]; xs indexes columns, ys indexes rows.
    vx_cm = (origin_x + xs.astype(np.float32)) * _BC
    vz_cm = (origin_z + ys.astype(np.float32)) * _BC
    vy_cm = surface[ys, xs].astype(np.float32) * _BC
    verts = np.stack([vx_cm, vy_cm, vz_cm], axis=1)

    v00 = remap[j, i]
    v01 = remap[j + 1, i]
    v10 = remap[j, i + 1]
    v11 = remap[j + 1, i + 1]
    # Wound so the normal points +Y
    tri_a = np.stack([v00, v01, v11], axis=-1)
    tri_b = np.stack([v00, v11, v10], axis=-1)
    faces = np.concatenate([tri_a, tri_b], axis=0).astype(np.int64)
    return {"name": name, "vertices": verts, "faces": faces,
            "quad_count": int(quads.shape[0]),
            "grid": [int(W), int(H)],
            "grid_origin": [int(origin_x), int(origin_z)]}


def _valid_quads(valid):
    """(i, j) pairs where valid[j][i..i+1] x valid[j+1][i..i+1] are all True."""
    v = valid.astype(np.uint8)
    # a quad is interior when all 4 corners are set
    quad = (v[:-1, :-1] & v[:-1, 1:] & v[1:, :-1] & v[1:, 1:]) > 0
    jj, ii = np.nonzero(quad)
    return np.stack([ii, jj], axis=1)


def sea_level_info(surface, valid):
    """
    Summarise the water body for the engine side.

    `sea_level_blocks` (and its `_cm` twin) is the modal surface height, which is
    the right anchor for a depth-fade material; min/max bracket the range so the
    shader can clamp. Both are reported in blocks and centimetres because the
    two consumers are different: the UE5 importer thinks in cm, the debug
    overlay in blocks.
    """
    if not valid.any():
        return {"has_water": False, "sea_level_blocks": None,
                "sea_level_cm": None}
    s = surface[valid]
    hist, edges = np.histogram(s, bins=64)
    peak = float(edges[int(np.argmax(hist))])
    return {
        "has_water": True,
        "columns": int(valid.sum()),
        "sea_level_blocks": peak,
        "sea_level_cm": peak * BLOCK_CM,
        "min_blocks": float(s.min()),
        "max_blocks": float(s.max()),
        "min_cm": float(s.min()) * BLOCK_CM,
        "max_cm": float(s.max()) * BLOCK_CM,
        "mean_cm": float(s.mean()) * BLOCK_CM,
    }
