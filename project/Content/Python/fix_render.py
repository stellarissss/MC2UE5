# -*- coding: utf-8 -*-
"""
fix_render.py -- repair the terrain material and the lighting.

Two independent defects make the campus render as a black void.

1. MC_Terrain has the whole 18-node graph (four samplers, the slope and height
   blends, the final LinearInterpolate) but MP_BASE_COLOR has **no input**.
   build_release_level.py calls ``connect_material_property(final, "Result",
   MP_BASE_COLOR)`` and then "verifies" it -- but its verifier falls back to
   counting expressions (>= 12) when the input-reading getters are unavailable,
   and 18 >= 12, so the unconnected material was saved. BaseColor with nothing
   on it compiles to opaque black.

   ``connect_material_property`` needs the *output pin name* of the source
   expression, and that differs by expression class (most use "", some use
   "Result"). This script tries the plausible names, **verifies each with
   get_material_property_input_node**, and reports which one took -- rather than
   assuming.

2. Every light is STATIONARY/STATIC, which needs a baked lighting build. A
   headless build never runs Lightmass, so static meshes get no baked indirect
   light and the sky light contributes nothing until it is captured: any surface
   the directional light does not hit directly renders black. Moving the lights
   to Movable and turning on the sky light's real-time capture removes the
   dependency on a bake entirely.

Run inside the editor:
    UnrealEditor.exe MCReplica.uproject -ExecutePythonScript=<this file> \
        -unattended -nopause -nosplash
"""

import unreal

OUT = "Q:/MC2UE5/logs/fix_render.txt"
MATERIAL_DIR = "/Game/MC/Materials"
TERRAIN_MAT = "/Game/MC/Materials/MC_Terrain"
PROBE_MAT = "/Game/MC/Materials/MC_ConnProbe"

_lines = []


def say(m):
    _lines.append(str(m))
    unreal.log("[MCFIX3] " + str(m))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


# --------------------------------------------------------------------------
# 1. which output-pin name actually connects?
# --------------------------------------------------------------------------

def probe_output_name(mel):
    """
    Connect a Constant3Vector to BaseColor on a scratch material, trying each
    plausible output-pin name, and report which one the engine accepts.

    Returns the name that worked, or None.
    """
    if unreal.EditorAssetLibrary.does_asset_exist(PROBE_MAT):
        unreal.EditorAssetLibrary.delete_asset(PROBE_MAT)
    mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "MC_ConnProbe", MATERIAL_DIR, unreal.Material,
        unreal.MaterialFactoryNew())
    if mat is None:
        say("probe: could not create scratch material")
        return None

    node = mel.create_material_expression(
        mat, unreal.MaterialExpressionConstant3Vector, 0, 0)
    try:
        node.set_editor_property(
            "Constant", unreal.LinearColor(1.0, 0.0, 0.0, 1.0))
    except Exception as exc:
        say("probe: constant set failed: %s" % exc)

    winner = None
    for name in ("", "Result", "RGB", "Const", "Out"):
        ok = False
        try:
            ok = bool(mel.connect_material_property(
                node, name, unreal.MaterialProperty.MP_BASE_COLOR))
        except Exception as exc:
            say("probe: connect(%r) raised %s" % (name, exc))
            continue
        src = None
        try:
            src = mel.get_material_property_input_node(
                mat, unreal.MaterialProperty.MP_BASE_COLOR)
        except Exception:
            pass
        hit = src is not None
        say("probe: output=%-8r connect=%s readback=%s"
            % (name, ok, src.get_class().get_name() if hit else "NOTHING"))
        if hit and winner is None:
            winner = name
            break

    unreal.EditorAssetLibrary.delete_asset(PROBE_MAT)
    return winner


# --------------------------------------------------------------------------
# 2. repair MC_Terrain
# --------------------------------------------------------------------------

def find_base_source(mel, mat):
    """
    The expression that should drive BaseColor.

    build_release_level.py's graph ends in a LinearInterpolate (grass/sand ->
    stone -> dirt). The terminal one -- the last created, which sits furthest
    right -- is the node the builder wires to BaseColor, so pick it by editor X
    rather than by list order (get_material_expressions does not promise one).
    """
    exprs = mel.get_material_expressions(mat) or []
    lerps = [e for e in exprs
             if e.get_class().get_name() == "MaterialExpressionLinearInterpolate"]
    if not lerps:
        return None, exprs

    def x_of(e):
        try:
            return e.get_editor_property("material_expression_editor_x")
        except Exception:
            return 0

    lerps.sort(key=x_of)
    return lerps[-1], exprs


def repair_terrain_material(mel, out_name):
    mat = unreal.load_asset(TERRAIN_MAT)
    if mat is None:
        say("MC_Terrain: MISSING")
        return False

    src = None
    try:
        src = mel.get_material_property_input_node(
            mat, unreal.MaterialProperty.MP_BASE_COLOR)
    except Exception:
        pass
    if src is not None:
        say("MC_Terrain: BaseColor already <- %s" % src.get_class().get_name())
        return True

    node, _exprs = find_base_source(mel, mat)
    if node is None:
        say("MC_Terrain: no LinearInterpolate to drive BaseColor")
        return False
    say("MC_Terrain: candidate terminal node = %s at x=%s"
        % (node.get_class().get_name(),
           node.get_editor_property("material_expression_editor_x")))

    names = [out_name] if out_name is not None else []
    names += ["", "Result"]
    seen = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        try:
            mel.connect_material_property(
                node, name, unreal.MaterialProperty.MP_BASE_COLOR)
        except Exception as exc:
            say("  connect(%r) raised %s" % (name, exc))
            continue
        got = None
        try:
            got = mel.get_material_property_input_node(
                mat, unreal.MaterialProperty.MP_BASE_COLOR)
        except Exception:
            pass
        say("  connect(%r) -> readback %s"
            % (name, got.get_class().get_name() if got else "NOTHING"))
        if got is not None:
            try:
                mel.recompile_material(mat)
            except Exception:
                pass
            unreal.EditorAssetLibrary.save_loaded_asset(mat)
            say("MC_Terrain: BaseColor now driven by %s (output %r)"
                % (got.get_class().get_name(), name))
            return True
    return False


# --------------------------------------------------------------------------
# 3. lighting
# --------------------------------------------------------------------------

def fix_lights(w):
    changed = 0
    for a in unreal.EditorLevelLibrary.get_all_level_actors():
        cls = a.get_class().get_name()
        if cls in ("DirectionalLight", "PointLight", "SpotLight", "RectLight"):
            comp = a.get_component_by_class(unreal.LightComponent)
            if comp is None:
                say("light %s has no LightComponent" % cls)
                continue
            try:
                comp.set_mobility(unreal.ComponentMobility.MOVABLE)
                changed += 1
                say("light %s -> Movable" % cls)
            except Exception as exc:
                say("light %s mobility failed: %s" % (cls, exc))
        elif cls == "SkyLight":
            comp = a.get_component_by_class(unreal.SkyLightComponent)
            if comp is None:
                say("SkyLight has no SkyLightComponent")
                continue
            try:
                comp.set_mobility(unreal.ComponentMobility.MOVABLE)
            except Exception as exc:
                say("SkyLight mobility failed: %s" % exc)
            # Real-time capture is what makes a sky light contribute without a
            # baked lighting build; without it the level's indirect light is
            # whatever was captured at build time, which was nothing.
            for prop in ("real_time_capture", "bRealTimeCapture"):
                try:
                    comp.set_editor_property(prop, True)
                    say("SkyLight real_time_capture=True via %s" % prop)
                    break
                except Exception:
                    continue
            try:
                comp.set_editor_property("intensity", 1.0)
            except Exception:
                pass
            try:
                comp.recapture_sky()
                say("SkyLight recaptured")
            except Exception as exc:
                say("SkyLight recapture: %s" % exc)
            changed += 1
    return changed


def main():
    w = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    say("world=%s" % ("yes" if w else "NO"))
    mel = unreal.MaterialEditingLibrary

    say("")
    say("=== step 1: output-pin name probe ===")
    name = probe_output_name(mel)

    say("")
    say("=== step 2: repair MC_Terrain ===")
    ok_mat = repair_terrain_material(mel, name)

    say("")
    say("=== step 3: lights ===")
    n = fix_lights(w)
    say("lights changed: %d" % n)

    try:
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        say("saved dirty packages")
    except Exception as exc:
        say("save failed: %s" % exc)

    say("")
    say("RESULT material=%s" % ("OK" if ok_mat else "FAILED"))
    say("--- done ---")


if __name__ == "__main__":
    main()
