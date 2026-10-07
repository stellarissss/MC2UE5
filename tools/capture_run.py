# -*- coding: utf-8 -*-
"""
capture_run.py -- launch the packaged build, park the camera, grab frames.

Written for S6A. Three things here exist because each of them silently
produced a wrong result before:

1. **The process name.** A Shipping build is `MCReplica-Win64-Shipping.exe`.
   `taskkill /IM MCReplica.exe` matches nothing, exits 0, and leaves the real
   process running -- so the next capture photographs a *stale* build while the
   new one never launched. This script kills by the correct image name and then
   asserts the process is actually gone before launching.

2. **The capture path.** `ImageGrab.grab` reads the screen, not the window, so
   it photographs whatever is on top. `UMCFrameCapture`/`ReadPixels` returns
   pure white outside a real frame. The only working path is
   `Q:/MC2UE5/tools/focus_capture.py` (PrintWindow + PW_RENDERFULLCONTENT),
   which is invoked as a subprocess here rather than reimplemented.

3. **A fixed camera.** `-MCstop=N` teleports the pawn to tour stop N after 14 s
   and leaves it there; without it the pawn stays at the data-driven spawn,
   which is also fixed. Either way the camera must not be touched between the
   two frames of a noise-floor pair, and it is not: this script never sends
   input.

Usage:

    python capture_run.py --exe dist_verify/Windows/MCReplica/Binaries/Win64/MCReplica-Win64-Shipping.exe \
        --tag baked_default --args "-MCdiag -MCstop=1" --shots 2 --settle 22

Two frames are captured per launch by default, ~4 s apart, so that a single
launch yields its own noise floor. Compare across launches only on numbers that
exceed it.
"""

import argparse
import os
import subprocess
import sys
import time

SHOT_TOOL = "Q:/MC2UE5/repo/tools/shot_game.py"
PY = "Q:/MC2UE5/venv/Scripts/python.exe"
PROC = "MCReplica-Win64-Shipping.exe"


def running():
    """PIDs of the shipping process, via tasklist (no psutil dependency)."""
    try:
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % PROC, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, errors="replace", timeout=60)
    except Exception:                              # noqa: BLE001
        return []
    pids = []
    for line in p.stdout.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == PROC.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def kill_all(verbose=True):
    pids = running()
    if not pids:
        if verbose:
            print("  no %s running" % PROC)
        return True
    if verbose:
        print("  killing %s pids=%s" % (PROC, pids))
    subprocess.run(["taskkill", "/F", "/IM", PROC],
                   capture_output=True, text=True, timeout=120)
    for _ in range(30):
        if not running():
            return True
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--args", default="")
    ap.add_argument("--shots", type=int, default=2)
    ap.add_argument("--settle", type=float, default=22.0,
                    help="seconds after launch before the first capture "
                         "(must exceed the 14 s -MCstop kick)")
    ap.add_argument("--gap", type=float, default=4.0,
                    help="seconds between successive captures")
    ap.add_argument("--outdir", default="Q:/MC2UE5/shots/s6a")
    ap.add_argument("--keep-running", action="store_true")
    args = ap.parse_args()

    exe = args.exe.replace("\\", "/")
    if not os.path.isfile(exe):
        print("EXE NOT FOUND: %s" % exe)
        return 2
    os.makedirs(args.outdir, exist_ok=True)

    print("=== capture_run tag=%s ===" % args.tag)
    print("exe: %s" % exe)
    print("exe mtime: %s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(os.path.getmtime(exe))))
    print("args: %s" % args.args)

    if not kill_all():
        print("could not kill the previous process -- refusing to launch, a "
              "zombie would make every number below describe the wrong build")
        return 3

    argv = [exe] + [a for a in args.args.split() if a]
    # cwd = the exe's own Runtime dir so it resolves its own pak.
    workdir = os.path.dirname(exe)
    print("launching: %s  (cwd=%s)" % (" ".join(argv), workdir))
    proc = subprocess.Popen(argv, cwd=workdir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    time.sleep(args.settle)
    if proc.poll() is not None:
        print("process exited early with code %s -- no capture" % proc.returncode)
        return 4

    saved = []
    for i in range(args.shots):
        out = "%s/%s_%d.png" % (args.outdir.replace("\\", "/"), args.tag, i)
        r = subprocess.run([PY, SHOT_TOOL, "--out", out],
                           capture_output=True, text=True, errors="replace",
                           timeout=180)
        print("  shot %d: rc=%d  %s" % (i, r.returncode,
                                        (r.stdout or r.stderr).strip().replace("\n", " | ")))
        if os.path.isfile(out):
            saved.append(out)
        if i != args.shots - 1:
            time.sleep(args.gap)

    if not args.keep_running:
        kill_all()

    print("saved: %s" % saved)
    print("RESULT: %s" % ("OK" if len(saved) == args.shots else "INCOMPLETE"))
    return 0 if len(saved) == args.shots else 5


if __name__ == "__main__":
    sys.exit(main())
