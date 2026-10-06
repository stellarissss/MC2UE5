# -*- coding: utf-8 -*-
"""
setup_lighting.py -- configure the sun, sky and exposure for a readable campus.

The first lit render showed two problems that look like material bugs but are
lighting ones:

  * **surfaces in shadow crushed to a dark navy** -- a wall facing away from the
    sun is lit only by the sky light, and with a weak sky light there is nothing
    left to see. A concrete wall should read as concrete in shade, not as a
    silhouette.
  * **a very bright ground next to a very dark wall** -- that range is what
    auto-exposure then fights, and whichever way it settles it crushes one end.

So this sets an explicit exposure window instead of leaving auto-exposure free,
raises the sky light so shade has information in it, and keeps the sun at a
raking angle for shape. Everything here is a property on actors that already
exist in the level, so it is idempotent and re-runnable.

Run inside the editor.
"""

import traceback

import unreal

OUT = "Q:/MC2UE5/logs/setup_lighting.txt"
L = []


def say(s):
    L.append(str(s))
    try:
        unreal.log("[MCLIGHT] " + str(s))
    except Exception:
        pass
    try:
        open(OUT, "w").write("\n".join(L) + "\n")
    except Exception:
        pass


def set_prop(obj, name, value, label=""):
    try:
        obj.set_editor_property(name, value)
        say("  %s.%s = %s" % (label, name, value))
        return True
    except Exception as exc:
        say("  %s.%s FAILED: %s" % (label, name, str(exc)[:70]))
        return False


def main():
    say("start")
    try:
        w = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        if w is None:
            say("no editor world")
            return

        # ---- sun -----------------------------------------------------------
        sun = None
        for a in unreal.EditorLevelLibrary.get_all_level_actors():
            if a.get_class().get_name() == "DirectionalLight":
                sun = a
                break
        if sun:
            say("DirectionalLight: %s" % sun.get_actor_label())
            # Raking, not overhead: a low sun gives vertical surfaces a lit and
            # an unlit side, which is what makes a blocky building read as a
            # building instead of a flat mass.
            sun.set_actor_rotation(unreal.Rotator(pitch=-42.0, yaw=-125.0,
                                                  roll=0.0), False)
            c = sun.get_editor_property("directional_light_component")
            if c:
                set_prop(c, "intensity", 6.0, "sun")
                # Warm late-afternoon-ish white; slightly warmer than D65 reads
                # as sunlight rather than as a flat studio light.
                set_prop(c, "light_color",
                         unreal.Color(r=255, g=248, b=235, a=255), "sun")
                set_prop(c, "cast_shadows", True, "sun")
                # Keep some shadow contrast instead of a flat fill.
                try:
                    c.set_editor_property("dynamic_shadow_softness", 0.5)
                except Exception:
                    pass
        else:
            say("no DirectionalLight found")

        # ---- sky light -----------------------------------------------------
        sky = None
        for a in unreal.EditorLevelLibrary.get_all_level_actors():
            if a.get_class().get_name() == "SkyLight":
                sky = a
                break
        if sky:
            say("SkyLight: %s" % sky.get_actor_label())
            c = sky.get_editor_property("light_component")
            if c:
                # The single biggest cause of "everything in shade is black".
                set_prop(c, "intensity", 3.0, "skylight")
                set_prop(c, "mobility", unreal.ComponentMobility.MOVABLE,
                         "skylight")
                # Real-time capture so it picks up the atmosphere; without this
                # it keeps whatever it baked the first time the level loaded.
                set_prop(c, "real_time_capture", True, "skylight")
                set_prop(c, "b_lower_hemisphere_is_black", False, "skylight")
        else:
            say("no SkyLight found")

        # ---- atmosphere ----------------------------------------------------
        for a in unreal.EditorLevelLibrary.get_all_level_actors():
            if a.get_class().get_name() == "SkyAtmosphere":
                say("SkyAtmosphere: %s" % a.get_actor_label())
                for name, val in (("rayleigh_scattering_scale", 0.0331),
                                  ("mie_scattering_scale", 0.003996)):
                    set_prop(a, name, val, "atmosphere")

        # ---- fog -----------------------------------------------------------
        for a in unreal.EditorLevelLibrary.get_all_level_actors():
            if a.get_class().get_name() == "ExponentialHeightFog":
                c = a.get_editor_property("component")
                if c:
                    # Fog eats distance contrast. Enough to give depth, not
                    # enough to flatten the far side of the campus.
                    set_prop(c, "fog_density", 0.008, "fog")
                    set_prop(c, "fog_height_falloff", 0.15, "fog")
                    set_prop(c, "volumetric_fog", True, "fog")

        # ---- post process --------------------------------------------------
        # One unbound volume: exposure window, bloom, and a mild grade. UE
        # applies the lowest-priority unbound volume; with exactly one there is
        # no ambiguity.
        ppv = None
        for a in unreal.EditorLevelLibrary.get_all_level_actors():
            if a.get_class().get_name() == "PostProcessVolume":
                ppv = a
                break
        if ppv is None:
            eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
            ppv = eas.spawn_actor_from_class(
                unreal.PostProcessVolume, unreal.Vector(0, 0, 0),
                unreal.Rotator(0, 0, 0))
            say("spawned PostProcessVolume")
        if ppv:
            ppv.set_actor_label("MC_PostProcess")
            set_prop(ppv, "unbound", True, "ppv")
            s = ppv.get_editor_property("settings")
            # Auto exposure inside a fixed window. Wide open it swings between
            # blown-out sky and black walls as the view rotates; a window keeps
            # it in a usable band while still adapting.
            over = []
            over.append(("auto_exposure_method",
                         unreal.AutoExposureMethod.AEM_HISTOGRAM))
            over.append(("auto_exposure_min_brightness", 0.4))
            over.append(("auto_exposure_max_brightness", 1.8))
            over.append(("auto_exposure_bias", 0.4))
            over.append(("bloom_intensity", 0.6))
            over.append(("bloom_threshold", 1.0))
            over.append(("scene_fringe_intensity", 0.2))
            over.append(("vignette_intensity", 0.25))
            over.append(("motion_blur_amount", 0.0))
            for k, v in over:
                try:
                    s.set_editor_property(k, v)
                    say("  ppv.%s = %s" % (k, v))
                except Exception as exc:
                    say("  ppv.%s FAILED: %s" % (k, str(exc)[:60]))
            try:
                ppv.set_editor_property("settings", s)
            except Exception as exc:
                say("  writing settings back failed: %s" % str(exc)[:60])

        unreal.EditorLevelLibrary.save_current_level()
        say("saved; save_asset -> %s" % unreal.EditorAssetLibrary.save_asset(
            "/Game/Maps/MCReplica", only_if_is_dirty=False))
    except Exception:
        say("FAILED:\n%s" % traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
