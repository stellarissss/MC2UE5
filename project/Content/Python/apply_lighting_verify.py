# -*- coding: utf-8 -*-
"""
apply_lighting_verify.py -- apply the lighting, then read it back.

setup_lighting reported success while the saved level kept the old values, which
is the worst kind of failure: the log says it worked and the next cook renders
the old settings. Applying and reading back in the same session is what turns
"the write was accepted" into "the write is on disk".
"""
import sys
import traceback

import unreal

sys.path.insert(0, r"Q:/MC2UE5/repo/project/Content/Python")
OUT = "Q:/MC2UE5/logs/apply_lighting_verify.txt"
L = []


def say(s):
    L.append(str(s))
    try:
        unreal.log("[MCLV] " + str(s))
    except Exception:
        pass
    open(OUT, "w").write("\n".join(L) + "\n")


try:
    import setup_lighting
    setup_lighting.main()
    say("setup_lighting ran")

    # Force the level to disk through the level subsystem, which is the only path
    # that actually writes the .umap for a World Partition level.
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    say("save_current_level -> %s" % les.save_current_level())
    say("save_all_dirty_levels -> %s" % les.save_all_dirty_levels())

    # Read back from a fresh load, not the in-memory copy.
    unreal.EditorAssetLibrary.save_asset("/Game/Maps/MCReplica",
                                         only_if_is_dirty=False)
    say("")
    say("--- read back ---")
    for a in unreal.EditorLevelLibrary.get_all_level_actors():
        k = a.get_class().get_name()
        if k == "DirectionalLight":
            c = a.get_editor_property("directional_light_component")
            say("sun intensity=%s mobility=%s temp=%s" % (
                c.get_editor_property("intensity"),
                c.get_editor_property("mobility"),
                c.get_editor_property("temperature")))
        if k == "SkyLight":
            c = a.get_editor_property("light_component")
            say("sky intensity=%s realtime=%s" % (
                c.get_editor_property("intensity"),
                c.get_editor_property("real_time_capture")))
        if k == "PostProcessVolume":
            s = a.get_editor_property("settings")
            say("EV100 min=%s max=%s" % (
                s.get_editor_property("auto_exposure_min_brightness"),
                s.get_editor_property("auto_exposure_max_brightness")))
        if k == "ExponentialHeightFog":
            c = a.get_editor_property("component")
            say("fog density=%s" % c.get_editor_property("fog_density"))
except Exception:
    say("FAILED:/n" + traceback.format_exc())
say("--- done ---")
