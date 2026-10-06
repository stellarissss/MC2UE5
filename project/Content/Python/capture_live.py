# -*- coding: utf-8 -*-
"""
capture_live.py -- render the running game and save the frame.

The capture has to happen several seconds into the session, once World
Partition has streamed the level in and the pawn has settled. ``-ExecutePythonScript``
runs before any of that, so this installs a one-shot timer on the world instead
of capturing immediately, and the game is left to run.

Run with -game and a real RHI:

    UnrealEditor.exe MCReplica.uproject -game -windowed -ResX=1280 -ResY=720 \
        -ExecutePythonScript=project/Content/Python/capture_live.py \
        -MCShotDelay=12
"""

import os
import sys

import unreal

OUT_DIR = os.environ.get("MC2UE5_SHOT_DIR", r"Q:\MC2UE5\shots")
PREFIX = "live"

_state = {"fired": False}


def _capture(world):
    if _state["fired"]:
        return None
    _state["fired"] = True

    unreal.log("[LIVE] capturing after the world has settled ...")
    try:
        stats = unreal.MCFrameCapture.capture_viewport(
            unreal.GameplayStatics.get_world_context(world, False)
            if hasattr(unreal, "GameplayStatics") else world,
            "")
    except Exception as exc:
        unreal.log_warning("[LIVE] capture threw: %s" % exc)
        return None

    try:
        valid = stats.get_editor_property("valid")
        note = stats.get_editor_property("note")
        w = stats.get_editor_property("width")
        h = stats.get_editor_property("height")
        mean = stats.get_editor_property("mean_rgb")
        dark = stats.get_editor_property("dark_percent")
        sky = stats.get_editor_property("sky_percent")
        lit = stats.get_editor_property("lit_percent")
        distinct = stats.get_editor_property("distinct_colours")

        unreal.log("[LIVE] valid=%s size=%dx%d" % (valid, w, h))
        unreal.log("[LIVE] note      : %s" % note)
        unreal.log("[LIVE] mean RGB  : (%d, %d)" % (mean.x, mean.y))
        unreal.log("[LIVE] near-black: %.1f%%" % dark)
        unreal.log("[LIVE] sky-blue  : %.1f%%" % sky)
        unreal.log("[LIVE] lit       : %.1f%%" % lit)
        unreal.log("[LIVE] colours   : %d" % distinct)

        if valid:
            if lit < 2.0:
                unreal.log("[LIVE] VERDICT: no lit surface -- geometry is "
                           "unlit or its material resolves to black")
            elif dark > 55.0:
                unreal.log("[LIVE] VERDICT: geometry present but black")
            elif sky > 55.0:
                unreal.log("[LIVE] VERDICT: mostly sky -- terrain not filling "
                           "the view")
            else:
                unreal.log("[LIVE] VERDICT: sky and lit ground both present")
    except Exception as exc:
        unreal.log_warning("[LIVE] could not read the stats struct: %s" % exc)

    # Copy out of the project's Saved tree into the shared shots folder.
    src = os.path.join(unreal.Paths.project_saved_dir(), "MCFrame", "frame.png")
    if os.path.isfile(src):
        import shutil
        dst = os.path.join(OUT_DIR, "%s.png" % PREFIX)
        os.makedirs(OUT_DIR, exist_ok=True)
        shutil.copyfile(src, dst)
        unreal.log("[LIVE] copied to %s (%.1f KiB)"
                   % (dst, os.path.getsize(dst) / 1024.0))
    else:
        unreal.log_warning("[LIVE] expected capture at %s" % src)

    return None


def main():
    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        unreal.log_error("[LIVE] no world")
        return 1

    delay = 12.0
    try:
        delay = float(unreal.SystemLibrary.get_command_line()[-1].split("=")[-1])
    except Exception:
        pass
    unreal.log("[LIVE] will capture in %.0f s" % delay)

    unreal.register_slate_post_tick_callback(lambda dt: _tick(world, dt, delay))
    return 0


def _tick(world, dt, delay):
    _state.setdefault("t", 0.0)
    _state["t"] += dt
    if _state["t"] < delay:
        return
    _capture(world)
    return True      # unregister


if __name__ == "__main__":
    sys.exit(main())
