# -*- coding: utf-8 -*-
"""
mclayers_repro.py -- is `-MClayers=-vox` (a SINGLE token) actually reliable?

Context: the layer-spec parser mishandles comma-separated tokens
(`-MClayers=-vox,-struct` has been seen to yield `vox=0 struct=0 terrain=0`).
The open question is whether a single token is safe, because eng-3's
"voxel layer on/off differs by only 0.134%" comparison rests on that toggle
having actually flipped the voxel layer and nothing else.

This launches the game N times with the SAME single-token argument, and reads the
layer spec the game itself wrote for each run (the game appends a `parse ... spec=`
line to Q:/MC2UE5/logs/mclayers.txt at BeginPlay, and an `apply ... -> census`
line 6s later). The file is emptied before each run so each run's line is
unambiguous -- size-offset reading failed earlier because the file was truncated
underneath it.

If the spec differs between runs of the same argument, the switch is
non-deterministic and any A/B built on it is invalid. If every run parses to the
same `vox=0 struct=1 terrain=1`, the switch is trustworthy for a single token and
eng-3's comparison stands.

Read-only w.r.t. assets; it only launches the game, empties a log file, and reads.
"""

import argparse
import os
import subprocess
import sys
import time

GAME_EXE = "Q:/UE/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe"
PROJECT = "Q:/MC2UE5/repo/project/MCReplica.uproject"
SHOTS = "Q:/MC2UE5/shots"
LAYER_LOG = "Q:/MC2UE5/logs/mclayers.txt"


def kill_game():
    for exe in ("UnrealEditor.exe", "UnrealEditor-Cmd.exe"):
        subprocess.call("taskkill /F /IM %s" % exe, shell=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_specs():
    """Both the parse and the apply line the game wrote, as raw text."""
    try:
        with open(LAYER_LOG, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def run_once(tag, extra, stop, wait, shot):
    kill_game()
    time.sleep(2)
    # Empty the log so this run's lines cannot be confused with the last one's.
    open(LAYER_LOG, "w").close()
    cmd = [GAME_EXE, PROJECT, "-game", "-windowed", "-ResX=1280", "-ResY=720",
           "-nosplash"] + extra + ["-MCstop=%d" % stop, "-log"]
    log = "Q:/MC2UE5/logs/mclayers_repro_%s.log" % tag
    with open(log, "wb") as fh:
        subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
    print("  [%s] %s  -> waiting %ds" % (tag, " ".join(extra) or "(no args)",
                                         wait))
    time.sleep(wait)
    if shot:
        subprocess.run([sys.executable, "Q:/MC2UE5/tools/focus_capture.py",
                        "--match", "mcreplica", "--out", shot],
                       capture_output=True, text=True)
    kill_game()
    return read_specs()


def measure(path):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    px = list(im.getdata())
    n = len(px)
    r = sum(p[0] for p in px) / n
    g = sum(p[1] for p in px) / n
    b = sum(p[2] for p in px) / n
    dark = sum(1 for p in px if max(p) < 24) * 100.0 / n
    green = sum(1 for p in px
                if p[1] > p[0] + 8 and p[1] > p[2] + 8) * 100.0 / n
    return r, g, b, dark, green


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--stop", type=int, default=3)
    ap.add_argument("--wait", type=int, default=115)
    args = ap.parse_args()

    os.makedirs(SHOTS, exist_ok=True)
    rows = []

    # Control: no layer arguments at all -- the reference "everything on" state.
    stamp = time.strftime("%Y%m%d_%H%M%S")
    shots = {"default": os.path.join(SHOTS, "%s_repro_default.png" % stamp)}
    spec = run_once("default", [], args.stop, args.wait, shots["default"])
    rows.append(("default", "(none)", spec, shots["default"]))

    # The toggle under test: one token, repeated.
    for i in range(args.reps):
        tag = "vox_off_%d" % i
        shots[tag] = os.path.join(SHOTS, "%s_repro_%s.png" % (stamp, tag))
        spec = run_once(tag, ["-MClayers=-vox"], args.stop, args.wait,
                        shots[tag])
        rows.append((tag, "-MClayers=-vox", spec, shots[tag]))

    print()
    print("=== layer spec the game wrote, per run ===")
    seen = set()
    for tag, arg, spec, _ in rows:
        spec_one = ""
        for line in spec.splitlines():
            if line.startswith("parse "):
                spec_one = line.split("spec=", 1)[1].split(" t=")[0].strip()
        seen.add((arg, spec_one))
        print("  %-14s %-18s parse spec = %s" % (tag, arg, spec_one or "(none)"))
        for line in spec.splitlines():
            if line.startswith("apply "):
                print("        %s" % line)

    vox_runs = {s for a, s in seen if a == "-MClayers=-vox"}
    print()
    if len(vox_runs) > 1:
        print("  ==> `-MClayers=-vox` parsed to %d DIFFERENT specs across "
              "%d identical runs." % (len(vox_runs), args.reps))
        print("      The switch is non-deterministic even for a single token;")
        print("      any A/B built on it is invalid until the parser is fixed.")
    else:
        print("  ==> `-MClayers=-vox` parsed to the same spec every run "
              "(%s)." % (vox_runs.pop() if vox_runs else "n/a"))
        print("      A single-token toggle looks deterministic here.")

    print()
    print("=== per-shot measurement ===")
    print("  run            mean RGB                dark%%   green%%")
    for tag, arg, spec, path in rows:
        if not os.path.isfile(path):
            print("  %-14s (no shot)" % tag)
            continue
        r, g, b, dark, green = measure(path)
        print("  %-14s (%6.1f,%6.1f,%6.1f)   %5.1f   %5.2f"
              % (tag, r, g, b, dark, green))
    print()
    for tag, arg, spec, path in rows:
        print("  %-14s %s" % (tag, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
