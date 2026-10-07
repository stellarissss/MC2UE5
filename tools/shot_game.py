# -*- coding: utf-8 -*-
"""
shot_game.py -- capture the *game* window, not an editor that happens to share
its title.

`focus_capture.py --match mcreplica` takes the first visible window whose title
contains the substring and is larger than 400x300. On this host a running
UnrealEditor has the title "MCReplica （64-位 Development PCD3D_SM5）", which
also contains "mcreplica", so whichever window EnumWindows happens to return
first wins. During S6A that produced a capture of the *editor* while the game
was the thing under test -- the exact failure mode focus_capture.py was written
to prevent, one level up.

This wrapper does not reimplement the capture. It reuses focus_capture's
`capture()` (PrintWindow + PW_RENDERFULLCONTENT) and only changes window
*selection*: exact title, with editor/crash-report titles rejected outright.

    python shot_game.py --out Q:/MC2UE5/shots/s6a/foo.png
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "Q:/MC2UE5/tools")

import focus_capture as fc           # noqa: E402


REJECT = ("development", "pcd3d", "crash", "reporter", "editor")


def find_game_window():
    cands = []
    for hwnd, title, w, h, _ in fc.all_windows():
        low = title.lower()
        if "mcreplica" not in low or w < 400 or h < 300:
            continue
        if any(k in low for k in REJECT):
            continue
        cands.append((hwnd, title, w, h))
    # Prefer the bare title "MCReplica" -- that is the packaged game.
    cands.sort(key=lambda t: (t[1].strip().lower() != "mcreplica", -t[2] * t[3]))
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cands = find_game_window()
    if not cands:
        print("NO GAME WINDOW -- candidates were:")
        for hwnd, title, w, h, _ in fc.all_windows():
            print("   %-10d %4dx%-4d %r" % (hwnd, w, h, title))
        return 1

    hwnd, title, w, h = cands[0]
    if len(cands) > 1:
        print("note: %d game-like windows, using the first" % len(cands))
        for c in cands:
            print("   cand %-10d %4dx%-4d %r" % (c[0], c[2], c[3], c[1]))
    print("target: %d %r (%dx%d)" % (hwnd, title.strip(), w, h))

    img = fc.capture(hwnd)
    img.save(args.out)
    print("saved %s %s" % (args.out, img.size))

    px = img.convert("L").getdata()
    lo, hi = min(px), max(px)
    if hi - lo < 8:
        print("WARNING: nearly uniform (luma %d..%d) -- likely blank" % (lo, hi))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
