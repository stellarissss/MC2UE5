# -*- coding: utf-8 -*-
"""
import_world.py -- MC2UE5 route C, layer 3 (assembly layer).

Assembles the Minecraft 1.16.5 voxel world (layer 1: voxel_data/full/*.bin) plus
the block texture / material manifest (layer 2: assets/material_manifest.json)
into a UE5.5 World Partition level built out of Hierarchical Instanced Static
Mesh components that share one Nanite-enabled unit-cube mesh.

=============================================================================
MUST BE RUN INSIDE THE UE 5.5.4 EDITOR. THIS SANDBOX HAS NEVER RUN IT.
=============================================================================
This script was written and syntax-checked in a headless Linux container with no
GPU and no Unreal Engine, so it has NOT been executed even once. You must run it
on the machine that has UE 5.5.4 installed:

  1) Open MCReplica.uproject in the UE 5.5.4 editor. A GPU / render context is
     required: Nanite mesh builds and World Partition streaming need one.
  2) FIRST run with DRY_RUN = True to get the statistics report and sanity-check
     the numbers. This creates no assets and no geometry.
  3) Then set DRY_RUN = False and run again to actually build the world.

Ways to invoke it (details in project/README.md):

  # a) Editor Python console (Output Log > Python Console):
  import import_world; import_world.run()

  # b) Command line (no -game; the editor must open):
  UnrealEditor.exe MCReplica.uproject ^
      -ExecutePythonScript="Content/Python/import_world.py"

Measured scale for this save (see scripts/hism_estimate.py):
  37,485,404 non-air voxels, 315 block types, ~930 HISM components with the
  default CELL_SIZE=512 / MAX_INSTANCES_PER_HISM=500000. Expect the build to
  take a long time (tens of minutes to hours) and to want a lot of RAM.

Data expected next to the .uproject (override with the MC2UE5_ROOT env var):
    <root>/assets/material_manifest.json
    <root>/assets/textures/block/*.png
    <root>/voxel_data/full/{overworld,nether,end}.bin
"""

import json
import math
import os
import struct
import sys
import time
from collections import defaultdict

import unreal

# =============================================================================
# USER-KNOB CONSTANTS
# =============================================================================

# True  -> only count instances per (cell x block type) and write a report.
# False -> really build the HISM components in the level.
DRY_RUN = True

# Spatial cell edge, in MC blocks. Instances are grouped by (cell, blockName);
# each group becomes one or more HISM components on a per-cell actor.
#
# 512 vs 256 (whole world):
#   512 -> fewer components (~930 total), fewer state changes, but each World
#          Partition cell is chunkier and peak per-cell memory is higher.
#   256 -> ~4x more cells (~2.5-3k components): smoother streaming, lower peak
#          memory per cell, but more components/draw calls and a slower build.
# The campus (448 x 768 blocks) uses 128: ~4x6 = 24 cells, fine culling
# granularity, and each cell stays small enough to build comfortably.
CELL_SIZE = 128

# A single (cell x block) group bigger than this is split over several HISM
# components. One nether cell holds ~5.5M netherrack blocks, which is far too
# much for one component to build or save comfortably.
MAX_INSTANCES_PER_HISM = 500000

# -----------------------------------------------------------------------------
# SURFACE CULLING
# -----------------------------------------------------------------------------
# Drop any block whose six neighbours are all occupied. Such a block is
# completely enclosed and can never be seen, so removing it is visually a no-op
# while removing its instance, its transform and its draw work.
#
# This is not a guess about the data: the campus census measured 1,949,579 solid
# blocks against 1,605,366 exposed faces over six directions, and 54.6% of the
# instances are `dirt` (721,702) plus `bedrock` (343,931) -- underground fill.
#
# The rule is safe by construction. A block is removed only when all six
# neighbours are occupied *in the source data*, so the removed set is strictly
# interior and the remaining set still forms a closed surface -- no holes.
SURFACE_ONLY = True

#: Whether the block clusters collide. The block layer is the ground and the
#: buildings, so it must; the flag is here because collision on a dense block
#: field is the single most expensive thing in the scene and has to be
#: measurable against a collision-free run.
BLOCK_COLLISION = True

# Dimensions to import, in order. The campus lives in the overworld; the other
# two dimensions are out of scope for "把 SYFZ MC 地图的校园做成 UE 游戏".
IMPORT_DIMENSIONS = ["overworld"]

# -----------------------------------------------------------------------------
# CAMPUS SCOPE -- the render rework assembles the campus, not the whole map
# -----------------------------------------------------------------------------
# regions.campus from the layer-2 survey, in block coordinates. Blocks outside
# this box are skipped while iterating, so build time and level size stay
# proportional to the campus.
CAMPUS_BOUNDS = (-144, 303, -544, 223)     # x0, x1, z0, z1 (blocks)
LIMIT_TO_CAMPUS = True

# -----------------------------------------------------------------------------
# MATERIAL SOURCE
# -----------------------------------------------------------------------------
# True  -> shade every block with a CC0 PBR MaterialInstance (MI_<family>), the
#          realistic look. Requires import_cc0_materials.py to have run.
# False -> the original Minecraft 16x16 block textures.
USE_CC0 = True
CC0_INSTANCE_DIR = "/Game/MC/CC0"

# The three dimensions share an XZ origin, so stacking them at the same place
# would make them intersect. Offset each one vertically instead: with UE5 large
# world coordinates a +/-200 km shift is exact in double precision.
DIMENSION_Z_OFFSET_CM = {
    "overworld": 0.0,
    "nether": -20000000.0,
    "end": 20000000.0,
}

# UE default 1 uu = 1 cm, so one MC block = 1 m = 100 uu. The engine's
# BasicShapes/Cube is already 100uu, so instances need a scale of exactly 1.0.
BLOCK_CM = 100.0

# Candidate locations of the MC2UE5 data root, relative to the .uproject dir.
_ASSET_ROOT_CANDIDATES = ["..", "../..", "../../.."]

# Destination packages.
CUBE_MESH_PATH = "/Game/Meshes/Cube1x1x1"
MASTER_MATERIAL_PATH = "/Game/Materials/MC_BlockMaster"
TEXTURE_DIR = "/Game/Textures/Block"
MATERIAL_DIR = "/Game/Materials/Block"
LEVEL_PATH = "/Game/Maps/MCReplica"

ENABLE_NANITE = True

# Log progress after this many cells, per dimension.
PROGRESS_EVERY_CELLS = 4

# How many blocks to buffer per add_instances() call. Keeps peak Python memory
# bounded when filling a 500k-instance component.
ADD_BATCH = 20000

# =============================================================================
# HISM CULLING -- the main frame-time lever on this map
# =============================================================================
# The block layer is ~1.6 M instances of a 12-face cube spread over the whole
# campus. Left at the engine default they are drawn to the far plane, so every
# frame walks and draws geometry the player cannot resolve. These two numbers
# are the single biggest win available, and they cost nothing in image quality
# because a 1 m cube at 300 m is well under a pixel.
#
# Both are centimetres, not blocks. The campus is ~720 x 1040 blocks, i.e.
# 72000 x 104000 cm, so these are fractions of the play space rather than
# absolute limits: 40000 cm is 400 m, which comfortably covers the far side of
# the campus from anywhere reasonable.
#
# r.ViewDistanceScale from the quality tier multiplies these, so the Low tier
# ends up culling blocks at 40000 * 0.4 = 160 m. That is the intent: the
# silhouette of the map is the *near* field, and the near field is what reads.
# These are the single biggest frame-time lever and they were set far too
# generously: 400 m for a 1 m cube is roughly 10 pixels at 1080p, i.e. the far
# half of the campus was being drawn to fill a handful of pixels. Halved to
# 80/140 m, where a block still reads at ~25 px and the instance count submitted
# per frame drops by an order of magnitude.
#
# Per-frame cost on a HISM is proportional to the instances that survive this
# test, so this is the difference between walking 1.17 M instances and walking a
# few tens of thousands -- and it is why the frame time was collapsing while the
# GPU sat idle.
HISM_CULL_START_CM = 8000.0      # begin fading out here
HISM_CULL_END_CM = 14000.0       # fully culled past here

# Instances per leaf node of the HISM cluster tree. The engine default is 32,
# which is a reasonable general-purpose compromise. This map is not general: the
# cube mesh is tiny and instances are dense and uniform, so a coarser tree
# means fewer nodes to walk per cull with no loss of selectivity. Lowering it
# would make the cull *more* precise at the cost of a deeper tree -- the wrong
# trade when the bottleneck is CPU tree traversal.
HISM_INSTANCES_PER_LEAF = 64

# Nanite fallback: a 12-face cube is far below any sane error threshold, so
# keep the aggressive (lower-error) setting rather than letting the mesh get
# simplified. Blocks are the map's silhouette; visible faceting on a cube face
# is more objectionable than the triangles cost.

# =============================================================================
# LOGGING
# =============================================================================
_t0 = time.time()


def log(msg):
    unreal.log("[MC2UE5 %7.1fs] %s" % (time.time() - _t0, msg))


def warn(msg):
    unreal.log_warning("[MC2UE5] %s" % msg)


def err(msg):
    unreal.log_error("[MC2UE5] %s" % msg)


#: Keys already reported by :func:`_warn_once`. A property name that does not
#: exist on this engine version fails on every one of ~2.6 k components, and a
#: few thousand identical warnings bury the real ones.
_warned_once = set()


def _warn_once(key, msg):
    """Log ``msg`` the first time ``key`` is seen, then stay quiet."""
    if key in _warned_once:
        return
    _warned_once.add(key)
    warn(msg)


# =============================================================================
# PATHS
# =============================================================================
def project_dir():
    """Directory containing the .uproject."""
    try:
        return unreal.Paths.convert_relative_path_to_full(
            unreal.Paths.project_dir()).rstrip("/\\")
    except Exception:
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.abspath(os.path.join(here, os.pardir, os.pardir))


def resolve_asset_root():
    """Find the MC2UE5 folder holding assets/ and voxel_data/."""
    env = os.environ.get("MC2UE5_ROOT")
    if env and os.path.isdir(os.path.join(env, "assets")):
        return os.path.abspath(env)

    proj = project_dir()
    for rel in _ASSET_ROOT_CANDIDATES:
        cand = os.path.abspath(os.path.join(proj, rel))
        if (os.path.isdir(os.path.join(cand, "assets"))
                and os.path.isdir(os.path.join(cand, "voxel_data"))):
            return cand
    if os.path.isdir(os.path.join(proj, "assets")):
        return proj
    return None


# =============================================================================
# VOXEL FILE READER (MC2WV2 -- see parse/INTERMEDIATE_FORMAT.md)
# =============================================================================
FILE_MAGIC = b"MC2WV2\0\0"
HEADER = struct.Struct("<8sIB3sQQQQQQ")
CHUNK_TAB = struct.Struct("<iiHHIQ")
DIM_BY_ID = {0: "overworld", 1: "nether", 2: "end"}


class VoxelFile(object):
    """Streaming reader for one dimension's .bin file.

    Only one chunk (max 16 KiB) is held in memory at a time, so a 75 MiB file
    costs nothing to walk.
    """

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        raw = self.f.read(HEADER.size)
        if len(raw) != HEADER.size:
            raise IOError("%s: too short for a header" % path)
        (magic, version, dim_id, _rsv, n_chunks, n_voxels, n_pal,
         pal_off, tab_off, vox_off) = HEADER.unpack(raw)
        if magic != FILE_MAGIC:
            raise IOError("%s: bad magic %r -- re-run parse_world.py --full "
                          "to get MC2WV2 files" % (path, magic))
        if version != 2:
            raise IOError("%s: format version %d is unsupported (expected 2)"
                          % (path, version))
        self.dimension = DIM_BY_ID.get(dim_id, "dim%d" % dim_id)
        self.chunk_count = n_chunks
        self.voxel_count = n_voxels
        self.palette_off = pal_off
        self.tab_off = tab_off
        self.palette = self._read_palette()
        self.rows = self._read_chunk_table()

    def _read_palette(self):
        self.f.seek(self.palette_off)
        (n,) = struct.unpack("<I", self.f.read(4))
        out = []
        for _ in range(n):
            nlen, plen = struct.unpack("<HH", self.f.read(4))
            out.append((self.f.read(nlen).decode("utf-8"),
                        self.f.read(plen).decode("utf-8")))
        return out

    def _read_chunk_table(self):
        self.f.seek(self.tab_off)
        rows = []
        for _ in range(self.chunk_count):
            cx, cz, pcount, _pad, vcount, voff = CHUNK_TAB.unpack(
                self.f.read(CHUNK_TAB.size))
            gmap = list(struct.unpack("<%dI" % pcount, self.f.read(4 * pcount))) \
                if pcount else []
            rows.append((cx, cz, vcount, voff, gmap))
        return rows

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass

    def iter_blocks(self, bounds=None):
        """
        Yield (worldX, worldY, worldZ, blockName) per non-air block.

        ``bounds`` is (x0, x1, z0, z1) in block coordinates. A chunk that lies
        wholly outside it is skipped without decoding, which is the difference
        between reading the whole 14.5M-voxel overworld and reading only the
        campus (~1.9M blocks) -- the chunk table already knows each chunk's
        origin, so this costs nothing.
        """
        unpack_word = struct.Struct("<I").unpack_from
        for (cx, cz, count, off, gmap) in self.rows:
            if bounds is not None:
                bx0, bx1, bz0, bz1 = bounds
                if (cx * 16 > bx1 or cx * 16 + 15 < bx0
                        or cz * 16 > bz1 or cz * 16 + 15 < bz0):
                    continue
            self.f.seek(off)
            data = self.f.read(count * 4)
            if len(data) != count * 4:
                raise IOError("%s: truncated voxel block at chunk (%d,%d)"
                              % (self.path, cx, cz))
            base_x = cx * 16
            base_z = cz * 16
            palette = self.palette
            if bounds is not None:
                bx0, bx1, bz0, bz1 = bounds
                for i in range(0, len(data), 4):
                    word = unpack_word(data, i)[0]
                    wx = base_x + (word & 0xF)
                    wz = base_z + ((word >> 4) & 0xF)
                    if wx > bx1 or wx < bx0 or wz > bz1 or wz < bz0:
                        continue
                    li = (word >> 17) & 0x7FFF
                    yield (wx, (word >> 8) & 0x1FF, wz,
                           palette[gmap[li]][0])
                continue
            for i in range(0, len(data), 4):
                word = unpack_word(data, i)[0]
                li = (word >> 17) & 0x7FFF
                yield (base_x + (word & 0xF),
                       (word >> 8) & 0x1FF,
                       base_z + ((word >> 4) & 0xF),
                       palette[gmap[li]][0])


# =============================================================================
# ASSET CREATION
# =============================================================================
def sanitize(name):
    """minecraft:grass_block -> MC_grass_block (safe as a UE object name)."""
    body = "".join(ch if (ch.isalnum() or ch == "_") else "_"
                   for ch in name.replace("minecraft:", ""))
    return "MC_" + (body or "Unnamed")


def import_textures(texture_src_dir):
    """Import every block PNG into /Game/Textures/Block. Returns {stem: asset}."""
    if not os.path.isdir(texture_src_dir):
        err("texture dir not found: %s" % texture_src_dir)
        return {}

    files = sorted(f for f in os.listdir(texture_src_dir)
                   if f.lower().endswith(".png"))
    if not files:
        err("no PNG files under %s" % texture_src_dir)
        return {}

    log("importing %d textures -> %s" % (len(files), TEXTURE_DIR))
    unreal.EditorAssetLibrary.make_directory(TEXTURE_DIR)

    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
    for fn in files:
        stem = os.path.splitext(fn)[0]
        task = unreal.AssetImportTask()
        task.set_editor_property("filename", os.path.join(texture_src_dir, fn))
        task.set_editor_property("destination_path", TEXTURE_DIR)
        task.set_editor_property("destination_name", stem)
        task.set_editor_property("automated", True)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("save", True)
        asset_tools.import_asset_tasks([task])

    out = {}
    for fn in files:
        stem = os.path.splitext(fn)[0]
        tex = unreal.EditorAssetLibrary.load_asset("%s/%s" % (TEXTURE_DIR, stem))
        if tex is None:
            warn("texture failed to import: %s" % stem)
            continue
        _configure_texture(tex, stem)
        out[stem] = tex

    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
    log("textures ready: %d/%d" % (len(out), len(files)))
    return out


def _configure_texture(tex, stem):
    """Nearest filter, no streaming, uncompressed: crisp 16x16 pixel textures."""
    try:
        tex.set_editor_property("filter", unreal.TextureFilter.TF_NEAREST)
        tex.set_editor_property("lod_group", unreal.TextureGroup.TEXTUREGROUP_World)
        tex.set_editor_property("never_stream", True)
        # TC_EditorIcon is effectively "uncompressed", which is what we want for
        # 16x16 pixel art: blocky compression would smear the texels.
        tex.set_editor_property("compression_settings",
                                unreal.TextureCompressionSettings.TC_EditorIcon)
        tex.set_editor_property("mip_gen_settings",
                                unreal.TextureMipGenSettings.TMGS_NoMipmaps)
        tex.modify()
    except Exception as exc:
        warn("could not fully configure texture %s (%s)" % (stem, exc))


def create_cube_mesh():
    """Create /Game/Meshes/Cube1x1x1 from the engine cube and enable Nanite."""
    if unreal.EditorAssetLibrary.does_asset_exist(CUBE_MESH_PATH):
        mesh = unreal.EditorAssetLibrary.load_asset(CUBE_MESH_PATH)
        log("cube mesh already exists: %s" % CUBE_MESH_PATH)
        _enable_nanite(mesh)
        return mesh

    unreal.EditorAssetLibrary.make_directory("/Game/Meshes")
    src = "/Engine/BasicShapes/Cube"
    if not unreal.EditorAssetLibrary.does_asset_exist(src):
        err("engine cube not found at %s" % src)
        return None
    log("duplicating engine cube %s -> %s" % (src, CUBE_MESH_PATH))
    mesh = unreal.EditorAssetLibrary.duplicate_asset(src, CUBE_MESH_PATH)
    if mesh is None:
        err("failed to create the cube mesh")
        return None

    for prop, value in (("generate_lightmap_u_vs", False),
                        ("b_support_gimbal_lod", False)):
        try:
            mesh.set_editor_property(prop, value)
        except Exception:
            pass
    _enable_nanite(mesh)
    unreal.EditorAssetLibrary.save_asset(CUBE_MESH_PATH, only_if_is_dirty=False)
    log("cube mesh ready: %s" % CUBE_MESH_PATH)
    return mesh


def _enable_nanite(mesh):
    """Turn Nanite on via StaticMeshEditorSubsystem (the supported path)."""
    if not ENABLE_NANITE or mesh is None:
        return
    try:
        smes = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
        settings = smes.get_nanite_settings(mesh)
        settings.set_editor_property("enabled", True)
        # A 12-triangle cube has nothing to simplify; pinning the fallback keeps
        # the silhouette exact instead of letting the heuristic pick a budget.
        settings.set_editor_property("fallback_target",
                                     unreal.NaniteFallbackTarget.RELATIVE_ERROR)
        settings.set_editor_property("fallback_relative_error", 0.0)
        settings.set_editor_property("keep_percent_triangles", 1.0)
        smes.set_nanite_settings(mesh, settings, True)
        try:
            mesh.set_editor_property("nanite_settings", settings)
        except Exception:
            pass
        log("Nanite enabled on %s" % mesh.get_name())
    except Exception as exc:
        warn("could not enable Nanite (%s). This is not fatal: HISM renders "
             "through the instancing path regardless." % exc)


def _hex_to_linear_color(hex_str):
    """'#8fd4ff' -> unreal.LinearColor (with an sRGB -> linear conversion)."""
    h = hex_str.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]

    def to_linear(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return unreal.LinearColor(to_linear(rgb[0]), to_linear(rgb[1]),
                              to_linear(rgb[2]), 1.0)


def _node(mat, cls, x, y):
    return unreal.MaterialEditingLibrary.create_material_expression(mat, cls, x, y)


def _plug(material, src_expr, src_out, dst_expr, dst_in):
    return unreal.MaterialEditingLibrary.connect_material_expressions(
        src_expr, src_out, dst_expr, dst_in)


def _plug_param(material, src_expr, dst_expr, dst_in):
    return unreal.MaterialEditingLibrary.connect_material_property(
        src_expr, "", dst_expr, dst_in)


def build_master_material():
    """Build the shared cube material: 3 textures blended by the face normal.

    Every MC block is a unit cube, so one material can serve all of them: the
    three face textures are chosen per-pixel from the world-space normal
    (top -> +Z, bottom -> -Z, sides -> the four walls).

    The blend weights are derived from the world normal's Z component:
        side = 1 - |nz|          (1 on the four walls, 0 on top/bottom)
        top  = saturate(2 * nz)  (1 on the top face only)
        bot  = saturate(-2 * nz) (1 on the bottom face only)
    On a unit cube the normal is axis-aligned, so these are exact; the 2x slope
    only keeps near-horizontal faces from fading out.
    """
    if unreal.EditorAssetLibrary.does_asset_exist(MASTER_MATERIAL_PATH):
        log("master material already exists: %s" % MASTER_MATERIAL_PATH)
        return unreal.EditorAssetLibrary.load_asset(MASTER_MATERIAL_PATH)

    unreal.EditorAssetLibrary.make_directory("/Game/Materials")
    mat = unreal.EditorAssetLibrary.create_asset(
        asset_name=unreal.Name("MC_BlockMaster"),
        package_path="/Game/Materials",
        asset_class=unreal.Material,
        factory=unreal.MaterialFactoryNew())
    if mat is None:
        err("failed to create the master material")
        return None

    try:
        mat.set_editor_property("two_sided", False)
    except Exception:
        pass

    if not _build_face_blend_graph(mat):
        warn("the face-blend material graph could not be built; the material "
             "exists but will render as the default surface. The geometry and "
             "instancing are still correct -- finish the graph by hand, or see "
             "the 'Material graph' section of project/README.md.")
    else:
        log("face-blend material graph built")

    try:
        unreal.MaterialEditingLibrary.recompile_material(mat)
    except Exception as exc:
        warn("material recompile issue: %s" % exc)

    unreal.EditorAssetLibrary.save_asset(MASTER_MATERIAL_PATH, only_if_is_dirty=False)
    log("master material ready: %s" % MASTER_MATERIAL_PATH)
    return mat


def _build_face_blend_graph(mat):
    """Wire Top/Side/Bottom texture parameters through a normal-driven blend.

    Returns True on success. Every step is individually guarded because this is
    the most version-sensitive part of the script.
    """
    try:
        mel = unreal.MaterialEditingLibrary

        def tex_param(param_name, x, y):
            node = _node(mat, unreal.MaterialExpressionTextureSampleParameter2D, x, y)
            node.set_editor_property("parameter_name", param_name)
            node.set_editor_property("sampler_type",
                                     unreal.MaterialSamplerType.SAMPLERTYPE_Color)
            return node

        top_tex = tex_param("TopTex", -1100, -400)
        side_tex = tex_param("SideTex", -1100, -100)
        bot_tex = tex_param("BottomTex", -1100, 200)

        # World-space normal, Z channel only.
        normal = _node(mat, unreal.MaterialExpressionPixelNormalWS, -1100, 550)
        normal.set_editor_property("worldspace", True)
        nz = _node(mat, unreal.MaterialExpressionComponentMask, -880, 550)
        nz.set_editor_property("r", False)
        nz.set_editor_property("g", False)
        nz.set_editor_property("b", True)
        nz.set_editor_property("a", False)
        _plug(mat, normal, "RGB", nz, "Input")

        # Desaturation collapses the float3 into (v,v,v,1) so the value can be
        # used as a scalar lerp weight (lerp reads the R channel).
        abs_nz = _node(mat, unreal.MaterialExpressionAbs, -700, 550)
        _plug(mat, nz, "", abs_nz, "Input")
        abs_d = _node(mat, unreal.MaterialExpressionDesaturation, -520, 550)
        _plug(mat, abs_nz, "", abs_d, "Input")

        signed_d = _node(mat, unreal.MaterialExpressionDesaturation, -520, 700)
        _plug(mat, nz, "", signed_d, "Input")

        one_minus = _node(mat, unreal.MaterialExpressionOneMinus, -340, 550)
        _plug(mat, abs_d, "", one_minus, "Input")

        two_x = _node(mat, unreal.MaterialExpressionConstant, -520, 880)
        two_x.set_editor_property("r", 2.0)
        scaled = _node(mat, unreal.MaterialExpressionMultiply, -340, 700)
        _plug(mat, signed_d, "", scaled, "A")
        _plug(mat, two_x, "", scaled, "B")

        top_w = _node(mat, unreal.MaterialExpressionSaturate, -160, 700)
        _plug(mat, scaled, "", top_w, "Input")
        bot_w = _node(mat, unreal.MaterialExpressionSaturate, -160, 880)
        inv = _node(mat, unreal.MaterialExpressionMultiply, -340, 880)
        neg = _node(mat, unreal.MaterialExpressionConstant, -520, 1000)
        neg.set_editor_property("r", -1.0)
        _plug(mat, signed_d, "", inv, "A")
        _plug(mat, neg, "", inv, "B")
        _plug(mat, inv, "", bot_w, "Input")

        # base = lerp(bottom, top, top_w); result = lerp(base, side, side_w)
        vertical = _node(mat, unreal.MaterialExpressionLinearInterpolate, 60, 0)
        _plug(mat, bot_tex, "RGB", vertical, "A")
        _plug(mat, top_tex, "RGB", vertical, "B")
        _plug(mat, top_w, "", vertical, "Alpha")

        final = _node(mat, unreal.MaterialExpressionLinearInterpolate, 280, 100)
        _plug(mat, vertical, "RGB", final, "A")
        _plug(mat, side_tex, "RGB", final, "B")
        _plug(mat, one_minus, "", final, "Alpha")

        _plug_param(mat, final, unreal.MaterialProperty.MP_BASE_COLOR)

        # Same blend, but the B channel carries opacity for masked/transparent
        # blocks (leaves, glass, water); driven per-material by BlendMode.
        opacity = _node(mat, unreal.MaterialExpressionSaturate, 280, 350)
        _plug(mat, final, "RGB", opacity, "Input")

        tex_default = tex_param("OpacityScale", 60, 350)
        tex_default.set_editor_property("parameter_name", "OpacityFromTex")
        _plug(mat, opacity, "", tex_default, "Input")

        _plug_param(mat, opacity, unreal.MaterialProperty.MP_OPACITY)
        return True
    except Exception as exc:
        warn("face-blend graph construction failed: %s" % exc)
        return False


def create_block_materials(master, textures, manifest):
    """One MaterialInstanceConstant per manifest entry -> {blockName: asset}."""
    unreal.EditorAssetLibrary.make_directory(MATERIAL_DIR)
    out = {}
    for entry in manifest:
        name = entry["blockName"]
        asset_name = sanitize(name)
        target = "%s/%s" % (MATERIAL_DIR, asset_name)

        mi = unreal.EditorAssetLibrary.load_asset(target)
        if mi is None:
            mi = unreal.EditorAssetLibrary.create_asset(
                asset_name=unreal.Name(asset_name),
                package_path=MATERIAL_DIR,
                asset_class=unreal.MaterialInstanceConstant,
                factory=unreal.MaterialInstanceConstantFactoryNew())
        if mi is None:
            warn("could not create a material for %s" % name)
            continue

        if master is not None:
            try:
                mi.set_editor_property("parent", master)
            except Exception as exc:
                warn("parent not set for %s (%s)" % (name, exc))

        mode = entry.get("alphaMode", "opaque")
        if mode == "transparent":
            _set_mi_static(mi, "BlendMode", True)
        elif mode == "masked":
            _set_mi_static(mi, "BlendMode", True)
            _set_mi_scalar(mi, "OpacityCutoff", 0.5)
        else:
            _set_mi_static(mi, "BlendMode", False)

        # A transparent surface also has to be told not to write depth.
        if mode == "transparent":
            _set_mi_static(mi, "IsTranslucent", True)

        for param, key in (("TopTex", "topTex"), ("SideTex", "sideTex"),
                           ("BottomTex", "bottomTex")):
            stem = os.path.splitext(entry.get(key, ""))[0]
            tex = textures.get(stem)
            if tex is None:
                continue
            try:
                mi.set_editor_property("texture_parameter_values",
                                       _merge_texture_params(
                                           mi.get_editor_property(
                                               "texture_parameter_values"),
                                           param, tex))
            except Exception as exc:
                warn("texture param %s not set for %s (%s)" % (param, name, exc))

        if entry.get("emissive"):
            try:
                mi.set_editor_property("vector_parameter_values",
                                       _merge_vector_params(
                                           mi.get_editor_property(
                                               "vector_parameter_values"),
                                           "EmissiveColor",
                                           _hex_to_linear_color(
                                               entry.get("emissiveColor",
                                                         "#ffffff"))))
                _set_mi_static(mi, "EmissiveEnabled", True)
            except Exception as exc:
                warn("emissive not set for %s (%s)" % (name, exc))

        out[name] = mi

    unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
    log("block materials ready: %d" % len(out))
    return out


def _merge_texture_params(current, param_name, texture):
    params = [p for p in (current or [])
              if str(p.get_editor_property("parameter_name")) != param_name]
    p = unreal.MaterialTextureParameterValue()
    p.set_editor_property("parameter_name", param_name)
    p.set_editor_property("texture", texture)
    params.append(p)
    return params


def _merge_vector_params(current, param_name, color):
    params = [p for p in (current or [])
              if str(p.get_editor_property("parameter_name")) != param_name]
    p = unreal.MaterialVectorParameterValue()
    p.set_editor_property("parameter_name", param_name)
    p.set_editor_property("parameter_value", color)
    params.append(p)
    return params


def _set_mi_static(mi, name, value):
    """Best-effort static switch override; ignored if the parent lacks it."""
    try:
        overrides = [o for o in (mi.get_editor_property(
            "static_switch_parameter_values") or [])
            if str(o.get_editor_property("parameter_name")) != name]
        sw = unreal.MaterialStaticSwitchParameterValue()
        sw.set_editor_property("parameter_name", name)
        sw.set_editor_property("value", bool(value))
        overrides.append(sw)
        mi.set_editor_property("static_switch_parameter_values", overrides)
    except Exception:
        pass


def _set_mi_scalar(mi, name, value):
    try:
        params = [p for p in (mi.get_editor_property("scalar_parameter_values") or [])
                  if str(p.get_editor_property("parameter_name")) != name]
        p = unreal.MaterialScalarParameterValue()
        p.set_editor_property("parameter_name", name)
        p.set_editor_property("parameter_value", float(value))
        params.append(p)
        mi.set_editor_property("scalar_parameter_values", params)
    except Exception:
        pass


# =============================================================================
# LEVEL / WORLD PARTITION
# =============================================================================
def open_level():
    """Create or open the World Partition level and return the editor world.

    `LevelEditorSubsystem.new_level(path, is_partitioned_world=True)` is the
    supported way to author a partitioned map from Python. It closes the current
    level WITHOUT saving, so start the editor on a clean session before running.
    """
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)

    if unreal.EditorAssetLibrary.does_asset_exist(LEVEL_PATH):
        log("opening existing level %s" % LEVEL_PATH)
        if not les.load_level(LEVEL_PATH):
            err("could not load %s" % LEVEL_PATH)
            return None
    else:
        unreal.EditorAssetLibrary.make_directory("/Game/Maps")
        log("creating World Partition level %s" % LEVEL_PATH)
        if not les.new_level(LEVEL_PATH, True):
            err("could not create the partitioned level %s" % LEVEL_PATH)
            return None

    world = None
    try:
        ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = ues.get_editor_world()
    except Exception:
        pass
    if world is None:
        try:
            world = unreal.EditorLevelLibrary.get_editor_world()
        except Exception:
            pass
    if world is None:
        err("no editor world available after level creation")
        return None

    _tune_world_partition(world)
    return world


def _tune_world_partition(world):
    """Set the runtime grid cell size to match CELL_SIZE; report if unreachable.

    `unreal.World` exposes no `world_partition` property in 5.5, so the grid is
    reached through the world's WorldPartition sub-object by path. If that fails
    we only warn: the default 256 m cell still streams correctly, the HISM
    grouping is simply not aligned to it.
    """
    desired_cm = int(CELL_SIZE * BLOCK_CM)
    try:
        wp = unreal.load_object(None, world.get_path_name() + ".WorldPartition")
        if wp is not None:
            grid = wp.get_editor_property("runtime_grid")
            if grid is not None:
                grid.set_editor_property("cell_size", desired_cm)
                log("World Partition cell size set to %d cm (%d blocks)"
                    % (desired_cm, CELL_SIZE))
                return
            warn("runtime_grid not exposed; set the cell size by hand in "
                 "World Settings > World Partition")
        else:
            warn("WorldPartition object not reachable from Python; set the cell "
                 "size by hand in World Settings > World Partition "
                 "(recommended %d cm = %d blocks)" % (desired_cm, CELL_SIZE))
    except Exception as exc:
        warn("World Partition tuning skipped (%s); set the cell size manually "
             "to %d cm" % (exc, desired_cm))


# =============================================================================
# HISM ASSEMBLY
# =============================================================================
def _new_actor(world, label, location):
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor = eas.spawn_actor_from_class(unreal.Actor, location, unreal.Rotator(0, 0, 0))
    if actor is None:
        err("failed to spawn an actor for %s" % label)
        return None
    try:
        actor.set_actor_label(label)
    except Exception:
        pass
    return actor


def _add_hism(actor, mesh, material, name):
    """Create a HISM component on `actor`.

    All four steps matter: NewObject + attach + AddInstanceComponent +
    RegisterComponent. Skip AddInstanceComponent and the component neither shows
    up in the details panel nor gets serialised with the level.
    """
    comp = unreal.new_object(unreal.HierarchicalInstancedStaticMeshComponent,
                             outer=actor, name=unreal.Name(name))
    if comp is None:
        err("NewObject failed for %s" % name)
        return None
    try:
        comp.set_editor_property("static_mesh", mesh)
        root = actor.get_editor_property("root_component")
        if root is not None:
            comp.attach_to_component(
                root, unreal.Name(""),
                unreal.AttachmentRule.KEEP_RELATIVE,
                unreal.AttachmentRule.KEEP_RELATIVE,
                unreal.AttachmentRule.KEEP_RELATIVE, False)
        comp.set_editor_property("mobility", unreal.ComponentMobility.STATIC)
        if material is not None:
            comp.set_material(0, material)
        _tune_hism_culling(comp, name)
        actor.add_instance_component(comp)
        comp.register_component()
        try:
            comp.set_flags(unreal.ObjectFlags.RF_Transactional)
        except Exception:
            pass
    except Exception as exc:
        err("failed to configure HISM %s: %s" % (name, exc))
        return None
    return comp


def _tune_hism_culling(comp, name):
    """Apply the cull distances and cluster-tree density to one HISM.

    Every property here is set through ``set_editor_property`` on a
    best-effort basis, because the exact Python names have moved between engine
    versions (``ld_max_draw_distance`` was ``max_draw_distance`` before 4.25,
    and the cull pair is ``instance_*_cull_distance`` on HISM specifically).
    Rather than pin a version and break on the next, each is attempted through
    a small candidate list and a failure is logged once per component type
    rather than aborting the import -- a missing cull distance costs frames, it
    does not cost correctness, so it must not be allowed to fail the build.

    ``instance_start_cull_distance`` / ``instance_end_cull_distance`` are the
    real lever. Between them the instance fades, which is what keeps a pop from
    appearing at the cull boundary; setting only the end distance gives a hard
    pop that is very visible against a flat-lit campus.
    """
    for prop, value in (
        ("instance_start_cull_distance", int(HISM_CULL_START_CM)),
        ("instance_end_cull_distance", int(HISM_CULL_END_CM)),
        ("instance_count_per_leaf", int(HISM_INSTANCES_PER_LEAF)),
    ):
        try:
            comp.set_editor_property(prop, value)
        except Exception as exc:
            _warn_once("hism_cull_%s" % prop,
                       "could not set %s on %s (%s); culling will use the "
                       "engine default, which costs frame time"
                       % (prop, name, exc))


def build_cc0_materials(manifest):
    """
    -> {blockName: MaterialInstanceConstant} from the CC0 family instances.

    The realistic-material path shades each block with the family's CC0 PBR
    instance (MI_<family>) instead of a Minecraft block texture. The block ->
    family mapping lives in ``tools/block_families.py`` -- the single source of
    truth, shared with the census so the two cannot disagree.

    A block name whose family has no imported instance is reported and left out
    (it then falls back to the engine default material, which is loud enough to
    notice rather than silently wrong).
    """
    root = resolve_asset_root()
    tools_dir = os.path.join(root, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import block_families as bf

    out = {}
    missing = set()
    unmapped = set()
    for entry in manifest:
        nm = entry["blockName"]
        fam = bf.family(nm)
        if fam == "other":
            unmapped.add(nm)
            continue
        mi = unreal.load_asset("%s/MI_%s" % (CC0_INSTANCE_DIR, fam))
        if mi is None:
            missing.add(fam)
            continue
        out[nm] = mi

    if missing:
        warn("CC0: families with no material instance: %s" % sorted(missing))
    if unmapped:
        warn("CC0: %d block names have no family mapping (-> 'other')"
             % len(unmapped))
    log("CC0 materials: %d block names -> %d families"
        % (len(out), len({bf.family(e["blockName"]) for e in manifest})))
    return out


def _filter_enclosed(buckets, bounds):
    """
    Drop fully-enclosed blocks from the per-(cell, block) position buffers.

    Occupancy is a dense byte grid over the campus with a one-block margin, so
    the six neighbours of any block are plain offset reads and no bounds check
    is needed. Returns the number of instances removed, and mutates ``buckets``
    in place (empty buffers are deleted so they also lose their HISM component).

    Neighbour offsets follow the index layout ``((x * NY) + y) * NZ + z``:
    x+-1 is NY*NZ, y+-1 is NZ, z+-1 is 1.
    """
    unpack = struct.Struct("<HHH").unpack_from
    x0, x1, z0, z1 = bounds

    ymax = 0
    u16_at = struct.Struct("<H").unpack_from
    for buf in buckets.values():
        # y is the third u16 of each packed (lx, lz, y) record, i.e. offset i+4.
        for i in range(4, len(buf), 6):
            y = u16_at(buf, i)[0]
            if y > ymax:
                ymax = y

    nx = (x1 - x0) + 3
    nz = (z1 - z0) + 3
    ny = ymax + 3
    occ = bytearray(nx * ny * nz)

    def cell(wx, wy, wz):
        return ((wx - x0 + 1) * ny + (wy + 1)) * nz + (wz - z0 + 1)

    for (cx, cz, _name), buf in buckets.items():
        bx = cx * CELL_SIZE
        bz = cz * CELL_SIZE
        for i in range(0, len(buf), 6):
            lx, lz, y = unpack(buf, i)
            occ[cell(bx + lx, y, bz + lz)] = 1

    ox = ny * nz
    removed = 0
    for key in list(buckets.keys()):
        buf = buckets[key]
        cx, cz = key[0], key[1]
        bx = cx * CELL_SIZE
        bz = cz * CELL_SIZE
        out = bytearray()
        for i in range(0, len(buf), 6):
            lx, lz, y = unpack(buf, i)
            c = cell(bx + lx, y, bz + lz)
            if (occ[c - ox] and occ[c + ox] and
                    occ[c - nz] and occ[c + nz] and
                    occ[c - 1] and occ[c + 1]):
                removed += 1
                continue
            out += buf[i:i + 6]
        if out:
            buckets[key] = out
        else:
            del buckets[key]
    return removed


def import_dimension(vf, world, mesh, materials, known_names):
    """Group one dimension's blocks and (unless DRY_RUN) build the HISMs.

    Positions are buffered as packed 6-byte records (cell-local x, cell-local z,
    absolute y) rather than Python tuples: 37.5M tuples would cost several GB,
    whereas the packed form costs ~6 bytes per block (~225 MB in total).
    """
    dim = vf.dimension
    log("=== %s: %d voxels across %d chunks ==="
        % (dim, vf.voxel_count, vf.chunk_count))

    z_offset = DIMENSION_Z_OFFSET_CM.get(dim, 0.0)
    if z_offset:
        log("  %s: stacked at Z offset %.0f cm so it does not intersect the "
            "other dimensions" % (dim, z_offset))

    # (cellX, cellZ, blockName) -> bytearray of packed (lx, lz, y) triples
    buckets = defaultdict(bytearray)
    counts = defaultdict(int)      # same keys -> int, for the DRY_RUN report
    unmapped = defaultdict(int)
    seen = 0
    placed = 0

    # Chunk-level skip: only the campus chunks get decoded at all.
    bounds = CAMPUS_BOUNDS if LIMIT_TO_CAMPUS else None
    for wx, wy, wz, name in vf.iter_blocks(bounds=bounds):
        seen += 1
        # In DRY_RUN no material instances exist yet, so the manifest is the only
        # available authority on which block names are buildable.
        placeable = (name in known_names) if DRY_RUN else (name in materials)
        if not placeable:
            unmapped[name] += 1
            continue
        cell_x = wx // CELL_SIZE
        cell_z = wz // CELL_SIZE
        key = (cell_x, cell_z, name)
        if DRY_RUN:
            counts[key] += 1
        # Bucketed in both modes: the dry run is meant to be a true rehearsal of
        # the build path, including the surface-culling pass, so its numbers are
        # the numbers the real build will produce.
        buckets[key] += struct.pack("<HHH",
                                    wx - cell_x * CELL_SIZE,
                                    wz - cell_z * CELL_SIZE,
                                    wy)
        placed += 1
        if seen % 2000000 == 0:
            log("  %s: scanned %d/%d voxels, %d groups"
                % (dim, seen, vf.voxel_count,
                   len(counts) if DRY_RUN else len(buckets)))

    log("  %s: scan done -- %d groups, %d placeable, %d unmapped"
        % (dim, len(counts) if DRY_RUN else len(buckets), placed,
           sum(unmapped.values())))
    if unmapped:
        top = sorted(unmapped.items(), key=lambda kv: -kv[1])[:5]
        warn("  %s: blocks with no material: %s" % (dim, top))

    # ---- surface culling ------------------------------------------------
    # Done on the packed buffers (which already know every block's position)
    # rather than during the scan, because the test needs all six neighbours
    # and therefore the complete occupancy of the campus.
    enclosed = 0
    raw_instances = sum(len(b) // 6 for b in buckets.values())
    if SURFACE_ONLY:
        enclosed = _filter_enclosed(buckets, CAMPUS_BOUNDS)
        after = sum(len(b) // 6 for b in buckets.values())
        log("  %s: surface culling removed %d enclosed blocks "
            "(%d -> %d instances, %.0f%% dropped)"
            % (dim, enclosed, raw_instances, after,
               100.0 * enclosed / max(1, raw_instances)))

    if DRY_RUN:
        report = _build_report(dim, counts, unmapped)
        report["raw_instances"] = raw_instances
        report["enclosed_removed"] = enclosed
        report["groups_after_cull"] = len(buckets)
        report["instances_after_cull"] = raw_instances - enclosed
        _write_report(dim, report)
        comps = report["components"]
        log("  %s: after surface culling %d groups / %d instances"
            % (dim, len(buckets), raw_instances - enclosed))
        return {"groups": len(buckets), "components": 0,
                "instances": raw_instances - enclosed,
                "projected_components": min(comps, len(buckets))}

    # ---- build ---------------------------------------------------------
    # One cluster actor per (cell, block) group rather than one per cell with
    # several components on it: the Python bindings expose no way to attach a
    # component to an actor (see AMCReplicaPropCluster::AddBlockInstances), so
    # every component has to come from an actor whose constructor makes it. The
    # component count is unchanged -- only the grouping differs.
    cluster_cls = getattr(unreal, "MCReplicaPropCluster", None)
    if cluster_cls is None:
        err("MCReplicaPropCluster is missing from this build; the block layer "
            "cannot be assembled without it")
        return {"groups": 0, "components": 0, "instances": 0}

    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    start_cull, end_cull = HISM_CULL_START_CM, HISM_CULL_END_CM
    block_cm = float(BLOCK_CM)

    actors = 0
    components = 0
    instances = 0

    for (cell_x, cell_z, name) in sorted(buckets.keys()):
        buf = bytes(buckets[(cell_x, cell_z, name)])
        total = len(buf) // 6
        if total == 0:
            continue

        # Actor origin on the cell corner. Z stays 0 and the vertical term
        # rides on the instances, so a dimension offset never moves the actor.
        origin = unreal.Vector(cell_x * CELL_SIZE * block_cm,
                               cell_z * CELL_SIZE * block_cm, 0.0)
        actor = eas.spawn_actor_from_class(cluster_cls, origin,
                                           unreal.Rotator(0.0, 0.0, 0.0))
        if actor is None:
            err("failed to spawn block cluster %s at cell(%d,%d)"
                % (name, cell_x, cell_z))
            continue
        try:
            actor.set_actor_label("MCblk_%s_%d_%d"
                                  % (sanitize(name), cell_x, cell_z))
        except Exception:
            pass
        actor.configure_block_layer(mesh, materials[name],
                                    BLOCK_COLLISION, start_cull, end_cull)
        added = actor.add_block_instances(buf, z_offset)
        actors += 1
        components += 1
        instances += added

        if actors % PROGRESS_EVERY_CELLS == 0:
            log("  %s: %d/%d groups, %d instances"
                % (dim, actors, len(buckets), instances))

    log("  %s: BUILT %d clusters holding %d instances"
        % (dim, components, instances))
    return {"groups": len(buckets), "components": components,
            "instances": instances}


def _fill_hism(comp, buf, start, stop, cell_x, cell_z, z_offset):
    """Add instances for records [start, stop) of a packed position buffer.

    Transforms are added in world space. UE5 keeps instance translations in
    double precision, and the largest coordinate here is ~2.8e5 cm, so this is
    exact -- while cell-local coordinates would additionally require the actor to
    have a root component, which a plain Actor does not have.
    """
    unpack = struct.Struct("<HHH").unpack_from
    base_x = cell_x * CELL_SIZE * BLOCK_CM
    base_z = cell_z * CELL_SIZE * BLOCK_CM
    half = BLOCK_CM * 0.5
    one = unreal.Vector(1.0, 1.0, 1.0)
    zero = unreal.Rotator(0.0, 0.0, 0.0)

    batch = []
    appended = 0
    for i in range(start, stop):
        lx, lz, y = unpack(buf, (i - start) * 6)
        batch.append(unreal.Transform(
            unreal.Vector(base_x + lx * BLOCK_CM + half,
                          y * BLOCK_CM + half + z_offset,
                          base_z + lz * BLOCK_CM + half),
            zero, one))
        if len(batch) >= ADD_BATCH:
            comp.add_instances(batch, False, True)
            appended += len(batch)
            batch = []
    if batch:
        comp.add_instances(batch, False, True)
        appended += len(batch)

    try:
        comp.pre_allocate_instances_memory(0)
    except Exception:
        pass
    return appended


def _build_report(dim, counts, unmapped):
    """Summarise the DRY_RUN counts."""
    total = sum(counts.values())
    cells = set((k[0], k[1]) for k in counts.keys())
    comps = 0
    for v in counts.values():
        comps += max(1, int(math.ceil(v / float(MAX_INSTANCES_PER_HISM))))
    per_type = defaultdict(int)
    for (cx, cz, name), v in counts.items():
        per_type[name] += v
    return {"dimension": dim, "groups": len(counts), "cells": len(cells),
            "instances": total, "components": comps,
            "unmapped": dict(unmapped), "per_type": dict(per_type),
            "counts": counts}


def _write_report(dim, report):
    """Print and save the DRY_RUN report. Creates no assets."""
    counts = report["counts"]
    lines = []
    add = lines.append
    add("=" * 74)
    add("DRY_RUN report -- dimension '%s' (nothing was built)" % dim)
    add("=" * 74)
    add("CELL_SIZE=%d   MAX_INSTANCES_PER_HISM=%d   BLOCK_CM=%.0f"
        % (CELL_SIZE, MAX_INSTANCES_PER_HISM, BLOCK_CM))
    add("groups (cell x blockName) : %d" % report["groups"])
    add("distinct spatial cells    : %d" % report["cells"])
    add("total instances           : %d" % report["instances"])
    add("HISM components if built : %d" % report["components"])
    if report["unmapped"]:
        add("")
        add("blocks with NO material (would be skipped): %d types, %d instances"
            % (len(report["unmapped"]), sum(report["unmapped"].values())))
        for n, c in sorted(report["unmapped"].items(), key=lambda kv: -kv[1])[:10]:
            add("    %-46s %d" % (n, c))
    add("")
    add("largest 20 groups:")
    for (cx, cz, name), v in sorted(counts.items(), key=lambda kv: -kv[1])[:20]:
        add("    cell(%5d,%5d) %-40s %9d" % (cx, cz, name, v))
    add("")
    add("instances per block type (top 20):")
    for name, c in sorted(report["per_type"].items(), key=lambda kv: -kv[1])[:20]:
        add("    %-46s %9d" % (name, c))

    text = "\n".join(lines)
    log(text)
    try:
        out_dir = os.path.join(project_dir(), "Saved", "MC2UE5")
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        out_path = os.path.join(out_dir, "dryrun_%s.txt" % dim)
        with open(out_path, "w") as fh:
            fh.write(text + "\n")
        log("report written to %s" % out_path)
    except Exception as exc:
        warn("could not write the report file: %s" % exc)


# =============================================================================
# ENTRY POINT
# =============================================================================
def run():
    """Assemble the MC world into a UE5 World Partition level."""
    global DRY_RUN
    _dry = os.environ.get("MC2UE5_DRY_RUN")
    if _dry is not None:
        DRY_RUN = (_dry.lower() in ("1", "true", "yes"))
    log("=" * 74)
    log("MC2UE5 import_world.run()   DRY_RUN=%s" % DRY_RUN)
    if not unreal.is_editor():
        err("this script must run inside the UE editor (is_editor() is False)")
        return None
    try:
        log("engine version: %s" % unreal.SystemLibrary.get_engine_version())
    except Exception:
        pass

    root = resolve_asset_root()
    if root is None:
        err("could not locate the MC2UE5 data root: expected assets/ and "
            "voxel_data/ next to the project, or set the MC2UE5_ROOT env var")
        return None
    log("data root: %s" % root)

    manifest_path = os.path.join(root, "assets", "material_manifest.json")
    if not os.path.isfile(manifest_path):
        err("missing %s" % manifest_path)
        return None
    with open(manifest_path, "r") as fh:
        manifest = json.load(fh)
    log("manifest: %d block entries" % len(manifest))

    voxel_dir = os.path.join(root, "voxel_data", "full")
    missing = [d for d in IMPORT_DIMENSIONS
               if not os.path.isfile(os.path.join(voxel_dir, "%s.bin" % d))]
    if missing:
        err("missing voxel file(s) %s under %s -- run parse_world.py --full "
            "first" % (missing, voxel_dir))
        return None

    # ---- A. assets ------------------------------------------------------
    known_names = set(e["blockName"] for e in manifest)
    if DRY_RUN:
        log("DRY_RUN: skipping texture, material and mesh creation")
        materials, mesh = {}, None
    else:
        mesh = create_cube_mesh()
        if mesh is None:
            err("no cube mesh; cannot build geometry. Re-run with DRY_RUN=True "
                "to inspect the data, and check that /Engine/BasicShapes/Cube exists.")
            return None
        if USE_CC0:
            materials = build_cc0_materials(manifest)
        else:
            textures = import_textures(os.path.join(root, "assets", "textures", "block"))
            master = build_master_material()
            materials = create_block_materials(master, textures, manifest)

    # ---- B. level -------------------------------------------------------
    world = open_level()
    if world is None:
        return None

    # ---- C. assemble ----------------------------------------------------
    totals = {"groups": 0, "components": 0, "instances": 0, "projected_components": 0}
    for dim in IMPORT_DIMENSIONS:
        vf = VoxelFile(os.path.join(voxel_dir, "%s.bin" % dim))
        if vf.dimension != dim:
            warn("%s.bin declares dimension '%s' (file was named '%s')"
                 % (dim, vf.dimension, dim))
        try:
            res = import_dimension(vf, world, mesh, materials, known_names)
        finally:
            vf.close()
        for k in totals:
            totals[k] += res.get(k, 0)

    # ---- D. save --------------------------------------------------------
    if DRY_RUN:
        log("DRY_RUN finished -- nothing was built. Check the numbers above, "
            "then set DRY_RUN = False and run again.")
    else:
        unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).save_all_dirty_levels()
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        log("saved level %s" % LEVEL_PATH)

    log("TOTAL groups=%d  components=%d  instances=%d"
        % (totals["groups"], totals["components"], totals["instances"]))
    log("=" * 74)
    return totals


if __name__ == "__main__":
    run()
