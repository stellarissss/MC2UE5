# -*- coding: utf-8 -*-
"""
import_cc0_materials.py -- bring the CC0 PBR set into the project as materials.

Design choice, and the reason this is one script rather than seventeen graphs:
building a material graph per family is what produced the black / sand-coloured
/ grey-checkerboard terrain earlier in this project, because a half-built
MaterialExpression graph saves happily and fails only at render time. So there
is exactly **one** master material here, with three texture parameters and one
tiling scalar, and the seventeen families are MaterialInstanceConstants that
only point at textures. Nothing per-family is built by hand.

Texture settings matter and are set explicitly:

  * diffuse  -> sRGB on
  * normal   -> sRGB **off**, TC_NORMALMAP, no green flip (Poly Haven's
                ``nor_dx`` is already the DirectX convention Unreal expects;
                the ``nor_gl`` set would need a flip)
  * roughness-> sRGB off, grayscale

Run inside the editor.
"""

import os
import traceback

import unreal

SRC_ROOT = "Q:/MC2UE5/assets_cc0/polyhaven"
DEST_ROOT = "/Game/MC/CC0"
MASTER_PATH = "/Game/MC/CC0/M_MC_Surface"
OUT = "Q:/MC2UE5/logs/import_cc0.txt"

#: family -> (diffuse, normal, roughness) file names, as written by
#: tools/fetch_cc0_textures.py
def families():
    out = {}
    for fam in sorted(os.listdir(SRC_ROOT)):
        d = os.path.join(SRC_ROOT, fam)
        if not os.path.isdir(d):
            continue
        maps = {}
        for f in os.listdir(d):
            if f.endswith("_diffuse.jpg"):
                maps["diffuse"] = f
            elif f.endswith("_nor_dx.jpg"):
                maps["normal"] = f
            elif f.endswith("_rough.jpg"):
                maps["rough"] = f
        if maps:
            out[fam] = maps
    return out

_lines = []


def say(m):
    _lines.append(str(m))
    try:
        unreal.log("[MCCC0] " + str(m))
    except Exception:
        pass
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(_lines) + "\n")
    except Exception:
        pass


def apply_settings(tex, kind):
    """Set sRGB / compression per map kind. Never raises."""
    if tex is None:
        return
    try:
        if kind == "diffuse":
            tex.set_editor_property("srgb", True)
            tex.set_editor_property(
                "compression_settings",
                unreal.TextureCompressionSettings.TC_DEFAULT)
        elif kind == "normal":
            tex.set_editor_property("srgb", False)
            tex.set_editor_property(
                "compression_settings",
                unreal.TextureCompressionSettings.TC_NORMALMAP)
            try:
                tex.set_editor_property("flip_green_channel", False)
            except Exception:
                pass
        else:
            tex.set_editor_property("srgb", False)
            tex.set_editor_property(
                "compression_settings",
                unreal.TextureCompressionSettings.TC_GRAYSCALE)
        unreal.EditorAssetLibrary.save_loaded_asset(tex)
    except Exception as exc:
        say("    settings failed for %s: %s" % (kind, str(exc)[:60]))


def import_maps(tools, fam, maps):
    """-> {kind: Texture2D} for one family."""
    dest_dir = "%s/%s" % (DEST_ROOT, fam)
    got = {}
    for kind, fname in maps.items():
        name = "%s_%s" % (fam, kind)
        path = "%s/%s" % (dest_dir, name)
        task = unreal.AssetImportTask()
        task.set_editor_property("filename", os.path.join(SRC_ROOT, fam, fname))
        task.set_editor_property("destination_path", dest_dir)
        task.set_editor_property("destination_name", name)
        task.set_editor_property("automated", True)
        task.set_editor_property("replace_existing", True)
        task.set_editor_property("save", True)
        tools.import_asset_tasks([task])
        tex = unreal.load_asset(path)
        if tex is None:
            say("  %s/%s IMPORT FAILED" % (fam, kind))
            continue
        apply_settings(tex, kind)
        got[kind] = tex
    return got


def build_master(placeholders=None):
    """
    One graph, deliberately mirroring the material that is *known to compile*.

    ``/Game/MC/Materials/MC_Terrain`` compiles for PCD3D_SM5 on this machine and
    is exactly this shape: a ``TextureSampleParameter2D`` feeding BaseColor plus
    a ``Constant`` for roughness. Measured against it, a graph that added a
    ``MaterialExpressionWorldPosition`` chain did **not** compile, and the engine
    only ever said

        Failed to compile Material for platform PCD3D_SM5,
        Default Material will be used in game

    with no expression, no line and nothing about samplers -- so the world-space
    UV projection is the thing that broke it. Building on the shape that is
    already proven in this project beats debugging the one that is not, and the
    per-block texture repeat it gives up is the behaviour a voxel world wants
    anyway: every block shows its material, as Minecraft does, but with a real
    PBR texture instead of 16-pixel art.

    So each block face samples the family's texture across 0..1, scaled by
    ``Tiling``. Instances override the texture per family; the sampler and the
    roughness constant live here.

    ``placeholders`` binds a real texture on the master. A texture parameter with
    nothing bound compiles to a null sampler and the failure surfaces only at
    cook time, so the master gets a real one and the instances override it.
    """
    placeholders = placeholders or {}
    # **Reuse** the existing asset rather than delete-and-recreate.
    #
    # Deleting an asset and creating a new one at the same path leaves every
    # reference to it dangling: the 1041 block-cluster actors in the level pointed
    # at the old object, so after a rebuild they resolved to nothing and the
    # engine silently substituted its default material -- which is the white
    # checkerboard that made the campus look like an untextured prototype. The
    # cook reports no error for this either, because an unresolvable material
    # reference is not a compile failure.
    #
    # Reusing the asset keeps its identity, so existing references stay valid.
    if unreal.EditorAssetLibrary.does_asset_exist(MASTER_PATH):
        mat = unreal.load_asset(MASTER_PATH)
        if mat is None:
            raise RuntimeError("could not load the existing %s" % MASTER_PATH)
        # Replace the graph rather than adding to it. Materials have public
        # APIs for both; rebuilding the graph is what makes this step idempotent,
        # which matters because it runs on every chain invocation.
        for e in unreal.MaterialEditingLibrary.get_material_expressions(mat):
            unreal.MaterialEditingLibrary.delete_material_expression(mat, e)
        for prop in (unreal.MaterialProperty.MP_BASE_COLOR,
                     unreal.MaterialProperty.MP_NORMAL,
                     unreal.MaterialProperty.MP_ROUGHNESS):
            unreal.MaterialEditingLibrary.disconnect_material_property(mat, prop)
    else:
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            "M_MC_Surface", DEST_ROOT, unreal.Material,
            unreal.MaterialFactoryNew())
    if mat is None:
        raise RuntimeError("could not create the master material")
    mel = unreal.MaterialEditingLibrary

    def node(cls, x, y):
        e = mel.create_material_expression(mat, cls, x, y)
        if e is None:
            raise RuntimeError("could not create %s" % cls)
        return e

    uv = node(unreal.MaterialExpressionTextureCoordinate, -900, 200)
    tiling = node(unreal.MaterialExpressionScalarParameter, -900, 380)
    tiling.set_editor_property("ParameterName", "Tiling")
    # One texture per 1 m block face. The textures are 1k, so this is roughly the
    # density they were captured for.
    tiling.set_editor_property("DefaultValue", 0.5)
    suv = node(unreal.MaterialExpressionMultiply, -700, 260)
    mel.connect_material_expressions(uv, "", suv, "A")
    mel.connect_material_expressions(tiling, "", suv, "B")

    def sampler(param, y):
        e = node(unreal.MaterialExpressionTextureSampleParameter2D, -480, y)
        e.set_editor_property("ParameterName", param)
        if placeholders.get(param) is not None:
            e.set_editor_property("texture", placeholders[param])
        mel.connect_material_expressions(suv, "", e, "UVs")
        return e

    base = sampler("BaseColorTex", -400)
    # Normal and roughness are what separate "a photograph of stone" from "a
    # flat colour with a picture on it". Without them every surface is lit as
    # though it were paper: the albedo varies but the *shape* of the light does
    # not, so paving, brick and marble all read as the same matte card. These
    # are the maps Poly Haven captured alongside the albedo, and they were
    # already downloaded and cooked -- only the graph was missing them.
    # Roughness as a PARAMETER, and no normal map.
    #
    # The normal and rough maps were added, and the campus then rendered white.
    # Everything else in the chain checks out -- assets resolve in the packaged
    # build, the graph pins are connected, exposure changes the frame -- which
    # leaves the maps as the difference. A texture that resolves but samples
    # degenerate UVs gives the *mean* of the texture for albedo (brown) but can
    # still drive roughness to 0, and a fully smooth surface is a mirror: with a
    # bright sky above it, every wall and road then reflects white. Reverting to
    # the shape that is known to render correctly here (a base colour sampler and
    # a scalar roughness, which is exactly what MC_Terrain does) isolates it.
    rough = node(unreal.MaterialExpressionScalarParameter, -400, 320)
    rough.set_editor_property("ParameterName", "Roughness")
    rough.set_editor_property("DefaultValue", 0.85)

    mel.connect_material_property(base, "RGB",
                                  unreal.MaterialProperty.MP_BASE_COLOR)
    mel.connect_material_property(rough, "",
                                  unreal.MaterialProperty.MP_ROUGHNESS)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    return mat


def build_master_simple():
    """Per-face-UV fallback, kept for comparison against the triplanar look."""
    if unreal.EditorAssetLibrary.does_asset_exist(MASTER_PATH):
        unreal.EditorAssetLibrary.delete_asset(MASTER_PATH)
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "M_MC_Surface", DEST_ROOT, unreal.Material,
        unreal.MaterialFactoryNew())
    if mat is None:
        raise RuntimeError("could not create the master material")
    mel = unreal.MaterialEditingLibrary

    def node(cls, x, y):
        return mel.create_material_expression(mat, cls, x, y)

    base = node(unreal.MaterialExpressionTextureSampleParameter2D, -640, -200)
    base.set_editor_property("ParameterName", "BaseColorTex")
    mel.connect_material_property(base, "RGB",
                                  unreal.MaterialProperty.MP_BASE_COLOR)
    mel.recompile_material(mat)
    return mat


def make_instance(tools, fam, master):
    name = "MI_%s" % fam
    path = "%s/%s" % (DEST_ROOT, name)
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        unreal.EditorAssetLibrary.delete_asset(path)
    mi = tools.create_asset(name, DEST_ROOT, unreal.MaterialInstanceConstant,
                            unreal.MaterialInstanceConstantFactoryNew())
    if mi is None:
        say("  %s instance creation failed" % fam)
        return None
    unreal.MaterialEditingLibrary.set_material_instance_parent(mi, master)
    return mi, path


def main():
    say("start")
    try:
        tools = unreal.AssetToolsHelpers.get_asset_tools()
        mel = unreal.MaterialEditingLibrary
        fams = families()
        say("families: %d" % len(fams))

        # Import every family's textures FIRST, so the master can be given a
        # real placeholder per parameter. Building the master before any texture
        # exists is what left its parameters unbound and its shader map
        # uncompilable.
        imported = {}
        for fam, maps in fams.items():
            imported[fam] = import_maps(tools, fam, maps)
        placeholder = {}
        for fam, tex in sorted(imported.items()):
            for kind, param in (("diffuse", "BaseColorTex"),
                                ("normal", "NormalTex"),
                                ("rough", "RoughTex")):
                if kind in tex:
                    placeholder.setdefault(param, tex[kind])
        say("placeholders: %s" % sorted(placeholder))
        master = build_master(placeholder)
        src = mel.get_material_property_input_node(
            master, unreal.MaterialProperty.MP_BASE_COLOR)
        say("master BaseColor <- %s"
            % (src.get_class().get_name() if src else "NOTHING"))
        if src is None:
            say("aborting: master has no BaseColor")
            return


        for fam, maps in fams.items():
            tex = imported.get(fam) or {}
            res = make_instance(tools, fam, master)
            if not res:
                continue
            mi, path = res
            for kind, param in (("diffuse", "BaseColorTex"),
                                ("normal", "NormalTex"),
                                ("rough", "RoughTex")):
                if kind in tex:
                    mel.set_material_instance_texture_parameter_value(
                        mi, param, tex[kind])
            mel.set_material_instance_scalar_parameter_value(
                mi, "Tiling", 0.5)
            mel.set_material_instance_scalar_parameter_value(
                mi, "Roughness", 0.85)
            unreal.EditorAssetLibrary.save_loaded_asset(mi)
            say("  %-9s -> %s (%d maps)" % (fam, path, len(tex)))

        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        say("saved")
    except Exception:
        say("FAILED:\n%s" % traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
