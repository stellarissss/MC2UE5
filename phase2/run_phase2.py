"""
Phase 2 end-to-end pipeline: MC2WV2 -> terrain / water / props.

Reads layer 1's output read-only and writes everything under out/phase2/.

    # one dimension, default settings
    python3 run_phase2.py --dim overworld

    # quick look at a small area (fast iteration)
    python3 run_phase2.py --dim overworld --chunk-range -10,-10,10,10 --dry-run

    # with a CC0 model library
    python3 run_phase2.py --dim overworld --models library --model-root ~/models

Design constraints
------------------
* **Never mutates layer 1.** voxel_data/ and parse/ are opened 'rb' only.
* **Bounded memory.** Work happens per chunk-rectangle tile; nothing ever
  allocates a dense volume for the whole world.
* **Every stage independently runnable and re-runnable.** `--only` /
  `--skip` let you redo terrain without redoing semantics.
* **Fails loud.** If a stage produces degenerate output it says so and (with
  --strict) exits non-zero, rather than writing a broken asset quietly.
"""

import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import landscape as lsc            # noqa: E402
import props as props_mod          # noqa: E402
import semantic as sem             # noqa: E402
import terrain as terr             # noqa: E402
import voxelio as vio              # noqa: E402
import water as water_mod          # noqa: E402

VOXEL_DIR = os.path.join(ROOT, "voxel_data", "full")
OUT_ROOT = os.path.join(ROOT, "out", "phase2")
SURVEY_DIR = os.path.join(ROOT, "out", "survey")


# --------------------------------------------------------------------------- #

def log(msg):
    print(msg, flush=True)


def chunk_bounds_of(dim):
    """Absolute chunk range that covers all non-air content of a dimension."""
    b = vio.world_bounds(os.path.join(VOXEL_DIR, dim + ".bin"))
    if b is None:
        return None
    xmin, xmax, zmin, zmax, _ymin, ymax = b
    return (int(np.floor(xmin / 16)), int(np.floor(zmin / 16)),
            int(np.floor(xmax / 16)), int(np.floor(zmax / 16)), int(ymax))


def chunk_bounds_of_region(dim, region, pad_chunks=8):
    """
    Chunk range for a named region recorded by survey.py.

    Regions are stored in world blocks; we snap outward to chunk borders and
    add a margin so buildings that straddle the measured edge are not clipped.
    """
    path = os.path.join(SURVEY_DIR, dim, "survey.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "%s missing -- run survey.py --dim %s first" % (path, dim))
    with open(path) as fh:
        info = json.load(fh)
    reg = (info.get("regions") or {}).get(region)
    if reg is None and info.get("region"):
        # single-region survey: accept it under the canonical name too
        r = info["region"]
        reg = {"x": r["world_x"], "z": r["world_z"]}
    if reg is None:
        raise KeyError("region %r not in %s (have: %s)"
                       % (region, path,
                          sorted((info.get("regions") or {}).keys())))
    x0, x1 = reg["x"]
    z0, z1 = reg["z"]
    return (int(np.floor(x0 / 16)) - pad_chunks,
            int(np.floor(z0 / 16)) - pad_chunks,
            int(np.ceil(x1 / 16)) + pad_chunks,
            int(np.ceil(z1 / 16)) + pad_chunks,
            None)


def make_label_fn(vf, provider):
    """Bind a provider to one dimension's palette -> callable(state, y)->labels."""
    def fn(state, y):
        return provider.label_chunk(vf.palette, state, y)
    return fn


# --------------------------------------------------------------------------- #

def stage_terrain(dim, chunk_range, args, out_dir):
    """
    heightmap -> optional de-staircasing -> Landscape heightmaps (+ optional mesh).

    Landscape PNGs are the primary product: they are what UE5 actually wants for
    a 704x1088 block world, they carry built-in LOD and GPU interpolation, and
    they cost ~1 MB instead of ~600 MB of triangles. The OBJ mesh path is kept
    for hero shots and stays off unless --mesh is passed.
    """
    t0 = time.time()
    cx0, cz0, cx1, cz1 = chunk_range
    y_max = args.y_max
    bx0, bz0 = cx0 * 16, cz0 * 16
    bw = (cx1 - cx0 + 1) * 16
    bh = (cz1 - cz0 + 1) * 16

    vpath = os.path.join(VOXEL_DIR, dim + ".bin")
    with vio.ChunkCursor(vpath, cache_chunks=args.chunk_cache) as cur:
        vf = cur.vf
        provider = args.provider or sem.make_provider(args.semantic, vpath)
        labelfn = make_label_fn(vf, provider)
        log("  extracting heightmap over %d x %d blocks ..." % (bw, bh))
        h_raw, valid, top_lab = terr.extract_heightmap(
            cur, chunk_range, y_max, labels_per_voxel=labelfn)
    log("  heightmap %s  valid=%.1f%%  y=[%.1f, %.1f]"
        % (h_raw.shape, 100.0 * valid.mean(), float(h_raw[valid].min()),
           float(h_raw[valid].max())))

    # De-staircase in block space. The Landscape grid is ~1 vertex per block,
    # so the 4x geometric upsample the paper uses for mesh export has nowhere
    # to go; doing the smoothing here keeps the same visual result without
    # inflating the heightmap 16x.
    if args.sigma > 0:
        log("  destaircasing (anisotropic, sigma=%.2f edge=%.2f) ..."
            % (args.sigma, args.edge_preserve))
        h_sm = terr.smooth_heights(h_raw, valid, sigma=args.sigma,
                                   edge_preserve=args.edge_preserve)
    else:
        h_sm = h_raw

    # Snap the range outward to whole blocks: the encoded PNG can only address
    # block-resolution steps anyway, and a fractional range would make the same
    # terrain encode differently on a re-run.
    gmin = float(np.floor(h_sm[valid].min())) if valid.any() else 0.0
    gmax = float(np.ceil(h_sm[valid].max())) if valid.any() else 1.0
    gspan = max(1.0, gmax - gmin)
    log("  height range for encoding: [%.1f .. %.1f] blocks (span %.1f)"
        % (gmin, gmax, gspan))

    # No timing field: the artefacts are committed to git, and a per-run
    # duration would make every rerun produce a meaningless binary-ish diff
    # while carrying zero information about the data. Elapsed time goes to the
    # log only.
    result = {"block_origin": [bx0, bz0], "block_size": [bw, bh],
              "y_range_blocks": [gmin, gmax], "y_span_blocks": gspan}

    # ---- primary: Landscape heightmaps ---------------------------------- #
    if not args.skip_landscape:
        lsc_dir = os.path.join(out_dir, "landscape")
        log("  exporting Landscape heightmaps ...")
        lmeta = lsc.export_landscapes(
            h_sm, valid, (bx0, bz0), gmin, gspan, lsc_dir, dim,
            section_size=args.section_size,
            sections_per_component=args.sections_per_component,
            max_tiles_per_axis=args.max_tiles,
            xy_scale_cm=args.xy_scale,
            max_tile_blocks=args.landscape_tile_blocks,
            tile_grid_search=args.tile_grid_search)
        result["landscape"] = {
            "dir": os.path.relpath(lsc_dir, out_dir),
            "resolution": lmeta["landscape_resolution"],
            "n_components": lmeta["n_components"],
            "section_size": lmeta["section_size"],
            "quads_per_component": lmeta["quads_per_component"],
            "xy_scale_cm": lmeta["xy_scale_cm"],
            "tile_count": lmeta["tile_count"],
            "tile_grid": lmeta["tile_grid"],
            "max_tile_blocks": lmeta.get("max_tile_blocks", 0),
        }
        log("    %dx%d verts/tile, %dx%d components total @%d quads, "
            "xy_scale=%.1f cm"
            % (lmeta["landscape_resolution"][0], lmeta["landscape_resolution"][1],
               lmeta["n_components"][0], lmeta["n_components"][1],
               lmeta["quads_per_component"], lmeta["xy_scale_cm"]))
        log("    %d tile(s) in %dx%d grid -> %s"
            % (lmeta["tile_count"], lmeta["tile_grid"][0], lmeta["tile_grid"][1],
               result["landscape"]["dir"]))

    # ---- optional: dense mesh for hero shots ---------------------------- #
    if args.mesh:
        result.update(_mesh_pass(dim, chunk_range, args, out_dir,
                                 h_raw, valid, gmin, gmax))

    # Keep the block-resolution heightmap for props (ground alignment) and
    # debug (semantic overlay). This is 1 sample per block, so the campus is
    # ~3 MB rather than the 48 MB an up×4 field would cost.
    np.save(os.path.join(out_dir, "heightmap_smooth.npy"), h_sm)
    np.save(os.path.join(out_dir, "heightmap_valid.npy"), valid)
    np.save(os.path.join(out_dir, "heightmap_labels.npy"), top_lab)

    elapsed = time.time() - t0
    terr.save_metadata(os.path.join(out_dir, "terrain.json"), result)
    return dict(result, seconds=elapsed)      # timing for logs, not the file


def _mesh_pass(dim, chunk_range, args, out_dir, h_raw, valid, gmin, gmax):
    """Optional OBJ mesh export. Only for small areas -- see module docstring."""
    cx0, cz0, cx1, cz1 = chunk_range
    bx0, bz0 = cx0 * 16, cz0 * 16
    up = args.mesh_upsample
    terr_dir = os.path.join(out_dir, "terrain")
    os.makedirs(terr_dir, exist_ok=True)

    h_up, v_up = terr.upsample(h_raw, valid, factor=up, order=1)
    if args.sigma > 0:
        h_up = terr.smooth_heights(h_up, v_up, sigma=args.sigma * up,
                                   edge_preserve=args.edge_preserve)

    tiles = []
    H, W = h_up.shape
    step = terr.TILE_BLOCKS * up
    gmin_m, gmax_m = gmin * terr.BLOCK_CM, gmax * terr.BLOCK_CM
    for (ts, te, cs, ce) in terr.tile_ranges(W, tile=step, overlap=args.overlap):
        for (zs, ze, zcs, zce) in terr.tile_ranges(H, tile=step, overlap=args.overlap):
            sub = h_up[zs:ze, ts:te]
            sv = v_up[zs:ze, ts:te]
            if not sv.any():
                continue
            ox = (bx0 * up) + ts
            oz = (bz0 * up) + zs
            mesh = terr.repair_mesh(terr.heightmap_to_mesh(
                sub, ox / up, oz / up, name="terrain_%d_%d" % (ts, zs)))
            st = terr.mesh_stats(mesh)
            fname = "terrain_%05d_%05d.obj" % (ts, zs)
            terr.save_obj(os.path.join(terr_dir, fname), mesh)
            tiles.append({"file": fname, "sample_origin": [int(ts), int(zs)],
                          "core": [int(cs), int(ce), int(zcs), int(zce)],
                          "block_origin": [int(bx0 + ts // up),
                                           int(bz0 + zs // up)],
                          "samples": [int(sub.shape[1]), int(sub.shape[0])],
                          "stats": st})
            log("    %s  v=%-8d tri=%-8d degen=%-4d down=%-4d"
                % (fname, st["vertices"], st["triangles"],
                   st["degenerate"], st["down_facing"]))
    return {"mesh": {"dir": os.path.relpath(terr_dir, out_dir),
                     "upsample": up, "tile_count": len(tiles),
                     "y_range_cm": [gmin_m, gmax_m], "tiles": tiles}}


def stage_water(dim, chunk_range, args, out_dir):
    """
    Water surfaces as a separate planar export (paper: "water is exported as
    its own plane").

    A quad is emitted only where all four corners carry a real liquid column, so
    the mesh has an actual shoreline instead of a rectangle bleeding over land.
    """
    vpath = os.path.join(VOXEL_DIR, dim + ".bin")
    cx0, cz0, cx1, cz1 = chunk_range
    bx0, bz0 = cx0 * 16, cz0 * 16
    bw = (cx1 - cx0 + 1) * 16
    bh = (cz1 - cz0 + 1) * 16

    with vio.ChunkCursor(vpath, cache_chunks=args.chunk_cache) as cur:
        vf = cur.vf
        provider = args.provider or sem.make_provider(args.semantic, vpath)
        labelfn = make_label_fn(vf, provider)
        surf, valid, is_lava = water_mod.extract_water(
            cur, chunk_range, args.y_max, labelfn)
    info = water_mod.sea_level_info(surf, valid)
    ncols = int(valid.sum())
    info.update({"block_origin": [bx0, bz0], "block_size": [bw, bh],
                 "columns": ncols, "lava_columns": int(is_lava.sum())})
    log("  water columns=%d  sea_level=%s blocks  lava=%d"
        % (ncols, info.get("sea_level_blocks"), int(is_lava.sum())))

    w_dir = os.path.join(out_dir, "water")
    os.makedirs(w_dir, exist_ok=True)
    written = []
    # Isolated single water columns cannot form a quad (a plane needs 2x2), so
    # count those separately instead of emitting a vertex-only, face-less OBJ.
    mesh = None
    if ncols > 0:
        mesh = terr.repair_mesh(water_mod.water_mesh(
            surf, valid, bx0, bz0, name="water_%s" % dim))
    if mesh is not None and mesh["faces"].shape[0] > 0:
        st = terr.mesh_stats(mesh)
        fname = "water_%s.obj" % dim
        terr.save_obj(os.path.join(w_dir, fname), mesh)
        terr.save_metadata(os.path.join(w_dir, "water.json"), {
            "dimension": dim, "file": fname, "stats": st, **info})
        written.append({"file": fname, "stats": st})
        log("    %s  v=%d tri=%d quads=%d"
            % (fname, st["vertices"], st["triangles"],
               mesh.get("quad_count", 0)))
    else:
        if ncols > 0:
            info["note"] = ("%d liquid columns but no 2x2 neighbourhood, so no "
                            "planar surface; these are isolated sources "
                            "(fountains, wells), not a water body" % ncols)
            log("    %d liquid columns, none form a surface quad -- skipping OBJ"
                % ncols)
        terr.save_metadata(os.path.join(w_dir, "water.json"),
                           {"dimension": dim, **info})
    return {"files": written, "columns": ncols,
            "sea_level_blocks": info.get("sea_level_blocks"),
            "has_water": ncols > 0}


def stage_props(dim, chunk_range, args, out_dir):
    """
    Object instances: connected-component extraction, ground alignment, and a
    model assignment per class.

    Everything here is in *block* space. The pipeline used to work in upsampled
    index space because the mesh export needed 4x density; with Landscape as the
    primary product there is no upsample, so removing that indirection also
    removed the /upsample fixups that used to be scattered through this stage.
    """
    vpath = os.path.join(VOXEL_DIR, dim + ".bin")
    cx0, cz0, cx1, cz1 = chunk_range
    bx0, bz0 = cx0 * 16, cz0 * 16

    # Ground alignment uses the smoothed block-resolution heightmap written by
    # the terrain stage. Falling back to None makes props skip the alignment
    # rather than crash, which is what you want when only running --only props
    # on a fresh output dir.
    hm_path = os.path.join(out_dir, "heightmap_smooth.npy")
    heightmap = None
    if os.path.isfile(hm_path):
        heightmap = np.load(hm_path)
    else:
        log("  NOTE: no heightmap_smooth.npy -- skipping ground alignment "
            "(run --only terrain first for aligned props)")

    with vio.ChunkCursor(vpath, cache_chunks=args.chunk_cache) as cur:
        vf = cur.vf
        provider = args.provider or sem.make_provider(args.semantic, vpath)
        labelfn = make_label_fn(vf, provider)
        t0 = time.time()
        pstats = {}
        insts = props_mod.extract_instances(
            cur, chunk_range, labelfn, vf.palette,
            heightmap=heightmap,
            origin_x=bx0, origin_z=bz0,
            max_instances=args.max_instances,
            min_blocks=args.min_prop_blocks,
            max_blocks=args.max_prop_blocks or None,
            stats=pstats)
        log("  %d instances in %.1fs" % (len(insts), time.time() - t0))
        log("  components seen=%d  dropped small=%d  dropped large=%d "
            "(largest drop %d voxels)"
            % (pstats["components_seen"], pstats["dropped_too_small"],
               pstats["dropped_too_large"], pstats["dropped_too_large_voxels"]))
        if pstats["dropped_too_large"] > 0:
            log("  NOTE: %d component(s) exceeded --max-prop-blocks=%d. If a "
                "building is missing, raise that limit."
                % (pstats["dropped_too_large"],
                   args.max_prop_blocks or props_mod.MAX_COMPONENT_VOXELS))

    model = props_mod.make_model_provider(args.models, args.model_root)
    by_class = {}
    for i in insts:
        i["model"] = model.resolve(i["class"], i["components"])
        by_class.setdefault(i["class"], 0)
        by_class[i["class"]] += 1

    p_dir = os.path.join(out_dir, "props")
    os.makedirs(p_dir, exist_ok=True)
    terr.save_metadata(os.path.join(p_dir, "prop_placements.json"), {
        "dimension": dim,
        "block_origin": [bx0, bz0],
        "chunk_range": list(chunk_range),
        "count": len(insts),
        "by_class": by_class,
        "model_provider": model.describe(),
        "ground_aligned": heightmap is not None,
        "instances": insts,
    })
    log("  by class: %s" % by_class)
    return {"count": len(insts), "by_class": by_class,
            "ground_aligned": heightmap is not None,
            "components_seen": int(pstats["components_seen"]),
            "dropped_too_small": int(pstats["dropped_too_small"]),
            "dropped_too_large": int(pstats["dropped_too_large"])}


def stage_debug(dim, chunk_range, args, out_dir):
    """
    Two previews for eyeballing correctness:
      * a hypsometric height map with a percentile-stretched colour ramp, so
        flat-but-not-identical terrain is still readable (a linear ramp over the
        full range renders a map that is 98% one height as a single flat
        colour, which hides exactly the detail you are trying to check);
      * a semantic top-down map of the surface labels.
    """
    from PIL import Image
    vpath = os.path.join(VOXEL_DIR, dim + ".bin")
    cx0, cz0, cx1, cz1 = chunk_range
    bx0, bz0 = cx0 * 16, cz0 * 16

    hm_path = os.path.join(out_dir, "heightmap_smooth.npy")
    if not os.path.isfile(hm_path):
        raise FileNotFoundError(
            "%s missing -- run the terrain stage first (--only terrain)"
            % hm_path)
    hm = np.load(hm_path)
    hv_path = os.path.join(out_dir, "heightmap_valid.npy")
    hv = np.load(hv_path) if os.path.isfile(hv_path) else np.ones_like(hm, bool)

    vals = hm[hv]
    if vals.size == 0:
        raise ValueError("heightmap is entirely invalid")
    lo, hi = float(vals.min()), float(vals.max())
    # percentile stretch: keeps the dominant plateau distinguishable while
    # still covering the full range for the rare tall structures
    p_lo, p_hi = np.percentile(vals, [1.0, 99.5])
    if p_hi - p_lo < 1e-6:
        p_lo, p_hi = lo, hi + 1.0
    norm = np.clip((hm - p_lo) / (p_hi - p_lo), 0.0, 1.0)

    rgb = _hypsometric(norm)
    rgb[~hv] = (25, 25, 32)

    d_dir = os.path.join(out_dir, "debug")
    os.makedirs(d_dir, exist_ok=True)
    p1 = os.path.join(d_dir, "%s_height_color.png" % dim)
    Image.fromarray(np.flipud(rgb), mode="RGB").save(p1)

    # raw height, stretched, as greyscale -- useful for measuring relief
    g = (np.clip((hm - lo) / max(1e-6, hi - lo), 0, 1) * 255).astype(np.uint8)
    g[~hv] = 0
    p2 = os.path.join(d_dir, "%s_height_gray.png" % dim)
    Image.fromarray(np.flipud(g), mode="L").save(p2)

    # semantic surface map
    p3 = _surface_label_png(dim, chunk_range, args, out_dir, bx0, bz0)

    log("  wrote %s  %s" % (os.path.basename(p1), rgb.shape))
    log("  wrote %s" % os.path.basename(p2))
    if p3:
        log("  wrote %s" % os.path.basename(p3))
    return {"height_color": os.path.basename(p1),
            "height_gray": os.path.basename(p2),
            "surface_labels": os.path.basename(p3) if p3 else None,
            "shape": list(rgb.shape),
            "height_range_blocks": [lo, hi],
            "percentile_range": [float(p_lo), float(p_hi)]}


def _hypsometric(t):
    """Blue -> green -> tan -> white ramp for a normalised height field."""
    t = np.clip(t, 0.0, 1.0)
    stops = np.array([
        [0.00, 0.16, 0.38],   # deep water / low
        [0.28, 0.48, 0.35],   # plain
        [0.55, 0.62, 0.36],   # field
        [0.74, 0.64, 0.44],   # dry
        [0.88, 0.85, 0.78],   # high rock
        [1.00, 1.00, 1.00],   # peak
    ], dtype=np.float32)
    pos = np.linspace(0.0, 1.0, stops.shape[0])
    out = np.zeros(t.shape + (3,), dtype=np.float32)
    for c in range(3):
        out[..., c] = np.interp(t, pos, stops[:, c])
    return (np.clip(out, 0, 1) * 255).astype(np.uint8)


def _surface_label_png(dim, chunk_range, args, out_dir, bx0, bz0):
    """
    Top-down view of the semantic label that produced each heightmap cell.
    Reads the saved top-label array if present, else returns None.
    """
    import semantic as sem
    lbl_path = os.path.join(out_dir, "heightmap_labels.npy")
    if not os.path.isfile(lbl_path):
        return None
    lab = np.load(lbl_path)
    rgb = np.zeros(lab.shape + (3,), dtype=np.uint8)
    for lid, col in sem.LABEL_COLORS.items():
        rgb[lab == lid] = col
    from PIL import Image
    p = os.path.join(out_dir, "debug", "%s_surface_labels.png" % dim)
    Image.fromarray(np.flipud(rgb), mode="RGB").save(p)
    return os.path.basename(p)


# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="MC2UE5 phase 2: semantic rebuild")
    ap.add_argument("--dim", default="overworld",
                    choices=("overworld", "nether", "end"))
    ap.add_argument("--region", default=None,
                    help="named region from survey.py (e.g. campus); "
                         "'full' means the whole dimension")
    ap.add_argument("--chunk-range", default=None,
                    help="cx0,cz0,cx1,cz1 (overrides --region)")
    ap.add_argument("--out", default=None, help="output dir (default out/phase2/<dim>)")
    ap.add_argument("--semantic", default="cpu", choices=("cpu", "unet"))
    ap.add_argument("--weights", default=None, help="U-Net weights (with --semantic unet)")
    ap.add_argument("--device", default="cpu", help="torch device for the U-Net")
    ap.add_argument("--models", default="builtin", choices=("builtin", "library"))
    ap.add_argument("--model-root", default=None, help="CC0 asset directory")

    g = ap.add_argument_group("landscape")
    g.add_argument("--section-size", type=int, default=None,
                   help="quads per section (7/15/31/63/127); "
                        "default auto-solves for the tightest fit")
    g.add_argument("--sections-per-component", type=int, default=1,
                   help="1 or 2 (UE5 convention)")
    g.add_argument("--xy-scale", dest="xy_scale", type=float, default=None,
                   help="cm per Landscape vertex (default 100 = 1 block)")
    g.add_argument("--max-tiles", type=int, default=16,
                   help="hard cap on Landscape actors per axis")
    g.add_argument("--landscape-tile-blocks", type=int, default=768,
                   help="preferred maximum size of a Landscape tile in blocks, "
                        "setting the MINIMUM number of tiles per axis. The "
                        "solver may merge tiles further when a finer grid would "
                        "multiply the Landscape component count (the unit of "
                        "culling/LOD). For the 720x1040 campus the measured "
                        "optimum is 2x2 tiles at 768: 4 actors, 816 components, "
                        "12 m of border -- the same render state as one "
                        "monolithic Landscape, in 4 streamable pieces. 0 = one "
                        "monolithic Landscape (never cullable).")
    g.add_argument("--tile-grid-search", type=int, default=8,
                   help="largest per-axis tile grid the solver may consider "
                        "when trading tile count against component count")
    g.add_argument("--skip-landscape", action="store_true",
                   help="heightmaps only, no Landscape export")

    m = ap.add_argument_group("mesh (optional)")
    m.add_argument("--mesh", action="store_true",
                   help="also export dense OBJ meshes (hero shots; large)")
    m.add_argument("--mesh-upsample", type=int, default=4,
                   help="geometric upsample for the mesh path")

    g2 = ap.add_argument_group("processing")
    g2.add_argument("--sigma", type=float, default=1.4,
                    help="de-staircase strength; 0 disables smoothing")
    g2.add_argument("--edge-preserve", dest="edge_preserve", type=float,
                    default=0.6)
    g2.add_argument("--overlap", type=int, default=terr.TILE_OVERLAP)
    g2.add_argument("--max-instances", type=int, default=4000)
    g2.add_argument("--min-prop-blocks", type=int, default=1,
                    help="drop connected components smaller than this")
    g2.add_argument("--max-prop-blocks", type=int, default=0,
                    help="drop connected components larger than this "
                         "(0 = module default, 20000). Raise it if a large "
                         "building goes missing from the props list.")
    g2.add_argument("--chunk-cache", type=int, default=512)
    g2.add_argument("--only", default=None,
                    help="comma list of stages: terrain,water,props,debug")
    g2.add_argument("--skip", default=None, help="comma list to skip")
    g2.add_argument("--dry-run", action="store_true",
                    help="compute everything and write intermediate arrays "
                         "(props needs them), but skip the final report file")
    g2.add_argument("--strict", action="store_true",
                    help="exit non-zero if any stage produced degenerate output")
    args = ap.parse_args(argv)

    vpath = os.path.join(VOXEL_DIR, args.dim + ".bin")
    if not os.path.isfile(vpath):
        log("MISSING %s -- run parse_world.py --save <save> --full first" % vpath)
        return 2

    bounds = chunk_bounds_of(args.dim)
    if bounds is None:
        log("%s has no non-air voxels" % args.dim)
        return 1
    auto = (bounds[0], bounds[1], bounds[2], bounds[3])
    if args.chunk_range:
        vals = [int(v) for v in args.chunk_range.split(",")]
        if len(vals) != 4:
            log("--chunk-range needs cx0,cz0,cx1,cz1")
            return 2
        chunk_range = tuple(vals)
    elif args.region and args.region != "full":
        chunk_range = chunk_bounds_of_region(args.dim, args.region)
        if chunk_range[4] is None:
            chunk_range = chunk_range[:4]
        log("region %r -> chunks %s" % (args.region, chunk_range))
    else:
        chunk_range = auto
    args.y_max = bounds[4]

    out_dir = args.out or os.path.join(OUT_ROOT, args.dim)
    os.makedirs(out_dir, exist_ok=True)

    log("=" * 68)
    log("MC2UE5 phase 2 | dimension=%s  chunks=%s  y_max=%d"
        % (args.dim, chunk_range, args.y_max))
    log("  blocks: x[%d..%d] z[%d..%d]  ->  %s"
        % (chunk_range[0] * 16, chunk_range[2] * 16 + 15,
           chunk_range[1] * 16, chunk_range[3] * 16 + 15, out_dir))
    if args.semantic == "unet":
        log("  semantic provider: unet (weights=%s device=%s)"
            % (args.weights, args.device))
    log("=" * 68)

    provider = sem.make_provider(args.semantic, vpath,
                                 weights=args.weights, device=args.device)
    args.provider = provider

    stages = ["terrain", "water", "props", "debug"]
    if args.only:
        want = [s.strip() for s in args.only.split(",") if s.strip()]
        stages = [s for s in stages if s in want]
    if args.skip:
        drop = set(s.strip() for s in args.skip.split(","))
        stages = [s for s in stages if s not in drop]

    fns = {"terrain": stage_terrain, "water": stage_water,
           "props": stage_props, "debug": stage_debug}

    t0 = time.time()
    report = {"dimension": args.dim, "chunk_range": list(chunk_range),
              "region": args.region, "block_cm": terr.BLOCK_CM,
              "semantic_provider": provider.describe(),
              "stages": {}}
    problems = []
    for name in stages:
        log("\n[%s] %s" % (name.upper(), name))
        try:
            res = fns[name](args.dim, chunk_range, args, out_dir)
            # Stage helpers may report how long they took; that is log-only.
            # Strip it before the dict lands in the committed report so reruns
            # on unchanged input stay byte-identical.
            report["stages"][name] = {k: v for k, v in res.items()
                                      if k != "seconds"}
            res.pop("seconds", None)
            if name == "terrain":
                lmeta = res.get("landscape")
                if not lmeta:
                    problems.append("terrain produced no Landscape heightmaps")
                elif lmeta["tile_count"] == 0:
                    problems.append("landscape produced no tiles")
            if name == "water":
                for f in res.get("files", []):
                    s = f["stats"]
                    if s.get("nan"):
                        problems.append("water mesh has NaN vertices")
                    if s.get("triangles", 0) == 0:
                        problems.append("water mesh has no triangles")
            if name == "props":
                if res.get("count") == 0:
                    log("  NOTE: no object instances found")
        except Exception as exc:
            import traceback
            traceback.print_exc()
            problems.append("%s failed: %r" % (name, exc))
            report["stages"][name] = {"error": repr(exc)}

    # The persisted report stays free of wall-clock timings so that reruns on
    # unchanged input produce byte-identical JSON. See stage_terrain().
    report["problems"] = problems
    if not args.dry_run:
        with open(os.path.join(out_dir, "phase2_report.json"), "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    log("\n" + "=" * 68)
    log("done in %.1fs -> %s" % (time.time() - t0, out_dir))
    for k, v in report["stages"].items():
        log("  %-8s %s" % (k, json.dumps(v, ensure_ascii=False)[:150]))
    if problems:
        log("PROBLEMS (%d):" % len(problems))
        for p in problems:
            log("  ! " + p)
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
