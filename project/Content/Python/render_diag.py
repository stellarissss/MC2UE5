# -*- coding: utf-8 -*-
"""
render_diag.py -- dump everything that can make the campus render black.

"All objects are black and the school cannot be seen" has a short list of
causes, and they are indistinguishable from a screenshot alone:

  * a material whose BaseColor property has no expression on it, or which is
    missing altogether (the mesh falls back to a default);
  * a mesh with no material assigned at all;
  * a texture that imported as black or as a no-op placeholder;
  * no light, or a light with zero intensity, or a light whose mobility means
    it needs baked lighting that a headless build never produced;
  * a sky/fog setup that drowns everything in a flat colour.

This writes one report covering all of them, so the next step is decided by
data rather than by guessing at another screenshot.

Run inside the editor:
    UnrealEditor.exe MCReplica.uproject -ExecutePythonScript=<this file> \
        -unattended -nopause -nosplash
"""

import unreal

OUT = "Q:/MC2UE5/logs/render_diag.txt"
_lines = []


def say(m):
    _lines.append(str(m))
    unreal.log("[MCDIAG3] " + str(m))


def flush():
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def world():
    return unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()


# --------------------------------------------------------------------------
# materials
# --------------------------------------------------------------------------

def material_report(path):
    mat = unreal.load_asset(path)
    if mat is None:
        return ["%s: MISSING" % path]

    out = ["%s" % path]
    mel = unreal.MaterialEditingLibrary
    try:
        exprs = mel.get_material_expressions(mat)
        out.append("  expressions=%d" % len(exprs))
    except Exception as exc:
        out.append("  expressions=<%s>" % exc)

    # The decisive question: is anything driving BaseColor?
    for prop, label in ((unreal.MaterialProperty.MP_BASE_COLOR, "base"),
                        (unreal.MaterialProperty.MP_ROUGHNESS, "rough"),
                        (unreal.MaterialProperty.MP_METALLIC, "metal"),
                        (unreal.MaterialProperty.MP_EMISSIVE_COLOR, "emis")):
        try:
            src = mel.get_material_property_input_node(mat, prop)
            out.append("  %s=%s" % (label, src.get_class().get_name()
                                    if src else "NOTHING"))
        except Exception as exc:
            out.append("  %s=<%s>" % (label, exc))

    try:
        out.append("  blend=%s shading=%s twosided=%s"
                   % (mat.get_editor_property("blend_mode"),
                      mat.get_editor_property("shading_model"),
                      mat.get_editor_property("two_sided")))
    except Exception:
        pass

    # The material must actually compile; a broken graph is a black surface.
    try:
        ok = mel.recompile_material(mat)
        out.append("  recompiled=%s" % ok)
    except Exception as exc:
        out.append("  recompile=<%s>" % exc)
    return out


# --------------------------------------------------------------------------
# textures
# --------------------------------------------------------------------------

def texture_report(path):
    tex = unreal.load_asset(path)
    if tex is None:
        return ["%s: MISSING" % path]
    bits = []
    for prop in ("srgb", "compression_settings", "mip_gen_settings",
                 "filter", "never_stream"):
        try:
            bits.append("%s=%s" % (prop, tex.get_editor_property(prop)))
        except Exception:
            pass
    try:
        bits.append("size=%dx%d" % (tex.blueprint_get_size_x(),
                                    tex.blueprint_get_size_y()))
    except Exception:
        pass
    return ["%s: %s" % (path, " ".join(bits))]


# --------------------------------------------------------------------------
# lighting
# --------------------------------------------------------------------------

def light_report(w):
    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    rows = []
    for a in actors:
        cls = a.get_class().get_name()
        if cls in ("DirectionalLight", "SkyLight", "SkyAtmosphere",
                   "ExponentialHeightFog", "PointLight", "SpotLight",
                   "RectLight", "AtmosphericFog"):
            try:
                comp = a.get_component_by_class(unreal.LightComponent)
            except Exception:
                comp = None
            detail = []
            if comp:
                for prop in ("intensity", "mobility", "visible",
                             "cast_shadows"):
                    try:
                        detail.append("%s=%s" % (prop,
                                                 comp.get_editor_property(prop)))
                    except Exception:
                        pass
            try:
                loc = a.get_actor_location()
                detail.append("at=(%.0f,%.0f,%.0f)" % (loc.x, loc.y, loc.z))
            except Exception:
                pass
            rows.append("  %-22s %s" % (cls, " ".join(detail)))
    return rows or ["  (no lighting actors found)"]


def main():
    w = world()
    say("world=%s" % ("yes" if w else "NO"))

    say("")
    say("=== materials ===")
    mat_dir = "/Game/MC/Materials"
    try:
        names = unreal.EditorAssetLibrary.list_assets(mat_dir, True, False)
    except Exception as exc:
        names = []
        say("  list_assets failed: %s" % exc)
    if not names:
        say("  (no materials under %s)" % mat_dir)
    for n in names:
        p = n.split(".")[0]
        for line in material_report(p):
            say(line)

    say("")
    say("=== textures (terrain + character) ===")
    for p in ("/Game/MC/Textures/T_grass_block_top",
              "/Game/MC/Textures/T_dirt",
              "/Game/MC/Textures/T_stone",
              "/Game/MC/Textures/T_sand",
              "/Game/MC/Character/char_skin"):
        for line in texture_report(p):
            say(line)

    say("")
    say("=== lights ===")
    for line in light_report(w):
        say(line)

    say("")
    say("=== terrain mesh material assignment ===")
    for name in ("T_overworld_00_00", "T_overworld_00_01",
                 "T_overworld_01_00", "T_overworld_01_01"):
        mesh = unreal.load_asset("/Game/MC/Terrain/%s" % name)
        if mesh is None:
            say("  %s: MISSING" % name)
            continue
        try:
            mats = [mesh.get_material(i)
                    for i in range(mesh.get_num_lods() and 1 or 1)]
        except Exception:
            mats = []
        try:
            n = mesh.get_num_sections(0)
        except Exception:
            n = 1
        got = []
        for i in range(n):
            try:
                m = mesh.get_material(i)
                got.append(m.get_name() if m else "NONE")
            except Exception as exc:
                got.append("<%s>" % exc)
        say("  %s sections=%d materials=%s" % (name, n, got))

    say("")
    say("=== character mesh materials ===")
    for name in ("CH_head", "CH_torso", "CH_arm", "CH_leg"):
        mesh = unreal.load_asset("/Game/MC/Character/%s" % name)
        if mesh is None:
            say("  %s: MISSING" % name)
            continue
        try:
            m = mesh.get_material(0)
            say("  %s material=%s" % (name, m.get_name() if m else "NONE"))
        except Exception as exc:
            say("  %s <%s>" % (name, exc))

    say("")
    say("--- done ---")
    flush()


if __name__ == "__main__":
    main()
