# -*- coding: utf-8 -*-
"""
pak_report.py -- what a packaged build actually contains, read through the
engine's own container reader.

Written for S6A after the finding that a whole pipeline's output had never been
in any package. The lesson that matters is *how* to check:

**Do not grep the raw bytes of a `.pak`/`.utoc`.** The index is compressed and
the payload is Oodle-compressed, so a byte search returns 0 for content that is
present and non-zero for content that is not -- either way the number is
meaningless. Ask `UnrealPak -List`, which parses the index, and count the lines
it prints.

This tool runs that for every container in a dist directory and prints, per
container, how many entries match a set of path prefixes, plus the total entry
count so a "0" can be distinguished from "the listing failed".

    python pak_report.py Q:/MC2UE5/dist_verify
    python pak_report.py Q:/MC2UE5/dist_verify --json out/pak_report.json
"""

import argparse
import glob
import json
import os
import subprocess
import sys

UNREALPAK = "Q:/UE/UE_5.8/Engine/Binaries/Win64/UnrealPak.exe"

#: Prefixes that must be present for the level to render the S3/S4/S5 output.
REQUIRED = [
    "Maps/MCReplica",
    "MC/Structures",
    "MC/Terrain",
    "MC/Families",
    "MC/Atlas",
]


def list_container(path):
    """-> (ok, lines, reason). Parses the index via UnrealPak -List."""
    try:
        p = subprocess.run([UNREALPAK, path, "-List"],
                           capture_output=True, text=True, errors="replace",
                           timeout=600)
    except Exception as exc:                       # noqa: BLE001
        return False, ["<UnrealPak failed: %s>" % exc], "spawn failed"
    lines = (p.stdout + "\n" + p.stderr).splitlines()
    # A listing that parsed shows one of these per entry.
    got_index = any(("offset:" in l and "size:" in l) or "mount point" in l
                    for l in lines)
    if p.returncode == 0 and got_index:
        return True, lines, None
    # `global.utoc` is the engine's *global shader* container. It is a real
    # container that the runtime mounts, but its directory index is not one
    # UnrealPak's standalone path resolves, so -List aborts with "Missing
    # directory index". That is not a project-content failure and treating it
    # as one would make this report cry wolf on every build. It is named and
    # reported instead.
    if any("Missing directory index" in l for l in lines):
        return False, lines, "engine-global container (expected: not listable)"
    return False, lines, "listing did not parse"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dist", help="archive directory (contains Windows/...)")
    ap.add_argument("--json", default=None)
    ap.add_argument("--save-lists", default=None,
                    help="directory to write the raw listings into")
    ap.add_argument("--prefix", action="append", default=None,
                    help="extra path prefix to count (repeatable)")
    args = ap.parse_args()

    wanted = list(REQUIRED)
    for extra in (args.prefix or []):
        wanted.append(extra)

    containers = sorted(set(
        glob.glob(os.path.join(args.dist, "**", "*.pak"), recursive=True) +
        glob.glob(os.path.join(args.dist, "**", "*.utoc"), recursive=True)))

    if not containers:
        print("NO CONTAINERS under %s -- the package never got that far"
              % args.dist)
        return 1

    report = {"dist": args.dist, "containers": {}}
    hard_failure = False

    for path in containers:
        ok, lines, reason = list_container(path)
        entries = [l for l in lines if "offset:" in l and "size:" in l]
        counts = {w: sum(1 for l in entries if w.lower() in l.lower())
                  for w in wanted}
        rec = {
            "ok": ok,
            "reason": reason,
            "size_bytes": os.path.getsize(path),
            "entries_listed": len(entries),
            "counts": counts,
        }
        report["containers"][os.path.relpath(path, args.dist)] = rec
        # A container that holds project content must parse. An engine-global
        # one is allowed to be unlistable; anything else that fails is fatal.
        if not ok and "engine-global" not in (reason or ""):
            hard_failure = True
        if args.save_lists:
            os.makedirs(args.save_lists, exist_ok=True)
            name = os.path.basename(path) + ".list.txt"
            with open(os.path.join(args.save_lists, name), "w",
                      encoding="utf-8") as fh:
                fh.write("\n".join(lines))

    # Roll-up: an asset may live in either container, so a prefix counts as
    # present if any container has it.
    rollup = {w: sum(r["counts"][w] for r in report["containers"].values())
              for w in wanted}
    report["rollup"] = rollup
    report["present"] = {w: rollup[w] > 0 for w in wanted}

    print("=" * 66)
    print("pak_report  %s" % args.dist)
    print("=" * 66)
    for name, rec in sorted(report["containers"].items()):
        note = "" if rec["ok"] else "  [%s]" % rec["reason"]
        print("\n%-46s %10.1f MB  entries=%d%s"
              % (name, rec["size_bytes"] / 1e6, rec["entries_listed"], note))
        for w, c in rec["counts"].items():
            if c:
                print("      %-24s %d" % (w, c))
    print("\n--- roll-up across all containers ---")
    for w in wanted:
        print("  %-24s %6d   %s"
              % (w, rollup[w], "PRESENT" if rollup[w] else "*** ABSENT ***"))
    print("\nproject containers parsed ok: %s" % (not hard_failure))

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=1)
        print("wrote %s" % args.json)

    return 0 if (not hard_failure and all(report["present"].values())) else 2


if __name__ == "__main__":
    sys.exit(main())
