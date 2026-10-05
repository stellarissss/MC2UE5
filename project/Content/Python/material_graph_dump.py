# -*- coding: utf-8 -*-
"""
material_graph_dump.py -- show every expression and connection in a material.

`render_diag.py` reported MC_Terrain with 18 expressions but no input on
BaseColor, which should be impossible: build_release_level.py connects a
LinearInterpolate to MP_BASE_COLOR and then verifies it before saving. Either
the property really is unconnected, or get_material_property_input_node does not
report a connection that connect_material_property made. This walks the graph so
the two can be told apart, and re-checks the textures at their real paths.

Run inside the editor:
    UnrealEditor.exe MCReplica.uproject -ExecutePythonScript=<this file> \
        -unattended -nopause -nosplash
"""

import unreal

OUT = "Q:/MC2UE5/logs/material_dump.txt"
_lines = []


def say(m):
    _lines.append(str(m))
    unreal.log("[MCDUMP] " + str(m))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def dump(path):
    mat = unreal.load_asset(path)
    if mat is None:
        say("%s: MISSING" % path)
        return
    mel = unreal.MaterialEditingLibrary
    say("=== %s ===" % path)
    exprs = mel.get_material_expressions(mat)
    say("expressions=%d" % len(exprs))
    for e in exprs:
        try:
            out_conns = []
            n = mel.get_num_material_expression_outputs
        except Exception:
            n = None
        line = "  %-46s" % e.get_class().get_name()
        # Which material property, if any, does this feed?
        try:
            for prop in (unreal.MaterialProperty.MP_BASE_COLOR,
                         unreal.MaterialProperty.MP_ROUGHNESS,
                         unreal.MaterialProperty.MP_METALLIC,
                         unreal.MaterialProperty.MP_EMISSIVE_COLOR,
                         unreal.MaterialProperty.MP_NORMAL):
                src = mel.get_material_property_input_node(mat, prop)
                if src is not None and src.get_name() == e.get_name():
                    line += " -> %s" % prop
        except Exception:
            pass
        say(line)

    say("  --- material property inputs ---")
    for prop in (unreal.MaterialProperty.MP_BASE_COLOR,
                 unreal.MaterialProperty.MP_ROUGHNESS,
                 unreal.MaterialProperty.MP_METALLIC,
                 unreal.MaterialProperty.MP_EMISSIVE_COLOR,
                 unreal.MaterialProperty.MP_NORMAL,
                 unreal.MaterialProperty.MP_OPACITY):
        try:
            src = mel.get_material_property_input_node(mat, prop)
            say("  %-28s <- %s" % (prop, src.get_class().get_name()
                                   if src else "NOTHING"))
        except Exception as exc:
            say("  %-28s <%s>" % (prop, exc))


def textures():
    say("")
    say("=== textures (real paths) ===")
    for n in ("grass_block_top", "dirt", "stone", "sand"):
        p = "/Game/MC/Textures/%s" % n
        t = unreal.load_asset(p)
        if t is None:
            say("%s: MISSING" % p)
            continue
        bits = []
        for prop in ("srgb", "compression_settings", "mip_gen_settings",
                     "filter", "never_stream", "lod_group"):
            try:
                bits.append("%s=%s" % (prop, t.get_editor_property(prop)))
            except Exception:
                pass
        try:
            bits.append("size=%dx%d" % (t.blueprint_get_size_x(),
                                        t.blueprint_get_size_y()))
        except Exception:
            pass
        say("%s: %s" % (p, " ".join(bits)))


def main():
    dump("/Game/MC/Materials/MC_Terrain")
    say("")
    dump("/Game/MC/Materials/MC_Character")
    textures()
    say("--- done ---")


if __name__ == "__main__":
    main()
