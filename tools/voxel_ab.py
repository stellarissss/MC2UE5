# -*- coding: utf-8 -*-
"""
voxel_ab.py -- A/B the voxel block layer with the *actual* parsed layer spec
recorded for each run.

Why this is not just "two screenshots and a diff": `-MClayers` parses
comma-separated tokens wrongly (REFACTOR_PLAN section, task #26), non-
deterministically -- `-MClayers=-vox,-struct` has been observed to produce
`vox=0 struct=0 terrain=0`, `vox=1 struct=1 terrain=0` and other states. So a
voxel-on/voxel-off comparison is only meaningful if you read back what the parser
actually produced for each run. Otherwise the two arms can be the same state and
the measured difference is noise.

The game writes every parse to `Q:/MC2UE5/logs/mclayers.txt` (appended). This
driver snapshots the file size before each launch and reads only the new bytes,
so each run's spec is attributed to that run.

Two arms, same camera:
  on   : no -MClayers            (expected spec vox=1 struct=1 terrain=1 ...)
  off  : -MClayers=-vox          (expected spec vox=0 struct=1 terrain=1 ...)

The expected values are printed next to the observed ones and a verdict is given,
so a run where the switch did not take effect is reported rather than compared.

Run outside the editor:
  Q:/MC2UE5/venv/Scripts/python.exe tools/voxel_ab.py --stop 3 --wait 115

Read-only with respect to assets; it only launches the game and reads logs.
"""

import argparse
import os
import subprocess
import sys
import time

GAME_EXE = "Q:/UE/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe"
PROJECT = "Q:/MC2UE5/repo/project/MCReplica.uproject"
FOCUS = "Q:/MC2UE5/tools/focus_capture.py"
SHOTS = "Q:/MC2UE5/shots"
LOG_DIR = "Q:/MC2UE5/logs"
LAYER_LOG = os.path.join(LOG_DIR, "mclayers.txt")

# name -> list of extra args (the toggle under test)
ARMS = {
    "on": [],
    "off": ["-MClayers=-vox"],
}
EXPECT = {
    "on": {"vox": 1, "struct": 1, "terrain": 1},
    "off": {"vox": 0, "struct": 1, "terrain": 1},
}


def kill_game():
    for exe in ("UnrealEditor.exe", "UnrealEditor-Cmd.exe"):
        subprocess.call("taskkill /F /IM %s" % exe, shell=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def layer_log_size():
    try:
        return os.path.getsize(LAYER_LOG)
    except OSError:
        return 0


def layer_log_delta(start):
    try:
        with open(LAYER_LOG, "rb") as fh:
            fh.seek(start)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def parse_spec(text):
    """Pull the last `parse ... spec=...` line's fields out of a delta.

    Line shape: `parse bAnyOff=1 spec=vox=0 struct=0 terrain=1 shadow=1 coll=1 t=0.00`
    so take everything after `spec=` up to the ` t=` timestamp.
    """
    spec = None
    for line in text.splitlines():
        if not (line.startswith("parse ") and "spec=" in line):
            continue
        tail = line.split("spec=", 1)[1]
        tail = tail.split(" t=")[0].strip()
        d = {}
        for kv in tail.split():
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    d[k] = int(v)
                except ValueError:
                    pass
        spec = d
    return spec


def run_arm(name, stop, wait, res):
    extra = ARMS[name]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(SHOTS, "%s_voxab_%s_stop%d.png" % (stamp, name, stop))
    kill_game()
    time.sleep(2)
    start = layer_log_size()
    cmd = [GAME_EXE, PROJECT, "-game", "-windowed",
           "-ResX=%d" % res[0], "-ResY=%d" % res[1], "-nosplash"]
    cmd += extra
    cmd += ["-MCstop=%d" % stop, "-log"]
    log = os.path.join(LOG_DIR, "voxel_ab_%s.log" % name)
    with open(log, "wb") as fh:
        subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
    print("  arm %-3s: launched (%s), waiting %ds"
          % (name, " ".join(extra) or "no layer args", wait))
    time.sleep(wait)
    r = subprocess.run([sys.executable, FOCUS, "--match", "mcreplica",
                        "--out", out], capture_output=True, text=True)
    kill_game()
    for line in (r.stdout or "").splitlines():
        if line.startswith(("saved", "WARNING", "NO WINDOW")) or "target:" in line:
            print("    " + line)
    spec = parse_spec(layer_log_delta(start))
    return out if os.path.isfile(out) else None, spec, log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", type=int, default=3)
    ap.add_argument("--wait", type=int, default=115)
    ap.add_argument("--res", default="1280x720")
    ap.add_argument("--arms", default="on,off")
    args = ap.parse_args()
    res = tuple(int(x) for x in args.res.lower().split("x"))

    os.makedirs(SHOTS, exist_ok=True)
    results = {}
    for name in [a.strip() for a in args.arms.split(",") if a.strip()]:
        if name not in ARMS:
            print("unknown arm %r (known: %s)" % (name, list(ARMS)))
            continue
        path, spec, log = run_arm(name, args.stop, args.wait, res)
        results[name] = {"path": path, "spec": spec, "log": log}

    print()
    print("=== layer spec actually parsed per arm ===")
    effective = {}
    for name, r in results.items():
        got = r["spec"]
        exp = EXPECT[name]
        want = "vox=%d struct=%d terrain=%d" % (exp["vox"], exp["struct"],
                                                exp["terrain"])
        if got is None:
            print("  %-3s spec: NOT WRITTEN (no parse line seen)   expected %s"
                  % (name, want))
            effective[name] = None
            continue
        have = "vox=%d struct=%d terrain=%d" % (got.get("vox", -1),
                                                got.get("struct", -1),
                                                got.get("terrain", -1))
        same = all(got.get(k) == v for k, v in exp.items())
        print("  %-3s spec: %s   expected %s   %s"
              % (name, have, want, "as expected" if same else "*** NOT AS EXPECTED ***"))
        effective[name] = got

    print()
    differs = (effective.get("on") is not None
               and effective.get("off") is not None
               and effective["on"] != effective["off"])
    print("  the two arms parsed to different specs: %s" % differs)
    if not differs:
        print("  ==> the toggle did NOT take effect; the pixel comparison below "
              "is between two runs of the same state and means nothing")

    if "on" in results and "off" in results:
        a, b = results["on"]["path"], results["off"]["path"]
        if a and b:
            from PIL import Image, ImageChops
            ia = Image.open(a).convert("RGB").resize((1280, 720))
            ib = Image.open(b).convert("RGB").resize((1280, 720))
            d = ImageChops.difference(ia, ib).convert("L")
            px = list(d.getdata())
            n = len(px)
            mean = sum(px) / n
            big = sum(1 for x in px if x > 12) * 100.0 / n
            print()
            print("=== pixel difference  vox-ON vs vox-OFF  (stop %d) ==="
                  % args.stop)
            print("  mean abs diff = %.2f / 255   pixels >12 = %.1f%%"
                  % (mean, big))
            pb = list(ib.getdata())
            g = sum(1 for p in pb if p[1] > p[0] + 8 and p[1] > p[2] + 8)
            print("  vox-OFF green pixels = %.2f%%" % (100.0 * g / len(pb)))
            print("  (a large diff means the voxel layer dominates the frame;")
            print("   a near-zero diff means the toggle did nothing or the layer")
            print("   is not visible from here)")

    print()
    for name, r in results.items():
        print("  %-3s shot: %s" % (name, r["path"] or "(none)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
