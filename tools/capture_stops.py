# -*- coding: utf-8 -*-
"""
capture_stops.py -- drive the editor game build through the named camera stops,
grab one frame per stop, and measure each frame.

Why a driver and not a single screenshot: the spawn point sits on the sports
track (pick_spawn chose the track inner edge), so a spawn screenshot shows
almost no grass and no landmark to judge materials against. One frame cannot
tell "the materials are wrong" from "the camera is pointed at gravel". So the
materials get judged from several viewpoints, and the numbers come back with
the pictures.

Camera stops are the ones already compiled into the game
(`MCConsoleCommands.cpp` GTourStops) and reached with `-MCstop=<index>`; they
are mirrored here as data so the set of shots is visible in one place rather
than spread across shell history.

`-MClayers=-vox` hides the 1,041 voxel-layer clusters. Without it the old block
layer dominates every frame and hides the meshes this is meant to judge. It is a
runtime visibility switch (MCLayerControl.cpp), not a destructive edit.

Run with the project venv (it needs PIL), NOT inside the editor:

    Q:/MC2UE5/venv/Scripts/python.exe tools/capture_stops.py
    ... --stops 0,1,2 --wait 130 --layers=-vox

Output: Q:/MC2UE5/shots/<stamp>_stop<idx>_<name>.png + a printed measurement
table, and the same table written next to the shots.
"""

import argparse
import os
import subprocess
import sys
import time

EDITOR = "Q:/UE/UE_5.8/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
# The game build is the editor in -game mode: the packaged builds on disk are
# older than the current assets, so they would show the wrong world.
GAME_EXE = "Q:/UE/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe"
PROJECT = "Q:/MC2UE5/repo/project/MCReplica.uproject"
FOCUS = "Q:/MC2UE5/tools/focus_capture.py"
SHOTS = "Q:/MC2UE5/shots"

# (index, name) -- index is what -MCstop= takes. Mirrors GTourStops.
STOPS = [
    (0, "sports_field"),     # over the running track, looking down the field
    (1, "buildings"),        # eyeline at the teaching buildings
    (2, "tall_block"),       # looking up at the tallest block
    (3, "west_band"),        # oblique across the west facade band
    (4, "field_axis"),       # along the field axis
]


def kill_game():
    for exe in ("UnrealEditor.exe", "UnrealEditor-Cmd.exe"):
        subprocess.call("taskkill /F /IM %s" % exe, shell=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def shoot(stop_idx, name, wait, layers, res):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(SHOTS, "%s_stop%d_%s.png" % (stamp, stop_idx, name))
    kill_game()
    time.sleep(2)
    cmd = [GAME_EXE, PROJECT, "-game", "-windowed",
           "-ResX=%d" % res[0], "-ResY=%d" % res[1], "-nosplash"]
    if layers:
        cmd.append("-MClayers=%s" % layers)
    cmd += ["-MCstop=%d" % stop_idx, "-log"]
    log = "Q:/MC2UE5/logs/capture_stop%d.log" % stop_idx
    with open(log, "wb") as fh:
        subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
    print("  stop %d (%s): launched, waiting %ds" % (stop_idx, name, wait))
    time.sleep(wait)
    r = subprocess.run([sys.executable, FOCUS, "--match", "mcreplica",
                        "--out", out], capture_output=True, text=True)
    kill_game()
    for line in (r.stdout or "").splitlines():
        if line.startswith(("saved", "WARNING", "NO WINDOW")) or "target:" in line:
            print("    " + line)
    return out if os.path.isfile(out) else None


def measure(path):
    """Whole-frame colour plus a ground band's local contrast (tiling proxy).

    Local contrast is the mean absolute difference between horizontally
    adjacent pixels in a lower band: flat averaged colours score near 0, a
    per-block texture repeat scores several units. That is the number that
    separates "one flat tone" from "tiling is working".
    """
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = list(im.getdata())
    n = len(px)
    r = sum(p[0] for p in px) / n
    g = sum(p[1] for p in px) / n
    b = sum(p[2] for p in px) / n
    band = im.crop((int(0.08 * w), int(0.72 * h), int(0.94 * w), int(0.97 * h)))
    bw, bh = band.size
    d = list(band.convert("L").getdata())
    diffs = [abs(d[y * bw + x] - d[y * bw + x + 1])
             for y in range(0, bh, 3) for x in range(0, bw - 1, 2)]
    lc = sum(diffs) / len(diffs) if diffs else 0.0
    lo = min(d)
    hi = max(d)
    return r, g, b, lc, lo, hi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stops", default=",".join(str(s[0]) for s in STOPS),
                    help="comma-separated stop indices")
    ap.add_argument("--wait", type=int, default=115,
                    help="seconds to wait for load + Lumen convergence")
    ap.add_argument("--layers", default="-vox",
                    help="value for -MClayers= (default hides the voxel layer)")
    ap.add_argument("--res", default="1280x720")
    args = ap.parse_args()

    os.makedirs(SHOTS, exist_ok=True)
    want = {int(x) for x in args.stops.split(",") if x.strip()}
    res = tuple(int(x) for x in args.res.lower().split("x"))

    taken = []
    for idx, name in STOPS:
        if idx not in want:
            continue
        p = shoot(idx, name, args.wait, args.layers, res)
        if p:
            taken.append((idx, name, p))

    rows = ["stop  name          frame_mean          ground_local_contrast  luma_lo hi"]
    for idx, name, p in taken:
        r, g, b, lc, lo, hi = measure(p)
        rows.append("%4d  %-12s (%5.1f,%5.1f,%5.1f)   %5.2f                  %3d %3d"
                    % (idx, name, r, g, b, lc, lo, hi))
    report = "\n".join(rows)
    print()
    print(report)
    with open(os.path.join(SHOTS, "capture_stops_report.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(report + "\n")
    print()
    print("shots: %d, report -> %s" % (len(taken),
                                       os.path.join(SHOTS,
                                                    "capture_stops_report.txt")))
    for _, _, p in taken:
        print("  " + p)
    return 0 if taken else 1


if __name__ == "__main__":
    sys.exit(main())
