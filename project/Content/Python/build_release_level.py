# -*- coding: utf-8 -*-
"""
build_release_level.py -- assemble the shippable MC2UE5 level.

The pipeline in this repository produces two layers. Layer 1 (the HISM block
layer) needs ``voxel_data/full/*.bin``, a 144 MB export that is deliberately
not committed -- it is rebuildable from the Minecraft save, and the save is
the source of truth. Layer 2 (the semantic rebuild) committed its output:
``out/phase2/overworld/`` holds the campus terrain as four 16-bit heightmap
PNGs plus the encoding metadata.

This script builds the release level out of layer 2, the part that can be
reconstructed from the repository alone: real terrain at 1 vertex per block,
textured from the Minecraft block set, with a pawn that can walk it.

Terrain representation, and why it is a StaticMesh
--------------------------------------------------
The upstream scripts build the terrain as UE5 Landscape actors through
``LandscapeEditorSubsystem``. On UE 5.8 none of that API is reachable from
Python -- the editor subsystems, ``LandscapeInfo`` and the heightmap import
functions are all absent, and ``unreal.Landscape`` spawns a componentless
``LandscapePlaceholder`` because ``ALandscapeProxy::CreateLandscapeInfo`` has
no Python binding. So the heightmaps are converted to meshes by
``tools/build_terrain_mesh.py`` and imported here instead.

The coordinate contract is preserved exactly: 1 vertex = 1 block, XY scale
100 cm, heights from UE's own decode
``z = offset + (v - 32768) / 128 * ZScale``. The surface therefore occupies
the same world coordinates the Landscape would have, and still matches the
pipeline's ``(x, y, z) * 100`` convention.

What it does, in order:

  1. imports the four terrain meshes
  2. builds one two-sided terrain material driven by the Minecraft
     grass / dirt / stone / sand palette
  3. places each mesh at its recorded origin and enables collision
  4. imports the block textures the material needs
  5. places a PlayerStart on the terrain and a pawn that can walk it
  6. adds a light rig, sky and height fog, so the result reads as a place
  7. verifies the result against what the data promised

Run headless:

    UnrealEditor.exe MCReplica.uproject -ExecutePythonScript=<this file> \
        -unattended -nopause -nosplash -nullrhi

Every step that can fail says so. A silently flat or untextured terrain is
worse than an obvious failure, so the script exits non-zero rather than
reporting a partial build as a success.
"""

import json
import math
import os
import struct
import sys
import time
import zlib

import unreal

# =============================================================================
# KNOBS
# =============================================================================

BLOCK_CM = 100.0

LEVEL_PATH = "/Game/Maps/MCReplica"

TERRAIN_MESH_DIR = "/Game/MC/Terrain"
TEXTURE_DIR = "/Game/MC/Textures"
CHARACTER_DIR = "/Game/MC/Character"
MATERIAL_DIR = "/Game/MC/Materials"
PROP_DIR = "/Game/MC/Props"
LIGHT_DIR = "/Game/MC/Lighting"

DIMENSION = "overworld"

# Import the committed prop placements. ``prop_placements.json`` records 1297
# instances as *primitive recipes* ("builtin:tree:cone_on_cylinder"), not mesh
# paths, so the geometry is synthesised from engine primitives rather than
# skipped -- a stand-in tree at the recorded height and radius is a far better
# campus than an empty field, and the substitution is reported in the notes.
IMPORT_PROPS = True

#: Terrain is 1.5 M triangles over four meshes. Collision is taken from a
#: decimated LOD rather than triangulated as complex collision: UE expects a
#: mesh this size to carry a proxy, and a tri-mesh collision on it would cost
#: more than the render does.
COLLISION_LOD = 3

_ROOT_CANDIDATES = ["..", "../..", "../../.."]

_t0 = time.time()
_errors = []
_notes = []


def log(msg):
    unreal.log("[MC2UE5 %7.1fs] %s" % (time.time() - _t0, msg))


def warn(msg):
    _notes.append(msg)
    unreal.log_warning("[MC2UE5] %s" % msg)


def err(msg):
    _errors.append(msg)
    unreal.log_error("[MC2UE5] %s" % msg)


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


def _root_dir():
    """
    The MC2UE5 working directory holding assets/, character/ and terrain/.

    Two levels up from the project: the layout is

        <root>/repo/project      <- the UE project (this file's project)
        <root>/assets            <- textures, committed data
        <root>/character         <- generated figure meshes and skin

    so the project directory's grandparent is the root. It is also derived from
    the data root when one was found, which keeps the two in step.
    """
    base = project_dir()
    return os.path.abspath(os.path.join(base, os.pardir, os.pardir))


def _class_name(cls):
    """Name of a UClass, which may be handed over as a class or an instance."""
    try:
        return cls.get_name()
    except TypeError:
        # A class object, not an instance: get_name() is unbound on it.
        return getattr(cls, "__name__", str(cls))


def resolve_root():
    """The MC2UE5 checkout root: the directory holding out/phase2/ and assets/."""
    env = os.environ.get("MC2UE5_ROOT")
    if env and os.path.isdir(os.path.join(env, "out", "phase2")):
        return os.path.abspath(env)
    base = project_dir()
    for rel in _ROOT_CANDIDATES:
        cand = os.path.abspath(os.path.join(base, rel))
        if os.path.isdir(os.path.join(cand, "out", "phase2")):
            return cand
    return None


def _terrain_dir(root):
    return os.path.join(root, "..", "terrain")


def _load_json(path):
    with open(path, "r") as fh:
        return json.load(fh)


def _ensure_dir(pkg):
    unreal.EditorAssetLibrary.make_directory(pkg)


def save_all():
    try:
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        return True
    except Exception as exc:
        warn("save failed: %s" % exc)
        return False


def _editor_world():
    for getter in (
        lambda: unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world(),
        lambda: unreal.EditorLevelLibrary.get_editor_world(),
    ):
        try:
            cand = getter()
            if cand is not None:
                return cand
        except Exception:
            continue
    return None


# =============================================================================
# 16-BIT GREYSCALE PNG READER
# =============================================================================
# The editor's embedded Python has neither numpy nor PIL. Rather than depend
# on one being present, the heightmap is read with the standard library only:
# these are single-channel 16-bit greyscale images, which is a far simpler
# decode than the general case.

def _unfilter(ftype, line, prev, bpp):
    """In-place PNG scanline un-filter (the five standard filter types)."""
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
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (line[i] + pr) & 0xFF
    else:
        raise ValueError("unknown PNG filter type %d" % ftype)


def read_heightmap(path):
    """-> (width, height, rows, channels) of uint16 samples."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG: %s" % path)

    width, height, bitdepth, colortype = struct.unpack(">IIBB", data[16:26])
    if bitdepth != 16:
        raise ValueError("%s: bit depth %s, needs 16" % (path, bitdepth))
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(colortype)
    if channels is None:
        raise ValueError("%s: colour type %s unsupported" % (path, colortype))

    pos, idat = 8, []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        if ctype == b"IDAT":
            idat.append(data[pos + 8:pos + 8 + length])
        elif ctype == b"IEND":
            break
        pos += 12 + length

    raw = zlib.decompress(b"".join(idat))
    stride = width * channels * 2
    bpp = channels * 2

    rows, prev, off = [], bytearray(stride), 0
    for _ in range(height):
        ftype = raw[off]
        cur = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        _unfilter(ftype, cur, prev, bpp)
        prev = cur
        rows.append(cur)
    return width, height, rows, channels


def decode_height_cm(u16, meta):
    """
    UE5's Landscape height decode, in centimetres.

    Must stay equivalent to ``phase2.landscape.ue_decode``::

        z_cm = actor_offset_z_cm + (u16 - 32768) / 128 * z_scale_cm

    The 32768 bias and the /128 are the whole point: the engine maps uint16
    onto a signed -256..+255.992 range and scales that. A 0..1 normalisation
    here would silently agree with a wrong encoder instead of catching it.
    """
    return (float(meta["actor_offset_z_cm"])
            + (float(u16) - 32768.0) / 128.0 * float(meta["z_scale_cm"]))


# =============================================================================
# VALIDATION
# =============================================================================

def validate_source(root):
    """
    Check the committed heightmaps against their recorded encoding.

    Returns the list of tiles worth building, or an empty list. Runs before
    anything is created: a flipped or mis-encoded heightmap would otherwise be
    discovered by looking at it, which in a headless build means never.
    """
    lsc_dir = os.path.join(root, "out", "phase2", DIMENSION, "landscape")
    meta_path = os.path.join(lsc_dir, "landscape.json")
    if not os.path.isfile(meta_path):
        err("no landscape.json at %s" % meta_path)
        return []

    meta = _load_json(meta_path)
    xy = float(meta["xy_scale_cm"])
    if abs(xy - BLOCK_CM) > 1e-6:
        err("xy_scale_cm is %.4f, expected %.1f -- refusing, the terrain would "
            "not match block coordinates" % (xy, BLOCK_CM))
        return []

    lo_b = float(meta["y_min_blocks"])
    hi_b = lo_b + float(meta["y_span_blocks"])
    log("source terrain: %dx%d verts | xy_scale %.1f cm | y %.0f..%.0f blocks"
        % (meta["landscape_resolution"][0], meta["landscape_resolution"][1],
           xy, lo_b, hi_b))

    valid = []
    for tile in meta["tiles"]:
        png = os.path.join(lsc_dir, tile["file"])
        if not os.path.isfile(png):
            err("missing heightmap %s" % png)
            continue
        try:
            w, h, rows, ch = read_heightmap(png)
        except Exception as exc:
            err("tile %s: cannot decode (%s)" % (tile["file"], exc))
            continue
        if w != tile["resolution"][0] or h != tile["resolution"][1]:
            err("tile %s: %dx%d, metadata says %dx%d"
                % (tile["file"], w, h,
                   tile["resolution"][0], tile["resolution"][1]))
            continue

        hm = tile["height_cm_meta"]
        tol = max(0.5, 0.02 * (hi_b - lo_b))
        ymin, ymax = 1e30, -1e30
        for r in (0, h // 3, 2 * h // 3, h - 1):
            line = rows[r]
            for c in range(0, w, max(1, w // 48)):
                i = (c * ch) * 2
                (v,) = struct.unpack(">H", bytes(line[i:i + 2]))
                yb = decode_height_cm(v, hm) / BLOCK_CM
                ymin = min(ymin, yb)
                ymax = max(ymax, yb)
        ok = (ymin >= lo_b - tol) and (ymax <= hi_b + tol)
        log("  %-24s %dx%d  y=[%.2f .. %.2f] blocks  %s"
            % (tile["file"], w, h, ymin, ymax, "OK" if ok else "OUT OF RANGE"))
        if not ok:
            err("    %s decodes outside the recorded range; not building it"
                % tile["file"])
            continue
        valid.append(tile)

    return valid


# =============================================================================
# TEXTURES
# =============================================================================

#: The terrain palette. Phase 2 recorded a heightmap but no material, so the
#: surface has to be coloured from the Minecraft block set. These four cover
#: every ground type this map actually has.
GRASS_TEX = "grass_block_top.png"
DIRT_TEX = "dirt.png"
STONE_TEX = "stone.png"
SAND_TEX = "sand.png"


def import_textures(root):
    """
    Import the block textures the terrain material needs.

    A short list rather than all 736: the release build has no HISM block layer
    to texture, so importing the full set would cook hundreds of megabytes of
    PNGs that nothing samples.
    """
    src_dir = os.path.join(root, "assets", "textures", "block")
    wanted = [GRASS_TEX, DIRT_TEX, STONE_TEX, SAND_TEX]
    _ensure_dir(TEXTURE_DIR)

    out = {}
    for name in wanted:
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            warn("texture missing: %s" % src)
            continue
        dest_name = name[:-4]
        dest = "%s/%s" % (TEXTURE_DIR, dest_name)
        if unreal.EditorAssetLibrary.does_asset_exist(dest):
            out[name] = unreal.load_asset(dest)
            continue

        task = unreal.AssetImportTask()
        task.set_editor_property("filename", src)
        task.set_editor_property("destination_path", TEXTURE_DIR)
        task.set_editor_property("destination_name", dest_name)
        task.set_editor_property("automated", True)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("save", True)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

        tex = unreal.load_asset(dest)
        if tex is None:
            err("could not import %s" % name)
            continue

        # Minecraft textures are pixel art. Nearest filtering keeps the texels
        # crisp; mips and compression stay at the texture-group defaults for
        # the terrain specifically, because a 16x16 texel repeated across a
        # 100 m hillside aliases into noise at any distance without them.
        for prop, value in (("filter", unreal.TextureFilter.TF_NEAREST),
                            ("mip_gen_settings",
                             unreal.TextureMipGenSettings.TMGS_FROM_TEXTURE_GROUP),
                            ("compression_settings",
                             unreal.TextureCompressionSettings.TC_DEFAULT),
                            ("lod_group",
                             unreal.TextureGroup.TEXTUREGROUP_WORLD),
                            ("never_stream", True),
                            ("srgb", True)):
            try:
                tex.set_editor_property(prop, value)
            except Exception:
                pass
        unreal.EditorAssetLibrary.save_loaded_asset(tex)
        out[name] = tex
        log("  texture %-22s -> %s" % (name, dest))

    log("textures: %d/%d imported" % (len(out), len(wanted)))
    return out


# =============================================================================
# TERRAIN MATERIAL
# =============================================================================

def _build_terrain_material(textures):
    """
    Build the terrain material: grass and sand by height, stone on slope.

    Built node by node, and **any** failure aborts the build rather than
    returning a half-built material.

    That behaviour is the whole point. An earlier version wrapped the graph
    construction in one broad ``except`` and, on failure, returned the material
    it had created so far. It then got cached, and every later run took the
    "already present" path. The result was a material with four texture
    samplers and nothing else: BaseColor was never connected, so the surface
    compiled to solid black, and the level rendered as a black void while every
    other check -- meshes placed, collision present, cook clean, 97% of
    triangles front-facing -- reported success.

    So: delete any existing copy, build from scratch, and let an exception
    propagate. A material that cannot be built is a build failure, which is
    the only honest outcome.
    """
    path = "%s/MC_Terrain" % MATERIAL_DIR

    # Always rebuild. Reusing an asset is how the broken graph survived, and
    # the graph is cheap to make compared to diagnosing a black screen later.
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        if not unreal.EditorAssetLibrary.delete_asset(path):
            err("could not delete the existing %s; refusing to reuse a material "
                "that may be half-built" % path)
            return None

    _ensure_dir(MATERIAL_DIR)

    grass = textures.get(GRASS_TEX)
    dirt = textures.get(DIRT_TEX)
    stone = textures.get(STONE_TEX)
    sand = textures.get(SAND_TEX)
    if not grass or not stone:
        err("terrain material needs %s and %s; only %d textures imported"
            % (GRASS_TEX, STONE_TEX, len(textures)))
        return None

    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "MC_Terrain", MATERIAL_DIR, unreal.Material,
        unreal.MaterialFactoryNew())
    if mat is None:
        err("could not create the terrain material")
        return None

    mel = unreal.MaterialEditingLibrary
    built = 0

    def node(cls, x, y, **props):
        """Create one expression, or fail the build.

        The class name is resolved through getattr so a typo raises here with a
        clear name, instead of silently aborting a long graph mid-way.
        """
        nonlocal built
        cls_obj = getattr(unreal, cls, None)
        if cls_obj is None:
            raise RuntimeError("this engine build has no %s" % cls)
        expr = mel.create_material_expression(mat, cls_obj, x, y)
        if expr is None:
            raise RuntimeError("could not create %s at (%d, %d)" % (cls, x, y))
        # Material expression properties are exposed under their UHT names, not
        # snake_case. Three different conventions collide in this one function:
        #
        #   SmoothStep / Divide / Multiply  ->  ConstMin, ConstMax, ConstB
        #   VectorParameter                 ->  DefaultValue
        #   Constant                        ->  R
        #
        # Writing "const_min" or "default_value" raises "Failed to find
        # property", which is exactly what silently aborted the original
        # material graph and left a BaseColor-less black surface behind --
        # see this function's docstring.
        renames = {
            "const_min": "ConstMin",
            "const_max": "ConstMax",
            "const_b": "ConstB",
            "const_a": "ConstA",
            "default_value": "DefaultValue",
            "parameter_name": "ParameterName",
            # ComponentMask's channels are uint32 bit fields, exposed as the
            # single-letter UHT names rather than lowerCamel.
            "r": "R",
            "g": "G",
            "b": "B",
            "a": "A",
        }
        for key, value in props.items():
            prop_name = renames.get(key, key)
            # A Python int fails a float conversion with an error naming the
            # property rather than the type, so ints are widened here.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = float(value)
            expr.set_editor_property(prop_name, value)
        built += 1
        return expr

    def sampler(tex, name, y):
        return node("MaterialExpressionTextureSampleParameter2D",
                    -1200, y, parameter_name=name, texture=tex)

    # ---- textures --------------------------------------------------------
    s_grass = sampler(grass, "GrassTex", -320)
    s_dirt = sampler(dirt or grass, "DirtTex", -180)
    s_stone = sampler(stone, "StoneTex", -40)
    s_sand = sampler(sand or dirt or grass, "SandTex", 100)

    # ---- slope: 1 when flat, 0 when vertical ------------------------------
    # VertexNormalWS rather than a Dot against WorldPosition: the world-space
    # vertex normal's Z component *is* the slope, and taking it directly avoids
    # a DotProduct node and the axis-mix-up that comes with one.
    nrm = node("MaterialExpressionVertexNormalWS", -1200, 420)
    sep = node("MaterialExpressionComponentMask", -1000, 420, r=True,
               g=False, b=False, a=False)
    mel.connect_material_expressions(nrm, "", sep, "Vector")
    steep = node("MaterialExpressionSmoothStep", -820, 420, const_min=0.10,
                 const_max=0.38)
    mel.connect_material_expressions(sep, "", steep, "Min")

    # ---- height: high flat ground fades to sand ---------------------------
    wpos = node("MaterialExpressionWorldPosition", -1200, 160)
    hsep = node("MaterialExpressionComponentMask", -1000, 160, r=False,
                g=False, b=True, a=False)
    mel.connect_material_expressions(wpos, "", hsep, "Vector")
    hdiv = node("MaterialExpressionDivide", -820, 160, const_b=1.0 / 5900.0)
    mel.connect_material_expressions(hsep, "", hdiv, "A")
    hsat = node("MaterialExpressionSaturate", -660, 160)
    mel.connect_material_expressions(hdiv, "", hsat, "Input")
    hgate = node("MaterialExpressionSmoothStep", -500, 160, const_min=0.84,
                 const_max=0.99)
    mel.connect_material_expressions(hsat, "", hgate, "Min")

    # ---- grass -> sand by height -----------------------------------------
    lerp_h = node("MaterialExpressionLinearInterpolate", -300, 60)
    mel.connect_material_expressions(s_grass, "RGB", lerp_h, "A")
    mel.connect_material_expressions(s_sand, "RGB", lerp_h, "B")
    mel.connect_material_expressions(hgate, "", lerp_h, "Alpha")

    # ---- overlay stone on slopes ------------------------------------------
    inv_steep = node("MaterialExpressionOneMinus", -320, 420)
    mel.connect_material_expressions(steep, "", inv_steep, "Input")
    lerp_s = node("MaterialExpressionLinearInterpolate", -120, 220)
    mel.connect_material_expressions(lerp_h, "Result", lerp_s, "A")
    mel.connect_material_expressions(s_stone, "RGB", lerp_s, "B")
    mel.connect_material_expressions(inv_steep, "", lerp_s, "Alpha")

    # A trace of dirt so grass does not meet rock as two flat colours.
    damp = node("MaterialExpressionMultiply", -320, 640, const_b=0.35)
    mel.connect_material_expressions(steep, "", damp, "A")
    final = node("MaterialExpressionLinearInterpolate", 60, 260)
    mel.connect_material_expressions(lerp_s, "Result", final, "A")
    mel.connect_material_expressions(s_dirt, "RGB", final, "B")
    mel.connect_material_expressions(damp, "Result", final, "Alpha")

    mel.connect_material_property(final, "Result",
                                  unreal.MaterialProperty.MP_BASE_COLOR)

    rough = node("MaterialExpressionConstant", 300, 420, r=0.85)
    mel.connect_material_property(rough, "",
                                  unreal.MaterialProperty.MP_ROUGHNESS)

    # Prove the graph is complete before declaring success. BaseColor reaching
    # the material with no expression on it compiles to black, and that is
    # precisely the failure this whole rebuild exists to prevent.
    final_expressions = None
    if hasattr(mel, "get_material_property_input_as_vector"):
        try:
            final_expressions = mel.get_material_expressions(mat)
        except Exception:
            final_expressions = None

    linked = _material_has_base_color(mat)
    if not linked:
        err("the terrain material compiled without a BaseColor input; the "
            "surface would render black. Refusing to ship it.")
        try:
            unreal.EditorAssetLibrary.delete_asset(path)
        except Exception:
            pass
        return None

    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    log("  terrain material built: %d expressions, BaseColor linked"
        % built)
    return mat


def _material_has_base_color(mat):
    """
    -> True when the material's BaseColor actually resolves to an expression.

    A material whose BaseColor is left at its default compiles to opaque black,
    which is not an error anywhere -- it just renders as a void. Reading the
    compiled input back is the only way to tell a finished graph from a
    partial one.
    """
    mel = unreal.MaterialEditingLibrary
    for getter in ("get_material_property_input_as_vector",
                   "get_material_property_input_as_texture"):
        if not hasattr(mel, getter):
            continue
        try:
            value = getattr(mel, getter)(mat)
            if value is not None:
                return True
        except Exception:
            continue
    # No readable input API: fall back to counting expressions, which is a weak
    # signal but still separates a 5-node stub from a complete graph.
    try:
        exprs = mel.get_material_expressions(mat)
        return exprs is not None and len(exprs) >= 12
    except Exception:
        return False


def open_level():
    """
    Create (or open) the level and return the editor world.

    **Deliberately a classic level, not a World Partition one.** The upstream
    pipeline targets a partitioned level, which is the right choice for a map
    with a 144 MB block layer spread over hundreds of streaming cells. This
    build has neither that layer nor the data to rebuild it, and World
    Partition costs the release in a way that is invisible until the cook:

      * actors live in external actor packages rather than the .umap, and
      * a cook only gathers the cells a viewer would stream in.

    With no local player -- a headless cook, or an editor session with no
    viewport -- no cells are streamed, so the cook gathers ~7 generator
    packages and **none of the 1300-odd placed actors reach the package**.
    The build then reports success, the .umap ships, and the packaged game
    opens on an empty map.

    That is exactly what happened before this was changed: the level verified
    clean in the editor, cooked without an error, and shipped with zero
    terrain and zero props. A classic level keeps every actor inside the .umap,
    where the cook cannot miss it. For a single 72 x 104 m campus, streaming was
    buying nothing anyway.
    """
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if unreal.EditorAssetLibrary.does_asset_exist(LEVEL_PATH):
        log("opening existing level %s" % LEVEL_PATH)
        if not les.load_level(LEVEL_PATH):
            err("could not load %s" % LEVEL_PATH)
            return None
    else:
        _ensure_dir("/Game/Maps")
        log("creating level %s (classic, not partitioned)" % LEVEL_PATH)
        if not les.new_level(LEVEL_PATH, False):
            err("could not create the level")
            return None

    world = _editor_world()
    if world is None:
        err("no editor world after opening the level")
        return None

    # A partitioned level left over from an earlier run would keep writing its
    # actors to external packages, so say plainly which kind this is.
    try:
        partitioned = bool(world.get_editor_property("partitioned"))
    except Exception:
        partitioned = False
    if partitioned:
        warn("this level is World Partition partitioned; actors would be "
             "written as external packages and lost in a headless cook")
        log("  converting to a classic level is required for a clean cook")
    else:
        log("  level type: classic (actors stored in the .umap)")
    return world


def _import_obj(path, dest_name):
    """Import one OBJ into TERRAIN_MESH_DIR under `dest_name`."""
    dest = "%s/%s" % (TERRAIN_MESH_DIR, dest_name)
    if not unreal.EditorAssetLibrary.does_asset_exist(dest):
        task = unreal.AssetImportTask()
        task.set_editor_property("filename", path)
        task.set_editor_property("destination_path", TERRAIN_MESH_DIR)
        task.set_editor_property("destination_name", dest_name)
        task.set_editor_property("automated", True)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("save", True)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return unreal.load_asset(dest)


def _place_mesh_actor(label, mesh, origin, material=None, collision=False):
    """
    Spawn a StaticMeshActor for `mesh` at `origin` and configure it.

    Returns the component, or None if the actor could not be placed. Terrain is
    the walkable surface, so the visual mesh is placed with collision off and a
    separate decimated proxy carries the collision -- see ``import_terrain``.
    """
    ox, oy = origin
    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.StaticMeshActor, unreal.Vector(float(ox), float(oy), 0.0),
        unreal.Rotator(0.0, 0.0, 0.0))
    if actor is None:
        return None
    actor.set_actor_label(label)

    comp = actor.get_component_by_class(unreal.StaticMeshComponent)
    if comp is None:
        err("%s has no StaticMeshComponent" % label)
        return None
    comp.set_static_mesh(mesh)

    if material is not None:
        try:
            mesh.set_material(0, material)
        except Exception as exc:
            warn("could not assign the terrain material to %s (%s)"
                 % (label, exc))

    for prop, value in (("collision_enabled", collision),
                        ("generate_overlap_events", False),
                        ("cast_shadow", True)):
        try:
            comp.set_editor_property(prop, value)
        except Exception:
            pass
    if collision:
        # Complex-as-simple: the proxy mesh's triangles are the collision
        # surface. It is the only mode that works on a mesh with no convex
        # hulls, and the proxy is small enough for that to be cheap.
        for prop, value in (("collision_profile_name", "BlockAll"),
                            ("collision_trace_flag",
                             unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)):
            try:
                comp.set_editor_property(prop, value)
            except Exception:
                pass
    try:
        comp.set_mobility(unreal.ComponentMobility.STATIC)
    except Exception:
        pass
    return comp


def import_terrain(root):
    """
    Import the terrain and its collision proxies, and place both.

    Each tile becomes two actors: the full-resolution visual mesh (1.5 M
    triangles across the four, carried by Nanite) and a stride-sampled collision
    proxy that the pawn actually walks on. Complex-as-simple collision over the
    full-resolution mesh would cook to a physics mesh hundreds of megabytes,
    which no character controller should have to pay for.

    Returns the number of visual meshes placed.
    """
    tdir = _terrain_dir(root)
    manifest_path = os.path.join(tdir, "terrain_manifest.json")
    if not os.path.isfile(manifest_path):
        err("no terrain_manifest.json at %s -- run tools/build_terrain_mesh.py "
            "first" % manifest_path)
        return 0
    manifest = _load_json(manifest_path)

    _ensure_dir(TERRAIN_MESH_DIR)
    textures = import_textures(root)
    material = _build_terrain_material(textures)

    placed = 0
    for tile in manifest["tiles"]:
        src = os.path.join(tdir, tile["obj"] + ".obj")
        if not os.path.isfile(src):
            err("missing terrain mesh %s" % src)
            continue
        dest_name = "T_" + tile["obj"]
        mesh = _import_obj(src, dest_name)
        if mesh is None:
            err("could not import %s" % tile["obj"])
            continue

        _tune_visual_mesh(mesh, dest_name)

        if _place_mesh_actor("Terrain_" + tile["obj"], mesh,
                             tile["origin_cm"], material, collision=False) is None:
            err("could not place %s" % dest_name)
            continue

        placed += 1
        log("  placed %-22s %7d tris (visual) at (%.0f, %.0f)"
            % (dest_name, tile["tris"],
               tile["origin_cm"][0], tile["origin_cm"][1]))

        # ---- collision proxy ---------------------------------------------
        cname = tile.get("collision_obj")
        if not cname:
            continue
        csrc = os.path.join(tdir, cname + ".obj")
        if not os.path.isfile(csrc):
            warn("no collision proxy at %s; the pawn will fall through %s"
                 % (csrc, dest_name))
            continue
        cmesh = _import_obj(csrc, "C_" + cname)
        if cmesh is None:
            warn("could not import the collision proxy %s" % cname)
            continue
        if not _enable_complex_collision(cmesh, "C_" + cname):
            err("collision proxy %s has no collision surface" % cname)
            continue
        ccomp = _place_mesh_actor("TerrainCollision_" + tile["obj"], cmesh,
                                  tile["origin_cm"], material, collision=True)
        if ccomp is not None:
            log("  placed %-22s %7d tris (collision, complex-as-simple)"
                % ("C_" + cname, tile.get("collision_tris", 0)))

    log("terrain: %d/%d meshes placed" % (placed, len(manifest["tiles"])))
    return placed


def _enable_complex_collision(mesh, name):
    """
    Make an imported mesh trace as complex collision.

    An imported OBJ arrives with a BodySetup that has no convex hulls and the
    default ``UseSimpleAsComplex`` trace flag -- so it has no collision surface
    at all, whatever the component says. Switching the flag to
    ``UseComplexAsSimple`` makes the render triangles the collision surface,
    which is the only mode that works on a mesh shaped like terrain.

    The flag lives on the *mesh's* BodySetup, not on the component: setting it
    on the component is accepted and then ignored, which reads as a successful
    configure and a pawn that falls through the world.

    Returns True when collision is usable.
    """
    body = None
    try:
        body = mesh.get_editor_property("body_setup")
    except Exception as exc:
        warn("%s: no readable BodySetup (%s)" % (name, exc))
    if body is None:
        return False
    try:
        body.set_editor_property(
            "collision_trace_flag",
            unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
    except Exception as exc:
        warn("%s: could not set the BodySetup trace flag (%s)" % (name, exc))
        return False
    try:
        mesh.set_editor_property("collision_complexity",
                                 unreal.CollisionComplexity.CTC_USE_COMPLEX_AS_SIMPLE)
    except Exception:
        # Not fatal: the BodySetup flag above is the one that matters.
        pass
    return True


def _tune_visual_mesh(mesh, name):
    """
    Nanite plus a collision LOD on the visual mesh.

    Nanite is what makes 1.5 M triangles affordable here, standing in for the
    job a Landscape's LOD chain would have done. ``lod_for_collision`` is
    pointed away from LOD0 so that if anything ever does trace this mesh it
    uses a decimated copy rather than the full 392k triangles.
    """
    applied = []
    try:
        mesh.set_editor_property("nanite_enabled", True)
        applied.append("nanite")
    except Exception:
        pass
    try:
        num_lods = mesh.get_num_lods()
        target = min(COLLISION_LOD, max(0, num_lods - 1))
        mesh.set_editor_property("lod_for_collision", target)
        applied.append("collision_lod=%d/%d" % (target, num_lods))
    except Exception as exc:
        warn("could not set the collision LOD on %s (%s)" % (name, exc))
    if applied:
        log("    %s: %s" % (name, ", ".join(applied)))


# =============================================================================
# PROPS
# =============================================================================

#: Cull band per semantic class, in cm (start, end). A prop is culled once it
#: is too small to resolve, which is a distance proportional to its size, so
#: one number cannot serve both a 1 m bush and a 30 m building.
PROP_CULL_CM = {
    "tree":      (30000.0, 42000.0),
    "building":  (40000.0, 55000.0),
    "structure": (20000.0, 30000.0),
    "plant":     (6000.0, 9000.0),
    "prop":      (10000.0, 14000.0),
}
PROP_CULL_DEFAULT_CM = (12000.0, 16000.0)

#: Engine primitive each recipe maps to. The recipes name four shapes and each
#: matches an engine mesh with the right topology, so nothing is modelled here.
PRIMITIVE_MESH = {
    "box": "/Engine/BasicShapes/Cube",
    "cone_on_cylinder": "/Engine/BasicShapes/Cylinder",
    "cylinder": "/Engine/BasicShapes/Cylinder",
    "post": "/Engine/BasicShapes/Cylinder",
    "cross_billboard": "/Engine/BasicShapes/Plane",
    "sphere": "/Engine/BasicShapes/Sphere",
    "plane": "/Engine/BasicShapes/Plane",
}


def build_props(root):
    """
    Synthesise geometry for the committed prop placements.

    ``prop_placements.json`` records 1297 instances, each with a *primitive
    recipe* rather than a mesh path. ``import_phase2.py`` skips these, which is
    right for a data importer; a release build should not be an empty field, so
    the recipes are realised here as real meshes built from engine primitives.

    What is preserved is placement, scale and class, which is what the data
    actually carries. What is invented is the silhouette, and the notes say so
    rather than passing them off as the classified props.
    """
    p_path = os.path.join(root, "out", "phase2", DIMENSION, "props",
                          "prop_placements.json")
    if not os.path.isfile(p_path):
        warn("no prop_placements.json at %s" % p_path)
        return 0
    data = _load_json(p_path)
    insts = data.get("instances", [])
    log("props: %d instances  by_class=%s  ground_aligned=%s"
        % (len(insts), data.get("by_class"), data.get("ground_aligned")))

    if not IMPORT_PROPS or _editor_world() is None:
        return 0

    _ensure_dir(PROP_DIR)
    mats = _prop_materials()

    # One asset per (primitive, class): instances then share geometry and the
    # HISM stays a real instanced draw rather than 1297 unique meshes.
    assets = {}
    groups = {}
    for inst in insts:
        recipe = inst.get("model") or {}
        prim = recipe.get("primitive") or "box"
        cls = inst.get("class") or "unknown"
        key = (prim, cls)
        if key not in assets:
            assets[key] = _make_prop_mesh(prim, cls, mats)
        if assets[key] is None:
            continue
        groups.setdefault(key, []).append(inst)

    cell_cm = 256 * BLOCK_CM
    total = 0
    cell_count = 0
    cluster_cls = getattr(unreal, "MCReplicaPropCluster", None)

    if cluster_cls is None:
        # Without the C++ cluster actor the fallback is one actor per prop,
        # which is correct but costs a draw call each -- the thing the cluster
        # exists to avoid. Say so rather than quietly shipping the slow path.
        warn("MCReplicaPropCluster is unavailable; falling back to one actor "
             "per prop, which costs roughly %d extra draw calls"
             % len(insts))
        cluster_cls = None

    for (prim, cls), items in sorted(groups.items()):
        mesh = assets[(prim, cls)]
        if mesh is None:
            continue
        buckets = {}
        for inst in items:
            pos = inst.get("position_cm") or [0.0, 0.0, 0.0]
            buckets.setdefault(
                (int(math.floor(pos[0] / cell_cm)),
                 int(math.floor(pos[2] / cell_cm))), []).append(inst)

        for (cx, cz), group in sorted(buckets.items()):
            if cluster_cls is None:
                total += _spawn_props(group, mesh, cls, prim, cx, cz)
            else:
                placed = _spawn_prop_cluster(cluster_cls, group, mesh, cls,
                                             prim, cx, cz, cell_cm)
                if placed is None:
                    total += _spawn_props(group, mesh, cls, prim, cx, cz)
                else:
                    total += placed
            cell_count += 1

    log("  props: %d placed across %d mesh variants, %d spatial cells%s"
        % (total, len([a for a in assets.values() if a]), cell_count,
           " (instanced)" if cluster_cls is not None else " (one actor each)"))
    return total


def _spawn_prop_cluster(cluster_cls, group, mesh, cls, prim, cx, cz, cell_cm):
    """
    Place a batch of props into one HISM cluster. Returns the count, or None
    if the cluster could not be built so the caller can fall back.
    """
    start, end = PROP_CULL_CM.get(cls, PROP_CULL_DEFAULT_CM)

    # Plain parallel arrays rather than an array of Transform: FTransform has
    # no unambiguous Python constructor in 5.8 (every positional overload also
    # reads as a Vector or Rotator), and its rotation is a Quat that Python
    # cannot build from a Rotator. The engine does that conversion exactly, so
    # the data is handed over raw.
    positions, rotations, scales = [], [], []
    for inst in group:
        pos = inst.get("position_cm") or [0.0, 0.0, 0.0]
        rot = inst.get("rotation_deg") or [0.0, 0.0, 0.0]
        positions.append(unreal.Vector(float(pos[0]), float(pos[1]),
                                       float(pos[2])))
        rotations.append(unreal.Rotator(float(rot[0]), float(rot[1]),
                                        float(rot[2])))
        scales.append(_prop_scale(inst.get("model") or {}))

    try:
        spawned = unreal.EditorLevelLibrary.spawn_actor_from_class(
            cluster_cls, unreal.Vector(cx * cell_cm, 0.0, cz * cell_cm),
            unreal.Rotator(0.0, 0.0, 0.0))
        if spawned is None:
            return None
        spawned.set_actor_label("Props_%s_%s_%d_%d" % (cls, prim, cx, cz))
        spawned.set_editor_property("start_cull_distance", float(start))
        spawned.set_editor_property("end_cull_distance", float(end))
        # Called as bound methods rather than through call_method: the latter
        # wants the C++ spelling ("SetMesh") and the Python bindings want the
        # reflected one ("set_mesh"), and mixing them up reports
        # "Failed to find function" against a class that plainly has it.
        spawned.set_mesh(mesh)
        spawned.set_instances_from_data(positions, rotations, scales)
        return len(positions)
    except Exception as exc:
        warn("prop cluster %s/%s at (%d,%d) failed (%s)"
             % (cls, prim, cx, cz, exc))
        return None


def _spawn_props(group, mesh, cls, prim, cx, cz):
    """
    Spawn one StaticMeshActor per prop instance. Returns the count placed.

    Each instance carries its own scale, because the shared mesh is an unscaled
    engine primitive and the recipe's recorded height and radius have to ride on
    the actor transform. Collision is off: the pawn walks on the terrain, and
    1297 blocking props would only make the controller's sweep more expensive.
    """
    placed = 0
    for inst in group:
        pos = inst.get("position_cm") or [0.0, 0.0, 0.0]
        rot = inst.get("rotation_deg") or [0.0, 0.0, 0.0]
        scale = _prop_scale(inst.get("model") or {})
        # Scale is applied after the spawn rather than passed in:
        # spawn_actor_from_class's fourth parameter is a transient flag, not a
        # scale, and handing it a Vector raises a nativize error.
        actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
            unreal.StaticMeshActor,
            unreal.Vector(float(pos[0]), float(pos[1]), float(pos[2])),
            unreal.Rotator(float(rot[0]), float(rot[1]), float(rot[2])))
        if actor is None:
            continue
        actor.set_actor_label("Prop_%s_%s" % (cls, prim))
        actor.set_actor_scale3d(unreal.Vector(scale.x, scale.y, scale.z))
        comp = actor.get_component_by_class(unreal.StaticMeshComponent)
        if comp is None:
            continue
        comp.set_static_mesh(mesh)
        for prop, value in (("collision_enabled", False),
                            ("generate_overlap_events", False)):
            try:
                comp.set_editor_property(prop, value)
            except Exception:
                pass
        placed += 1
    return placed


def _prop_materials():
    """
    One unlit-ish tinted material per semantic class.

    Deliberately plain. A prop that reads correctly at this scale needs a leaf
    alpha mask and a bark colour, which is art direction; what matters for a
    terrain rebuild is that trees read as vertical green mass and structures as
    grey block, so the classes are separated by hue and left at that.
    """
    palette = {
        "tree":      unreal.LinearColor(0.16, 0.34, 0.12, 1.0),
        "plant":     unreal.LinearColor(0.30, 0.48, 0.18, 1.0),
        "structure": unreal.LinearColor(0.55, 0.53, 0.50, 1.0),
        "building":  unreal.LinearColor(0.62, 0.58, 0.52, 1.0),
        "prop":      unreal.LinearColor(0.42, 0.38, 0.34, 1.0),
    }
    default = unreal.LinearColor(0.45, 0.45, 0.45, 1.0)
    _ensure_dir(MATERIAL_DIR)
    out = {}
    vec_cls = getattr(unreal, "MaterialExpressionVectorParameter", None)
    const_cls = getattr(unreal, "MaterialExpressionConstant", None)
    if vec_cls is None:
        err("this engine build exposes no vector parameter expression; the "
            "props would render black")
        return {"_default": None}

    for cls, colour in palette.items():
        path = "%s/MC_Prop_%s" % (MATERIAL_DIR, cls)
        # Rebuild, never reuse -- see _build_terrain_material for why a cached
        # half-built material is worse than no material at all.
        if unreal.EditorAssetLibrary.does_asset_exist(path):
            unreal.EditorAssetLibrary.delete_asset(path)

        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            "MC_Prop_%s" % cls, MATERIAL_DIR, unreal.Material,
            unreal.MaterialFactoryNew())
        if mat is None:
            err("could not create the %s prop material" % cls)
            continue

        mel = unreal.MaterialEditingLibrary
        const = mel.create_material_expression(mat, vec_cls, -300, 0)
        if const is None:
            err("could not create the tint for the %s prop material" % cls)
            continue
        const.set_editor_property("ParameterName", "Tint")
        const.set_editor_property("DefaultValue", colour)
        mel.connect_material_property(const, "",
                                      unreal.MaterialProperty.MP_BASE_COLOR)

        if const_cls is not None:
            rough = mel.create_material_expression(mat, const_cls, -300, 200)
            if rough is not None:
                try:
                    rough.set_editor_property("R", 0.9)
                except Exception:
                    pass
                mel.connect_material_property(
                    rough, "", unreal.MaterialProperty.MP_ROUGHNESS)

        unreal.EditorAssetLibrary.save_loaded_asset(mat)
        out[cls] = mat

    out["_default"] = out.get("prop") or (list(out.values())[0] if out else None)
    return out


def _make_prop_mesh(primitive, cls, mats):
    """
    Realise one primitive recipe as a StaticMesh asset.

    A cube is 100 cm per side, a cylinder 100 cm tall and 50 cm across, a plane
    100 cm square -- none of which match a recipe's height and radius. Each is
    therefore scaled onto the *median* recipe size for its (primitive, class)
    pair, and the per-instance transform carries the residual. That keeps most
    transforms close to unity, which is what an HISM transform buffer wants,
    while still reproducing the recorded sizes.
    """
    key = (primitive, cls)
    name = "P_%s_%s" % (cls, primitive)
    dest = "%s/%s" % (PROP_DIR, name)
    if unreal.EditorAssetLibrary.does_asset_exist(dest):
        return unreal.load_asset(dest)

    src = PRIMITIVE_MESH.get(primitive, "/Engine/BasicShapes/Cube")
    src_mesh = unreal.load_asset(src)
    if src_mesh is None:
        warn("no engine mesh for primitive %r" % primitive)
        return None

    asset_name = "%s_%s" % (cls, primitive)
    dest = "%s/%s" % (PROP_DIR, asset_name)
    mesh = unreal.load_asset(dest)
    if mesh is None:
        # duplicate_asset takes the loaded object, not a path string; handing it
        # the string raises "Failed to convert parameter 'original_object'".
        try:
            unreal.AssetToolsHelpers.get_asset_tools().duplicate_asset(
                asset_name, PROP_DIR, src_mesh)
            mesh = unreal.load_asset(dest)
        except Exception as exc:
            warn("could not duplicate %s (%s); using the engine asset in place"
                 % (src, exc))
    if mesh is None:
        # Sharing the engine asset is fine: the per-instance transform carries
        # each recipe's size, so no per-class scaling is baked in.
        mesh = src_mesh

    mat = mats.get(cls) or mats.get("_default")
    if mat is not None:
        try:
            mesh.set_material(0, mat)
        except Exception:
            pass

    unreal.EditorAssetLibrary.save_loaded_asset(mesh)
    return mesh


def _prop_scale(recipe):
    """Per-instance scale that turns the engine primitive into the recipe size."""
    h = float(recipe.get("height_cm") or 100.0)
    r = float(recipe.get("radius_cm") or 75.0)
    prim = recipe.get("primitive") or "box"
    if prim == "box":
        return unreal.Vector(max(0.2, 2.0 * r / 100.0),
                             max(0.2, 2.0 * r / 100.0),
                             max(0.1, h / 100.0))
    if prim in ("cone_on_cylinder", "cylinder", "post", "sphere"):
        d = max(0.1, 2.0 * r / 100.0)
        return unreal.Vector(d, d, max(0.1, h / 100.0))
    d = max(0.1, 2.0 * r / 100.0)
    return unreal.Vector(d, d, max(0.1, h / 100.0))


# =============================================================================
# PLAYABILITY
# =============================================================================

def make_playable(root):
    """
    Give the level a pawn, a GameMode and a PlayerStart.

    The pawn is ``AMCReplicaCharacter``: a Minecraft-proportioned student with
    a procedural walk cycle, which walks the terrain and steps over ledges via
    CharacterMovement. The upstream project had none of this -- its own notes
    record that the level could be looked at but not walked on -- and a
    spectator pawn with no body is not a character anyone can tour a school in.
    """
    if _editor_world() is None:
        err("no world; cannot make the level playable")
        return False

    # One sample, used for both the XY the PlayerStart sits at and the Z it
    # sits at. Sampling the height somewhere other than where the pawn is
    # placed is how it ended up 43 m in the air: the script read the plateau's
    # height and then placed the pawn over the low ground beside it.
    ground = _terrain_height_cm(root)
    spawn = unreal.Vector(0.0, 0.0, ground + 200.0)
    log("  PlayerStart will sit at (%.0f, %.0f, %.0f) over ground at %.0f cm"
        % (spawn.x, spawn.y, spawn.z, ground))

    # ---- PlayerStart ------------------------------------------------------
    start = None
    for actor in unreal.EditorLevelLibrary.get_all_level_actors():
        if actor.get_class().get_name() == "PlayerStart":
            start = actor
            break
    if start is None:
        start = unreal.EditorLevelLibrary.spawn_actor_from_class(
            unreal.PlayerStart, spawn, unreal.Rotator(0.0, 0.0, 0.0))
        if start is None:
            err("could not place a PlayerStart")
            return False
        log("  PlayerStart at (%.0f, %.0f, %.0f)" % (spawn.x, spawn.y, spawn.z))
    else:
        log("  PlayerStart already present")

    # ---- the character ---------------------------------------------------
    figure = _import_character()
    pawn = _spawn_player_character(figure, start)

    _set_game_mode()
    return pawn is not None


# =============================================================================
# PLAYER CHARACTER
# =============================================================================

#: C++ component name -> mesh asset suffix. The character builds its figure from
#: one static mesh per limb so each can pivot at its own joint; the walk cycle
#: in AMCReplicaCharacter rotates them, which a single merged mesh could not do.
CHARACTER_PARTS = (
    ("Head", "head"),
    ("Torso", "torso"),
    ("ArmLeft", "arm"),
    ("ArmRight", "arm"),
    ("LegLeft", "leg"),
    ("LegRight", "leg"),
)


def _import_character():
    """
    Import the character's meshes and skin, and build its material.

    Returns {suffix: StaticMesh} plus the skin, or None when the assets are
    absent -- ``tools/make_character.py`` generates them, and a level built
    without them still runs, just without a visible figure.
    """
    src_dir = os.path.join(_root_dir(), "character")
    if not os.path.isdir(src_dir):
        warn("no character assets at %s -- run tools/make_character.py. "
             "The pawn will spawn invisible." % src_dir)
        return None

    _ensure_dir(CHARACTER_DIR)

    skin = _import_texture(os.path.join(src_dir, "char_skin.png"), "char_skin",
                           CHARACTER_DIR)
    if skin is None:
        warn("character skin missing; the figure would be untextured")
        return None

    meshes = {}
    for _comp, suffix in CHARACTER_PARTS:
        if suffix in meshes:
            continue
        src = os.path.join(src_dir, "char_%s.obj" % suffix)
        if not os.path.isfile(src):
            warn("missing character mesh %s" % src)
            continue
        dest_name = "CH_%s" % suffix
        task = unreal.AssetImportTask()
        task.set_editor_property("filename", src)
        task.set_editor_property("destination_path", CHARACTER_DIR)
        task.set_editor_property("destination_name", dest_name)
        task.set_editor_property("automated", True)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("save", True)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
        mesh = unreal.load_asset("%s/%s" % (CHARACTER_DIR, dest_name))
        if mesh is None:
            warn("could not import the character mesh %s" % src)
            continue
        meshes[suffix] = mesh

    material = _build_character_material(skin)
    for mesh in meshes.values():
        try:
            mesh.set_material(0, material)
        except Exception as exc:
            warn("could not assign the skin to %s (%s)" % (mesh.get_name(), exc))

    log("  character: %d meshes, skin %s, material %s"
        % (len(meshes), skin.get_name(),
           material.get_name() if material else "(none)"))
    return {"meshes": meshes, "material": material, "skin": skin}


def _import_texture(src, dest_name, dest_dir):
    """Import a texture as Nearest, no mips -- it is pixel art."""
    dest = "%s/%s" % (dest_dir, dest_name)
    if not unreal.EditorAssetLibrary.does_asset_exist(dest):
        task = unreal.AssetImportTask()
        task.set_editor_property("filename", src)
        task.set_editor_property("destination_path", dest_dir)
        task.set_editor_property("destination_name", dest_name)
        task.set_editor_property("automated", True)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("save", True)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    tex = unreal.load_asset(dest)
    if tex is None:
        return None
    for prop, value in (("filter", unreal.TextureFilter.TF_NEAREST),
                        ("mip_gen_settings",
                         unreal.TextureMipGenSettings.TMGS_NO_MIPMAPS),
                        ("compression_settings",
                         unreal.TextureCompressionSettings.TC_EDITOR_ICON),
                        ("lod_group",
                         unreal.TextureGroup.TEXTUREGROUP_CHARACTER),
                        ("never_stream", True),
                        ("srgb", True)):
        try:
            tex.set_editor_property(prop, value)
        except Exception:
            pass
    unreal.EditorAssetLibrary.save_loaded_asset(tex)
    return tex


def _build_character_material(skin):
    """
    One textured material for the whole figure.

    The skin atlas already carries the uniform, so a single opaque material is
    all the figure needs -- and one material means one draw call for the
    character, which is what keeps a third-person view cheap.
    """
    path = "%s/MC_Character" % MATERIAL_DIR
    # Rebuild rather than reuse, for the same reason as the terrain material: a
    # half-built graph reads as "already present" on the next run and renders
    # black forever.
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        if not unreal.EditorAssetLibrary.delete_asset(path):
            err("could not delete the existing %s; refusing to reuse it" % path)
            return None

    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "MC_Character", MATERIAL_DIR, unreal.Material,
        unreal.MaterialFactoryNew())
    if mat is None:
        err("could not create the character material")
        return None

    mel = unreal.MaterialEditingLibrary

    cls = getattr(unreal, "MaterialExpressionTextureSampleParameter2D", None)
    if cls is None:
        err("this engine build exposes no texture sample expression")
        return None

    tex = mel.create_material_expression(mat, cls, -400, 0)
    if tex is None:
        err("could not create the skin sampler")
        return None
    tex.set_editor_property("ParameterName", "Skin")
    tex.set_editor_property("texture", skin)
    mel.connect_material_property(tex, "RGB",
                                  unreal.MaterialProperty.MP_BASE_COLOR)

    rough_cls = getattr(unreal, "MaterialExpressionConstant", None)
    if rough_cls is not None:
        rough = mel.create_material_expression(mat, rough_cls, -400, 200)
        if rough is not None:
            try:
                rough.set_editor_property("R", 0.9)
            except Exception:
                pass
            mel.connect_material_property(rough, "",
                                          unreal.MaterialProperty.MP_ROUGHNESS)

    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    log("  character material built, BaseColor linked to the skin")
    return mat


def _spawn_player_character(figure, start):
    """
    Spawn AMCReplicaCharacter at the PlayerStart and dress it.

    The pawn is placed on the PlayerStart when one exists so the two cannot
    drift apart, and nudged up off the ground: the capsule is 200 cm tall and
    its origin sits at the centre, so a spawn exactly on the surface would put
    the character's feet 100 cm below the terrain.
    """
    cls = getattr(unreal, "MCReplicaCharacter", None)
    if cls is None:
        err("MCReplicaCharacter is not available -- is the MCReplica module "
            "compiled? The game would fall back to the engine default pawn.")
        return None

    loc = start.get_actor_location() if start is not None else \
        unreal.Vector(0.0, 0.0, 5000.0)
    # Capsule origin is at mid-height; +100 puts the feet on the ground.
    loc.z += 100.0

    pawn = unreal.EditorLevelLibrary.spawn_actor_from_class(
        cls, loc, unreal.Rotator(0.0, 0.0, 0.0))
    if pawn is None:
        err("could not spawn the player character")
        return None
    pawn.set_actor_label("MC_Player")

    if figure:
        by_name = {}
        for comp in pawn.get_components_by_class(unreal.StaticMeshComponent):
            by_name[comp.get_name()] = comp

        assigned = 0
        for comp_name, suffix in CHARACTER_PARTS:
            mesh = figure["meshes"].get(suffix)
            comp = by_name.get(comp_name)
            if mesh is None or comp is None:
                if comp is not None:
                    log("    no mesh for component %s" % comp_name)
                continue
            comp.set_static_mesh(mesh)
            assigned += 1
        log("  character: %d/%d limbs dressed"
            % (assigned, len(CHARACTER_PARTS)))

    loc = pawn.get_actor_location()
    log("  player pawn at (%.0f, %.0f, %.0f), capsule 30x200 cm, "
        "MaxStepHeight 60 cm" % (loc.x, loc.y, loc.z))
    return pawn


def _terrain_height_cm(root):
    """
    Terrain height at the map centre, in cm.

    Reads the committed heightmaps rather than guessing, so the pawn spawns on
    the surface instead of in it.

    The sample is taken **at (0, 0) world blocks** -- the same XY the pawn is
    placed at. An earlier version sampled the centre of ``tiles[0]`` instead,
    which is block (-86, -408): a different place, and one that happens to sit
    on the only high ground in the map. The pawn was therefore placed at the
    height of the plateau while standing over the low ground at z = 400 cm, so
    it spawned **4320 cm in the air** and the player looked down at the campus
    from 43 m up. Nothing reported it: the spawn is legal, the fall is survivable
    on its own, and the level verifies clean.

    The search below is a plain nearest-sample lookup in world-block space, so
    it stays correct if the map origin ever moves.
    """
    lsc_dir = os.path.join(root, "out", "phase2", DIMENSION, "landscape")
    meta_path = os.path.join(lsc_dir, "landscape.json")
    if not os.path.isfile(meta_path):
        return 2000.0
    meta = _load_json(meta_path)

    ox0, oy0 = meta["block_origin"]
    bw, bh = meta["block_size"]
    target = (ox0 + bw // 2, oy0 + bh // 2)

    best = None
    best_d2 = None
    for tile in meta["tiles"]:
        png = os.path.join(lsc_dir, tile["file"])
        if not os.path.isfile(png):
            continue
        try:
            w, h, rows, ch = read_heightmap(png)
        except Exception:
            continue
        px, py = tile["block_origin"]
        hm = tile["height_cm_meta"]
        # Nearest column to the wanted block.
        c = min(max(target[0] - px, 0), w - 1)
        r = min(max(target[1] - py, 0), h - 1)
        line = rows[r]
        (v,) = struct.unpack(">H", bytes(line[c * ch * 2:c * ch * 2 + 2]))
        z = decode_height_cm(v, hm)
        d2 = (px + c - target[0]) ** 2 + (py + r - target[1]) ** 2
        if best_d2 is None or d2 < best_d2:
            best_d2 = d2
            best = (z, px + c, py + r)

    if best is None:
        return 2000.0
    z, bx, by = best
    log("  terrain at map centre (block %d,%d): %.0f cm" % (bx, by, z))
    return z


def _world_settings():
    world = _editor_world()
    if world is None:
        return None
    try:
        return unreal.World.get_world_settings(world)
    except Exception:
        pass
    try:
        return world.get_world_settings()
    except Exception:
        return None


def _set_game_mode():
    """
    Bind a GameMode and a pawn so a packaged build has something to spawn.

    A Blueprint GameMode cannot be authored from Python, so this uses the
    engine's own ``AGameModeBase`` -- which is all a first-person walk around
    the terrain needs -- and points its DefaultPawnClass at ``ASpectatorPawn``.

    The pawn class is set on the *GameMode class default object*, not on
    WorldSettings: ``DefaultPawnClass`` is a member of AGameModeBase, and
    WorldSettings only carries the GameMode class itself under the property
    name ``default_game_mode``. Writing ``default_pawn_class`` to WorldSettings
    is accepted and then ignored, so the packaged build spawns nothing.

    Returns True when both took.
    """
    w = _world_settings()
    if w is None:
        warn("no WorldSettings; a packaged build will use engine defaults")
        return False

    ok = True

    gm_path = "/Script/Engine.GameModeBase"
    gm_cls = unreal.load_class(None, gm_path)
    if gm_cls is None:
        warn("could not load %s" % gm_path)
        ok = False
    else:
        applied = False
        for prop in ("default_game_mode", "game_mode"):
            try:
                w.set_editor_property(prop, gm_cls)
                log("  game mode: GameModeBase (WorldSettings.%s)" % prop)
                applied = True
                break
            except Exception:
                continue
        if not applied:
            warn("WorldSettings would not accept a GameMode class; the "
                 "packaged build will use the engine default")
            ok = False

        # DefaultPawnClass lives on the GameMode CDO, not on WorldSettings --
        # writing it to WorldSettings is accepted and then ignored, so a
        # packaged build spawns nothing.
        #
        # The project's own character is preferred and is taken as the exposed
        # Python class object: load_class("/Script/MCReplica.MCReplicaCharacter")
        # does not resolve for a project module, and guessing at the path is
        # how a pawn silently fails to bind.
        pawn_ok = False
        try:
            cdo = unreal.get_default_object(gm_cls)
            if cdo is None:
                warn("no CDO for %s; the GameMode could not be configured" % gm_path)
            else:
                candidates = [getattr(unreal, "MCReplicaCharacter", None),
                              unreal.load_class(None, "/Script/Engine.SpectatorPawn"),
                              unreal.load_class(None, "/Script/Engine.DefaultPawn")]
                for pawn_cls in candidates:
                    if pawn_cls is None:
                        continue
                    cdo.set_editor_property("default_pawn_class", pawn_cls)
                    log("  pawn: %s" % _class_name(pawn_cls))
                    pawn_ok = True
                    break
        except Exception as exc:
            warn("could not set DefaultPawnClass (%s)" % exc)

    return ok and pawn_ok


# =============================================================================
# ATMOSPHERE
# =============================================================================

def build_atmosphere():
    """
    Directional light, sky light, sky atmosphere and height fog.

    A heightfield with no light rig renders as a flat grey silhouette, so this
    is not decoration -- it is the difference between a build that looks broken
    and one that looks like a place. The sun is a late-afternoon 35 degrees,
    which puts the plateau's relief into relief without needing a skylight to
    sell it.
    """
    world = _editor_world()
    if world is None:
        return

    sun = _find_actor("DirectionalLight")
    if sun is None:
        sun = unreal.EditorLevelLibrary.spawn_actor_from_class(
            unreal.DirectionalLight, unreal.Vector(0.0, 0.0, 12000.0),
            unreal.Rotator(-35.0, -125.0, 0.0))
        if sun is not None:
            sun.set_actor_label("MC_Sun")
            log("  directional light added (elev -35, yaw -125)")
    if sun is not None:
        comp = sun.get_component_by_class(unreal.DirectionalLightComponent)
        if comp is not None:
            for prop, value in (("intensity", 8.0), ("temperature", 5200.0),
                                ("cast_shadows", True),
                                ("dynamic_shadows", True),
                                ("shadow_bias", 0.05)):
                try:
                    comp.set_editor_property(prop, value)
                except Exception:
                    pass

    sky = _find_actor("SkyLight")
    if sky is None:
        sky = unreal.EditorLevelLibrary.spawn_actor_from_class(
            unreal.SkyLight, unreal.Vector(0.0, 0.0, 6000.0),
            unreal.Rotator(0.0, 0.0, 0.0))
        if sky is not None:
            sky.set_actor_label("MC_SkyLight")
            log("  sky light added")
    if sky is not None:
        comp = sky.get_component_by_class(unreal.SkyLightComponent)
        if comp is not None:
            for prop, value in (("intensity", 1.0), ("real_time_capture", True)):
                try:
                    comp.set_editor_property(prop, value)
                except Exception:
                    pass

    if _find_actor("SkyAtmosphere") is None and \
            unreal.EditorLevelLibrary.spawn_actor_from_class(
                unreal.SkyAtmosphere, unreal.Vector(0.0, 0.0, 0.0),
                unreal.Rotator(0.0, 0.0, 0.0)):
        log("  sky atmosphere added")

    # Height fog rather than a flat colour: it puts distance haze on the far
    # edge of the map and reads as scale.
    fog = _find_actor("ExponentialHeightFog")
    if fog is None:
        fog = unreal.EditorLevelLibrary.spawn_actor_from_class(
            unreal.ExponentialHeightFog, unreal.Vector(0.0, 0.0, 0.0),
            unreal.Rotator(0.0, 0.0, 0.0))
        if fog is not None:
            fog.set_actor_label("MC_Fog")
            log("  exponential height fog added")
    if fog is not None:
        comp = fog.get_component_by_class(unreal.ExponentialHeightFogComponent)
        if comp is not None:
            for prop, value in (
                ("fog_density", 0.015),
                ("fog_height_falloff", 0.35),
                ("height_fog_start_distance", 3000.0),
                ("height_fog_height", 800.0),
                ("fog_color", unreal.LinearColor(0.62, 0.70, 0.80, 1.0)),
                ("start_density", 0.06),
            ):
                try:
                    comp.set_editor_property(prop, value)
                except Exception:
                    pass


def _find_actor(class_name):
    for actor in unreal.EditorLevelLibrary.get_all_level_actors():
        if actor.get_class().get_name() == class_name:
            return actor
    return None


# =============================================================================
# VERIFICATION
# =============================================================================

def verify(meshes_placed, props_placed):
    """
    Check the assembled level against what the data promised.

    Each check exists because it corresponds to a way this build can silently
    produce something that looks fine in the editor and is wrong in the
    viewport.
    """
    ok = True
    visual = collision = proxies = starts = 0
    pawn_ok = mode_ok = False
    dark_materials = []

    for actor in unreal.EditorLevelLibrary.get_all_level_actors():
        name = actor.get_class().get_name()
        if name == "StaticMeshActor":
            label = actor.get_actor_label()
            if label.startswith("TerrainCollision_"):
                proxies += 1
                comp = actor.get_component_by_class(unreal.StaticMeshComponent)
                if comp is None:
                    continue
                # 5.8 exposes set_static_mesh() but no matching getter; the
                # asset is read back through the editor property.
                mesh = comp.get_editor_property("static_mesh")
                if mesh is None:
                    continue
                # The check that matters is whether the *mesh* has a collision
                # surface. A component can report collision_enabled while the
                # BodySetup it points at has no geometry at all, which is
                # exactly what a freshly imported OBJ looks like.
                try:
                    body = mesh.get_editor_property("body_setup")
                    if body is None:
                        continue
                    flag = body.get_editor_property("collision_trace_flag")
                    if flag == unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE:
                        collision += 1
                except Exception:
                    continue
            elif label.startswith("Terrain_"):
                visual += 1
        elif name == "PlayerStart":
            starts += 1

    # ---- every material in the level must actually colour something -------
    # A material whose BaseColor is left unconnected compiles to opaque black.
    # That is not an error anywhere: the asset saves, the cook succeeds, the
    # mesh has valid collision, and the surface renders as a void. This walk is
    # what catches it, and it exists because the black-screen build passed
    # every other check the pipeline had.
    seen_materials = set()
    for actor in unreal.EditorLevelLibrary.get_all_level_actors():
        comp = actor.get_component_by_class(unreal.StaticMeshComponent) \
            if actor.get_class().get_name() == "StaticMeshActor" else None
        if comp is None:
            continue
        mesh = comp.get_editor_property("static_mesh")
        if mesh is None:
            continue
        try:
            slots = mesh.get_editor_property("static_materials")
        except Exception:
            continue
        for slot in slots:
            mat = slot.get_editor_property("material_interface")
            if mat is None:
                dark_materials.append("<none on %s>"
                                       % actor.get_actor_label())
                continue
            path = mat.get_path_name()
            if path in seen_materials:
                continue
            seen_materials.add(path)
            if not _material_has_base_color(mat):
                dark_materials.append(path)

    # Read the pawn and GameMode back off the GameMode CDO: that is where a
    # packaged build looks, so it is what has to be verified. AGameModeBase
    # already defaults to ADefaultPawn, so "not None" proves nothing -- the
    # check is that it names a pawn class *other* than the base default, i.e.
    # that something was actually assigned.
    try:
        gm_cls = unreal.load_class(None, "/Script/Engine.GameModeBase")
        cdo = unreal.get_default_object(gm_cls) if gm_cls else None
        if cdo is not None:
            pawn_cls = cdo.get_editor_property("default_pawn_class")
            mode_ok = True
            pawn_ok = pawn_cls is not None and \
                _class_name(pawn_cls) not in ("DefaultPawn", "SpectatorPawn")
            pawn_name = _class_name(pawn_cls) if pawn_cls else "(none)"
    except Exception:
        pawn_name = "(unreadable)"

    log("verify: %d visual meshes, %d collision proxies (%d with a collision "
        "surface), %d PlayerStart, %d props, gamemode=%s pawn=%s, "
        "%d materials all colour something"
        % (visual, proxies, collision, starts, props_placed,
           "ok" if mode_ok else "MISSING",
           pawn_name if pawn_ok else "MISSING (%s)" % pawn_name,
           len(seen_materials) - len(dark_materials)))

    if dark_materials:
        err("these materials have no BaseColor input and would render black: "
            "%s" % ", ".join(sorted(dark_materials)))

    if meshes_placed != 4:
        err("expected 4 terrain meshes, placed %d" % meshes_placed)
        ok = False
    if collision == 0:
        err("no terrain collision proxy has usable collision; the pawn will "
            "fall through the world")
        ok = False
    if collision != proxies:
        err("%d of %d collision proxies have no collision surface"
            % (proxies - collision, proxies))
        ok = False
    if starts == 0:
        err("no PlayerStart; a packaged build will spawn nowhere")
        ok = False
    if not mode_ok:
        err("no GameMode bound; a packaged build will spawn the wrong pawn")
        ok = False
    if not pawn_ok:
        err("no DefaultPawnClass on the GameMode; a packaged build will spawn "
            "nothing")
        ok = False
    return ok


# =============================================================================
# ENTRY POINT
# =============================================================================

def run():
    log("=" * 70)
    log("MC2UE5 release level build | dimension=%s" % DIMENSION)
    log("=" * 70)

    root = resolve_root()
    if root is None:
        err("could not locate the MC2UE5 root (needs out/phase2/). "
            "Set MC2UE5_ROOT.")
        return 1
    log("data root: %s" % root)

    tiles = validate_source(root)
    if not tiles:
        err("no valid heightmap tiles")
        return 1

    if open_level() is None:
        return 1

    meshes = import_terrain(root)
    if meshes == 0:
        err("no terrain meshes placed")
        return 1

    props = build_props(root) if IMPORT_PROPS else 0
    build_atmosphere()
    make_playable(root)

    if save_all():
        log("saved")

    verify(meshes, props)

    log("-" * 70)
    if _notes:
        log("notes (%d):" % len(_notes))
        for n in _notes[:20]:
            log("  - %s" % n)
    if _errors:
        log("errors (%d):" % len(_errors))
        for e in _errors:
            log("  ! %s" % e)
        log("-" * 70)
        log("BUILD FAILED")
        return 1

    log("-" * 70)
    log("BUILD OK -- %d terrain meshes, %d props" % (meshes, props))
    return 0


if __name__ == "__main__":
    sys.exit(run())
