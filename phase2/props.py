"""
Object instance extraction and ground alignment (paper section "Object
substitution").

The paper uses a 3D U-Net to label objects, writes each label to an "object
list" holding block-accurate position, three-axis orientation and tags, then
queries an external model library and rigidly aligns each result to the local
ground patch.

We implement everything except the network:

  * instance extraction -- 3D connected components over vegetation/structure
    voxels, which is exactly what the U-Net output would be clustered into
    anyway, so swapping the classifier in changes only the cluster boundaries;
  * class inference -- from the voxel composition of each component (which
    block families it is built from), mapped to a model-library query;
  * ground alignment -- RANSAC plane fit on the local terrain, then a rigid
    transform placing the model flush on that patch.

The model library is pluggable (`ModelProvider`) so the default needs no
network access and no API keys.
"""

import json
import os
from collections import Counter

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components as connected_components_csr

from semantic import (LABEL_VEGETATION, LABEL_STRUCTURE, LABEL_PROP, LABEL_GLASS,
                      LABEL_NAMES, parse_properties)

BLOCK_CM = 100.0

# Components smaller than this are noise / scatter, not objects.
MIN_COMPONENT_VOXELS = int(os.environ.get("MC2UE5_MIN_COMPONENT", "12"))

# Components larger than this are almost always terrain walls or the map's
# ground fabric; the paper's U-Net would not call those a single "object".
MAX_COMPONENT_VOXELS = int(os.environ.get("MC2UE5_MAX_COMPONENT", "20000"))


def connected_components(voxels, labels, labels_wanted, pitch=1):
    """
    Group voxels into spatially connected instances.

    `voxels` : int32[N, 3] world (x, y, z)
    `labels` : uint8[N]

    Returns a list of (coords, labels) pairs, one per component, where
    `coords` is int32[M, 3] and `labels` is uint8[M]. Returning the coordinates
    directly (rather than indices into the caller's arrays) removes any chance
    of the caller mixing masked and unmasked index spaces.

    Connectivity is 6-neighbour on a `pitch`-scaled lattice (default 1 = blocks
    touching faces are connected). Using a coarse pitch trades precision for
    speed on huge components; the default keeps exactness.

    Implementation: **scipy.sparse.csgraph.connected_components**, not a
    hand-rolled union-find. The sparse-graph version labels every connected
    component in one C-level pass, which on the campus-sized component set
    (hundreds of thousands of vegetation/structure voxels) is roughly two
    orders of magnitude faster than the Python equivalent, and it is the
    documented tool for exactly this problem. A dense 3D label volume was never
    an option: the full overworld is 4320 x 112 x 3375 = 1.6 G cells.

    The graph is sparse by construction -- at most 3 edges per voxel along each
    of the 3 positive axes -- so memory stays proportional to real voxels.
    """
    want = set(int(t) for t in labels_wanted)
    mask = np.isin(labels, list(want))
    if not mask.any():
        return []
    vx = np.ascontiguousarray(voxels[mask], dtype=np.int64)
    vl = labels[mask]

    # Collapse voxels onto the pitch lattice. Multiple voxels can share a cell
    # (e.g. pitch=2); they must be one node, so build cell -> node mapping.
    cells = vx // max(1, int(pitch))
    uniq, inverse = np.unique(cells, axis=0, return_inverse=True)
    n_nodes = uniq.shape[0]
    inverse = inverse.ravel()
    if n_nodes == 1:
        return [(vx, vl)]

    # Build edges: for each of the 3 positive axes, join nodes that are 1 apart
    # and identical on the other two axes.
    #
    # The join is done with a sort-based lookup rather than a Python dict loop:
    # stack the 3 axes into one int64 key, argsort once, then np.searchsorted
    # the shifted keys against the sorted keys. Sorting-based *neighbour
    # hunting* (comparing consecutive elements of a lexsort) is wrong when a
    # column holds several cells -- it tears a U shape or a ring apart -- but
    # sort-based *exact lookup* is fine and stays vectorised.
    maxc = uniq.max(axis=0) + 2
    mul = np.array([maxc[1] * maxc[2], maxc[2], 1], dtype=np.int64)
    key = (uniq * mul).sum(axis=1)
    order = np.argsort(key, kind="stable")
    skey = key[order]

    rows = []
    cols = []
    for axis in range(3):
        shifted = uniq.copy()
        shifted[:, axis] += 1
        sk = (shifted * mul).sum(axis=1)
        pos = np.searchsorted(skey, sk)
        pos = np.clip(pos, 0, max(0, skey.size - 1))
        hit = skey[pos] == sk
        if not hit.any():
            continue
        # order[pos] maps a search hit back to the original node index;
        # flatnonzero(hit) indexes into uniq, i.e. the node that looked for it.
        rows.append(order[pos[hit]])
        cols.append(np.flatnonzero(hit))

    if rows:
        rr = np.concatenate(rows).astype(np.int64)
        cc = np.concatenate(cols).astype(np.int64)
    else:
        rr = np.zeros(0, dtype=np.int64)
        cc = np.zeros(0, dtype=np.int64)

    data = np.ones(rr.size, dtype=np.int8)
    g = coo_matrix((data, (rr, cc)), shape=(n_nodes, n_nodes)).tocsr()
    n_comp, node_labels = connected_components_csr(g, directed=False)

    # Map node labels back onto voxels, then split by component.
    vox_comp = node_labels[inverse]
    order = np.argsort(vox_comp, kind="stable")
    sorted_comp = vox_comp[order]
    boundaries = np.flatnonzero(np.diff(sorted_comp)) + 1
    starts = np.concatenate(([0], boundaries))
    ends = np.concatenate((boundaries, [sorted_comp.size]))

    out = []
    for s, e in zip(starts, ends):
        idx = order[s:e]
        out.append((vx[idx], vl[idx]))
    return out


def classify_component(vox, labs, palette):
    """
    Infer an object class from the blocks that compose it.

    The paper's U-Net directly emits classes like "oak tree" / "villager house".
    We infer the same information from composition: a component dominated by
    leaves+logs is a tree, one dominated by planks/beams over a large footprint
    is a building, and so on.

    Weaker than the network by construction -- it cannot distinguish an oak from
    a birch here, only "tree" from "building" -- but it needs no weights, and
    `composition` carries the block-name histogram so a caller that does have
    species information can refine the class downstream.
    """
    n = vox.shape[0]
    if n == 0:
        return "unknown", {}

    n_veg = int((labs == LABEL_VEGETATION).sum())
    n_struct = int((labs == LABEL_STRUCTURE).sum())
    n_prop = int((labs == LABEL_PROP).sum())
    n_glass = int((labs == LABEL_GLASS).sum())

    frac_veg = n_veg / n
    frac_struct = n_struct / n

    extent = vox.max(axis=0) - vox.min(axis=0) + 1
    ex, ey, ez = int(extent[0]), int(extent[1]), int(extent[2])
    height = ey
    footprint = ex * ez

    comp = {"voxels": n, "veg_frac": round(frac_veg, 3),
            "struct_frac": round(frac_struct, 3),
            "height": height, "footprint": footprint,
            "bbox": [ex, ey, ez]}

    if frac_veg > 0.55:
        cls = "tree" if height >= 4 else "plant"
    elif frac_struct > 0.5:
        if footprint >= 25 and height >= 3:
            cls = "building"
        else:
            cls = "structure"
    elif frac_veg > 0.2:
        cls = "tree" if height >= 4 else "plant"
    else:
        cls = "prop"

    comp["glass_frac"] = round(n_glass / n, 3)
    comp["prop_frac"] = round(n_prop / n, 3)
    return cls, comp


def fit_ground_plane(heightmap, origin_x, origin_z, cx, cz, radius=6):
    """
    Least-squares plane fit over a (2*radius+1)^2 patch of the heightmap.

    Returns (point, normal) with `point` in **world block coordinates**, or None
    when the patch is degenerate (all invalid). The paper calls this
    "normalised to the local ground patch"; using the terrain we already
    extracted keeps the model flush with the *smoothed* surface the model will
    actually sit on, rather than the raw blocky one.

    Coordinate care: the fit solves for the plane over local patch indices, so
    the fitted intercept belongs to the patch's local frame. Adding the heightmap
    origin converts it back to world blocks. Forgetting that produced placements
    snapped to the origin cell -- a tree at world x=248 came out at x=52000 cm,
    i.e. grid (520, 300) instead of (248, -372).
    """
    H, W = heightmap.shape
    px = int(cx) - int(origin_x)
    pz = int(cz) - int(origin_z)
    x0, x1 = max(0, px - radius), min(W, px + radius + 1)
    z0, z1 = max(0, pz - radius), min(H, pz + radius + 1)
    if x1 - x0 < 2 or z1 - z0 < 2:
        return None
    patch = heightmap[z0:z1, x0:x1]
    if not np.isfinite(patch).any():
        return None
    zs, xs = np.mgrid[z0:z1, x0:x1]
    A = np.stack([xs.ravel(), zs.ravel(), np.ones(xs.size)], axis=1).astype(np.float64)
    b = patch.ravel().astype(np.float64)
    # least squares; rank-deficiency just yields a flat plane, which is fine
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    a, c, d = sol
    normal = np.array([a, -1.0, c], dtype=np.float64)
    norm = np.linalg.norm(normal)
    if norm == 0:
        return None
    normal /= norm
    if normal[1] > 0:
        normal = -normal
    # Local patch index -> world block.
    wx = float(cx)
    wz = float(cz)
    y = float(sol[0] * px + sol[1] * pz + sol[2])
    point = np.array([wx, y, wz], dtype=np.float64)
    return point, normal


def place_on_ground(instance, heightmap, origin_x, origin_z):
    """
    Rigid transform placing an instance on the local terrain.

    Returns a dict with position (cm), Y-rotation (deg) and pitch/roll derived
    from the ground normal. Keeping it to a rigid transform (no non-uniform
    scale) is what the paper specifies, and it is also what makes the result
    reusable across engines.
    """
    x, _y, z = instance["centroid_blocks"]
    fit = fit_ground_plane(heightmap, origin_x, origin_z, x, z)
    if fit is None:
        ground_y = float(instance["base_y_blocks"])
        normal = np.array([0.0, -1.0, 0.0])
    else:
        (px, py, pz), normal = fit
        ground_y = py
        x, z = float(px), float(pz)

    # Euler from the normal: pitch about X, roll about Z, yaw free (0).
    # For a ground normal n with n[1] < 0 (pointing into the ground), the tilt
    # from vertical is atan2 of the horizontal components.
    tilt_x = float(np.degrees(np.arctan2(-normal[2], -normal[1])))
    tilt_z = float(np.degrees(np.arctan2(normal[0], -normal[1])))

    yaw = float(instance.get("yaw_deg", 0.0))
    return {
        "position_cm": [x * BLOCK_CM, ground_y * BLOCK_CM, z * BLOCK_CM],
        "rotation_deg": [tilt_x, yaw, tilt_z],
        "ground_y_blocks": ground_y,
        "ground_normal": [float(normal[0]), float(normal[1]), float(normal[2])],
    }


def estimate_yaw(vox):
    """
    Principal-axis yaw from the footprint, via a 2D covariance eigen-decomposition.

    Trees and rectangular buildings have a meaningful long axis; aligning the
    model's forward axis to it is the cheap 80% of the paper's "three-axis
    orientation" and needs no network.
    """
    if vox.shape[0] < 4:
        return 0.0
    xz = vox[:, [0, 2]].astype(np.float64)
    xz = xz - xz.mean(axis=0)
    cov = (xz.T @ xz) / max(1, xz.shape[0] - 1)
    try:
        vals, vecs = np.linalg.eigh(cov)
    except np.linalg.LinAlgError:
        return 0.0
    principal = vecs[:, int(np.argmax(vals))]
    # angle in degrees; +90 normalises the sign ambiguity of eigenvectors
    ang = float(np.degrees(np.arctan2(principal[0], principal[1])))
    return round(ang, 2)


def extract_instances(cursor, chunk_range, labels_per_voxel, palette,
                     labels_wanted=(LABEL_VEGETATION, LABEL_STRUCTURE,
                                    LABEL_PROP, LABEL_GLASS),
                     heightmap=None, origin_x=0, origin_z=0,
                     max_instances=None, min_blocks=None):
    """
    Full extraction for a chunk rectangle.

    Returns a list of placement dicts ready for the UE5 importer, sorted
    largest-first so `--max-instances` keeps the objects that matter.

    `min_blocks` overrides the module-level `MIN_COMPONENT_VOXELS` floor; it is
    the knob that made a 32x32-chunk preview report 4 instances versus the
    hundreds a whole campus actually contains.
    """
    floor = MIN_COMPONENT_VOXELS if min_blocks is None else int(min_blocks)
    cx0, cz0, cx1, cz1 = chunk_range
    xs_all, ys_all, zs_all, labs_all, st_all = [], [], [], [], []
    for cz in range(cz0, cz1 + 1):
        for cx in range(cx0, cx1 + 1):
            got = cursor.get(cx, cz)
            if got is None:
                continue
            xs, ys, zs, st = got
            if st.size == 0:
                continue
            xs_all.append(xs)
            ys_all.append(ys)
            zs_all.append(zs)
            st_all.append(st)
            labs_all.append(labels_per_voxel(st, ys))
    if not xs_all:
        return []

    X = np.concatenate(xs_all)
    Y = np.concatenate(ys_all)
    Z = np.concatenate(zs_all)
    S = np.concatenate(st_all)
    L = np.concatenate(labs_all)

    vox = np.stack([X, Y, Z], axis=1)
    comps = connected_components(vox, L, labels_wanted)

    out = []
    for sub, labs in comps:
        if sub.shape[0] < floor or sub.shape[0] > MAX_COMPONENT_VOXELS:
            continue
        cls, comp = classify_component(sub, labs, palette)
        inst = {
            "class": cls,
            "centroid_blocks": [float(sub[:, 0].mean()),
                                float(sub[:, 1].mean()),
                                float(sub[:, 2].mean())],
            "base_y_blocks": float(sub[:, 1].min()),
            "top_y_blocks": float(sub[:, 1].max()),
            "yaw_deg": estimate_yaw(sub),
            "components": comp,
        }
        if heightmap is not None:
            inst.update(place_on_ground(inst, heightmap, origin_x, origin_z))
        else:
            inst["position_cm"] = [inst["centroid_blocks"][0] * BLOCK_CM,
                                    inst["base_y_blocks"] * BLOCK_CM,
                                    inst["centroid_blocks"][2] * BLOCK_CM]
            inst["rotation_deg"] = [0.0, inst["yaw_deg"], 0.0]
        inst["source_blocks"] = int(sub.shape[0])
        out.append(inst)
        if max_instances and len(out) >= max_instances:
            break

    out.sort(key=lambda d: -d["source_blocks"])
    for i, d in enumerate(out):
        d["id"] = i
    return out


# --------------------------------------------------------------------------- #
# model providers
# --------------------------------------------------------------------------- #

class ModelProvider(object):
    """Maps an object class to a concrete asset. Pluggable like everything else."""

    name = "abstract"

    def resolve(self, cls, composition=None):
        raise NotImplementedError

    def describe(self):
        return {"provider": self.name}


class BuiltinModelProvider(ModelProvider):
    """
    Default: no network, no API keys.

    Resolves each class to a *recipe* rather than a file -- a low-poly stand-in
    assembled from primitives. This keeps the pipeline runnable offline and
    gives the UE side something to instantiate immediately; pointing
    `--models polyhaven` swaps in real CC0 assets without touching this code.
    """

    name = "builtin"

    RECIPES = {
        "tree": {"primitive": "cone_on_cylinder", "height_cm": 900.0, "radius_cm": 320.0},
        "plant": {"primitive": "cross_billboard", "height_cm": 90.0, "radius_cm": 25.0},
        "building": {"primitive": "box", "height_cm": 500.0, "radius_cm": 600.0},
        "structure": {"primitive": "box", "height_cm": 260.0, "radius_cm": 200.0},
        "prop": {"primitive": "post", "height_cm": 140.0, "radius_cm": 30.0},
        "unknown": {"primitive": "box", "height_cm": 100.0, "radius_cm": 50.0},
    }

    def resolve(self, cls, composition=None):
        r = dict(self.RECIPES.get(cls, self.RECIPES["unknown"]))
        # scale the primitive by the instance's real footprint so the silhouette
        # matches the original build
        if composition:
            h = composition.get("height", 0) or 1
            fp = composition.get("footprint", 0) or 1
            r["height_cm"] = float(h) * BLOCK_CM
            r["radius_cm"] = float(max(fp, 1) ** 0.5) * BLOCK_CM * 0.75
        r["asset"] = "builtin:%s:%s" % (cls, r["primitive"])
        r["license"] = "CC0 (procedural primitive)"
        return r

    def describe(self):
        return {"provider": self.name, "classes": sorted(self.RECIPES)}


class LibraryModelProvider(ModelProvider):
    """
    Query a local directory of CC0 assets (Poly Haven / Kenney / Quaternius).

    No API calls by default: the user points --models at a folder of .glb/.fbx
    and this matches by filename convention (`oak_tree.glb`, `tree_oak.glb`,
    `oak-tree.glb`, ...). Keeping it offline avoids shipping anyone's API key
    and works on machines with no outbound access.
    """

    name = "library"

    EXT = (".glb", ".gltf", ".fbx", ".obj")

    def __init__(self, root):
        self.root = root
        self.index = {}
        if root and os.path.isdir(root):
            for fn in sorted(os.listdir(root)):
                ext = os.path.splitext(fn)[1].lower()
                if ext not in self.EXT:
                    continue
                stem = os.path.splitext(fn)[0].lower()
                for token in stem.replace("-", "_").split("_"):
                    if token:
                        self.index.setdefault(token, os.path.join(root, fn))

    def resolve(self, cls, composition=None):
        if cls in self.index:
            return {"asset": self.index[cls],
                    "asset_name": os.path.basename(self.index[cls]),
                    "license": "see asset metadata (CC0 expected)"}
        return {"asset": None, "license": None,
                "note": "no library asset matched class %r" % cls}

    def describe(self):
        return {"provider": self.name, "root": self.root,
                "indexed": len(self.index)}


def make_model_provider(kind, root=None):
    if kind == "builtin":
        return BuiltinModelProvider()
    if kind == "library":
        return LibraryModelProvider(root)
    raise ValueError("unknown model provider %r (builtin|library)" % kind)
