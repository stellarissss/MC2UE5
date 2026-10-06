# -*- coding: utf-8 -*-
"""
apply_render_fixes.py -- run the whole render-fix chain in one editor session.

Each step already writes its own report; this only sequences them, and each is
individually guarded so one failure still leaves the others' output on disk
instead of losing the run.

Order matters:
  1. clear     -- drop the old MCblk_/Terrain_/Props_ actors (superseded geometry)
  2. cc0       -- rebuild the CC0 material instances (Tiling corrected)
  3. voxel     -- rebuild the block layer against those materials + new cull band
  4. lighting  -- sun/sky/exposure window
"""

import os
import sys
import traceback

import unreal

sys.path.insert(0, r"Q:/MC2UE5/repo/project/Content/Python")

OUT = "Q:/MC2UE5/logs/apply_render_fixes.txt"
L = []


def say(s):
    L.append(str(s))
    try:
        unreal.log("[MCFIX] " + str(s))
    except Exception:
        pass
    try:
        open(OUT, "w").write("\n".join(L) + "\n")
    except Exception:
        pass


os.environ["MC2UE5_DRY_RUN"] = "0"

STEPS = (
    ("clear", "clear_generated", "main"),
    ("cc0 materials", "import_cc0_materials", "main"),
    ("voxel blocks", "import_world", "run"),
    ("lighting", "setup_lighting", "main"),
)

for label, module_name, func_name in STEPS:
    say("")
    say("=" * 60)
    say("STEP: %s" % label)
    say("=" * 60)
    try:
        mod = __import__(module_name)
        getattr(mod, func_name)()
        say("STEP OK: %s" % label)
    except Exception:
        say("STEP FAILED: %s\n%s" % (label, traceback.format_exc()))

say("")
say("ALL STEPS DONE")
