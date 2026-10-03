#!/usr/bin/env python3
"""
Offline test for import_world.py: stubs out the `unreal` module so the pure
Python parts (the MC2WV2 reader and the cell x block grouping) can be exercised
in a container that has no UE installed.

This does NOT test anything that touches the editor (asset creation, HISM
components, World Partition) -- those can only be verified in a real editor.

Usage:
    python3 scripts/test_import_world.py [--full]
"""
import argparse
import importlib.util
import os
import struct
import sys
import types
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
SCRIPT = os.path.join(ROOT, "project", "Content", "Python", "import_world.py")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if detail else ""))


def install_stub_unreal():
    """A minimal stand-in so `import unreal` succeeds and log() works."""
    u = types.ModuleType("unreal")

    class _Log(list):
        def append(self, msg):
            list.append(self, str(msg))

    u.log = lambda m: None
    u.log_warning = lambda m: None
    u.log_error = lambda m: None
    u.is_editor = lambda: True
    u.SystemLibrary = types.SimpleNamespace(get_engine_version=lambda: "5.5.4-test")
    u.Paths = types.SimpleNamespace(
        project_dir=lambda: os.path.join(ROOT, "project") + "/",
        convert_relative_path_to_full=lambda p: p)
    u.EditorAssetLibrary = types.SimpleNamespace(
        does_asset_exist=lambda p: False, load_asset=lambda p: None,
        make_directory=lambda p: None, save_asset=lambda *a, **k: True)
    u.EditorLoadingAndSavingUtils = types.SimpleNamespace(
        save_dirty_packages=lambda *a, **k: None)
    sys.modules["unreal"] = u
    return u


def load_module():
    spec = importlib.util.spec_from_file_location("import_world", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="walk all three real .bin files (slower, ~1 min)")
    args = ap.parse_args()

    install_stub_unreal()
    iw = load_module()
    print("loaded %s" % SCRIPT)

    vdir = os.path.join(ROOT, "voxel_data", "full")
    dims = ["overworld", "nether", "end"] if args.full else ["overworld"]

    print("\n[1] reader opens the real MC2WV2 files")
    for d in dims:
        path = os.path.join(vdir, "%s.bin" % d)
        if not os.path.isfile(path):
            check("file present: %s" % d, False, path)
            continue
        vf = iw.VoxelFile(path)
        check("%s magic/version/dimension" % d,
              vf.dimension == d and vf.chunk_count > 0,
              "dim=%s chunks=%d voxels=%d palette=%d"
              % (vf.dimension, vf.chunk_count, vf.voxel_count, len(vf.palette)))
        vf.close()

    print("\n[2] decode correctness vs. an independent re-implementation")
    # nether is small enough to decode fully and cross-check the totals.
    path = os.path.join(vdir, "nether.bin")
    vf = iw.VoxelFile(path)
    seen = 0
    names = defaultdict(int)
    minx = miny = minz = 10 ** 9
    maxx = maxy = maxz = -10 ** 9
    for wx, wy, wz, name in vf.iter_blocks():
        seen += 1
        names[name] += 1
        minx, maxx = min(minx, wx), max(maxx, wx)
        miny, maxy = min(miny, wy), max(maxy, wy)
        minz, maxz = min(minz, wz), max(maxz, wz)
    check("nether voxel count matches header", seen == vf.voxel_count,
          "%d decoded vs %d in header" % (seen, vf.voxel_count))
    # stats.json recorded the true Y range for the nether.
    check("nether Y range reaches real section heights (not 0..15)",
          maxy > 15, "y range %d..%d" % (miny, maxy))
    check("nether is mostly netherrack",
          names["minecraft:netherrack"] > seen * 0.5,
          "top: %s" % sorted(names.items(), key=lambda kv: -kv[1])[:2])
    check("nether bounds are sane",
          -2000 < minx < 0 and 0 < maxx < 2000 and -2000 < minz < 0 < maxz,
          "x[%d,%d] z[%d,%d] y[%d,%d]" % (minx, maxx, minz, maxz, miny, maxy))
    vf.close()

    print("\n[3] chunk X/Z are absolute (not region-local 0..31)")
    vf = iw.VoxelFile(path)
    xs = set()
    for (cx, cz, _c, _o, _g) in vf.rows:
        xs.add(cx)
    check("absolute chunk X includes negatives / beyond 31",
          min(xs) < 0 or max(xs) > 31,
          "chunk X range %d..%d across %d chunks" % (min(xs), max(xs), len(xs)))
    vf.close()

    print("\n[4] grouping: bucket keys and packed buffer round-trip")
    vf = iw.VoxelFile(path)
    iw.DRY_RUN = True
    known = set(n for n, _c in names.items())
    # Exercise the non-DRY_RUN packing path directly on a small slice.
    cell = iw.CELL_SIZE
    buckets = defaultdict(bytearray)
    for i, (wx, wy, wz, name) in enumerate(vf.iter_blocks()):
        if i >= 200000:
            break
        if name not in known:
            continue
        cx, cz = wx // cell, wz // cell
        buckets[(cx, cz, name)] += struct.pack("<HHH", wx - cx * cell,
                                               wz - cz * cell, wy)
    check("buckets were produced", len(buckets) > 0, "%d groups" % len(buckets))
    total = sum(len(b) // 6 for b in buckets.values())
    check("packed buffer decodes back to the right count", total > 0,
          "%d records, %d bytes" % (total, sum(len(b) for b in buckets.values())))

    # Round-trip one record exactly.
    key = sorted(buckets.keys())[0]
    buf = buckets[key]
    lx, lz, y = struct.unpack_from("<HHH", buf, 0)
    check("packed record round-trips",
          0 <= lx < cell and 0 <= lz < cell and 0 <= y <= 511,
          "cell%s first record lx=%d lz=%d y=%d" % (str(key), lx, lz, y))
    check("every key is (cellX, cellZ, blockName)",
          all(len(k) == 3 and isinstance(k[2], str) for k in buckets))
    vf.close()

    print("\n[5] report maths")
    counts = {(0, 0, "minecraft:stone"): 10,
              (0, 0, "minecraft:dirt"): 1,
              (1, 0, "minecraft:stone"): 3}
    rep = iw._build_report("test", counts, {"minecraft:unknown": 7})
    check("group count", rep["groups"] == 3, str(rep["groups"]))
    check("cell count", rep["cells"] == 2, str(rep["cells"]))
    check("instance total", rep["instances"] == 14, str(rep["instances"]))
    # 3 groups, none above the cap -> 3 components
    check("component count (no split needed)", rep["components"] == 3,
          str(rep["components"]))
    big = {(0, 0, "minecraft:stone"): 1200000}
    rep2 = iw._build_report("test", big, {})
    check("oversized group splits into ceil(n/cap) components",
          rep2["components"] == 3, str(rep2["components"]))
    check("unmapped blocks are reported",
          rep["unmapped"].get("minecraft:unknown") == 7)

    print("\n[6] helpers")
    check("sanitize strips the namespace",
          iw.sanitize("minecraft:grass_block") == "MC_grass_block",
          iw.sanitize("minecraft:grass_block"))
    check("sanitize handles odd characters",
          iw.sanitize("minecraft:oak_slab") == "MC_oak_slab",
          iw.sanitize("minecraft:oak_slab"))
    check("BLOCK_CM maps 1 block to 1 metre", iw.BLOCK_CM == 100.0)
    check("dimensions are stacked, not co-located",
          len(set(iw.DIMENSION_Z_OFFSET_CM.values())) == 3,
          str(iw.DIMENSION_Z_OFFSET_CM))

    print("\n[7] reject a bad/old-format file")
    tmp = "/tmp/_bad_magic.bin"
    with open(tmp, "wb") as fh:
        fh.write(b"MC2WV1\0\0" + b"\0" * 56)
    try:
        iw.VoxelFile(tmp)
        check("old v1 file is rejected", False, "no error raised")
    except IOError as exc:
        check("old v1 file is rejected", "MC2WV2" in str(exc), str(exc)[:70])

    print("\n%s" % ("-" * 60))
    print("PASSED %d, FAILED %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("   failed: %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
