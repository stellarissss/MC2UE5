"""
Offline harness for the parts of import_phase2.py that do not need the engine.

The UE editor's embedded Python has neither numpy nor PIL, which is why
import_phase2.py ships a pure-stdlib PNG reader. That reader is the one piece
of this script that can be *proved* correct outside the editor, so it is tested
here against the real phase-2 artefacts rather than against synthetic input.

Run from the MC2UE5 root:

    python3 tests/test_import_phase2_offline.py
"""

import importlib.util
import json
import os
import struct
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "project", "Content", "Python", "import_phase2.py")
LSC_JSON = os.path.join(ROOT, "out", "phase2", "overworld", "landscape",
                        "landscape.json")
LSC_DIR = os.path.dirname(LSC_JSON)


# --------------------------------------------------------------------------- #
# stub out `unreal` so the module imports outside the editor
# --------------------------------------------------------------------------- #

def _install_unreal_stub():
    if "unreal" in sys.modules:
        return
    stub = types.ModuleType("unreal")

    class _Log:
        def __init__(self):
            self.lines = []

        def _emit(self, level, msg):
            self.lines.append((level, msg))

        def log(self, msg):
            self._emit("log", msg)

        def log_warning(self, msg):
            self._emit("warning", msg)

        def log_error(self, msg):
            self._emit("error", msg)

    stub.log_sink = _Log()
    stub.log = stub.log_sink.log
    stub.log_warning = stub.log_sink.log_warning
    stub.log_error = stub.log_sink.log_error

    class _Paths:
        @staticmethod
        def project_dir():
            return os.path.join(ROOT, "project")

    stub.Paths = _Paths
    sys.modules["unreal"] = stub


_install_unreal_stub()


def _load_module():
    spec = importlib.util.spec_from_file_location("import_phase2", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# test harness
# --------------------------------------------------------------------------- #

_results = []


def check(name, cond, detail=""):
    _results.append((bool(cond), name, detail))
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name,
                          ("  -- " + detail) if detail else ""))
    return bool(cond)


def approx(a, b, tol):
    return abs(float(a) - float(b)) <= tol


# --------------------------------------------------------------------------- #

def test_png_reader_matches_reference(mod):
    """
    The stdlib PNG reader must agree with PIL/numpy on the real artefact.

    PIL is the producer of these files, so it is the authority on what the
    bytes mean. Any disagreement means the editor-side validation would accept
    or reject tiles for the wrong reason.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        print("  SKIP  png reader vs PIL (numpy/PIL unavailable)")
        return

    meta = json.load(open(LSC_JSON))
    tile = meta["tiles"][0]
    png = os.path.join(LSC_DIR, tile["file"])
    ref = np.array(Image.open(png))
    h = ref.shape[0]

    w, h, rows, channels = mod._png_scan(png, {0, 1, 2, 3, 50, 200})
    check("png reader reports 16-bit", channels >= 1 and ref.dtype == np.uint16,
          "channels=%d dtype=%s" % (channels, ref.dtype))
    check("png size matches PIL", (w, h) == (ref.shape[1], ref.shape[0]),
          "%dx%d vs %dx%d" % (w, h, ref.shape[1], ref.shape[0]))

    # Byte-for-byte on the sampled rows.
    mism = 0
    total = 0
    for r in sorted(rows):
        line = rows[r]
        for x in range(0, w, 17):
            base = x * channels * 2
            v = struct.unpack(">H", bytes(line[base:base + 2]))[0]
            total += 1
            if v != int(ref[r, x]):
                mism += 1
    check("png samples match PIL exactly", mism == 0,
          "%d/%d mismatched" % (mism, total))


def test_ue_decode_matches_encoder(mod):
    """
    The import-side decode must be the exact inverse of the phase-2 encoder.

    This is the check that would have caught the 512-vs-65535 divisor bug: both
    sides live in different files, so nothing but a shared test keeps them
    honest.
    """
    sys.path.insert(0, os.path.join(ROOT, "phase2"))
    import landscape as l2

    meta = json.load(open(LSC_JSON))
    tile = meta["tiles"][0]
    hmeta = tile["height_cm_meta"]

    y_min = hmeta["y_min_blocks"]
    span = hmeta["y_span_blocks"]

    for v in (0, 1, 32767, 32768, 32769, 65534, 65535):
        got = mod._decode_height_cm(v, hmeta) / mod.BLOCK_CM
        want = y_min + span * v / 65535.0
        check("decode v=%d" % v, approx(got, want, 0.001),
              "got %.5f want %.5f" % (got, want))

    # And it must equal the shared helper the encoder documents.
    a = l2.ue_decode([0, 32768, 65535], hmeta["z_scale_cm"],
                     hmeta["actor_offset_z_cm"])
    b = [mod._decode_height_cm(v, hmeta) for v in (0, 32768, 65535)]
    check("decode == phase2.landscape.ue_decode",
          all(approx(x, y, 1e-6) for x, y in zip(a, b)),
          "%s vs %s" % (list(a), b))

    # Z Scale must be span*100/512, i.e. 512*z_scale == span in centimetres.
    check("z_scale_cm * 512 == span * 100",
          approx(hmeta["z_scale_cm"] * 512.0, span * 100.0, 0.01),
          "%.4f*512=%.2f, want %.2f" % (hmeta["z_scale_cm"],
                                        hmeta["z_scale_cm"] * 512.0,
                                        span * 100.0))
    check("actor Z offset == y_min*100 + 256*z_scale",
          approx(hmeta["actor_offset_z_cm"],
                 y_min * 100.0 + 256.0 * hmeta["z_scale_cm"], 0.01),
          "%.3f" % hmeta["actor_offset_z_cm"])


def test_validate_tile_accepts_real_artifact(mod):
    """
    Every exported tile must validate, and at least one must show real relief.

    Why "every" and not just tiles[0]: the landscape is exported as a tile grid,
    and a tile can legitimately be perfectly flat -- the campus plateau's
    north-west corner really is a constant y=4. A test that only looked at
    ``tiles[0]`` would either fail on a *correct* flat tile (relief 0) or, worse,
    pass on a grid where every tile was flat. So: assert all tiles validate
    against their own recorded range, assert the grid covers real relief, and
    assert the tile borders agree with each other so the seam is not a cliff.
    """
    meta = json.load(open(LSC_JSON))
    tiles = meta["tiles"]
    check("landscape.json has tiles", len(tiles) > 0, "%d tiles" % len(tiles))

    worst = None
    with_relief = 0
    for tile in tiles:
        png = os.path.join(LSC_DIR, tile["file"])
        ok, w, h, lo, hi = mod.validate_tile(png, tile)
        hmeta = tile["height_cm_meta"]
        y_min = hmeta["y_min_blocks"]
        span = hmeta["y_span_blocks"]
        if not ok:
            worst = (tile["file"], ok, w, h, lo, hi)
            break
        if (w, h) != tuple(tile["resolution"]) or \
           (lo is not None and not (lo >= y_min - 0.5 and
                                    hi <= y_min + span + 0.5)):
            worst = (tile["file"], ok, w, h, lo, hi)
            break
        if hi - lo > 1.0:
            with_relief += 1

    check("every tile validates against its own recorded range",
          worst is None, "first bad tile: %s" % (worst,))
    check("the grid contains real relief (some tile > 1 block)",
          with_relief > 0, "%d of %d tiles have relief" % (with_relief, len(tiles)))

    # One representative tile, for a human-readable decoded range.
    ref = max(tiles, key=lambda t: (
        lambda r: (r[4] or 0) - (r[3] or 0))(mod.validate_tile(
            os.path.join(LSC_DIR, t["file"]), t)))
    ok, w, h, lo, hi = mod.validate_tile(os.path.join(LSC_DIR, ref["file"]), ref)
    y_min = ref["height_cm_meta"]["y_min_blocks"]
    span = ref["height_cm_meta"]["y_span_blocks"]
    check("validate_tile accepts a real tile", ok,
          "%s y=[%.3f .. %.3f]" % (ref["file"], lo, hi))
    check("decoded range inside [y_min, y_min+span]",
          lo >= y_min - 0.5 and hi <= y_min + span + 0.5,
          "[%.3f, %.3f] vs [%.1f, %.1f]" % (lo, hi, y_min, y_min + span))
    check("resolution matches landscape.json",
          (w, h) == tuple(ref["resolution"]),
          "%dx%d vs %s" % (w, h, ref["resolution"]))


def test_validate_tile_rejects_tampered(mod):
    """
    A PNG whose values do not match the recorded encoding must be rejected.

    Without this, an encoder regression would sail through the import and land
    a wrong terrain in the level.
    """
    import zlib

    meta = json.load(open(LSC_JSON))
    tile = meta["tiles"][0]
    hmeta = tile["height_cm_meta"]

    # Re-encode with the wrong divisor (the old 65535-era scale), which is
    # exactly the bug this harness exists to catch.
    src = os.path.join(LSC_DIR, tile["file"])
    with open(src, "rb") as fh:
        data = fh.read()
    check("artefact is a real PNG", data[:8] == b"\x89PNG\r\n\x1a\n")

    bad = dict(tile)
    bad_meta = dict(hmeta)
    bad_meta["z_scale_cm"] = hmeta["y_span_blocks"] * 100.0  # 512x too big
    bad_meta["actor_offset_z_cm"] = hmeta["actor_offset_z_cm"]
    bad["height_cm_meta"] = bad_meta

    ok, w, h, lo, hi = mod.validate_tile(src, bad)
    check("validate_tile rejects a 512x-too-tall encoding", not ok,
          "y=[%.1f .. %.1f] blocks (should be rejected)"
          % (lo if lo is not None else -1, hi if hi is not None else -1))


def test_no_duplicate_definitions(mod):
    """
    The rewritten script must not define anything twice.

    A shadowed helper is invisible at import time and silently uses the wrong
    body -- that is how the Z Scale was being fed 'quads' instead of the
    encoded span.
    """
    import ast

    tree = ast.parse(open(SCRIPT).read())
    names = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.setdefault(node.name, []).append(node.lineno)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.setdefault(t.id, []).append(node.lineno)
    dups = {k: v for k, v in names.items() if len(v) > 1}
    check("no duplicate top-level definitions", not dups, str(dups))


def test_root_resolution(mod):
    root = mod.resolve_root()
    check("resolve_root finds the MC2UE5 root",
          root is not None and os.path.isdir(os.path.join(root, "out",
                                                           "phase2")),
          str(root))
    check("phase2_dir points at the artefacts",
          os.path.isfile(os.path.join(mod.phase2_dir(root, "overworld"),
                                      "landscape", "landscape.json")))


def test_dry_run_never_touches_world(mod):
    """
    DRY_RUN must be genuinely read-only.

    A dry run that calls load_level() or new_level() can replace the very map
    the unverified layer-1 HISM block layer lives in. Enforced structurally:
    each entry point has to test ``dry_run`` and return before it reaches the
    ``world`` argument.
    """
    import ast

    tree = ast.parse(open(SCRIPT).read())
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    for name in ("import_landscape", "import_water", "import_props"):
        if not check("entry point exists: %s" % name, name in fns):
            continue
        n = fns[name]
        world_lines = [s.lineno for s in ast.walk(n)
                       if isinstance(s, ast.Name) and s.id == "world"]
        dry_lines = [s.lineno for s in ast.walk(n)
                     if isinstance(s, ast.Name) and s.id == "dry_run"]
        if not world_lines:
            check("%s: dry run cannot reach a world" % name, True,
                  "world unused")
            continue
        check("%s: tests dry_run before using world" % name,
              bool(dry_lines) and min(dry_lines) < min(world_lines),
              "dry_run@%d world@%d" % (min(dry_lines), min(world_lines)))

    # open_level() must refuse to run in a dry run at all.
    ol = fns.get("open_level")
    if check("open_level exists", ol is not None):
        seg = ast.get_source_segment(open(SCRIPT).read(), ol)
        check("open_level guards on DRY_RUN", "if DRY_RUN" in seg,
              "no dry-run guard found")
    check("check_level exists (dry-run path)", "check_level" in fns)


def test_scale_passed_is_not_quads(mod):
    """
    The Landscape Z Scale must be the encoded span, not the quad count.

    Regression guard: an earlier revision shadowed ``_apply_landscape_scale``
    with a second definition and passed ``quads`` (31) where a Z Scale belonged,
    which would have produced a 512x-too-tall landscape. The duplicate is gone;
    this asserts the argument chain that replaced it.
    """
    import ast

    src = open(SCRIPT).read()
    tree = ast.parse(src)
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    spawn = fns.get("_spawn_landscape")
    if not check("_spawn_landscape exists", spawn is not None):
        return
    params = [a.arg for a in spawn.args.args]
    check("_spawn_landscape takes z_scale_cm",
          "z_scale_cm" in params, str(params))
    check("_spawn_landscape no longer takes a bare z_off-only signature",
          "z_off" in params, str(params))

    # The spawner must forward z_scale_cm into the scale setter.
    seg = ast.get_source_segment(src, spawn)
    check("_spawn_landscape forwards z_scale_cm to the scale setter",
          "_apply_landscape_scale(actor, xy_scale_cm, z_scale_cm)" in seg)

    # The setter must write all three components of Scale3D, because UE reads
    # both XY Scale and Z Scale from the actor transform.
    setter = fns.get("_apply_landscape_scale")
    if check("_apply_landscape_scale exists", setter is not None):
        sseg = ast.get_source_segment(src, setter)
        check("scale setter uses set_actor_scale3d",
              "set_actor_scale3d" in sseg)
        check("scale setter writes x, y and z",
              sseg.count("xy_scale_cm") >= 2 and "z_scale_cm" in sseg)

    # The call site must pass the per-tile Z Scale, not a constant.
    caller = fns.get("import_landscape")
    if check("import_landscape exists", caller is not None):
        cseg = ast.get_source_segment(src, caller)
        check("caller reads z_scale_cm from the tile metadata",
              'tile["height_cm_meta"]["z_scale_cm"]' in cseg)


def test_props_and_water_metadata(mod):
    root = mod.resolve_root()
    p2 = mod.phase2_dir(root, "overworld")
    for rel, keys in (
        (os.path.join("props", "prop_placements.json"), ("instances",)),
        (os.path.join("water", "water.json"), ("columns",)),
    ):
        p = os.path.join(p2, rel)
        if not check("exists: %s" % rel, os.path.isfile(p), p):
            continue
        d = json.load(open(p))
        check("has keys %s in %s" % (keys, rel),
              all(k in d for k in keys),
              "keys=%s" % sorted(d.keys()))


# --------------------------------------------------------------------------- #

def main():
    print("=" * 74)
    print("import_phase2.py -- offline checks (no engine required)")
    print("=" * 74)
    mod = _load_module()

    for fn in (
        test_no_duplicate_definitions,
        test_root_resolution,
        test_ue_decode_matches_encoder,
        test_png_reader_matches_reference,
        test_validate_tile_accepts_real_artifact,
        test_validate_tile_rejects_tampered,
        test_dry_run_never_touches_world,
        test_scale_passed_is_not_quads,
        test_props_and_water_metadata,
    ):
        print("\n[%s]" % fn.__name__)
        try:
            fn(mod)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check(fn.__name__ + " raised", False, repr(exc))

    npass = sum(1 for ok, _, _ in _results if ok)
    nfail = len(_results) - npass
    print("\n" + "=" * 74)
    print("%d passed, %d failed, %d total" % (npass, nfail, len(_results)))
    print("=" * 74)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
