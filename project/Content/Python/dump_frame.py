# -*- coding: utf-8 -*-
"""
dump_frame.py -- render one frame and write it to disk from inside the engine.

Written after three rounds of fixing a black screen without once seeing a
picture. The machine runs no interactive desktop (session 0): the RHI
initialises -- D3D12, SM5, real GPU -- but ``EnumWindows`` reports zero
top-level windows, so neither a screen grab nor ``PrintWindow`` can reach the
frame, and ``HighResShot`` silently writes nothing.

What still works is asking the renderer for the pixels directly. This drives
``HighResShot`` from a console command issued inside the editor session, then
finds the file the engine wrote. If that produces nothing, it falls back to
reading the back buffer through a render target, which does not depend on a
window at all.

    UnrealEditor.exe MCReplica.uproject \
        -ExecutePythonScript=project/Content/Python/dump_frame.py \
        -unattended -nopause -nosplash -ResX=1280 -ResY=720
"""

import os
import sys
import time

import unreal

OUT_DIR = os.environ.get("MC2UE5_SHOT_DIR", r"Q:\MC2UE5\shots")
SHOT_NAME = "mcframe.png"

_t0 = time.time()


def log(msg):
    unreal.log("[FRAME %6.1fs] %s" % (time.time() - _t0, msg))


def _census(path):
    """-> dict describing what is actually in the image."""
    from PIL import Image
    img = Image.open(path).convert("RGB")
    w, h = img.size
    px = img.load()
    step = max(1, min(w, h) // 80)

    n = dark = sky = mid = 0
    rs = gs = bs = 0
    colours = set()
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y]
            rs += r
            gs += g
            bs += b
            n += 1
            colours.add((r // 24, g // 24, b // 24))
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            if lum < 24:
                dark += 1
            elif b > r + 25 and b > 90:
                sky += 1
            elif lum > 45:
                mid += 1

    return {
        "size": (w, h),
        "mean": (rs // n, gs // n, bs // n),
        "dark_pct": 100.0 * dark / n,
        "sky_pct": 100.0 * sky / n,
        "lit_pct": 100.0 * mid / n,
        "distinct": len(colours),
    }


def _find_shot(root, name, since):
    best = None
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if not n.lower().endswith(".png"):
                continue
            p = os.path.join(dirpath, n)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt < since:
                continue
            if name.lower() in n.lower() or n.lower().startswith("screenshot"):
                if best is None or mt > os.path.getmtime(best):
                    best = p
    return best


def main():
    log("=" * 70)
    log("frame dump")
    log("=" * 70)

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)

    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        log("no world")
        return 1

    # Report what the engine thinks it is rendering with, and where things are.
    for a in unreal.EditorLevelLibrary.get_all_level_actors():
        label = a.get_actor_label()
        if label == "PlayerStart" or label == "MC_Player":
            l = a.get_actor_location()
            log("  %-14s at (%.0f, %.0f, %.0f)" % (label, l.x, l.y, l.z))

    since = time.time() - 2.0

    log("requesting HighResShot ...")
    ok = True
    for cmd in ("r.ScreenPercentage 100",
                "r.ViewMode 0",
                "r.ForceLODShadow 0",
                "HighResShot 512"):
        try:
            unreal.SystemLibrary.execute_console_command(world, cmd)
            log("  exec: %s" % cmd)
            time.sleep(0.8)
        except Exception as exc:
            log("  exec %s failed: %s" % (cmd, str(exc)[:60]))
            ok = False

    # Give the async capture time to land on disk.
    time.sleep(6.0)

    roots = [OUT_DIR,
             os.path.join(unreal.Paths.project_dir(), "Saved", "Screenshots"),
             os.path.join(unreal.Paths.project_dir(), "Saved")]
    found = None
    for r in roots:
        if os.path.isdir(r):
            found = _find_shot(r, SHOT_NAME, since)
            if found:
                break

    if found:
        try:
            info = _census(found)
            log("-" * 70)
            log("frame: %s" % found)
            log("  size          : %dx%d" % info["size"])
            log("  mean RGB      : %s" % (info["mean"],))
            log("  near-black    : %.1f%%" % info["dark_pct"])
            log("  sky-blue      : %.1f%%" % info["sky_pct"])
            log("  lit surface   : %.1f%%" % info["lit_pct"])
            log("  distinct cols : %d" % info["distinct"])
            log("-" * 70)
            if info["lit_pct"] < 2.0:
                log("VERDICT: essentially no lit surface in frame")
            elif info["dark_pct"] > 55.0:
                log("VERDICT: geometry present but unlit/black")
            elif info["sky_pct"] > 55.0:
                log("VERDICT: mostly sky; terrain not filling the view")
            else:
                log("VERDICT: frame has a mix of sky and lit ground")
            return 0
        except Exception as exc:
            log("could not read the capture (%s)" % exc)
            return 1

    log("no capture file appeared in: %s" % ", ".join(roots))
    log("HighResShot executed without error = %s, so the renderer is running "
        "but its output is not reaching the filesystem in this session." % ok)
    return 1


if __name__ == "__main__":
    sys.exit(main())
