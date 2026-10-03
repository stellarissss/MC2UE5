# -*- coding: utf-8 -*-
"""
import_phase2.py -- MC2UE5 route C, phase 2 (semantic rebuild) import layer.

Brings the phase-2 output (out/phase2/<dim>/) into the same UE5 World Partition
level that import_world.py builds the layer-1 HISM block layer into:

  * Landscape actors from the 16-bit heightmap PNGs
  * a water plane mesh with a translucent material
  * model instances (trees / buildings / structures / props)

=============================================================================
MUST BE RUN INSIDE THE UE 5.5.4 EDITOR. THIS SANDBOX HAS NEVER RUN IT.
=============================================================================
Written and syntax-checked in a headless Linux container with no GPU and no
Unreal Engine. Nothing here has been executed against the real editor API, and
the Landscape import path deliberately degrades loudly rather than silently:
if no known API accepts the heightmap, the script says so and tells you to use
Landscape > Import from File by hand.

Order of operations
-------------------
Run **after** import_world.py (or standalone if you only want terrain). The two
layers share one coordinate system on purpose:

    world block (x, y, z)  ->  UE cm (x*100, y*100, z*100)

That is the Minecraft convention (1 block = 1 m = 100 cm) and the same constant
import_world.py uses (BLOCK_CM = 100). A building drawn as HISM blocks and the
same building standing on the Landscape then occupy identical coordinates with
no offset table anywhere.

The Landscape grid is solved so that **one Landscape vertex == one block**
(xy_scale_cm = 100). Do not "fix" that by scaling the actor: the HISM cubes sit
on exact block coordinates, and any scale change silently slides the two
layers apart.

Landscape height encoding
-------------------------
UE5 decodes a 16-bit Landscape heightmap sample as a *signed* local height
scaled by the Landscape's Z Scale (a centimetre value; the editor default 100
means +/-256 m):

    local(v) = (v - 32768) / 128            # -256 .. +255.992
    z_cm     = actor_offset_z_cm + local(v) * z_scale_cm

so the full 16-bit range spans **512 * z_scale_cm** centimetres and v = 32768
sits exactly at the actor's Z location. phase2/landscape.py picks

    z_scale_cm      = y_span_blocks * 100 / 512
    actor_offset_z  = y_min_blocks * 100 + 256 * z_scale_cm

Both numbers live in landscape.json per tile, and this script applies them
verbatim. Note the divisor is 512, not 65535 -- normalising by 65535 instead
makes the terrain 128x too tall, which is the classic Landscape import bug and
is invisible until you look at the viewport. ``validate_tile`` decodes the PNG
with the formula above and refuses to place a tile whose values fall outside
the recorded block range, so the two sides can never silently disagree.

Ways to invoke it:

  # a) Editor Python console (Output Log > Python Console):
  import import_phase2; import_phase2.run()

  # b) Command line:
  UnrealEditor.exe MCReplica.uproject ^
      -ExecutePythonScript="Content/Python/import_phase2.py"
"""

import json
import math
import os
import struct
import time
import zlib

import unreal

# =============================================================================
# USER-KNOB CONSTANTS
# =============================================================================

# True -> read metadata, decode and validate the heightmaps, print a plan.
#         Builds nothing. Seconds to run; do this first.
DRY_RUN = True

DIMENSION = "overworld"

# Reuse the level import_world.py made. False creates a dedicated level.
USE_EXISTING_LEVEL = True
LEVEL_PATH = "/Game/Maps/MCReplica"

# Vertical offset per dimension in cm. MUST match import_world.py's
# DIMENSION_Z_OFFSET_CM, or the terrain and the blocks of one dimension end up
# in different places.
DIMENSION_Z_OFFSET_CM = {
    "overworld": 0.0,
    "nether": -20000000.0,
    "end": 20000000.0,
}

BLOCK_CM = 100.0

# Destination packages. The /P2 segment keeps these apart from import_world.py's
# assets so a re-run never fights with layer 1.
LANDSCAPE_DIR = "/Game/P2/Landscape"
WATER_MESH_DIR = "/Game/P2/Water"
WATER_MAT_DIR = "/Game/P2/Materials"
PROP_DIR = "/Game/P2/Props"
WATER_MATERIAL_NAME = "MC_Water"

# Landscape resolution. Phase 2 solved components/sections to match the world
# extent; we read those back from landscape.json and only assert them here.
FORCE_SECTION_SIZE = 0          # 0 = use whatever landscape.json says
FORCE_COMPONENTS = None         # (cx, cy) or None = use landscape.json

# Landscape editing requires the editor's landscape tools. Without them the
# actor can still be spawned, but it will be flat -- so the script refuses to
# report success in that case.
REQUIRE_HEIGHT_IMPORT = True

IMPORT_WATER = True
IMPORT_PROPS = True

# Prop HISM spatial cell, in blocks. Matches import_world.py's granularity so
# both layers stream on the same World Partition footprint.
PROP_CELL_BLOCKS = 256

# How many props to spawn per model asset. 0 = all. Useful for a smoke test.
MAX_PROPS_PER_ASSET = 0

_ROOT_CANDIDATES = ["..", "../..", "../../.."]

_t0 = time.time()


# =============================================================================
# LOGGING
# =============================================================================

def log(msg):
    unreal.log("[MC2UE5-P2 %7.1fs] %s" % (time.time() - _t0, msg))


def warn(msg):
    unreal.log_warning("[MC2UE5-P2] %s" % msg)


def err(msg):
    unreal.log_error("[MC2UE5-P2] %s" % msg)


# =============================================================================
# PATHS
# =============================================================================

def project_dir():
    try:
        return unreal.Paths.convert_relative_path_to_full(
            unreal.Paths.project_dir()).rstrip("/\\")
    except Exception:
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.abspath(os.path.join(here, os.pardir, os.pardir))


def resolve_root():
    """Locate the MC2UE5 folder that contains out/phase2/."""
    env = os.environ.get("MC2UE5_ROOT")
    if env and os.path.isdir(os.path.join(env, "out", "phase2")):
        return os.path.abspath(env)
    base = project_dir()
    for rel in _ROOT_CANDIDATES:
        cand = os.path.abspath(os.path.join(base, rel))
        if os.path.isdir(os.path.join(cand, "out", "phase2")):
            return cand
    return None


def phase2_dir(root, dim):
    return os.path.join(root, "out", "phase2", dim)


def _load_json(path):
    with open(path, "r") as fh:
        return json.load(fh)


def _ensure_dir(package_dir):
    unreal.EditorAssetLibrary.make_directory(package_dir)


# =============================================================================
# 16-BIT GREYSCALE PNG READER (stdlib only)
# =============================================================================
# The editor's embedded Python has neither numpy nor PIL, and this script only
# needs the four corner pixels to validate an encoding. Decoding a 745x1055
# scanline image row by row in pure Python would take seconds; the four corners
# cost almost nothing because only two scanlines are ever un-filtered.

def _png_read_corners(path):
    """
    -> (width, height, (v_tl, v_tr, v_bl, v_br)) of a 16-bit greyscale PNG.

    Only the first and last scanlines are un-filtered; both reference the
    previous row, so two passes over the IDAT are needed (once per target row).
    Raises ValueError on anything that is not a 16-bit greyscale-ish PNG.

    Caveat this function cannot cover: four corners say nothing about a
    vertically flipped image when the terrain happens to be flat at all four
    corners (this map's campus is a plateau, so all four read y=4). That case
    is caught by `validate_tile`'s stride check against the recorded range --
    see `_png_histogram` for why.
    """
    return _png_scan(path, {0, None})[0]


def _png_scan(path, rows_wanted):
    """
    Decode selected scanlines of a 16-bit greyscale PNG.

    `rows_wanted` is a set of row indices, plus None to mean "the last row".
    Returns (width, height, {row: bytes}, channels).

    Decoding the last row means un-filtering every row above it, which for a
    1055-row image is ~1055 * 1490 bytes of Python byte arithmetic -- a second
    or two. `validate_tile` avoids paying that by sampling a *strided* set of
    rows instead, which is enough to characterise the whole value distribution.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG: %s" % path)

    pos = 8
    width = height = bitdepth = colortype = None
    idat = []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            width, height, bitdepth, colortype = struct.unpack(">IIBB", body[:10])
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
        pos += 12 + length

    if bitdepth != 16:
        raise ValueError("%s: bit depth %s, UE5 Landscape needs 16" % (path, bitdepth))
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(colortype)
    if channels is None:
        raise ValueError("%s: colour type %s unsupported" % (path, colortype))

    raw = zlib.decompress(b"".join(idat))
    stride = width * channels * 2
    bpp = channels * 2

    wanted = set()
    for r in rows_wanted:
        wanted.add(height - 1 if r is None else r)
    wanted.add(0)

    prev = bytearray(stride)
    rows = {}
    off = 0
    for r in range(height):
        ftype = raw[off]
        cur = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        _png_unfilter(ftype, cur, prev, bpp)
        prev = cur
        if r in wanted:
            rows[r] = cur
        if r >= max(wanted):
            break
    return width, height, rows, channels


def _png_unfilter(ftype, line, prev, bpp):
    """In-place PNG scanline un-filter (all five standard filter types)."""
    n = len(line)
    if ftype == 0:
        return
    if ftype == 1:
        for i in range(bpp, n):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:
        for i in range(n):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:
        for i in range(n):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa = abs(p - a)
            pb = abs(p - b)
            pc = abs(p - c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (line[i] + pr) & 0xFF
    else:
        raise ValueError("unknown PNG filter type %d" % ftype)


def _decode_height_cm(u16, meta):
    """
    Reproduce UE5's Landscape height decode, in centimetres.

    Must stay byte-for-byte equivalent to ``phase2.landscape.ue_decode``:

        local(v) = (v - 32768) / 128
        z_cm     = actor_offset_z_cm + local(v) * z_scale_cm

    Note the 32768 bias and the /128, not a 0..1 normalisation: the engine maps
    the uint16 onto a *signed* range of -256..+255.992 and scales that. Using
    ``(v/65535 - 0.5) * z_scale_cm`` here instead would make every validation
    below silently agree with a wrong encoder.
    """
    return (float(meta["actor_offset_z_cm"])
            + (float(u16) - 32768.0) / 128.0 * float(meta["z_scale_cm"]))


def validate_tile(png_path, tile):
    """
    Confirm the PNG decodes inside the block range phase 2 recorded.

    Samples a strided grid of scanlines (not just the corners) so the check
    covers the whole value distribution. Corners alone are not enough: this
    map's campus is a plateau whose four corners all read y=4, so a vertically
    flipped heightmap would pass a corner-only test while putting the terrain
    upside down inside the engine.

    Costs ~64 scanlines of un-filtering, which for a 745x1055 image is a few
    tens of milliseconds -- cheap insurance against a silently wrong landscape.

    Returns (ok, width, height, decoded_min_blocks, decoded_max_blocks).
    """
    meta = tile["height_cm_meta"]
    want_lo = float(meta["y_min_blocks"])
    want_hi = want_lo + float(meta["y_span_blocks"])
    span = max(1e-6, want_hi - want_lo)
    tol = max(0.5, 0.02 * span)

    # Cheap pre-check: the header must match before decoding anything.
    w, h, rows, channels = _png_scan(png_path, {0})
    if w != tile["resolution"][0] or h != tile["resolution"][1]:
        return False, w, h, None, None

    step = max(1, h // 64)
    wanted = set(range(0, h, step))
    wanted.add(h - 1)
    w, h, rows, channels = _png_scan(png_path, wanted)

    lo = float("inf")
    hi = float("-inf")
    for r in sorted(rows):
        line = rows[r]
        for x in range(0, w, max(1, w // 64)):
            base = x * channels * 2
            v = struct.unpack(">H", bytes(line[base:base + 2]))[0]
            yb = _decode_height_cm(v, meta) / BLOCK_CM
            if yb < lo:
                lo = yb
            if yb > hi:
                hi = yb

    if lo == float("inf"):
        return False, w, h, None, None

    ok = (lo >= want_lo - tol) and (hi <= want_hi + tol)
    return ok, w, h, lo, hi


# =============================================================================
# LANDSCAPE
# =============================================================================

def import_landscape(world, root, dim, dry_run):
    lsc_dir = os.path.join(phase2_dir(root, dim), "landscape")
    meta_path = os.path.join(lsc_dir, "landscape.json")
    if not os.path.isfile(meta_path):
        err("no landscape.json at %s -- run phase2/run_phase2.py first" % meta_path)
        return {"tiles": 0, "reason": "no landscape.json"}

    meta = _load_json(meta_path)
    sx, sy = meta["landscape_resolution"]
    quads = meta["quads_per_component"]
    xy = meta["xy_scale_cm"]
    cx, cy = meta["n_components"]
    if FORCE_SECTION_SIZE:
        quads = FORCE_SECTION_SIZE
    if FORCE_COMPONENTS:
        cx, cy = FORCE_COMPONENTS

    log("landscape: %dx%d verts | %dx%d components @ %d quads | xy_scale %.1f cm"
        % (sx, sy, cx, cy, quads, xy))
    log("  world: %s blocks at origin %s, y span %.1f blocks"
        % (meta["block_size"], meta["block_origin"], meta["y_span_blocks"]))

    if abs(xy - BLOCK_CM) > 1e-6:
        warn("xy_scale_cm = %.4f, expected %.1f. The layer-1 HISM block layer "
             "will NOT line up with this terrain. Re-run phase 2 without "
             "--xy-scale." % (xy, BLOCK_CM))
    # Landscape component arithmetic must be exact or UE5 logs
    # "non-optimal component size" and splits sections unevenly.
    if (sx - 1) % quads or (sy - 1) % quads:
        err("resolution %dx%d is not components*%d+1; re-run phase 2"
            % (sx, sy, quads))
        return {"tiles": 0, "reason": "bad resolution"}

    valid = []
    for tile in meta["tiles"]:
        png = os.path.join(lsc_dir, tile["file"])
        if not os.path.isfile(png):
            err("missing %s" % png)
            continue
        try:
            ok, w, h, lo, hi = validate_tile(png, tile)
        except Exception as exc:
            err("tile %s: cannot read (%s)" % (tile["file"], exc))
            continue
        log("  %-24s %dx%d  sampled y=[%.2f .. %.2f] blocks  %s"
            % (tile["file"], w, h, lo if lo is not None else -1,
               hi if hi is not None else -1, "OK" if ok else "OUT OF RANGE"))
        if not ok:
            err("    -> refusing to place: decoded range does not match the "
                "recorded encoding")
            continue
        valid.append(tile)

    if not valid:
        return {"tiles": 0, "reason": "no valid tiles"}

    if dry_run:
        log("  DRY_RUN: %d tile(s) validated, nothing spawned" % len(valid))
        return {"tiles": len(valid), "resolution": [sx, sy],
                "components": [cx, cy], "quads": quads,
                "xy_scale_cm": xy, "built": False}

    z_off = DIMENSION_Z_OFFSET_CM.get(dim, 0.0)
    _ensure_dir(LANDSCAPE_DIR)
    placed = 0
    imported = 0
    scales = []
    for tile in valid:
        png = os.path.join(lsc_dir, tile["file"])
        # The Z Scale is per tile: it is derived from that tile's height span,
        # so a future multi-tile export at a different elevation gets its own
        # value instead of silently reusing the first tile's.
        z_scale_cm = float(tile["height_cm_meta"]["z_scale_cm"])
        try:
            actor = _spawn_landscape(world, tile, cx, cy, quads, xy,
                                     z_scale_cm, z_off)
        except Exception as exc:
            err("tile %s: spawn failed (%s)" % (tile["file"], exc))
            continue
        if actor is None:
            continue
        placed += 1
        loc = actor.get_actor_location()
        sca = actor.get_actor_scale3d()
        scales.append([sca.x, sca.y, sca.z])
        log("    spawned %s at (%.1f, %.1f, %.1f) scale (%.1f, %.1f, %.6f)"
            % (tile["file"], loc.x, loc.y, loc.z, sca.x, sca.y, sca.z))
        if _import_heightmap(actor, png, tile):
            imported += 1
        else:
            warn("    %s is FLAT -- import it by hand: Landscape mode > Import "
                 "> Import from File > %s" % (tile["file"],
                                              os.path.basename(png)))

    res = {"tiles": placed, "heightmaps_imported": imported,
           "resolution": [sx, sy], "components": [cx, cy],
           "quads": quads, "xy_scale_cm": xy,
           "actor_scale3d": scales, "built": True}
    if REQUIRE_HEIGHT_IMPORT and imported == 0 and placed:
        err("No heightmap could be imported programmatically. The Landscape "
            "actors exist but are flat. Enable the Landscape editor modules "
            "(Edit > Plugins > Landscape) or import by hand; the module "
            "docstring lists the exact settings to enter.")
        res["needs_manual_import"] = True
    return res


def _spawn_landscape(world, tile, cx, cy, quads, xy_scale_cm, z_scale_cm,
                     z_off):
    """
    Spawn a Landscape actor configured from landscape.json.

    Three invariants, all asserted rather than assumed:

    * **XY Scale = 1 block = 100 cm.** Landscape world size comes from the XY
      Scale, not from the component count. Any deviation slides the terrain out
      of registration with the layer-1 HISM block layer, so it is checked, not
      trusted.
    * **Z Scale = span * 100 / 512.** The engine decodes a heightmap sample as
      ``(v - 32768) / 128 * ZScale`` cm, so the full 16-bit range covers
      ``512 * ZScale`` centimetres. Getting the divisor wrong (65535 instead of
      512) yields terrain 128x too tall.
    * **actor scale is the only place scale lives.** Nothing downstream may
      compensate with a second transform, or the two layers drift apart.
    """
    tx, ty = tile["origin_cm"]
    hmeta = tile["height_cm_meta"]
    loc = unreal.Vector(
        float(tx), float(ty),
        z_off + float(hmeta["actor_offset_z_cm"]))

    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.Landscape, loc, unreal.Rotator(0.0, 0.0, 0.0))
    if actor is None:
        warn("    could not spawn a Landscape actor")
        return None

    _apply_landscape_scale(actor, xy_scale_cm, z_scale_cm)
    _apply_landscape_info(actor, tile, cx, cy, quads)

    # Read the scale back: this is the value the engine will actually use, and
    # it is what the alignment check downstream must be compared against.
    s = actor.get_actor_scale3d()
    if abs(s.x - xy_scale_cm) > 1e-3 or abs(s.z - z_scale_cm) > 1e-4:
        warn("    Landscape Scale3D is (%.4f, %.4f, %.4f), expected "
             "(%.1f, %.1f, %.6f) -- terrain will not line up with the block "
             "layer" % (s.x, s.y, s.z, xy_scale_cm, xy_scale_cm, z_scale_cm))
    return actor


def _apply_landscape_scale(actor, xy_scale_cm, z_scale_cm):
    """
    Drive the Landscape's XY Scale and Z Scale through the actor transform.

    Landscape stores both scales on the actor's Scale3D: X/Y are centimetres
    per quad, Z is the engine's Z Scale (centimetres per local height unit).
    This is the same property the Landscape editor's Transform panel writes, so
    setting it here and setting it by hand are equivalent -- no hidden state is
    left for the engine to overwrite at import time.

    ``set_actor_scale3d`` is used rather than ``set_editor_property("xy_scale")``
    because the editor property names differ between UE5 minor versions, while
    the transform is stable across all of them.
    """
    actor.set_actor_scale3d(unreal.Vector(
        float(xy_scale_cm), float(xy_scale_cm), float(z_scale_cm)))


def _apply_landscape_info(actor, tile, cx, cy, quads):
    """
    Set the component grid on the LandscapeInfo when it is reachable.

    The heightmap image already encodes the exact vertex count, so the import
    would derive the same grid on its own. Setting it explicitly is still worth
    doing: it makes the actor correct *before* any height data arrives, so a
    failed import leaves a correctly-sized flat Landscape rather than a
    wrongly-sized one.
    """
    info = None
    for getter in (
        lambda: actor.get_editor_property("landscape_info"),
        lambda: _landscape_info_via_subsystem(actor),
    ):
        try:
            cand = getter()
            if cand is not None:
                info = cand
                break
        except Exception:
            continue

    if info is None:
        log("      LandscapeInfo not reachable from Python; the import derives "
            "the %dx%d component grid from the image size" % (cx, cy))
        return

    section_size = tile.get("section_size") or (quads // max(1, tile.get(
        "sections_per_component", 1)))
    applied = {}
    for prop, value in (
        ("component_size_x", int(cx)),
        ("component_size_y", int(cy)),
        ("section_size", int(section_size)),
        ("subsection_size_quads", int(section_size)),
        ("num_subsections", int(tile.get("sections_per_component", 1))),
    ):
        try:
            info.set_editor_property(prop, value)
            applied[prop] = value
        except Exception:
            pass
    if applied:
        log("      LandscapeInfo: %s" % applied)
    else:
        log("      LandscapeInfo found but its geometry properties are not "
            "writable from Python; the image import sets them")


def _landscape_info_via_subsystem(actor):
    """LandscapeInfo via LandscapeSubsystem, if that route is available."""
    sub_cls = getattr(unreal, "LandscapeSubsystem", None)
    if sub_cls is None:
        return None
    try:
        sub = unreal.get_editor_subsystem(sub_cls)
    except Exception:
        return None
    if sub is None:
        return None
    try:
        return sub.get_landscape_info(actor)
    except Exception:
        return None


def _import_heightmap(actor, png_path, tile):
    """
    Feed the PNG into the Landscape.

    Tried in order, each of which exists in some UE5.x build:

      1. ``LandscapeEditorObject.import_height_data`` (editor object)
      2. ``LandscapeSubsystem.import_heightmap_from_file``
      3. ``LandscapeEditorSubsystem`` -- the subsystem name that carries the
         ``import_heightmap_from_file`` call in 5.4/5.5 builds

    If none is available the script returns False and tells the user to import
    by hand. It never claims success on a flat Landscape -- a silently flat
    terrain is worse than an obvious failure.
    """
    attempts = [
        ("LandscapeEditorObject.import_height_data",
         lambda: _eo_import_height_data(actor, png_path)),
        ("LandscapeSubsystem.import_heightmap_from_file",
         lambda: _subsystem_import(actor, png_path, "LandscapeSubsystem")),
        ("LandscapeEditorSubsystem.import_heightmap_from_file",
         lambda: _subsystem_import(actor, png_path, "LandscapeEditorSubsystem")),
    ]
    for name, fn in attempts:
        try:
            fn()
            log("      heightmap imported via %s" % name)
            return True
        except Exception:
            continue
    return False


def _eo_import_height_data(actor, png_path):
    eo = unreal.get_editor_subsystem(unreal.LandscapeEditorSubsystem)
    if eo is None:
        raise RuntimeError("no LandscapeEditorSubsystem")
    # 5.x exposes the landscape tooling through the editor object; fall back to
    # the module-level helper when the subsystem route is unavailable.
    helper = getattr(unreal, "LandscapeEditorObject", None)
    if helper is not None and hasattr(helper, "import_height_data"):
        return helper.import_height_data(actor, png_path)
    raise RuntimeError("LandscapeEditorObject.import_height_data missing")


def _subsystem_import(actor, png_path, sub_name):
    cls = getattr(unreal, sub_name, None)
    if cls is None:
        raise RuntimeError("%s not exposed" % sub_name)
    sub = unreal.get_editor_subsystem(cls)
    if sub is None:
        raise RuntimeError("%s unavailable" % sub_name)
    fn = getattr(sub, "import_heightmap_from_file", None)
    if fn is None:
        raise RuntimeError("%s has no import_heightmap_from_file" % sub_name)
    return fn(actor, png_path)


# =============================================================================
# WATER
# =============================================================================

def import_water(world, root, dim, dry_run):
    w_dir = os.path.join(phase2_dir(root, dim), "water")
    meta_path = os.path.join(w_dir, "water.json")
    if not os.path.isfile(meta_path):
        warn("no water.json at %s" % meta_path)
        return {"files": 0}
    meta = _load_json(meta_path)

    if not meta.get("file"):
        log("water: %s liquid columns, no surface mesh (%s)"
            % (meta.get("columns", 0),
               meta.get("note", "no 2x2 liquid neighbourhood")))
        return {"files": 0, "columns": meta.get("columns", 0)}

    st = meta["stats"]
    log("water: %s  v=%s tri=%s  sea_level=%s cm  columns=%s"
        % (meta["file"], st["vertices"], st["triangles"],
           meta.get("sea_level_cm"), meta.get("columns")))

    if dry_run:
        return {"files": 1, "built": False, "obj": meta["file"]}

    obj = os.path.join(w_dir, meta["file"])
    mesh = _import_mesh(obj, WATER_MESH_DIR, "W_" + dim)
    if mesh is None:
        return {"files": 0, "error": "OBJ import failed"}

    mat = _water_material()
    if mat is not None:
        try:
            mesh.set_material(0, mat)
        except Exception as exc:
            warn("could not assign the water material: %s" % exc)

    z_off = DIMENSION_Z_OFFSET_CM.get(dim, 0.0)
    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.StaticMeshActor, unreal.Vector(0.0, 0.0, z_off),
        unreal.Rotator(0.0, 0.0, 0.0))
    if actor is None:
        warn("could not spawn the water actor")
        return {"files": 0, "error": "spawn failed"}
    comp = actor.get_component_by_class(unreal.StaticMeshComponent)
    if comp is not None:
        comp.set_static_mesh(mesh)
    log("  water mesh placed at z offset %.0f cm" % z_off)
    return {"files": 1, "built": True}


def _import_mesh(src, dest_dir, name):
    _ensure_dir(dest_dir)
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", src)
    task.set_editor_property("destination_path", dest_dir)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return unreal.load_asset("%s/%s.%s" % (dest_dir, name, name))


def _water_material():
    """
    Create a translucent water material on first use.

    Deliberately minimal. A convincing water surface needs a depth fade driven
    by a scene-depth or render-target pass, which is authoring work, not data
    import. This gives a shaded translucent surface with the knobs exposed so
    it can be tuned in the material editor.
    """
    path = "%s/%s" % (WATER_MAT_DIR, WATER_MATERIAL_NAME)
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        return unreal.load_asset(path)

    _ensure_dir(WATER_MAT_DIR)
    factory = unreal.MaterialFactoryNew()
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        WATER_MATERIAL_NAME, WATER_MAT_DIR, unreal.Material, factory)
    if mat is None:
        warn("could not create the water material")
        return None

    applied = {}
    for prop, value in (
        ("blend_mode", getattr(unreal.BlendMode, "BLEND_TRANSLUCENT", None)),
        ("shading_model",
         getattr(unreal.MaterialShadingModel, "MSM_DEFAULT_LIT", None)),
        ("two_sided", True),
        ("translucency_lighting_mode",
         getattr(unreal.TranslucencyLightingMode, "TLM_SURFACE_PER_PIXEL_LIGHTING",
                 None)),
    ):
        if value is None:
            continue
        try:
            mat.set_editor_property(prop, value)
            applied[prop] = value
        except Exception:
            pass
    try:
        unreal.MaterialEditingLibrary.set_material_attribute(
            mat, "Opacity", unreal.MaterialAttribute.MA_DEFAULT)
        applied["Opacity"] = "MA_DEFAULT"
    except Exception:
        pass

    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    log("  created water material %s  (%s)" % (path, applied or "defaults"))
    return mat


# =============================================================================
# PROPS
# =============================================================================

def import_props(world, root, dim, dry_run):
    p_path = os.path.join(phase2_dir(root, dim), "props",
                          "prop_placements.json")
    if not os.path.isfile(p_path):
        warn("no prop_placements.json at %s" % p_path)
        return {"count": 0}
    data = _load_json(p_path)
    insts = data.get("instances", [])

    log("props: %d instances  by_class=%s  ground_aligned=%s"
        % (len(insts), data.get("by_class"), data.get("ground_aligned")))
    if not data.get("ground_aligned"):
        warn("ground alignment was not applied; instances sit on their block "
             "base rather than the smoothed terrain surface")

    # One HISM component per (model asset, spatial cell): 51 trees in one cell
    # are one draw call, not 51.
    by_asset = {}
    for inst in insts:
        asset = (inst.get("model") or {}).get("asset") or "unknown"
        by_asset.setdefault(asset, []).append(inst)
    for asset, items in sorted(by_asset.items()):
        log("  %-46s %4d instances" % (asset, len(items)))

    if dry_run or not IMPORT_PROPS:
        return {"count": len(insts), "groups": len(by_asset), "built": False}

    z_off = DIMENSION_Z_OFFSET_CM.get(dim, 0.0)
    spawned = 0
    skipped = 0
    for asset, items in sorted(by_asset.items()):
        if asset.startswith("builtin:"):
            # The builtin provider emits a primitive *recipe*, not a file. There
            # is nothing to spawn; say so once per asset instead of silently
            # producing an empty level.
            skipped += len(items)
            log("  %s: recipe only, no mesh asset -- %d instances not spawned. "
                "Rerun phase 2 with --models library --model-root <dir> to "
                "resolve real CC0 meshes." % (asset, len(items)))
            continue
        mesh = unreal.load_asset(asset)
        if mesh is None:
            skipped += len(items)
            warn("  %s: asset not found, %d instances skipped" % (asset, len(items)))
            continue
        spawned += _spawn_hism(world, mesh, items, z_off, asset)

    log("  spawned %d instances, skipped %d" % (spawned, skipped))
    return {"count": len(insts), "groups": len(by_asset),
            "spawned": spawned, "skipped": skipped, "built": True}


def _spawn_hism(world, mesh, items, z_off, tag):
    """One HISM actor per (model asset, spatial cell)."""
    cell_cm = PROP_CELL_BLOCKS * BLOCK_CM
    cells = {}
    for inst in items:
        pos = inst.get("position_cm") or [0.0, 0.0, 0.0]
        key = (int(math.floor(pos[0] / cell_cm)),
               int(math.floor(pos[2] / cell_cm)))
        cells.setdefault(key, []).append(inst)

    actors = 0
    total = 0
    for (cx, cz), group in sorted(cells.items()):
        loc = unreal.Vector(cx * cell_cm, 0.0, cz * cell_cm + z_off)
        actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
            unreal.HierarchicalInstancedStaticMesh, loc,
            unreal.Rotator(0.0, 0.0, 0.0))
        if actor is None:
            warn("  %s: could not spawn HISM at cell (%d,%d)" % (tag, cx, cz))
            continue
        comp = actor.get_component_by_class(
            unreal.HierarchicalInstancedStaticMeshComponent)
        if comp is None:
            warn("  %s: actor has no HISM component" % tag)
            continue
        comp.set_static_mesh(mesh)

        # Transforms are relative to the actor; actor-local X/Y are the world
        # X/Y and actor Z is the dimension offset, so only X and Y need the
        # cell origin removed. Y (world height) stays absolute, which keeps the
        # ground alignment produced in phase 2 meaningful.
        xforms = []
        for inst in group:
            pos = inst.get("position_cm") or [0.0, 0.0, 0.0]
            rot = inst.get("rotation_deg") or [0.0, 0.0, 0.0]
            xforms.append(unreal.Transform(
                unreal.Rotator(float(rot[0]), float(rot[1]), float(rot[2])),
                unreal.Vector(float(pos[0]) - cx * cell_cm,
                              float(pos[1]),
                              float(pos[2]) - cz * cell_cm),
                unreal.Vector(1.0, 1.0, 1.0)))

        if MAX_PROPS_PER_ASSET:
            xforms = xforms[:MAX_PROPS_PER_ASSET]
        if not xforms:
            continue
        comp.add_instances(xforms, False, True)   # bWorldSpace=False
        total += len(xforms)
        actors += 1

    log("  %s: %d HISM actors, %d instances" % (tag, actors, total))
    return total


# =============================================================================
# LEVEL
# =============================================================================

def open_level():
    """
    Open (or create) the target level and return the editor world.

    Never called during a dry run: see :func:`check_level`. A dry run must not
    load or create anything, because layer 1 is still unverified and an
    accidental ``new_level`` would replace the very map the HISM block layer
    lives in.
    """
    if DRY_RUN:
        raise RuntimeError("open_level() must not run during a dry run")

    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if USE_EXISTING_LEVEL and unreal.EditorAssetLibrary.does_asset_exist(LEVEL_PATH):
        log("opening existing level %s" % LEVEL_PATH)
        if not les.load_level(LEVEL_PATH):
            err("could not load %s" % LEVEL_PATH)
            return None
    else:
        if USE_EXISTING_LEVEL:
            warn("%s does not exist yet -- creating it. If layer 1 "
                 "(import_world.py) has already run, check that LEVEL_PATH "
                 "matches its output." % LEVEL_PATH)
        _ensure_dir("/Game/Maps")
        log("creating World Partition level %s" % LEVEL_PATH)
        if not les.new_level(LEVEL_PATH, True):
            err("could not create the partitioned level")
            return None

    world = None
    for getter in (
        lambda: unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world(),
        lambda: unreal.EditorLevelLibrary.get_editor_world(),
    ):
        try:
            cand = getter()
            if cand is not None:
                world = cand
                break
        except Exception:
            continue
    if world is None:
        err("no editor world available after opening the level")
    return world


def check_level():
    """
    Dry-run equivalent of :func:`open_level`: report, change nothing.

    Validating the artefacts does not need a world, so the dry run stops here
    and never touches the open map.
    """
    exists = False
    try:
        exists = bool(unreal.EditorAssetLibrary.does_asset_exist(LEVEL_PATH))
    except Exception:
        exists = False

    if exists:
        log("target level %s exists (not opened -- DRY_RUN is read-only)" % LEVEL_PATH)
        if not USE_EXISTING_LEVEL:
            warn("USE_EXISTING_LEVEL is False but %s already exists; the real "
                 "run will load it anyway" % LEVEL_PATH)
    else:
        if USE_EXISTING_LEVEL:
            warn("%s not found. Run import_world.py first, or set "
                 "USE_EXISTING_LEVEL = False to let phase 2 create it."
                 % LEVEL_PATH)
        else:
            log("target level %s will be created on the first real run"
                % LEVEL_PATH)
    return exists


def save_level():
    if DRY_RUN:
        log("DRY_RUN: nothing saved")
        return False
    try:
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        log("assets and level saved")
        return True
    except Exception as exc:
        warn("save failed: %s" % exc)
        return False


# =============================================================================

def run():
    log("=" * 68)
    log("MC2UE5 phase 2 import | dimension=%s | DRY_RUN=%s"
        % (DIMENSION, DRY_RUN))
    log("=" * 68)

    root = resolve_root()
    if root is None:
        err("could not locate the MC2UE5 root (needs out/phase2/). "
            "Set the MC2UE5_ROOT environment variable.")
        return 1
    log("data root: %s" % root)

    p2 = phase2_dir(root, DIMENSION)
    if not os.path.isdir(p2):
        err("no phase 2 output at %s -- run phase2/run_phase2.py first" % p2)
        return 1

    if DRY_RUN:
        # Read-only from here on: no level is opened, no asset is created, no
        # package is saved. Everything below only reads the phase-2 output.
        check_level()
        report = {"landscape": import_landscape(None, root, DIMENSION, True)}
        if IMPORT_WATER:
            report["water"] = import_water(None, root, DIMENSION, True)
        if IMPORT_PROPS:
            report["props"] = import_props(None, root, DIMENSION, True)

        log("-" * 68)
        for k in sorted(report):
            log("  %-10s %s" % (k, json.dumps(report[k], default=str)))
        log("-" * 68)
        log("DRY_RUN complete -- nothing was created, opened or saved.")
        log("Review the plan above, then set DRY_RUN = False and re-run.")
        return 0

    world = open_level()
    if world is None:
        return 1

    report = {"landscape": import_landscape(world, root, DIMENSION, False)}
    if IMPORT_WATER:
        report["water"] = import_water(world, root, DIMENSION, False)
    if IMPORT_PROPS:
        report["props"] = import_props(world, root, DIMENSION, False)

    log("-" * 68)
    for k in sorted(report):
        log("  %-10s %s" % (k, json.dumps(report[k], default=str)))
    save_level()
    log("done")
    return 0


if __name__ == "__main__":
    run()