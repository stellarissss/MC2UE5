# -*- coding: utf-8 -*-
"""
setup_lighting.py -- physically consistent lighting and exposure for the campus.

The previous pass set a sun of "6 intensity" and a loose auto-exposure window,
which is how a scene ends up simultaneously blown out and muddy: the light
levels mean nothing, so nothing can be judged, and auto-exposure then chases
whatever happens to be on screen.

This sets the whole chain to real values instead, so each number has one correct
answer rather than a taste:

    DirectionalLight   100,000 lux   clear-sky sun at midday
    SkyLight           1.0           real-time capture from the atmosphere,
                                     whose luminance is itself derived from the
                                     sun, so the two stay consistent
    SkyAtmosphere      physical      Rayleigh / Mie defaults
    PostProcess        EV100 15      the exposure that puts an 18% grey card at
                                     18% grey under 100,000 lux

The exposure is not a preference: EV100 = log2(Lux / pi), so 100,000 lux gives
log2(31831) = 15.0. Get that pair right and the image is correctly exposed by
construction; get it wrong and no amount of grading recovers it.

Sun angle is chosen for shape rather than drama: a high-ish sun at an oblique
azimuth lights one face of each building and leaves another in sky light, which
is what makes a blocky mass read as a building instead of a silhouette.
"""

import traceback

import unreal

OUT = "Q:/MC2UE5/logs/setup_lighting.txt"
L = []

#: Clear-sky midday sun, in lux. The single source of truth for scene brightness.
SUN_LUX = 100000.0

#: EV100 = log2(SUN_LUX / pi). Spelled out so the relationship is visible:
#: 100000 / 3.14159 = 31831, log2(31831) = 14.96.
EV100 = 15.0

#: Multiplier over the atmosphere's own luminance. 1.0 is physical; raising it
#: buys fill in shadow at the cost of contrast.
SKY_INTENSITY = 1.0


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


def of_class(actors, cls):
    return [a for a in actors if a.get_class().get_name() == cls]


def apply(obj, name, value, label):
    """set_editor_property that reports instead of raising."""
    try:
        obj.set_editor_property(name, value)
        say("  %-12s %-30s = %s" % (label, name, value))
        return True
    except Exception as exc:
        say("  %-12s %-30s FAILED: %s" % (label, name, str(exc)[:70]))
        return False


def main():
    say("start")
    try:
        w = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        if w is None:
            say("no editor world")
            return
        actors = unreal.EditorLevelLibrary.get_all_level_actors()

        # ---- sun -----------------------------------------------------------
        suns = of_class(actors, "DirectionalLight")
        say("DirectionalLight x%d" % len(suns))
        for sun in suns:
            # Oblique but high: one lit face, one sky-lit face, short shadows that
            # do not swallow the courtyard.
            sun.set_actor_rotation(
                unreal.Rotator(pitch=-48.0, yaw=-135.0, roll=0.0), False)
            c = sun.get_editor_property("directional_light_component")
            if not c:
                continue
            apply(c, "intensity", SUN_LUX, "sun")
            apply(c, "cast_shadows", True, "sun")
            # Mobility is a correctness requirement, not a preference: Lumen
            # gathers indirect light only from movable lights, so a Stationary sun
            # contributes nothing to GI and the scene stays flat however bright it
            # is set.
            apply(c, "mobility", unreal.ComponentMobility.MOVABLE, "sun")
            # Slightly warm white (5500 K): photographic daylight rather than a
            # clinical 6500 K, which reads cold on concrete.
            try:
                c.set_editor_property("use_temperature", True)
                c.set_editor_property("temperature", 5500.0)
                say("  sun          temperature                    = 5500K")
            except Exception as exc:
                say("  sun          temperature FAILED: %s" % str(exc)[:60])
        if not suns:
            say("WARNING: no DirectionalLight in the level")

        # ---- sky light ------------------------------------------------------
        skies = of_class(actors, "SkyLight")
        say("SkyLight x%d" % len(skies))
        for sky in skies:
            c = sky.get_editor_property("light_component")
            if not c:
                continue
            # Real-time capture is what keeps this physically consistent: the sky
            # light then carries the atmosphere's actual luminance, which scales
            # with the sun, instead of a baked constant that no longer matches it.
            apply(c, "real_time_capture", True, "skylight")
            apply(c, "mobility", unreal.ComponentMobility.MOVABLE, "skylight")
            apply(c, "intensity", SKY_INTENSITY, "skylight")
            # Ground bounce on. With it off every shaded face is flat black,
            # because nothing lights it from below -- and outdoors a great deal of
            # the fill comes from the ground.
            apply(c, "b_lower_hemisphere_is_black", False, "skylight")
            try:
                c.set_editor_property("lower_hemisphere_ground_color",
                                      unreal.LinearColor(0.18, 0.16, 0.14, 1.0))
                say("  skylight     lower_hemisphere_ground_color  = warm grey")
            except Exception as exc:
                say("  skylight     ground bounce FAILED: %s" % str(exc)[:60])
        if not skies:
            say("WARNING: no SkyLight in the level")

        # ---- fog ------------------------------------------------------------
        fogs = of_class(actors, "ExponentialHeightFog")
        say("ExponentialHeightFog x%d" % len(fogs))
        for fog in fogs:
            c = fog.get_editor_property("component")
            if not c:
                continue
            # Density from the reference range for a subtle depth cue
            # (0.005-0.015). The campus is ~500 m across, so 0.008 gives the far
            # buildings a touch of atmosphere without greying the middle distance.
            apply(c, "fog_density", 0.008, "fog")
            apply(c, "fog_height_falloff", 0.15, "fog")
            apply(c, "volumetric_fog", True, "fog")
            # Inscattering tinted toward blue reads as air; a neutral grey fog is
            # a large part of what makes renders look washed out.
            try:
                c.set_editor_property("fog_inscattering_luminance",
                                      unreal.LinearColor(0.6, 0.72, 0.95, 1.0))
                say("  fog          fog_inscattering_luminance    = blue")
            except Exception as exc:
                say("  fog          inscattering FAILED: %s" % str(exc)[:60])

        # ---- post process ---------------------------------------------------
        ppvs = of_class(actors, "PostProcessVolume")
        if not ppvs:
            eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
            ppv = eas.spawn_actor_from_class(
                unreal.PostProcessVolume, unreal.Vector(0, 0, 0),
                unreal.Rotator(0, 0, 0))
            say("spawned PostProcessVolume")
            ppvs = [ppv] if ppv else []
        for ppv in ppvs:
            ppv.set_actor_label("MC_PostProcess")
            apply(ppv, "unbound", True, "ppv")
            s = ppv.get_editor_property("settings")

            # Exposure pinned to the physically correct value for the sun above.
            # Histogram metering with Min == Max: the metering still runs but has
            # no range to move in, so the image cannot swim as the view turns --
            # which free auto-exposure does constantly on a scene this contrasty.
            for k, v in (
                ("auto_exposure_method", unreal.AutoExposureMethod.AEM_Histogram),
                ("auto_exposure_min_brightness", EV100),
                ("auto_exposure_max_brightness", EV100),
                ("auto_exposure_bias", 0.0),
                ("auto_exposure_apply_physical_camera_exposure", False),
                # Bloom sparingly: enough to soften the sky edge, not enough to
                # haze the whole frame.
                ("bloom_intensity", 0.35),
                ("bloom_threshold", 1.1),
                ("motion_blur_amount", 0.0),
                ("vignette_intensity", 0.18),
                ("scene_fringe_intensity", 0.15),
                # A filmic shoulder/toe so highlights roll off instead of clipping.
                ("film_slope", 0.88),
                ("film_toe", 0.55),
            ):
                # Both the value AND its override flag.
                #
                # FPostProcessSettings carries a bOverride_* flag per field, and a
                # value written without its flag is simply ignored -- with no
                # error. That is why the first attempt reported success on every
                # property while the saved volume still read 0.4 / 1.8: the log
                # was true, and the setting was still off.
                ok = True
                try:
                    s.set_editor_property(k, v)
                except Exception as exc:
                    ok = False
                    say("  ppv          %-30s FAILED: %s" % (k, str(exc)[:55]))
                if ok:
                    try:
                        s.set_editor_property("override_" + k, True)
                        say("  ppv          %-30s = %s (override on)" % (k, v))
                    except Exception as exc:
                        say("  ppv          override_%-21s FAILED: %s"
                            % (k, str(exc)[:45]))
            # Saturation slightly *down*. Outdoor footage is less saturated than
            # the raw albedo of a scanned texture, and pulling it back is most of
            # what stops a render reading as a game.
            try:
                cg = s.get_editor_property("color_grading")
                cg.set_editor_property("saturation",
                                       unreal.Vector4(0.92, 0.92, 0.92, 1.0))
                s.set_editor_property("color_grading", cg)
                s.set_editor_property("override_color_grading", True)
                say("  ppv          color_grading.saturation       = 0.92")
            except Exception as exc:
                say("  ppv          saturation FAILED: %s" % str(exc)[:60])
            ppv.set_editor_property("settings", s)

        unreal.EditorLevelLibrary.save_current_level()
        say("saved; save_asset -> %s" % unreal.EditorAssetLibrary.save_asset(
            "/Game/Maps/MCReplica", only_if_is_dirty=False))
    except Exception:
        say("FAILED:\n%s" % traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
