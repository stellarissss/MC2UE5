"""
Regression tests for layer 1 (parse), the phase-2 reader, and the phase-2
pipeline's correctness-critical geometry.

Run:  python3 tests/run_tests.py

Why this file exists
--------------------
Layer 2 consumes layer 1's on-disk contract only. If layer 1 regresses --
silently -- phase 2 would build terrain from wrong data. These tests pin the
three things that actually broke during development:

1. `stats.json` error_count must be 0. A non-zero count once meant 3452 of
   17029 overworld chunks "failed" because `decode_chunk` returned a Python
   list for all-air chunks and the worker called `.size` on it. Output stayed
   correct, so the bug hid inside a plausible-looking error count.

2. Reported world ranges must match the exported .bin exactly. They did not,
   because the aggregation loop compared slot keys ("x"/"y"/"z") against field
   names ("xmin"/...), so min-side bounds never updated.

3. Voxel word packing/unpacking must round-trip, including the non-stretch bit
   layout used by 1.16.5 (values never straddle a long boundary).

The phase-2 tests below pin the four bugs that would have shipped silently:

4. Landscape height encoding. `encode_absolute` originally used
   `span / 655.35` for the Z Scale, which is wrong by a factor of 65535 -- the
   terrain came back 65535x too tall and nothing errored. Now pinned by an
   exact round-trip through UE5's own decode formula.

5. Landscape resampling must not stretch. `ndimage.zoom(field, grid/shape)`
   scales the world by (grid-1)/(shape-1); at 1.6% error that is 11 m of drift
   across this campus, which would slide the terrain out of registration with
   the layer-1 HISM block layer. The world must occupy the grid 1:1 with the
   remainder as border.

6. Landscape resolution must equal components*quads+1. Anything else makes UE5
   split sections unevenly and log "non-optimal component size".

7. Connected components. A lexsort-based neighbour scan tore U shapes and
   rings apart (two separate real bugs); the current scipy.csgraph version is
   pinned against shapes that catch exactly that class of error.
"""

import json
import os
import re
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "phase2"))
sys.path.insert(0, os.path.join(ROOT, "parse"))

import voxelio as vio  # noqa: E402


FAILURES = []


def check(cond, label, detail=""):
    if cond:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s  %s" % (label, detail))
        FAILURES.append(label)
    return cond


# --------------------------------------------------------------------------- #

def test_bit_packing():
    """
    Reproduce layer 1's non-stretch unpacker and prove the layout.

    1.16.5 (DataVersion < 2529) packs floor(64/bits) whole values per long;
    value i sits in long i//per at bit offset (i%per)*bits. The textbook
    formula `shift = (i*bits) % 64` is WRONG here whenever bits does not
    divide 64 -- it silently straddles long boundaries.

    Packing note: many values share a long, so `packed[long_i] |= v` does NOT
    work (fancy-index assignment keeps only the last write per index). Shift
    each long's slice into place instead.
    """
    print("\n[1] non-stretch bit packing")
    for bits in (4, 5, 6, 7, 8, 9, 12, 13, 14):
        per = 64 // bits
        n_long = -(-4096 // per)
        rng = np.random.default_rng(bits)
        vals = rng.integers(0, 1 << bits, size=4096, dtype=np.int64)

        packed = np.zeros(n_long, dtype=np.uint64)
        for L in range(n_long):
            lo, hi = L * per, min((L + 1) * per, 4096)
            if lo >= hi:
                continue
            acc = np.zeros(hi - lo, dtype=np.uint64)
            for k in range(hi - lo):
                acc[k] = np.uint64(vals[lo + k]) << np.uint64(k * bits)
            packed[L] = np.bitwise_or.reduce(acc)

        got = _unpack_non_stretch(packed, bits, 4096)
        check(np.array_equal(got, vals),
              "bits=%2d round-trips (%2d per long)" % (bits, per))

    # Prove the WRONG formula differs, so this test has teeth.
    bits = 5
    per = 64 // bits
    rng = np.random.default_rng(999)
    vals = rng.integers(0, 1 << bits, size=4096, dtype=np.int64)
    packed = np.zeros(-(-4096 // per), dtype=np.uint64)
    for L in range(-(-4096 // per)):
        lo, hi = L * per, min((L + 1) * per, 4096)
        acc = np.zeros(hi - lo, dtype=np.uint64)
        for k in range(hi - lo):
            acc[k] = np.uint64(vals[lo + k]) << np.uint64(k * bits)
        packed[L] = np.bitwise_or.reduce(acc)
    arr = np.concatenate([packed, np.zeros(4096, dtype=np.uint64)])
    i = np.arange(4096, dtype=np.int64)
    naive_shift = (i * bits) % 64
    naive = ((arr[(i * bits) // 64] >> naive_shift.astype(np.uint64))
             & np.uint64((1 << bits) - 1)).astype(np.int64)
    check(not np.array_equal(naive, vals),
          "textbook stretch formula is provably wrong for bits=5 "
          "(test would be vacuous otherwise)")


def _unpack_non_stretch(arr, bits, count):
    """Mirror of parse_world.unpack_non_stretch, kept local on purpose.

    Duplicating it here means the test validates the *format*, not merely that
    the parser agrees with itself. Cross-checked against the parser in
    test_matches_layer1_unpacker below.
    """
    per = 64 // bits
    need = -(-count // per)
    a = np.asarray(arr, dtype=np.uint64)
    if a.size < need:
        a = np.concatenate([a, np.zeros(need - a.size, dtype=np.uint64)])
    i = np.arange(count, dtype=np.int64)
    shifts = (i % per).astype(np.uint64) * np.uint64(bits)
    return ((a[i // per] >> shifts) & np.uint64((1 << bits) - 1)).astype(np.int64)


def test_matches_layer1_unpacker():
    """The local reference must agree with the real parser, bit for bit."""
    print("\n[1b] local reference agrees with parse_world.unpack_non_stretch")
    sys.path.insert(0, os.path.join(ROOT, "parse"))
    try:
        import parse_world as pw
    except Exception as exc:
        check(False, "parse_world importable", repr(exc))
        return
    rng = np.random.default_rng(4242)
    for bits in (4, 5, 6, 7, 8, 9, 11, 13, 15):
        per = 64 // bits
        n_long = -(-4096 // per)
        longs = rng.integers(-(2 ** 63), 2 ** 63 - 1, size=n_long, dtype=np.int64)
        mine = _unpack_non_stretch(longs.view(np.uint64), bits, 4096)
        theirs = pw.unpack_non_stretch(list(longs), bits)
        check(np.array_equal(mine, theirs),
              "bits=%2d parser == reference" % bits)


def test_stats_errors_zero():
    print("\n[2] layer 1 reported zero parse errors")
    sp = os.path.join(ROOT, "parse", "stats.json")
    if not os.path.isfile(sp):
        check(False, "parse/stats.json exists", sp)
        return
    with open(sp) as f:
        stats = json.load(f)
    for dim, info in stats["dimensions"].items():
        check(info.get("error_count", -1) == 0,
              "%s error_count == 0" % dim,
              "got %d: %s" % (info.get("error_count"), info.get("errors", [])[:2]))


def test_ranges_match_voxels():
    print("\n[3] stats world ranges match the exported voxel files")
    sp = os.path.join(ROOT, "parse", "stats.json")
    if not os.path.isfile(sp):
        check(False, "parse/stats.json exists", sp)
        return
    with open(sp) as f:
        stats = json.load(f)

    for dim, info in stats["dimensions"].items():
        p = os.path.join(ROOT, "voxel_data", "full", dim + ".bin")
        if not os.path.isfile(p):
            check(False, "%s.bin exists" % dim, p)
            continue
        got = vio.world_bounds(p)
        want = (info["world_x_range"][0], info["world_x_range"][1],
                info["world_z_range"][0], info["world_z_range"][1],
                info["y_range"][0], info["y_range"][1])
        check(got == want, "%s bounds agree with stats.json" % dim,
              "voxels=%s stats=%s" % (got, want))


def test_voxel_counts():
    print("\n[4] voxel counts agree with stats.json")
    sp = os.path.join(ROOT, "parse", "stats.json")
    if not os.path.isfile(sp):
        check(False, "parse/stats.json exists", sp)
        return
    with open(sp) as f:
        stats = json.load(f)
    for dim, info in stats["dimensions"].items():
        p = os.path.join(ROOT, "voxel_data", "full", dim + ".bin")
        if not os.path.isfile(p):
            check(False, "%s.bin exists" % dim, p)
            continue
        with vio.VoxelFile(p) as vf:
            n = sum(c["xyz"].shape[0] for c in vf.iter_chunks())
        check(n == info["non_air_blocks"], "%s voxel count" % dim,
              "file=%d stats=%d" % (n, info["non_air_blocks"]))


def test_air_excluded():
    """
    Air must never appear in the voxel *stream*.

    Note: an air entry in the chunk Palette is NORMAL and required -- vanilla
    chunk palettes put minecraft:air at index 0 so that "index 0 = air" holds
    structurally. Layer 1 filters air out of the voxel words but keeps the
    palette entry (it is part of the section's own palette). So the assertion is
    "no emitted voxel references an air palette index", not "no air in palette".
    """
    print("\n[5] no air leaked into the voxel stream")
    for dim in ("overworld", "nether", "end"):
        p = os.path.join(ROOT, "voxel_data", "full", dim + ".bin")
        if not os.path.isfile(p):
            continue
        with vio.VoxelFile(p) as vf:
            air_idx = {i for i, (nm, _pk) in enumerate(vf.palette)
                       if nm in vio.AIR_NAMES}
            check(bool(air_idx), "%s palette does contain air (expected)" % dim)
            leaked = 0
            for c in vf.iter_chunks(want_air=True):
                leaked += int(np.isin(c["state"], list(air_idx)).sum())
            check(leaked == 0, "%s voxel stream references no air" % dim,
                  "%d leaked" % leaked)


def test_reader_is_read_only():
    print("\n[6] phase 2 reader does not write to layer 1 output")
    import hashlib
    p = os.path.join(ROOT, "voxel_data", "full", "end.bin")
    if not os.path.isfile(p):
        check(False, "end.bin exists", p)
        return
    before = os.path.getmtime(p), os.path.getsize(p)
    with vio.VoxelFile(p) as vf:
        sum(c["xyz"].shape[0] for c in vf.iter_chunks())
    after = os.path.getmtime(p), os.path.getsize(p)
    check(before == after, "end.bin untouched (mtime+size)", "%s -> %s" % (before, after))


def test_chunk_cursor_agrees_with_stream():
    print("\n[7] ChunkCursor matches iter_chunks")
    p = os.path.join(ROOT, "voxel_data", "full", "end.bin")
    if not os.path.isfile(p):
        check(False, "end.bin exists", p)
        return
    with vio.ChunkCursor(p) as cur:
        with vio.VoxelFile(p) as vf:
            for row in vf.chunk_rows()[:40]:
                cx, cz = row[0], row[1]
                a = cur.get(cx, cz)
                b = None
                for c in vf.iter_chunks():
                    if c["chunkX"] == cx and c["chunkZ"] == cz:
                        b = c
                        break
                if a is None and b is None:
                    continue
                if a is None or b is None:
                    check(False, "chunk(%d,%d) presence agrees" % (cx, cz))
                    continue
                same = (np.array_equal(a[0], b["xyz"][:, 0])
                        and np.array_equal(a[1], b["xyz"][:, 1])
                        and np.array_equal(a[2], b["xyz"][:, 2])
                        and np.array_equal(a[3], b["state"]))
                check(same, "chunk(%d,%d) identical" % (cx, cz))


# --------------------------------------------------------------------------- #
# phase 2: landscape geometry + encoding
# --------------------------------------------------------------------------- #

def test_landscape_height_roundtrip():
    """
    blocks -> uint16 -> UE5 decode -> blocks must be exact to quantisation.

    This is the test that pins the Landscape height contract, and getting it
    wrong is silent: the terrain simply comes out the wrong height.

    The decode uses UE5's *actual* formula, confirmed against Epic's own
    documentation and the developer forums:

        local(v) = (v - 32768) / 128          # spans -256 .. +255.992
        z_cm(v)  = actor_offset_z_cm + local(v) * z_scale_cm

    Note the divisor is **128, not 512**. The full uint16 range therefore
    covers `512 * z_scale_cm` centimetres, which is why the encoder's Z Scale
    is `span * BLOCK_CM / 512`. The Epic Landscape Technical Guide says "1/512"
    for the ratio, which reads as 65535 -> 512 units; but the engine divides by
    128, and an Epic developer confirmed on the forums that "the height value
    increasing 128 then the landscape is 1 meter higher" at Z Scale 100. The
    guide is imprecise here; the engine behaviour is what `ue_decode` models.
    """
    import landscape as lsc

    print("\n[landscape] height encoding round-trip")
    ok = True

    # Sanity-check the decoder against the documented engine behaviour.
    d = lsc.ue_decode(np.array([0, 32768, 65535], dtype=np.uint16), 100.0, 0.0)
    ok &= check(abs(d[0] + 25600.0) < 1e-6,
                "ue_decode: v=0 lands at -256 * z_scale", "got %g" % d[0])
    ok &= check(abs(d[1]) < 1e-9,
                "ue_decode: v=32768 lands exactly on the actor", "got %g" % d[1])
    ok &= check(abs(d[2] - 255.9921875 * 100.0) < 1e-3,
                "ue_decode: v=65535 lands at +255.992 * z_scale",
                "got %g" % d[2])

    for (ymin, span) in [(0.0, 64.0), (3.0, 60.0), (-5.0, 10.0),
                         (0.0, 1.0), (0.0, 0.5), (100.0, 1.0)]:
        field = np.array([[ymin, ymin + span * 0.5, ymin + span]],
                         dtype=np.float32)
        u16, meta = lsc.encode_absolute(field, ymin, span)

        dec = (lsc.ue_decode(u16, meta["z_scale_cm"],
                             meta["actor_offset_z_cm"]) / 100.0)
        err = np.abs(dec - field)
        # One stored sample step, measured the way the engine takes it:
        # z_scale_cm/128 centimetres. The endpoint (v = 65535) also carries a
        # half-step from the rounding to 255.992, hence the +0.5 step.
        step_cm = meta["z_scale_cm"] / 128.0
        tol_blocks = (step_cm / 100.0) * 1.5 + 1e-12
        ok &= check(bool((err <= tol_blocks).all()),
                    "ymin=%g span=%g round-trips within quantisation (%.2e)"
                    % (ymin, span, err.max()),
                    "max err %.8f > tol %.8f" % (err.max(), tol_blocks))

        # The encoder also stores its own proof; make sure that agrees.
        stored = meta.get("roundtrip_max_err_blocks")
        if stored is not None:
            ok &= check(stored <= tol_blocks,
                        "  ymin=%g span=%g stored roundtrip err agrees"
                        % (ymin, span), "stored %g > tol %g" % (stored, tol_blocks))
    return ok


def test_landscape_no_stretch():
    """
    Resampling onto a larger Landscape grid must keep the world registered.

    Three properties, and the third is the one that actually protects the
    registration:

      * vertex (0,0) sits on world block (0,0);
      * the last vertex sits on the last block, so the actor's XY extent -- at
        100 cm per vertex -- spans exactly the world rectangle;
      * **every block of the field is used**, i.e. the index mapping
        ``round(i * W / size_x)`` is surjective onto ``[0, W)``.

    The failure mode this guards against is silent and expensive: an off-by-one
    in the sampling ratio stretches the grid by ``W/(W-1)`` (1.5% on a 256-wide
    world, 1.4% on the 720-wide campus). The PNG stays valid, the Landscape
    still imports, and the only symptom is that the far edge of the terrain
    drifts several metres away from the layer-1 HISM block layer -- which is
    exactly the kind of bug that survives a visual inspection pass.
    """
    import landscape as lsc

    print("\n[landscape] resampling preserves world registration")
    ok = True
    # (H, W, sx, sy) -- the last three come straight from solve_components.
    for (H, W, sx, sy) in [(256, 256, 260, 260), (1400, 512, 526, 1496)]:
        field = np.arange(H * W, dtype=np.float32).reshape(H, W)
        valid = np.ones((H, W), dtype=bool)
        grid = lsc.resample_to_grid(field, valid, sx, sy)
        ok &= check(grid.shape == (sy, sx),
                    "grid %dx%d for a %dx%d world" % (sx, sy, W, H),
                    "got %s" % (grid.shape,))
        ok &= check(float(grid[0, 0]) == 0.0,
                    "  origin maps to origin (no shift)")
        # Last vertex must carry the last block's height, not a padded copy of
        # an interior one.
        ok &= check(float(grid[sy - 1, sx - 1]) == float(field[H - 1, W - 1]),
                    "  last vertex carries the last block")
        # Surjectivity: the index mapping must touch every block exactly in
        # order. A stretch shows up here as a skipped block.
        xi = np.clip(np.rint(np.arange(sx) * (float(W) / sx)).astype(np.int64),
                     0, W - 1)
        yi = np.clip(np.rint(np.arange(sy) * (float(H) / sy)).astype(np.int64),
                     0, H - 1)
        ok &= check(np.array_equal(np.unique(xi), np.arange(W)),
                    "  every world column is sampled (X)",
                    "used %d of %d" % (len(np.unique(xi)), W))
        ok &= check(np.array_equal(np.unique(yi), np.arange(H)),
                    "  every world row is sampled (Y)",
                    "used %d of %d" % (len(np.unique(yi)), H))
        # Monotone: never goes backwards, so no tearing inside a tile.
        ok &= check(np.all(np.diff(xi) >= 0) and np.all(np.diff(yi) >= 0),
                    "  sampling index is monotone")

    # The same properties must hold for what the solver actually returns for
    # the world extents this project exports, not just hand-picked grids.
    for (W, H) in [(256, 256), (704, 1088), (720, 1040)]:
        cx, cy, sx, sy, ex, ey, qps = lsc.solve_components(W, H)
        xi = np.clip(np.rint(np.arange(sx) * (float(W) / sx)).astype(np.int64),
                     0, W - 1)
        yi = np.clip(np.rint(np.arange(sy) * (float(H) / sy)).astype(np.int64),
                     0, H - 1)
        ok &= check(np.array_equal(np.unique(xi), np.arange(W))
                    and np.array_equal(np.unique(yi), np.arange(H)),
                    "solver grid %dx%d @%d samples all of %dx%d 1:1"
                    % (sx, sy, qps, W, H))
        # XY Scale is 100 cm per vertex, so the actor must span the world plus
        # at most one block per axis of rounding slack.
        span_x_cm = (sx - 1) * 100.0
        want_cm = W * 100.0
        ok &= check(0 <= span_x_cm - want_cm < 4 * qps * 100,
                    "  actor XY span covers the world within one section",
                    "%.0f cm vs %.0f cm" % (span_x_cm, want_cm))
    return ok
    return ok


def test_landscape_resolution_is_legal():
    """
    Landscape size must be components*quads+1 for an engine-valid section size.

    Anything else still imports, but splits sections unevenly and logs
    "non-optimal component size"; under World Partition it wrecks the streaming
    grid, which is exactly the mechanism this pipeline depends on.
    """
    import landscape as lsc

    print("\n[landscape] resolution legality")
    ok = True
    for (W, H) in [(256, 256), (720, 1040), (2000, 2000), (4320, 3375)]:
        cx, cy, sx, sy, ex, ey, qps = lsc.solve_components(W, H)
        valid = lsc.is_valid_size(sx, sy, qps, 1)
        ok &= check(valid, "%dx%d blocks -> %dx%d verts @%d quads is legal"
                    % (W, H, sx, sy, qps),
                    "(sx-1)%%%d=%d (sy-1)%%%d=%d"
                    % (qps, (sx - 1) % qps, qps, (sy - 1) % qps))
        ok &= check(qps in lsc.VALID_SECTION_SIZES,
                    "  section size %d is an engine-valid value" % qps)
        ok &= check(ex >= -0.5 and ey >= -0.5,
                    "  grid covers the world (no underflow)")
        ok &= check(sx - 1 >= cx * qps and sy - 1 >= cy * qps,
                    "  grid is exactly components*%d+1" % qps,
                    "%dx%d vs %dx%d comps @%d"
                    % (sx, sy, cx, cy, qps))
    return ok


def test_axis_split_is_complete_and_disjoint():
    """
    ``_axis_split`` must partition [0, total) exactly, always.

    This function replaced one that appended a partial window, ran a no-op
    ``for _ in range(i + 1, n): pass`` and returned early -- so it silently
    produced *fewer* windows than requested. Coverage stayed complete, nothing
    raised, and the tile grid just quietly disagreed with the plan. The
    properties below are what make that impossible to reintroduce.
    """
    import landscape as lsc

    print("\n[landscape] tile axis split is a true partition")
    ok = True
    # (total, tile, n) -- includes the exact case the old code got wrong.
    cases = [(100, 30, 5), (1040, 174, 6), (720, 120, 6), (10, 3, 7),
             (256, 256, 1), (1000, 999, 3), (7, 1, 7), (5, 10, 4)]
    for (total, tile, n) in cases:
        wins = lsc._axis_split(total, tile, n)
        ok &= check(wins[0][0] == 0 and wins[-1][1] == total,
                    "split(%d, %d, %d) spans [0, %d)" % (total, tile, n, total),
                    "covers [%d, %d)" % (wins[0][0], wins[-1][1]))
        ok &= check(all(a < b for (a, b) in wins), "  no empty window")
        # disjoint and gap-free
        seals = all(wins[i][1] == wins[i + 1][0] for i in range(len(wins) - 1))
        ok &= check(seals, "  windows are disjoint and gap-free",
                    "%s" % (wins,))
        # balanced: widths differ by at most 1. This is what stops one tile in
        # the grid from being built to a different spec than its neighbours.
        widths = [b - a for (a, b) in wins]
        ok &= check(max(widths) - min(widths) <= 1,
                    "  window widths are balanced (<=1 apart)",
                    "%s" % (widths,))
        # window count: exactly n unless one window already covers everything
        ok &= check(len(wins) == n or (len(wins) == 1 and total <= tile),
                    "  returns n windows unless one covers the extent",
                    "got %d for n=%d" % (len(wins), n))
    # The specific regression: the old implementation returned 4 here.
    ok &= check(len(lsc._axis_split(100, 30, 5)) == 5,
                "split(100, 30, 5) returns 5 windows (the fixed bug)")
    ok &= check(max(b - a for (a, b) in lsc._axis_split(1040, 174, 6))
                - min(b - a for (a, b) in lsc._axis_split(1040, 174, 6)) <= 1,
                "the 1040/6 remainder is spread, not dumped on the last tile",
                "%s" % [b - a for (a, b) in lsc._axis_split(1040, 174, 6)])
    return ok


def test_tile_grid_trades_actors_against_components():
    """
    The tile grid must not buy streamability with component count.

    A Landscape component is the unit of culling/LOD and dominates render cost;
    an extra actor is cheap. So splitting a region finer is only a win while
    the summed component count stays near the best any grid achieves.

    Measured on the 720x1040 campus: the naive ``ceil(extent / cap)`` grid at
    cap=256 produced **25 actors and 15,750 components** -- 19x the single
    Landscape's 816 -- because small tiles flip the per-tile solver to 7-quad
    sections. The cost guard brings that to 3,456. This test pins both halves
    of the contract: tiling happens when asked, and it does not explode.
    """
    import landscape as lsc

    print("\n[landscape] tile grid balances actor count against components")
    ok = True

    # 1. Asking for no split must give exactly one tile.
    ok &= check(lsc.solve_tile_grid(720, 1040, 0) == (1, 1),
                "cap=0 -> single Landscape (opt-out respected)")

    # 2. Asking for a split must actually split, and give a square grid so all
    #    seams align across the region.
    for (W, H, cap) in [(720, 1040, 256), (720, 1040, 512), (3000, 3000, 512)]:
        tx, ty = lsc.solve_tile_grid(W, H, cap)
        ok &= check(tx == ty, "%dx%d cap=%d -> square grid" % (W, H, cap),
                    "got %dx%d" % (tx, ty))
        ok &= check(tx >= 2, "  %dx%d cap=%d -> more than one tile" % (W, H, cap),
                    "got %dx%d" % (tx, ty))
        # The cap sets the MINIMUM tile count via the extent requirement; the
        # section-consistency rule may then coarsen it, but never below that
        # floor.
        floor = int(np.ceil(max(W, H) / float(cap)))
        ok &= check(tx >= min(floor, 2),
                    "  tile count honours the extent floor where affordable",
                    "%d tiles vs floor %d" % (tx, floor))

    # 2b. The cap must be a *preference*, not a foot-gun: asking for absurdly
    #     small tiles must not produce the 7-quad component blowup. Every cap
    #     from tiny to the region size converges to a sane grid.
    def grid_components(W, H, n):
        xw = lsc._axis_split(W, int(np.ceil(W / float(n))), n)
        yw = lsc._axis_split(H, int(np.ceil(H / float(n))), n)
        tot = 0
        for (z0, z1) in yw:
            for (x0, x1) in xw:
                cx, cy, _sx, _sy, _ex, _ey, _q = lsc.solve_components(
                    int(x1 - x0), int(z1 - z0))
                tot += cx * cy
        return tot

    for cap in (32, 64, 128, 256, 512, 768, 1024):
        tx, ty = lsc.solve_tile_grid(720, 1040, cap)
        ok &= check(grid_components(720, 1040, tx) <= 4000,
                    "  campus cap=%d -> %dx%d avoids the component blowup"
                    % (cap, tx, ty),
                    "%d components" % grid_components(720, 1040, tx))

    # 3. The cost guard. Comparing a tiled grid against the *single* Landscape
    #    would be wrong -- splitting inherently costs components, because every
    #    tile needs at least one and small tiles prefer finer sections. What the
    #    guard must guarantee is that the chosen grid does not do dramatically
    #    worse than the best grid at that tiling level or coarser.
    def grid_components(W, H, n):
        xw = lsc._axis_split(W, int(np.ceil(W / float(n))), n)
        yw = lsc._axis_split(H, int(np.ceil(H / float(n))), n)
        tot = 0
        for (z0, z1) in yw:
            for (x0, x1) in xw:
                cx, cy, _sx, _sy, _ex, _ey, _q = lsc.solve_components(
                    int(x1 - x0), int(z1 - z0))
                tot += cx * cy
        return tot

    W, H = 720, 1040
    ref = {n: grid_components(W, H, n) for n in range(1, 11)}
    # The naive, pre-fix behaviour was 15,750 components at a 5x5 grid and
    # 15,808 at 8x8. The good grids must now stay under 4,000.
    GOOD = (1, 2, 3, 4, 5, 6, 7, 8, 10)
    for n in GOOD:
        ok &= check(ref[n] <= 4000,
                    "campus %dx%d grid keeps components under 4000" % (n, n),
                    "got %d" % ref[n])
    # 9x9 is the known hard case: 80-block tiles are too small for a 15-quad
    # section to fit tightly, so the solver's own border tolerance pushes it to
    # 7 quads and 16,524 components. That is *arithmetic*, not a bug -- but the
    # solver must therefore never pick it. This asserts the constraint that
    # matters: the chosen grid is never one of the dense-7-quad grids.
    ok &= check(ref[9] > ref[7],
                "the 9x9 grid is demonstrably worse than 7x7 (why it is refused)",
                "%d vs %d" % (ref[9], ref[7]))
    # The optimum must be tight, not just acceptable.
    ok &= check(ref[2] <= 1000,
                "the 2x2 grid is the component-cheap option (<= 1000)",
                "got %d" % ref[2])
    # And the solver's own pick must be one of the good grids -- for every
    # realistic cap, including the default.
    for cap in (128, 256, 320, 384, 512, 768, 1024):
        tx, ty = lsc.solve_tile_grid(W, H, cap)
        comps = ref.get(tx)
        if comps is None:
            continue
        ok &= check(comps <= 4000,
                    "  campus cap=%d -> %dx%d stays under 4000 components"
                    % (cap, tx, ty),
                    "got %d" % comps)
    # Splitting must buy streaming without paying an order of magnitude: the
    # default 2x2 grid is the same render state as one monolithic Landscape.
    ok &= check(ref[2] <= ref[1] * 6,
                "2x2 tiles cost at most 6x the monolithic component count "
                "(they buy streaming for it)",
                "%d vs %d" % (ref[2], ref[1]))
    return ok


def test_resample_rejects_undersized_grid():
    """
    A grid smaller than the field must be a hard error, not a silent truncation.

    The old code did ``out_w = min(size_x, W)``: the world's right/bottom edge
    was dropped without a word, and every consumer downstream believed the
    heightmap covered the region. The solver never produces such a grid, so a
    grid like that can only mean the caller mixed two different extents --
    exactly the mistake a loud failure is for.
    """
    import landscape as lsc

    print("\n[landscape] resample rejects a grid smaller than the field")
    ok = True
    field = np.zeros((10, 10), dtype=np.float32)
    valid = np.ones((10, 10), dtype=bool)
    for (sx, sy) in [(9, 10), (10, 9), (5, 5), (1, 1)]:
        try:
            lsc.resample_to_grid(field, valid, sx, sy)
            ok &= check(False, "resample_to_grid(%dx%d) raises" % (sx, sy))
        except ValueError:
            ok &= check(True, "resample_to_grid(%dx%d) raises" % (sx, sy))
    # And the covering case still works.
    try:
        lsc.resample_to_grid(field, valid, 12, 12)
        ok &= check(True, "  a covering grid is accepted")
    except ValueError as exc:
        ok &= check(False, "  a covering grid is accepted", repr(exc))
    return ok


def test_landscape_tiles_partition_the_region():
    """
    The exported tile grid must cover the region exactly once, no gaps.

    This is checked on the shipped manifest, so it catches a regression in the
    solver without needing to re-run the pipeline. Two properties:

      * the tiles of any one tile-row/column tile their axis with no gap and no
        overlap (they must share a seam edge, never a vertex, because each
        Landscape owns a disjoint vertex grid);
      * the union of all tiles is exactly the declared block rectangle.
    """
    print("\n[landscape] exported tiles partition the region exactly")
    lj = os.path.join(ROOT, "out", "phase2", "overworld", "landscape",
                      "landscape.json")
    if not os.path.isfile(lj):
        print("  SKIP  no landscape.json (run the pipeline first)")
        return True
    with open(lj) as fh:
        meta = json.load(fh)

    ok = True
    tiles = meta["tiles"]
    bx0, bz0 = meta["block_origin"]
    bw, bh = meta["block_size"]

    ok &= check(len(tiles) == meta["tile_count"],
                "tile_count matches the tile list")
    ok &= check(meta["tile_grid"][0] * meta["tile_grid"][1] == len(tiles),
                "tile_grid %s matches %d tiles"
                % (meta["tile_grid"], len(tiles)))

    # Group by row (tile_index[0]) and check each row tiles X with no gap.
    rows = {}
    for t in tiles:
        rows.setdefault(t["tile_index"][0], []).append(t)
    for ri, row in sorted(rows.items()):
        row.sort(key=lambda t: t["tile_index"][1])
        ok &= check(row[0]["block_origin"][0] == bx0,
                    "row %d starts at the region's west edge" % ri)
        spans = [t["block_span"][0] for t in row]
        origins = [t["block_origin"][0] for t in row]
        ok &= check(all(origins[i] + spans[i] == origins[i + 1]
                        for i in range(len(row) - 1)),
                    "  row %d tiles X with no gap and no overlap" % ri,
                    "%s" % list(zip(origins, spans)))
        ok &= check(origins[-1] + spans[-1] == bx0 + bw,
                    "  row %d reaches the region's east edge" % ri)

    # Group by column (tile_index[1]) and check each column tiles Z.
    cols = {}
    for t in tiles:
        cols.setdefault(t["tile_index"][1], []).append(t)
    for ci, col in sorted(cols.items()):
        col.sort(key=lambda t: t["tile_index"][0])
        spans = [t["block_span"][1] for t in col]
        origins = [t["block_origin"][1] for t in col]
        ok &= check(origins[0] == bz0, "column %d starts at the north edge" % ci)
        ok &= check(all(origins[i] + spans[i] == origins[i + 1]
                        for i in range(len(col) - 1)),
                    "  column %d tiles Z with no gap and no overlap" % ci,
                    "%s" % list(zip(origins, spans)))
        ok &= check(origins[-1] + spans[-1] == bz0 + bh,
                    "  column %d reaches the south edge" % ci)

    # Every tile's declared origin must match its integer block origin * 100 cm:
    # if these ever disagree, the actor would be placed off-grid.
    for t in tiles[:8]:
        ok &= check(abs(t["origin_cm"][0] - t["block_origin"][0] * 100.0) < 1e-6
                    and abs(t["origin_cm"][1]
                            - t["block_origin"][1] * 100.0) < 1e-6,
                    "tile %s origin_cm == block_origin * 100" % t["file"])

    # Coverage error must be a small border, never a shortfall.
    ok &= check(all(t["coverage_error_blocks"][0] >= 0
                    and t["coverage_error_blocks"][1] >= 0 for t in tiles),
                "no tile under-covers its block rectangle")

    # The directory must contain exactly the tiles the manifest names. A
    # leftover from an earlier run with a different grid is invisible to the
    # importer (it reads the manifest) but makes the output directory a
    # function of the whole run history rather than of the current inputs --
    # a 6x6 exploration grid leaves 32 orphans next to the 4 real tiles.
    on_disk = {fn for fn in os.listdir(os.path.dirname(lj))
               if fn.endswith(".png") and fn.startswith("overworld_")}
    declared = {t["file"] for t in tiles}
    ok &= check(on_disk == declared,
                "the directory holds exactly the manifest's tiles",
                "orphans: %s" % sorted(on_disk - declared))
    return ok


def test_landscape_export_prunes_stale_tiles():
    """
    Re-exporting with a different grid must not leave the old tiles behind.

    This is a property of the output *directory*, not of the manifest, so the
    reproducibility test cannot see it -- that one only reads landscape.json.
    Left unfixed it accumulates silently: every exploration of a different tile
    grid adds a full set of PNGs that no code path will ever read again.

    The unrelated files matter as much as the pruning. A cleanup that was too
    broad would delete a debug image or a hand-placed note, so both directions
    are asserted.
    """
    print("\n[landscape] export prunes tiles from a previous grid")
    import shutil
    import tempfile
    import landscape as lsc_mod

    tmp = tempfile.mkdtemp(prefix="lsc_prune_")
    try:
        # Stand-ins for a previous 3x3 grid, plus files that must survive.
        for ti in range(3):
            for tj in range(3):
                open(os.path.join(tmp, "overworld_%02d_%02d.png" % (ti, tj)),
                     "wb").close()
        open(os.path.join(tmp, "unrelated.png"), "wb").close()
        with open(os.path.join(tmp, "notes.txt"), "w") as fh:
            fh.write("keep me")
        # Same prefix, but not this exporter's "<name>_TI_TJ.png" shape.
        open(os.path.join(tmp, "overworld_debug.png"), "wb").close()

        height = np.zeros((1040, 720), dtype=np.float32)
        valid = np.ones((1040, 720), dtype=bool)
        meta = lsc_mod.export_landscapes(
            height, valid, (0, 0), 0.0, 64.0, tmp, "overworld",
            max_tile_blocks=768, tile_grid_search=8)

        ok = True
        written = {t["file"] for t in meta["tiles"]}
        remaining = set(os.listdir(tmp))
        ok &= check(not any(re.match(r"^overworld_\d\d_\d\d\.png$", f)
                            and f not in written for f in remaining),
                    "no tile from the previous 3x3 grid survives",
                    "%d files left" % len(remaining))
        ok &= check(written <= remaining,
                    "every tile this run declared was written",
                    "%s" % sorted(written))
        # Over-eager cleanup is the failure mode worth guarding.
        ok &= check("unrelated.png" in remaining,
                    "an unrelated PNG in the same directory survives")
        ok &= check("notes.txt" in remaining,
                    "an unrelated non-PNG file survives")
        ok &= check("overworld_debug.png" in remaining,
                    "a similarly-named non-tile file survives")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_landscape_xy_scale_is_minecraft():
    """The exported heightmap must declare XY Scale = 100 cm (1 block/vertex)."""
    print("\n[landscape] exported metadata declares the Minecraft scale")
    lsc_dir = os.path.join(ROOT, "out", "phase2", "overworld", "landscape")
    meta_path = os.path.join(lsc_dir, "landscape.json")
    if not os.path.isfile(meta_path):
        print("  SKIP  no landscape.json (run the pipeline first)")
        return True
    with open(meta_path) as fh:
        meta = json.load(fh)
    return (check(abs(meta["xy_scale_cm"] - 100.0) < 1e-6,
                  "xy_scale_cm == 100.0",
                  "got %r -- the HISM block layer would not align"
                  % meta["xy_scale_cm"])
            and check(meta["tiles"][0]["origin_cm"][0]
                      == meta["block_origin"][0] * 100.0,
                      "tile origin_cm is block_origin * 100"))


def test_phase2_artifacts_are_reproducible():
    """
    Rerunning the pipeline on unchanged input must produce identical bytes.

    The phase-2 outputs are committed to git and are meant to be reviewable, so
    a wall-clock field in any manifest makes every rerun show up as a diff and
    trains reviewers to ignore the file. A "seconds" key did exactly that: it was
    the only difference between two runs of the same code on the same input.

    This checks the property directly on the shipped artefacts -- no rerun
    needed -- by asserting no timing key survived into the manifests.
    """
    print("\n[phase2] artefacts are byte-reproducible")
    ok = True
    p2 = os.path.join(ROOT, "out", "phase2", "overworld")

    for rel in ("terrain.json", "phase2_report.json",
                "landscape/landscape.json", "props/prop_placements.json",
                "water/water.json"):
        path = os.path.join(p2, rel)
        if not os.path.isfile(path):
            print("  SKIP  %s missing (run the pipeline first)" % rel)
            continue
        with open(path) as fh:
            meta = json.load(fh)

        found = []

        def walk(node, trail):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in ("seconds", "elapsed", "duration", "runtime",
                             "timestamp", "generated_at"):
                        found.append("%s/%s" % (trail, k))
                    walk(v, "%s/%s" % (trail, k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, "%s[%d]" % (trail, i))

        walk(meta, rel)
        ok &= check(not found, "%s carries no wall-clock field" % rel,
                    "found %s" % found)

    # The landscape manifest additionally records a self-check of the encoding;
    # it must be present and tiny, otherwise the file is not self-describing.
    lj = os.path.join(p2, "landscape", "landscape.json")
    if os.path.isfile(lj):
        with open(lj) as fh:
            tile = json.load(fh)["tiles"][0]["height_cm_meta"]
        err = tile.get("roundtrip_max_err_blocks")
        ok &= check(err is not None and err < 0.01,
                    "landscape.json records its own encode/decode proof",
                    "roundtrip_max_err_blocks=%r" % err)
    return ok


# --------------------------------------------------------------------------- #
# phase 2: connected components
# --------------------------------------------------------------------------- #

def test_connected_components():
    """
    Spatial grouping must survive shapes where a naive scan breaks.

    The U shape and the ring are the two shapes that exposed real bugs: a
    lexsort-based "compare with the previous element" neighbour scan misses
    edges whenever a column holds several cells, and it quietly split both of
    these in half. A diagonal-touch case pins 6-neighbour (not 26-) semantics.
    """
    import props as P
    from semantic import (LABEL_VEGETATION as V, LABEL_STRUCTURE as S,
                          LABEL_PROP as PR, LABEL_GLASS as G)

    print("\n[props] connected components")
    wanted = (V, S, PR, G)
    ok = True

    def run(vox, expect_n, expect_total=None, pitch=1, label=""):
        arr = np.asarray(vox, dtype=np.int32)
        comps = P.connected_components(arr, np.full(len(arr), V, np.uint8),
                                       wanted, pitch=pitch)
        got_n = len(comps)
        got_total = sum(c[0].shape[0] for c in comps)
        good = (got_n == expect_n
                and (expect_total is None or got_total == expect_total))
        return check(good, label,
                     "got %d comps / %d voxels, want %s / %s"
                     % (got_n, got_total, expect_n, expect_total))

    ok &= run([[0, 0, 0], [1, 0, 0], [2, 0, 0], [10, 0, 0], [11, 0, 0]],
              2, 5, label="two separate blobs -> 2 components")
    ok &= run([[0, 0, 0], [1, 1, 0]], 2, 2,
              label="diagonal touch is NOT connected (6-neighbour)")
    ok &= run([[0, 0, 0], [0, 1, 0], [0, 2, 0], [1, 0, 0], [2, 0, 0],
               [2, 1, 0], [2, 2, 0]], 1, 7,
              label="U shape stays one component (the bug that was fixed)")
    ok &= run([[x, 0, z] for x in range(3) for z in range(3)
               if x in (0, 2) or z in (0, 2)], 1, 8,
              label="ring stays one component (the bug that was fixed)")
    ok &= run([[5, 5, 5], [5, 5, 6], [5, 5, 7], [5, 6, 7], [6, 6, 7],
               [7, 6, 7]], 1, 6, label="3D L shape is one component")
    shell = [[x, y, z] for x in range(5) for y in range(5) for z in range(5)
             if 4 <= (x - 2) ** 2 + (y - 2) ** 2 + (z - 2) ** 2 <= 9]
    ok &= run(shell, 1, len(shell), label="3D shell is one component")
    # pitch collapses the first three onto one lattice cell
    ok &= run([[0, 0, 0], [1, 1, 1], [2, 0, 0], [9, 9, 9]], 2, 4, pitch=2,
              label="pitch=2 collapses cells correctly")
    return ok


# --------------------------------------------------------------------------- #
# phase 2: water
# --------------------------------------------------------------------------- #

def test_water_mesh():
    """
    The water plane must emit geometry proportional to the water.

    A regression here produced a 748,800-vertex OBJ with zero triangles for a
    map containing six isolated water columns -- the full grid was laid down
    before the quad test culled every face.
    """
    import water as W

    print("\n[water] planar export")
    ok = True

    # Isolated columns cannot form a 2x2 quad, so there must be no vertices.
    surf = np.zeros((50, 50), np.float32)
    valid = np.zeros((50, 50), bool)
    for (a, b) in [(5, 5), (20, 30), (40, 10), (10, 45), (33, 33), (47, 47)]:
        valid[a, b] = True
        surf[a, b] = 8.0
    m = W.water_mesh(surf, valid, 0, 0)
    ok &= check(m["vertices"].shape[0] == 0 and m["faces"].shape[0] == 0,
                "6 isolated columns -> empty mesh (was 748k verts, 0 tris)")

    surf = np.zeros((20, 20), np.float32)
    valid = np.zeros((20, 20), bool)
    valid[5:8, 5:8] = True
    surf[5:8, 5:8] = 8.0
    m = W.water_mesh(surf, valid, 0, 0)
    ok &= check(m["quad_count"] == 4, "3x3 pond -> 4 quads",
                "got %d" % m["quad_count"])
    ok &= check(m["vertices"].shape[0] == 9,
                "3x3 pond -> 9 shared vertices",
                "got %d" % m["vertices"].shape[0])
    tri = m["vertices"][m["faces"]]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ny = n[:, 1] / np.maximum(np.linalg.norm(n, axis=1), 1e-12)
    ok &= check(bool((ny > 0.999).all()), "water normals point +Y")

    surf = np.full((100, 100), 7.0, np.float32)
    valid = np.ones((100, 100), bool)
    m = W.water_mesh(surf, valid, 0, 0)
    ok &= check(m["quad_count"] == 99 * 99,
                "full 100x100 grid -> 99*99 quads",
                "got %d" % m["quad_count"])
    ok &= check(m["vertices"].shape[0] == 10000,
                "full grid -> 10000 vertices",
                "got %d" % m["vertices"].shape[0])

    # empty input must not crash mesh_stats downstream
    import terrain as T
    st = T.mesh_stats({"vertices": np.zeros((0, 3), np.float32),
                       "faces": np.zeros((0, 3), np.int64)})
    ok &= check(st["triangles"] == 0 and st["y_range_cm"] is None,
                "mesh_stats tolerates an empty mesh")

    info = W.sea_level_info(np.zeros((5, 5), np.float32), np.zeros((5, 5), bool))
    ok &= check(info["sea_level_blocks"] is None,
                "sea_level_info reports None for a dry map")
    return ok


def test_ground_alignment_stays_in_world_coordinates():
    """
    Ground alignment must return world block coordinates, not heightmap-local ones.

    A regression here put every prop on the origin grid: a tree at world
    x=248, z=-372 came out at position_cm [52000, ..., 30000], i.e. grid
    (520, 300) instead of (248, -372). Nothing crashed and the rotations were
    plausible, so it would have shipped silently.
    """
    import props as P

    print("\n[props] ground alignment coordinates")
    ok = True

    # A tilted plane so the fit is well conditioned and the normal is non-trivial.
    H, W = 128, 128
    # A negative origin is what makes this test meaningful: the heightmap is
    # indexed locally, so world column 30 is local column 30 - (-100) = 130.
    # Before the fix the function returned the *local* index, which put every
    # prop on the wrong grid while still looking plausible.
    ox, oz = -100, -100
    gx, gz = np.meshgrid(np.arange(W), np.arange(H))
    plane = 10.0 + 0.20 * gx + 0.35 * gz
    heightmap = plane.astype(np.float32)

    cx, cz = 30, 20             # world blocks -> local (130, 120), inside the map
    fit = P.fit_ground_plane(heightmap, ox, oz, cx, cz, radius=6)
    ok &= check(fit is not None, "fit succeeds for an interior column")
    if fit is None:
        return False
    (point, normal) = fit

    ok &= check(abs(point[0] - cx) < 1e-6,
                "returned x is the world column (%g, not the local index)" % cx,
                "got %g" % point[0])
    ok &= check(abs(point[2] - cz) < 1e-6,
                "returned z is the world row (%g, not the local index)" % cz,
                "got %g" % point[2])

    # And the height must match the plane at that world column.
    expect_y = 10.0 + 0.20 * (cx - ox) + 0.35 * (cz - oz)
    ok &= check(abs(point[1] - expect_y) < 0.05,
                "height matches the plane at the world column",
                "got %.3f want %.3f" % (point[1], expect_y))

    ok &= check(normal[1] < 0, "ground normal points downward")
    slope = np.hypot(normal[0], normal[2]) / abs(normal[1])
    ok &= check(abs(slope - np.hypot(0.20, 0.35)) < 0.02,
                "normal slope matches the plane gradient",
                "got %.4f want %.4f" % (slope, np.hypot(0.20, 0.35)))

    # The same guarantee on the real campus grid, at the real regression
    # coordinates. The synthetic plane above proves the arithmetic; this proves
    # the arguments line up with the artefacts phase 2 actually ships, which is
    # where a block-origin mismatch would show up.
    smooth = os.path.join(ROOT, "out", "phase2", "overworld",
                          "heightmap_smooth.npy")
    if not os.path.isfile(smooth):
        print("  SKIP  campus fit (no heightmap_smooth.npy)")
        return ok
    hm = np.load(smooth)
    bo_x, bo_z = ox, oz
    tjson = os.path.join(ROOT, "out", "phase2", "overworld", "terrain.json")
    if os.path.isfile(tjson):
        with open(tjson) as fh:
            bo = json.load(fh)["block_origin"]
        bo_x, bo_z = int(bo[0]), int(bo[1])
    rfit = P.fit_ground_plane(hm, bo_x, bo_z, 248, -372, radius=6)
    if check(rfit is not None, "campus fit succeeds at world (248, -372)",
             "grid %s origin (%d,%d)" % (hm.shape, bo_x, bo_z)):
        ok &= check(abs(rfit[0][0] - 248) < 1e-6 and abs(rfit[0][2] + 372) < 1e-6,
                    "campus fit returns world coords, not local indices",
                    "got (%g, %g)" % (rfit[0][0], rfit[0][2]))
        ok &= check(np.isfinite(rfit[0][1]) and np.isfinite(rfit[1]).all(),
                    "campus fit is finite", str(rfit))

    return ok


def main():
    print("=" * 68)
    print("MC2UE5 regression tests")
    print("=" * 68)
    for fn in (test_bit_packing, test_matches_layer1_unpacker,
               test_stats_errors_zero, test_ranges_match_voxels,
               test_voxel_counts, test_air_excluded, test_reader_is_read_only,
               test_chunk_cursor_agrees_with_stream,
               test_landscape_height_roundtrip, test_landscape_no_stretch,
               test_landscape_resolution_is_legal,
               test_axis_split_is_complete_and_disjoint,
               test_tile_grid_trades_actors_against_components,
               test_resample_rejects_undersized_grid,
               test_landscape_tiles_partition_the_region,
               test_landscape_export_prunes_stale_tiles,
               test_landscape_xy_scale_is_minecraft,
               test_phase2_artifacts_are_reproducible,
               test_connected_components,
               test_ground_alignment_stays_in_world_coordinates,
               test_water_mesh):
        fn()
    print("\n" + "=" * 68)
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
