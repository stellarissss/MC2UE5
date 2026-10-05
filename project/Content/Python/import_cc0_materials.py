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


def build_master():
    """One graph: three texture parameters and a tiling scalar."""
    if unreal.EditorAssetLibrary.does_asset_exist(MASTER_PATH):
        unreal.EditorAssetLibrary.delete_asset(MASTER_PATH)
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

    def sampler(param, x, y, normal=False):
        e = node(unreal.MaterialExpressionTextureSampleParameter2D, x, y)
        e.set_editor_property("ParameterName", param)
        return e

    # Tiling: vertex UVs are per-face 0..1, so one texture repeat per
    # `1/Tiling` faces. Driving it from a scalar keeps the real-world size of
    # the pattern adjustable per instance without touching the graph.
    uv = node(unreal.MaterialExpressionTextureCoordinate, -1100, 300)
    tiling = node(unreal.MaterialExpressionScalarParameter, -1100, 440)
    tiling.set_editor_property("ParameterName", "Tiling")
    tiling.set_editor_property("DefaultValue", 0.25)
    mul = node(unreal.MaterialExpressionMultiply, -880, 340)
    mel.connect_material_expressions(uv, "", mul, "A")
    mel.connect_material_expressions(tiling, "", mul, "B")

    base = sampler("BaseColorTex", -640, -200)
    nrm = sampler("NormalTex", -640, 40)
    rough = sampler("RoughTex", -640, 260)
    for s in (base, nrm, rough):
        mel.connect_material_expressions(mul, "", s, "UVs")

    mel.connect_material_property(base, "RGB",
                                  unreal.MaterialProperty.MP_BASE_COLOR)
    mel.connect_material_property(nrm, "RGB",
                                  unreal.MaterialProperty.MP_NORMAL)
    mel.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_loaded_asset(mat)
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

        master = build_master()
        src = mel.get_material_property_input_node(
            master, unreal.MaterialProperty.MP_BASE_COLOR)
        say("master BaseColor <- %s"
            % (src.get_class().get_name() if src else "NOTHING"))
        if src is None:
            say("aborting: master has no BaseColor")
            return

        for fam, maps in fams.items():
            tex = import_maps(tools, fam, maps)
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
                mi, "Tiling", 0.25)
            unreal.EditorAssetLibrary.save_loaded_asset(mi)
            say("  %-9s -> %s (%d maps)" % (fam, path, len(tex)))

        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        say("saved")
    except Exception:
        say("FAILED:\n%s" % traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
