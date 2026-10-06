# -*- coding: utf-8 -*-
"""
render_and_read.py -- render the level and read the pixels back.

Three earlier rounds of "fixes" were reasoned entirely from asset data while
never once looking at an image. Every check passed -- meshes placed, collision
present, cook clean, 97% of triangles front-facing, material graph complete --
and the game still rendered black. That is what motivated this script.

It renders the viewport from inside the editor and writes both a PNG and a
coarse colour census, so "is the terrain black?" becomes a number instead of an
inference.

The earlier failures to get an image were self-inflicted: the editor was being
launched with ``-nullrhi``, which disables the renderer outright and leaves
zero windows. This machine has a real GPU (RTX 4070) and Microsoft's WARP
software rasteriser as a fallback, so rendering is possible; it just has to be
asked for.

Run with a real RHI:

    UnrealEditor.exe MCReplica.uproject \
        -ExecutePythonScript=project/Content/Python/render_and_read.py \
        -game -windowed -ResX=960 -ResY=540 -nosplash
"""

import math
import os
import sys
import time

import unreal

OUT_DIR = os.environ.get("MC2UE5_SHOT_DIR", r"Q:\MC2UE5\shots")

_t0 = time.time()


def log(msg):
    unreal.log("[PIXELS %6.1fs] %s" % (time.time() - _t0, msg))


# ----------------------------------------------------------------------------
# Colour census: is the frame actually varied, and what colour dominates it?
# ----------------------------------------------------------------------------

def census(image, bins=6):
    """-> (unique_colours_quantised, mean RGB, fraction near-black, fraction near-sky)"""
    q = image.quantize(colors=bins)
    colours = q.getcolors() or []
    total = sum(c for c, _i in colours) or 1
    px = image.load()
    w, h = image.size
    step = max(1, min(w, h) // 64)

    n = 0
    dark = 0
    skyish = 0
    rs = gs = bs = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = px[x, y][:3]
            rs += r
            gs += g
            bs += b
            n += 1
            if r < 24 and g < 24 and b < 24:
                dark += 1
            if b > r + 25 and b > 90 and g > r:
                skyish += 1
    return (len(colours), total,
            (rs // n, gs // n, bs // n),
            dark / float(n), skyish / float(n))


def save_png(image, path):
    image.convert("RGB").save(path)
    return os.path.getsize(path)


def main():
    log("=" * 70)
    log("render + pixel census")
    log("=" * 70)

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)

    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        log("no world")
        return 1

    # Where is everything?
    terrain = [(a.get_actor_label(), a.get_actor_location())
               for a in unreal.EditorLevelLibrary.get_all_level_actors()
               if a.get_actor_label().startswith("Terrain_")]
    pawn = [a for a in unreal.EditorLevelLibrary.get_all_level_actors()
            if a.get_actor_label() == "MC_Player"]
    start = [a for a in unreal.EditorLevelLibrary.get_all_level_actors()
             if a.get_class().get_name() == "PlayerStart"]

    for label, loc in terrain[:8]:
        log("  %-34s (%.0f, %.0f, %.0f)" % (label, loc.x, loc.y, loc.z))
    for a in start:
        l = a.get_actor_location()
        log("  PlayerStart at (%.0f, %.0f, %.0f)" % (l.x, l.y, l.z))
    for a in pawn:
        l = a.get_actor_location()
        log("  MC_Player   at (%.0f, %.0f, %.0f)" % (l.x, l.y, l.z))

    # ---- render -----------------------------------------------------------
    console = unreal.SystemLibrary.get_console_variables()
    log("r.RHIName  = %s" % console.get("r.RHIName", "?") if hasattr(console, "get") else "")
    try:
        unreal.SystemLibrary.execute_console_command(world, "r.ScreenPercentage 100")
        unreal.SystemLibrary.execute_console_command(world, "r.ViewMode 0")
    except Exception as exc:
        log("console setup skipped (%s)" % str(exc)[:60])

    log("issuing HighResShot ...")
    try:
        unreal.SystemLibrary.execute_console_command(world, "HighResShot 512")
    except Exception as exc:
        log("HighResShot failed: %s" % exc)
        return 1

    # The screenshot is written on the next frame; give it time to land.
    deadline = time.time() + 25.0
    found = None
    while time.time() < deadline:
        for root, _dirs, names in os.walk(world.get_path_name()
                                           if hasattr(world, "get_path_name")
                                           else OUT_DIR):
            for n in names:
                if n.lower().endswith(".png") and n.startswith("Screenshot"):
                    found = os.path.join(root, n)
                    break
            if found:
                break
        if found:
            # let the write finish
            time.sleep(1.5)
            break
        unreal.SystemLibrary.execute_console_command(world, "HighResShot 512")
        time.sleep(1.5)

    if not found:
        log("no screenshot file appeared -- the renderer is not producing "
            "frames in this session")
        return 1

    log("screenshot: %s (%.1f KiB)" % (found, os.path.getsize(found) / 1024.0))

    try:
        from PIL import Image
    except ImportError:
        log("Pillow required to read the image back")
        return 1

    img = Image.open(found)
    out = os.path.join(OUT_DIR, "render_readback.png")
    save_png(img, out)
    log("copied to %s" % out)

    n_colours, total, mean, dark, skyish = census(img)
    log("-" * 70)
    log("unique quantised colours : %d (of %d pixels)" % (n_colours, total))
    log("mean RGB                 : %s" % (mean,))
    log("fraction near-black      : %.1f%%" % (dark * 100.0))
    log("fraction sky-blue        : %.1f%%" % (skyish * 100.0))
    log("-" * 70)

    if dark > 0.55:
        log("VERDICT: the frame is mostly black -- a surface is unlit or its "
            "material resolves to black")
    elif skyish > 0.45:
        log("VERDICT: the frame is mostly sky -- the terrain is not visible "
            "where it should be")
    else:
        log("VERDICT: the frame has varied content; the render is working")
    return 0


if __name__ == "__main__":
    sys.exit(main())
